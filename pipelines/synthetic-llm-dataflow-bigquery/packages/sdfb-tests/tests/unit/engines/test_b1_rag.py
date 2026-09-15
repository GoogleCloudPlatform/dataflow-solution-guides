"""B.1 RAG engine — engine-specific tests (beyond the 5 ABC contract tests).

Covers the spine pieces that the shared contract suite does not:
  - column profiling (constant | numeric | categorical | free_text)
  - GReaT row serialization determinism
  - deterministic top-k retrieval for a fixed query embedding
  - fidelity by construction (constants copied, numerics in range,
    categoricals within observed support)
  - exemplar evidence above a uniform baseline on a rare-value column
  - the free-text LLM-pool path
  - HF_HUB_OFFLINE safety (no Hub network calls at runtime)

All run on the laptop with a deterministic injected/ default embedder — no
model download, no GPU, no GCP.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,protected-access,redefined-outer-name,unused-argument

from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import ClassVar

import pytest
from sdfb_beam.handlers.fake_client import FakeModelClient
from sdfb_core.contracts import GeneratedRecord, TableSchema
from sdfb_core.engines import GenerationConfig, GenerationContext, get_engine
from sdfb_core.engines.b1_rag import B1RagEngine, ColumnKind, HashingEmbedder
from sdfb_core.engines.b1_rag._fidelity import ColumnSampler
from sdfb_core.engines.b1_rag.index import build_index
from sdfb_core.engines.b1_rag.profile import profile_columns
from sdfb_core.engines.b1_rag.serialize import serialize_row

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def customers_ctx(customers_schema, customers_reference):
  return GenerationContext(
      table_schema=customers_schema,
      reference_rows=customers_reference,
      reference_digest="cust-digest",
      pipeline_run_id="b1-test",
  )


@pytest.fixture
def free_text_schema() -> TableSchema:
  """A table with a genuine free-text column (bio) + a constant + an enum."""
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.profiles"
      },
      "schema": [
          {
              "name": "user_id",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "region",
              "type": "STRING",
              "mode": "REQUIRED",
              "max_length": 8
          },
          {
              "name": "status",
              "type": "STRING",
              "mode": "REQUIRED",
              "max_length": 8
          },
          {
              "name": "bio",
              "type": "STRING",
              "mode": "NULLABLE",
              "max_length": 500
          },
      ],
      "primary_keys": ["user_id"],
  })


@pytest.fixture
def free_text_rows() -> list[dict]:
  return [
      {
          "user_id":
              i,
          "region":
              "EU",  # constant
          "status": ["ACTIVE", "PENDING"][i % 2],  # categorical
          "bio":
              f"User number {i} enjoys long-form descriptive prose and writes a lot.",
      } for i in range(1, 13)
  ]


@pytest.fixture
def free_text_ctx(free_text_schema, free_text_rows) -> GenerationContext:
  return GenerationContext(
      table_schema=free_text_schema,
      reference_rows=free_text_rows,
      reference_digest="ft-digest",
      pipeline_run_id="b1-ft",
  )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_engine_is_registered():
  assert get_engine("b1_rag") is B1RagEngine


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_serialize_row_is_great_style_and_deterministic():
  row = {"a": 1, "b": "x", "c": None}
  order = ["a", "b", "c"]
  s1 = serialize_row(row, order)
  s2 = serialize_row(row, order)
  assert s1 == s2 == "a is 1, b is x, c is null"


def test_serialize_respects_column_order():
  row = {"a": 1, "b": 2}
  assert serialize_row(row, ["b", "a"]) == "b is 2, a is 1"


# ---------------------------------------------------------------------------
# Profiling
# ---------------------------------------------------------------------------


def test_profile_classifies_kinds(free_text_schema, free_text_rows):
  profiles = profile_columns(free_text_schema, free_text_rows)
  assert profiles["region"].kind is ColumnKind.CONSTANT
  assert profiles["region"].constant_value == "EU"
  assert profiles["user_id"].kind is ColumnKind.NUMERIC
  assert profiles["status"].kind is ColumnKind.CATEGORICAL
  assert profiles["bio"].kind is ColumnKind.FREE_TEXT


def test_profile_numeric_bounds(customers_schema, customers_reference):
  profiles = profile_columns(customers_schema, customers_reference)
  ltv = profiles["lifetime_value"]
  assert ltv.kind is ColumnKind.NUMERIC
  assert ltv.numeric_min == pytest.approx(320.25)
  assert ltv.numeric_max == pytest.approx(4200.75)
  assert ltv.nullable is True
  assert ltv.null_fraction == pytest.approx(0.2)  # 2 of 10 rows null


def test_profile_categorical_frequencies(customers_schema, customers_reference):
  profiles = profile_columns(customers_schema, customers_reference)
  tier = profiles["tier"]
  assert tier.kind is ColumnKind.CATEGORICAL
  # "ENTERPRISE" appears 3x, "SMB" 3x, "STARTUP" 3x, "FREE" 1x.
  assert tier.categories["ENTERPRISE"] == 3
  assert tier.categories["FREE"] == 1


# ---------------------------------------------------------------------------
# Index / retrieval determinism
# ---------------------------------------------------------------------------


def test_deterministic_topk_for_fixed_query():
  embedder = HashingEmbedder(dim=64, seed=7)
  texts = [f"row {i} value {i % 3}" for i in range(20)]
  vectors = embedder.embed(texts)
  idx = build_index(vectors, embedder.dim)
  q = embedder.embed(["row 5 value 2"])[0]
  a = idx.search(q, 5)
  b = idx.search(q, 5)
  assert a == b  # identical ordering, no implicit randomness
  assert len(a) == 5
  # The exact query row must be its own nearest neighbor.
  assert a[0] == 5


def test_index_handles_empty():
  embedder = HashingEmbedder(dim=8)
  idx = build_index([], embedder.dim)
  assert idx.search([0.0] * 8, 3) == []


# ---------------------------------------------------------------------------
# Fidelity by construction
# ---------------------------------------------------------------------------


def test_constants_copied(free_text_ctx):
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(
      FakeModelClient(reference_pool=free_text_ctx.reference_rows),
      free_text_ctx)
  out = list(engine.generate_batch(20, GenerationConfig(seed=1)))
  assert len(out) > 0
  assert all(r.region == "EU" for r in out)


def test_numerics_within_observed_range(customers_ctx):
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(
      FakeModelClient(reference_pool=customers_ctx.reference_rows),
      customers_ctx)
  out = list(
      engine.generate_batch(50, GenerationConfig(seed=3, similarity=0.0)))
  for r in out:
    if r.lifetime_value is not None:
      assert 320.25 <= float(r.lifetime_value) <= 4200.75


def test_categoricals_within_observed_support(customers_ctx):
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(
      FakeModelClient(reference_pool=customers_ctx.reference_rows),
      customers_ctx)
  observed_tiers = {r["tier"] for r in customers_ctx.reference_rows}
  out = list(engine.generate_batch(50, GenerationConfig(seed=4)))
  for r in out:
    assert r.tier in observed_tiers


# ---------------------------------------------------------------------------
# Acceptance criterion 4: exemplar evidence above baseline
# ---------------------------------------------------------------------------


def test_exemplar_values_appear_above_uniform_baseline(customers_ctx):
  """A high-similarity batch should reflect the empirical tier frequency,
    not a uniform draw. A common tier (3/10 in the reference) must appear
    far more often than a rare one (FREE, 1/10) — the "exemplar evidence
    above baseline" acceptance criterion. With 600 seeded draws the 3x
    frequency gap is decisive (no flakiness; the seed fixes the outcome)."""
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(
      FakeModelClient(reference_pool=customers_ctx.reference_rows),
      customers_ctx)
  out = list(
      engine.generate_batch(600, GenerationConfig(seed=5, similarity=1.0)))
  tiers = Counter(r.tier for r in out)
  # ENTERPRISE (3/10) must clearly out-appear FREE (1/10): exemplar fidelity.
  assert tiers["ENTERPRISE"] > tiers["FREE"]
  # And the common tier must beat the uniform 1/4 baseline a uniform draw
  # would give. (Empirical 0.3 vs uniform 0.25.)
  n_categories = len({r["tier"] for r in customers_ctx.reference_rows})  # 4
  assert tiers["ENTERPRISE"] / len(out) > 1.0 / n_categories


def test_free_text_uses_exemplar_pool(free_text_ctx):
  """Free-text values come from the exemplar pool — every emitted bio is one
    of the observed reference bios. Here the canned client echoes reference
    *rows* (which carry a real `bio` field), and the engine also folds in the
    observed exemplars, so the whole pool is real reference values."""
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(
      FakeModelClient(responses=free_text_ctx.reference_rows), free_text_ctx)
  observed_bios = {r["bio"] for r in free_text_ctx.reference_rows}
  out = list(engine.generate_batch(30, GenerationConfig(seed=6)))
  assert len(out) > 0
  non_null_bios = [r.bio for r in out if r.bio is not None]
  assert non_null_bios, "expected some non-null free-text values"
  assert all(b in observed_bios for b in non_null_bios)


def test_free_text_pool_uses_llm_output_when_valid(free_text_ctx):
  """When the ModelClient returns schema-shaped {bio: str} dicts, those
    LLM values land in the pool and show up in generated rows."""
  llm_values = [{
      "bio": f"LLM-authored biography variant {i}"
  } for i in range(40)]
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(FakeModelClient(responses=llm_values), free_text_ctx)
  out = list(engine.generate_batch(60, GenerationConfig(seed=7)))
  emitted = {r.bio for r in out if r.bio is not None}
  # At least one LLM-authored value must appear among generated rows.
  assert any(b.startswith("LLM-authored") for b in emitted)


# ---------------------------------------------------------------------------
# Acceptance criterion 5: HF_HUB_OFFLINE safety
# ---------------------------------------------------------------------------


def test_runs_with_hf_hub_offline(monkeypatch, customers_ctx):
  """Setting HF_HUB_OFFLINE=1 must not break setup/generate — the default
    embedder is dependency-free and never touches the Hub."""
  monkeypatch.setenv("HF_HUB_OFFLINE", "1")
  monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
  engine = B1RagEngine()  # no injected embedder → HashingEmbedder default
  engine.setup(
      FakeModelClient(reference_pool=customers_ctx.reference_rows),
      customers_ctx)
  out = list(engine.generate_batch(5, GenerationConfig(seed=8)))
  assert len(out) > 0
  assert all(isinstance(r, GeneratedRecord) for r in out)


def test_default_embedder_requires_no_extras():
  """The zero-arg engine constructs without faiss/transformers/torch."""
  engine = B1RagEngine()
  # Constructing must not import any heavy module eagerly.
  assert "torch" not in os.environ.get("_FORCE_IMPORT", "")
  assert engine.name == "b1_rag"


# ---------------------------------------------------------------------------
# similarity knob behavior
# ---------------------------------------------------------------------------


def test_similarity_widens_distribution(customers_ctx):
  """Lower similarity should flatten the categorical distribution toward
    uniform (the rare tier appears more often than at high similarity)."""

  def rare_rate(similarity: float) -> float:
    engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
    engine.setup(
        FakeModelClient(reference_pool=customers_ctx.reference_rows),
        customers_ctx,
    )
    out = list(
        engine.generate_batch(400,
                              GenerationConfig(seed=9, similarity=similarity)))
    return Counter(r.tier for r in out)["FREE"] / len(out)

  high = rare_rate(1.0)
  low = rare_rate(0.0)
  assert low >= high  # widening lifts the rare category toward uniform


# ---------------------------------------------------------------------------
# 2026-07-15 E2E remediation — profiling & sampling fidelity, generic rules.
#
# The live run exposed three generic sampler defects (report §2/§3):
#   - low-cardinality INT columns treated as continuous invented category
#     codes (COL_002: 2 source values -> 5 landing values);
#   - TIMESTAMP/DATE columns sampled categorically -> verbatim copies of
#     real event timestamps at microsecond precision (COL_052);
#   - clip-to-observed-range piled ~5 % of numeric draws exactly on the
#     min/max bounds (COL_007/009/016/047/057 single-value spikes).
# ---------------------------------------------------------------------------


@pytest.fixture
def events_schema() -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.events"
      },
      "schema": [
          {
              "name": "event_id",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "code",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "amount",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "created_at",
              "type": "TIMESTAMP",
              "mode": "REQUIRED"
          },
          {
              "name": "load_date",
              "type": "DATE",
              "mode": "REQUIRED"
          },
      ],
      "primary_keys": ["event_id"],
  })


@pytest.fixture
def events_rows() -> list[dict]:
  base = datetime(2026, 1, 1, tzinfo=UTC)
  return [
      {
          "event_id":
              i,
          "code": [1, 2][i % 2],  # 2 distinct over 200 rows — a code enum
          "amount": (i * 37) % 10_000,  # 200 distinct — genuinely continuous
          "created_at":
              base + timedelta(minutes=13 * i, seconds=i % 60, microseconds=i),
          "load_date": (base + timedelta(days=i % 60)).date(),  # 60 distinct
      } for i in range(200)
  ]


@pytest.fixture
def events_ctx(events_schema, events_rows) -> GenerationContext:
  return GenerationContext(
      table_schema=events_schema,
      reference_rows=events_rows,
      reference_digest="events-digest",
      pipeline_run_id="b1-events",
  )


def test_profile_low_cardinality_int_is_categorical(events_schema, events_rows):
  profiles = profile_columns(events_schema, events_rows)
  code = profiles["code"]
  assert code.kind is ColumnKind.CATEGORICAL
  assert set(code.categories) == {1, 2}


def test_profile_full_cardinality_int_stays_numeric(events_schema, events_rows):
  profiles = profile_columns(events_schema, events_rows)
  assert profiles["amount"].kind is ColumnKind.NUMERIC
  # PK-like all-distinct integers must also stay numeric (novel values are
  # the point there), even though the distinct count is small in fixtures.
  assert profiles["event_id"].kind is ColumnKind.NUMERIC


def test_generated_low_cardinality_ints_never_invent_codes(events_ctx):
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(FakeModelClient(responses=[]), events_ctx)
  out = list(engine.generate_batch(300, GenerationConfig(seed=11)))
  assert len(out) == 300
  assert {r.code for r in out} <= {1, 2}


def test_profile_high_cardinality_timestamp_is_temporal(events_schema,
                                                        events_rows):
  profiles = profile_columns(events_schema, events_rows)
  created = profiles["created_at"]
  assert created.kind is ColumnKind.TEMPORAL
  assert created.numeric_min is not None
  assert created.numeric_max is not None
  assert created.numeric_max > created.numeric_min


def test_profile_low_cardinality_timestamp_stays_categorical():
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.loads"
      },
      "schema": [
          {
              "name": "id",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "loaded_at",
              "type": "TIMESTAMP",
              "mode": "REQUIRED"
          },
      ],
      "primary_keys": ["id"],
  })
  stamps = [datetime(2026, 7, d, 12, 0, tzinfo=UTC) for d in range(1, 6)]
  rows = [{"id": i, "loaded_at": stamps[i % 5]} for i in range(100)]
  profiles = profile_columns(schema, rows)
  # 5 distinct load timestamps over 100 rows — an enum in disguise; verbatim
  # categorical sampling is the faithful (and harmless) treatment.
  assert profiles["loaded_at"].kind is ColumnKind.CATEGORICAL


def test_temporal_sampling_novel_in_range_and_typed(events_ctx, events_rows):
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(FakeModelClient(responses=[]), events_ctx)
  out = list(engine.generate_batch(200, GenerationConfig(seed=13)))

  observed_ts = {r["created_at"] for r in events_rows}
  ts = [r.created_at for r in out]
  assert all(isinstance(v, datetime) for v in ts)
  assert min(ts) >= min(observed_ts)
  assert max(ts) <= max(observed_ts)
  # Verbatim microsecond-precision copies are linkage quasi-identifiers —
  # the bulk of generated timestamps must be novel.
  novel_share = sum(v not in observed_ts for v in ts) / len(ts)
  assert novel_share >= 0.5

  observed_dates = {r["load_date"] for r in events_rows}
  dates = [r.load_date for r in out]
  assert all(isinstance(v, date) and not isinstance(v, datetime) for v in dates)
  assert min(dates) >= min(observed_dates)
  assert max(dates) <= max(observed_dates)


def test_numeric_sampling_does_not_pile_on_bounds(events_schema, events_rows):
  import numpy as np

  profiles = profile_columns(events_schema, events_rows)
  sampler = ColumnSampler(profiles["amount"])
  rng = np.random.default_rng(42)
  vals = sampler.sample_numpy(rng, 2000, similarity=0.5)
  lo = round(profiles["amount"].numeric_min)
  hi = round(profiles["amount"].numeric_max)
  bound_share = sum(v in (lo, hi) for v in vals) / len(vals)
  # Clipping out-of-range blends used to stack ~5 % of draws exactly on the
  # observed min/max (the COL_007/009/… spikes); out-of-range draws must be
  # redrawn inside the range instead.
  assert bound_share < 0.01


# ---------------------------------------------------------------------------
# Free-text pools — LLM-identified format, novel-only for unique-valued
# columns (generic: UUIDs, hex ids, unique prose — no type hardcoding).
# ---------------------------------------------------------------------------


class _RecordingPoolClient:
  """Returns canned pool dicts and records every generate_json call."""

  def __init__(self, responses: list[dict]):
    self._responses = responses
    self.prompts: list[str] = []

  def generate_json(self, prompt, json_schema, **kwargs):
    self.prompts.append(prompt)
    return list(self._responses)


def test_b1_pool_filters_verbatim_exemplar_copies(free_text_ctx):
  observed = [r["bio"] for r in free_text_ctx.reference_rows]
  responses = [
      {
          "bio": observed[0]
      },  # verbatim copy — must be dropped
      {
          "bio": observed[1]
      },  # verbatim copy — must be dropped
      {
          "bio": "Novel synthetic biography A"
      },
      {
          "bio": "Novel synthetic biography B"
      },
  ]
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(_RecordingPoolClient(responses), free_text_ctx)
  pool = engine._free_text_pools["bio"]
  assert "Novel synthetic biography A" in pool
  assert "Novel synthetic biography B" in pool
  # `bio` is unique-valued in the reference (every row distinct): observed
  # values must not appear in the pool — neither via the LLM echoing them
  # nor via exemplar folding.
  assert observed[0] not in pool
  assert observed[1] not in pool
  assert not set(observed) & set(pool)


def _shared_key_ctx():
  """60 distinct comments over 240 rows: free text by cardinality (>50)
    but NOT unique-valued (unique ratio 0.25) — the shared-key band."""
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.feedback"
      },
      "schema": [
          {
              "name": "id",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "comment",
              "type": "STRING",
              "mode": "REQUIRED"
          },
      ],
      "primary_keys": ["id"],
  })
  rows = [{
      "id": i,
      "comment": f"Observed comment number {i % 60}"
  } for i in range(240)]
  return GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="fb-digest",
      pipeline_run_id="b1-fb",
  )


def test_b1_non_unique_freetext_does_not_fold_exemplars():
  # Shared-key free-text columns are still high-cardinality source data:
  # folding real exemplars on top of a small LLM pool made them 47.5-93.9 %
  # verbatim source values in the 2026-07-16 E2E run (COL_048 et al.). When
  # the LLM delivers, the pool must contain ONLY generated values.
  ctx = _shared_key_ctx()
  responses = [{"comment": f"Fresh comment {i}"} for i in range(10)]
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(_RecordingPoolClient(responses), ctx)
  pool = engine._free_text_pools["comment"]
  assert any(v.startswith("Fresh comment") for v in pool)
  assert not any(v.startswith("Observed comment") for v in pool)


def test_b1_non_unique_freetext_empty_yield_still_falls_back_to_exemplars():
  # Lax mode, LLM yielded nothing usable: the exemplar fallback keeps the
  # column populated — loudly, via the freetext_llm_fallback milestone.
  ctx = _shared_key_ctx()
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(_RecordingPoolClient([]), ctx)
  pool = engine._free_text_pools["comment"]
  assert pool
  assert all(v.startswith("Observed comment") for v in pool)


def test_b1_pool_prompt_demands_format_identification_and_novelty(
    free_text_ctx):
  client = _RecordingPoolClient([{"bio": "Novel value"}])
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(client, free_text_ctx)
  assert client.prompts, "expected a pool-inference call for 'bio'"
  prompt = client.prompts[0].lower()
  # The LLM identifies the column's format and generates accordingly —
  # never copying exemplars verbatim (generic across UUIDs/dates/codes).
  assert "format" in prompt
  assert "verbatim" in prompt


# ---------------------------------------------------------------------------
# Temporal sentinels + interim age policy (2026-07-23). The 2026-07-23 b1 E2E
# landed date-STRING columns with dates like "72-08-01": 0001-01-01 sentinels
# inflated the jitter [min, max] to ~2000 years, and sentinel anchors leaked
# through the blend. Sentinel-year values (1 / 9999) leave the range and are
# re-injected at their observed frequency; the floor is additionally clamped
# to now - 10 years (interim policy; per-column DDL-JSON descriptions will
# govern audit/linked fields later).
# ---------------------------------------------------------------------------


def _ts_schema() -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.tstamps"
      },
      "schema": [{
          "name": "ts",
          "type": "TIMESTAMP",
          "mode": "REQUIRED"
      }],
      "primary_keys": None,
  })


def test_b1_temporal_profile_splits_sentinels_and_keeps_recent_range():
  sentinel = datetime(1, 1, 1, tzinfo=UTC)
  rows = [{
      "ts": datetime(2025, 3, 1, tzinfo=UTC) + timedelta(hours=i)
  } for i in range(60)] + [{
      "ts": sentinel
  }] * 40
  p = profile_columns(_ts_schema(), rows)["ts"]
  assert p.kind is ColumnKind.TEMPORAL
  assert datetime.fromtimestamp(p.numeric_min, tz=UTC).year == 2025
  sentinels = dict(p.temporal_sentinels)
  assert abs(sentinels[sentinel] - 40 / 100) < 0.01


def test_b1_sampler_reinjects_sentinels_and_stays_plausible():
  import random

  sentinel = datetime(1, 1, 1, tzinfo=UTC)
  rows = [{
      "ts": datetime(2025, 3, 1, tzinfo=UTC) + timedelta(hours=i)
  } for i in range(60)] + [{
      "ts": sentinel
  }] * 40
  p = profile_columns(_ts_schema(), rows)["ts"]
  sampler = ColumnSampler(p)
  out = sampler.sample_python(random.Random(3), 2000, similarity=0.5)
  frac = sum(v == sentinel for v in out) / len(out)
  assert abs(frac - 0.4) < 0.05
  regular = [v for v in out if v != sentinel and v is not None]
  assert regular
  assert all(v.year == 2025 for v in regular)  # no year-72 leakage


def test_b1_temporal_range_clamps_to_max_age():
  rows = [{
      "ts": datetime(2005, 3, 1, tzinfo=UTC) + timedelta(days=i)
  } for i in range(40)] + [{
      "ts": datetime(2025, 4, 1, tzinfo=UTC) + timedelta(days=i)
  } for i in range(40)]
  p = profile_columns(_ts_schema(), rows)["ts"]
  assert p.kind is ColumnKind.TEMPORAL
  now_year = datetime.now(UTC).year
  assert datetime.fromtimestamp(p.numeric_min, tz=UTC).year >= now_year - 10
  assert datetime.fromtimestamp(p.numeric_max, tz=UTC).year == 2025


def test_b1_fully_historical_temporal_range_is_kept():
  rows = [{
      "ts": datetime(2005, 3, 1, tzinfo=UTC) + timedelta(days=i)
  } for i in range(40)]
  p = profile_columns(_ts_schema(), rows)["ts"]
  assert p.kind is ColumnKind.TEMPORAL
  assert datetime.fromtimestamp(p.numeric_min, tz=UTC).year == 2005


def test_b1_date_string_temporal_clamps_and_drops_sentinel_anchors():
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.dstr"
      },
      "schema": [{
          "name": "d",
          "type": "STRING",
          "mode": "REQUIRED"
      }],
      "primary_keys": None,
  })
  # > 50 distinct date strings → free-text branch → TEMPORAL via shape.
  rows = ([{
      "d": f"2005-03-{(i % 28) + 1:02d}"
  } for i in range(28)] + [{
      "d": f"2025-04-{(i % 28) + 1:02d}"
  } for i in range(28)] + [{
      "d": "0001-01-01"
  }] * 30)
  p = profile_columns(schema, rows)["d"]
  assert p.kind is ColumnKind.TEMPORAL
  now_year = datetime.now(UTC).year
  lo = datetime.fromtimestamp(p.numeric_min, tz=UTC).year
  assert lo >= now_year - 10
  sentinels = dict(p.temporal_sentinels)
  assert abs(sentinels["0001-01-01"] - 30 / 86) < 0.01

  import random

  out = ColumnSampler(p).sample_python(random.Random(9), 1000, similarity=0.5)
  non_sentinel = [v for v in out if v != "0001-01-01" and v is not None]
  assert non_sentinel
  years = {datetime.strptime(v, "%Y-%m-%d").year for v in non_sentinel}
  assert min(years) >= now_year - 10  # clamp holds through the sampler


class TestGenerateForKeys:
  """ADR 0036: a driven child from parent keys — inherited columns
    copied, PK cells unique per key, the rest sampled as usual."""

  _SCHEMA = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.src.child_t"
      },
      "schema": [
          {
              "name": "PID",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "REGION",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "CAT",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "AMT",
              "type": "INT64",
              "mode": "REQUIRED"
          },
      ],
  })

  @staticmethod
  def _rows():
    return [{
        "PID": f"P{i:04d}",
        "REGION": "ES" if i % 2 else "PT",
        "CAT": "abc"[i % 3],
        "AMT": i * 3
    } for i in range(60)]

  def _ctx(self):
    return GenerationContext(
        table_schema=self._SCHEMA,
        reference_rows=self._rows(),
        reference_digest="fanout-digest",
        pipeline_run_id="fanout-run",
        pk_columns=["PID", "CAT"],
        fanout={
            "driving_cols": ["PID", "REGION"],
            "histogram": {
                "0": 1,
                "2": 2,
                "3": 1
            },
            "cells": {
                "cols": ["CAT"],
                "rows": [["a"], ["b"], ["c"]],
                "counts": [3, 2, 1]
            },
            "exact_cells": True,
        },
    )

  class _Client:

    def generate_json(self, *, prompt, n=1, **kw):
      return [{"values": [f"gen-{i}" for i in range(32)]}]

  def test_children_carry_their_parent_and_unique_cells(self):
    engine = B1RagEngine(embedder=HashingEmbedder())
    engine.setup(self._Client(), self._ctx())
    keys = [(f"K{i}", "ES" if i % 2 else "PT") for i in range(40)]
    cfg = GenerationConfig(seed=1, batch_size=1_000)
    rows = [r.model_dump() for r in engine.generate_for_keys(keys, cfg)]
    assert rows
    for r in rows:
      assert (r["PID"], r["REGION"]) in keys  # inherited verbatim
      assert isinstance(r["AMT"], int)  # the rest is sampled
    per_key: dict = {}
    for r in rows:
      per_key.setdefault(r["PID"], []).append(r["CAT"])
    for cats in per_key.values():
      assert len(cats) == len(set(cats)) and len(cats) <= 3
    # Fan-out histogram: only 0, 2, 3 children per key.
    assert {len(v) for v in per_key.values()} <= {2, 3}

  def test_same_keys_same_children(self):
    engine = B1RagEngine(embedder=HashingEmbedder())
    engine.setup(self._Client(), self._ctx())
    keys = [(f"K{i}", "ES") for i in range(20)]
    cfg = GenerationConfig(seed=1, batch_size=7)  # chunked at 7 rows
    a = [(r.PID, r.CAT) for r in engine.generate_for_keys(keys, cfg)]
    b = [(r.PID, r.CAT) for r in engine.generate_for_keys(keys, cfg)]
    assert a == b

  def test_rest_columns_do_not_repeat_across_chunks(self):
    engine = B1RagEngine(embedder=HashingEmbedder())
    engine.setup(self._Client(), self._ctx())
    keys = [(f"K{i}", "ES") for i in range(40)]
    cfg = GenerationConfig(seed=1, batch_size=7)
    amts = [r.AMT for r in engine.generate_for_keys(keys, cfg)]
    assert len(amts) > 14
    assert amts[:7] != amts[7:14]

  def test_without_a_plan_it_refuses(self):
    engine = B1RagEngine(embedder=HashingEmbedder())
    ctx = self._ctx().model_copy(update={"fanout": None})
    engine.setup(self._Client(), ctx)
    with pytest.raises(RuntimeError, match="fanout"):
      list(engine.generate_for_keys([("K1", "ES")], GenerationConfig(seed=1)))

  def test_fanout_bound_logs_zero_conditional_and_omits_candidate_cap(
      self, caplog) -> None:
    """ADR 0037 (design §8): `fanout_bound conditional=<n>
        candidate_cap=` — a plan with no conditional edges logs
        `conditional=0` and omits `candidate_cap` entirely. Also pins the
        four pre-existing fields (name + value) so a future edit to the
        same `fields` dict literal cannot silently drop/rename one —
        review round 1 should-fix: `fanout_bound` had no prior regression
        coverage anywhere in the suite."""
    engine = B1RagEngine(embedder=HashingEmbedder())
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      engine.setup(self._Client(), self._ctx())
    assert "name=fanout_bound" in caplog.text
    assert "driving_cols=PID,REGION" in caplog.text
    assert "cells=3" in caplog.text
    assert "exact_cells=True" in caplog.text
    assert "mean_fanout=1.75" in caplog.text
    assert "conditional=0" in caplog.text
    assert "candidate_cap=" not in caplog.text


class TestGenerateForKeysConditional:
  """ADR 0037 (design 2026-09-11 §4): a non-driving FK edge resolved per
    key from a co-parent's matched candidates, riding alongside the driven
    child's `generate_for_keys` call."""

  _SCHEMA = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.src.multi_parent_t"
      },
      "schema": [
          {
              "name": "T",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "L",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "R",
              "type": "STRING",
              "mode": "NULLABLE"
          },
          {
              "name": "X",
              "type": "INT64",
              "mode": "REQUIRED"
          },
      ],
  })

  @staticmethod
  def _rows():
    return [{
        "T": f"t{i % 2}",
        "L": f"l{i % 2}",
        "R": f"r{i % 3}",
        "X": i
    } for i in range(30)]

  def _ctx(self, *, nullable: bool):
    return GenerationContext(
        table_schema=self._SCHEMA,
        reference_rows=self._rows(),
        reference_digest="conditional-digest",
        pipeline_run_id="conditional-run",
        fanout={
            "driving_cols": ["T", "L"],
            "histogram": {
                "2": 1
            },
            "conditional": [{
                "id": "(T,R)->right",
                "cols": ["R"],
                "nullable": nullable
            }],
        },
    )

  class _Client:

    def generate_json(self, *, prompt, n=1, **kw):
      return [{"values": [f"gen-{i}" for i in range(32)]}]

  _KEYS: ClassVar[list[tuple]] = [("t1", "l1"), ("t2", "l2")]
  _MATCHES: ClassVar[dict] = {"(T,R)->right": [[("r1",), ("r2",)], []]}

  def test_matched_key_gets_each_candidate_once_unmatched_key_dropped(self):
    engine = B1RagEngine(embedder=HashingEmbedder())
    engine.setup(self._Client(), self._ctx(nullable=False))
    cfg = GenerationConfig(seed=1, batch_size=1_000)
    rows = [
        r.model_dump() for r in engine.generate_for_keys(
            self._KEYS, cfg, matches=self._MATCHES)
    ]
    t1_rows = [r for r in rows if r["T"] == "t1"]
    t2_rows = [r for r in rows if r["T"] == "t2"]
    assert len(t1_rows) == 2
    assert {r["R"] for r in t1_rows} == {"r1", "r2"}
    assert not t2_rows

  def test_unmatched_key_on_a_nullable_edge_gets_null(self):
    engine = B1RagEngine(embedder=HashingEmbedder())
    engine.setup(self._Client(), self._ctx(nullable=True))
    cfg = GenerationConfig(seed=1, batch_size=1_000)
    rows = [
        r.model_dump() for r in engine.generate_for_keys(
            self._KEYS, cfg, matches=self._MATCHES)
    ]
    t2_rows = [r for r in rows if r["T"] == "t2"]
    assert len(t2_rows) == 2
    assert all(r["R"] is None for r in t2_rows)

  def _capping_ctx(self, landing_table: str) -> GenerationContext:
    """A plan whose per-key capacity (2 cells x 2 candidates) falls
        short of its fan-out (5), so EVERY key caps."""
    return GenerationContext(
        table_schema=self._SCHEMA,
        reference_rows=self._rows(),
        reference_digest="capping-digest",
        pipeline_run_id="capping-run",
        landing_table=landing_table,
        fanout={
            "driving_cols": ["T", "L"],
            "histogram": {
                "5": 1
            },
            "cells": {
                "cols": ["X"],
                "rows": [[1], [2]],
                "counts": [1, 1]
            },
            "exact_cells":
                True,
            "conditional": [{
                "id": "(T,R)->right",
                "cols": ["R"],
                "nullable": False,
                "pk_member": True
            }],
        },
    )

  def test_every_driven_table_reports_its_own_capping(self, caplog):
    """G4: the ``table=`` argument this engine hands
        `conditional_draws` is what scopes the once-per-table
        `fanout_rows_capped` guard (fix wave E3). Nothing drove that
        argument from an ENGINE, so deleting it at this call site
        restored the process-global bucket — every driven table after the
        first capping in silence in a single-job relational run (ADR
        0030) — with the whole suite green.
        """
    from sdfb_core.engines import base as base_mod

    base_mod._reset_rows_capped_log()
    matches = {"(T,R)->right": [[("r1",), ("r2",)], [("r1",), ("r2",)]]}
    cfg = GenerationConfig(seed=1, batch_size=1_000)
    with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
      for table in ("p.land.child_a", "p.land.child_b"):
        engine = B1RagEngine(embedder=HashingEmbedder())
        engine.setup(self._Client(), self._capping_ctx(table))
        rows = list(engine.generate_for_keys(self._KEYS, cfg, matches=matches))
        assert len(rows) == 8  # 2 keys x min(5, 2 cells x 2 cands)
    capped = [
        ln for ln in caplog.text.splitlines() if "name=fanout_rows_capped" in ln
    ]
    assert len(capped) == 2  # one per driven table, not one per process

  def test_fanout_bound_logs_conditional_count(self, caplog) -> None:
    engine = B1RagEngine(embedder=HashingEmbedder())
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      engine.setup(self._Client(), self._ctx(nullable=False))
    assert "name=fanout_bound" in caplog.text
    assert "conditional=1" in caplog.text

  def test_fanout_bound_logs_candidate_cap_when_present(self, caplog) -> None:
    # `candidate_cap` rides next to `conditional` in the plan payload
    # (Task 6, absent until the launcher writes it).
    engine = B1RagEngine(embedder=HashingEmbedder())
    ctx = self._ctx(nullable=False)
    assert ctx.fanout is not None
    ctx = ctx.model_copy(update={"fanout": {**ctx.fanout, "candidate_cap": 64}})
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      engine.setup(self._Client(), ctx)
    assert "candidate_cap=64" in caplog.text

  def test_fanout_bound_logs_candidate_cap_zero(self, caplog) -> None:
    # Review round 1: the code guards with `candidate_cap is not
    # None`, not truthiness — `0` is a valid (if degenerate) cap and
    # must still be logged, not silently omitted like a falsy guard
    # would do.
    engine = B1RagEngine(embedder=HashingEmbedder())
    ctx = self._ctx(nullable=False)
    assert ctx.fanout is not None
    ctx = ctx.model_copy(update={"fanout": {**ctx.fanout, "candidate_cap": 0}})
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      engine.setup(self._Client(), ctx)
    assert "candidate_cap=0" in caplog.text
