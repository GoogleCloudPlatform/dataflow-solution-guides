"""B.2 library-wrapper engine — engine-specific tests.

The shared 5-test ABC contract lives in ``test_abc_contract.py`` (B.2 is in
its parametrize list). This module covers B.2's own surface: the FASTGEN
spine (fit-once profiling), post-hoc fidelity enforcement, the free-text
LLM hook honoring ``cfg.similarity``, pickling across the Beam worker
boundary, and the empirical-vs-sdgx backend seam.

All pure-laptop: the deterministic NumPy backend (``use_sdgx=False``) is
used so no GPU/GCP/torch/sdgx is required. ``sdgx`` itself is only fitted on
the M4 (the production path); its absence here is the intended fallback.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=missing-class-docstring,protected-access,redefined-outer-name,unused-argument,use-implicit-booleaness-not-comparison

from __future__ import annotations

import pickle

import numpy as np
import pytest
from sdfb_beam.handlers.fake_client import FakeModelClient
from sdfb_core.contracts import GeneratedRecord, TableSchema
from sdfb_core.engines import GenerationConfig, GenerationContext, get_engine
from sdfb_core.engines.b2_library import B2LibraryEngine
from sdfb_core.engines.b2_library.backends import EmpiricalBackend
from sdfb_core.engines.b2_library.fidelity import (
    ColumnKind,
    ColumnProfile,
    enforce_value,
    profile_table,
)
from sdfb_core.engines.b2_library.freetext import (
    FreeTextHook,
    similarity_to_temperature,
)

# ---------------------------------------------------------------------------
# A richer fixture table: a constant column, a numeric range, a categorical,
# a FREE_TEXT prose column, and a JSON column — so the free-text hook and the
# fidelity clamps are all exercised.
# ---------------------------------------------------------------------------


@pytest.fixture
def wide_ddl_dict() -> dict:
  return {
      "table_info": {
          "table_id": "demo.support_tickets"
      },
      "schema": [
          {
              "name": "ticket_id",
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
              "name": "priority",
              "type": "STRING",
              "mode": "REQUIRED",
              "max_length": 8
          },
          {
              "name": "score",
              "type": "FLOAT64",
              "mode": "REQUIRED"
          },
          # constant across the reference → must stay constant.
          {
              "name": "source_system",
              "type": "STRING",
              "mode": "REQUIRED",
              "max_length": 16
          },
          # long prose → FREE_TEXT → LLM hook.
          {
              "name": "summary",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          # JSON blob → FREE_TEXT → LLM hook.
          {
              "name": "metadata",
              "type": "JSON",
              "mode": "NULLABLE"
          },
      ],
      "primary_keys": ["ticket_id"],
  }


@pytest.fixture
def wide_reference() -> list[dict]:
  return [
      {
          "ticket_id":
              1001,
          "region":
              "EMEA",
          "priority":
              "HIGH",
          "score":
              0.82,
          "source_system":
              "zendesk",
          "summary":
              "Customer reports the export job hangs at 90 percent for large tables.",
          "metadata":
              '{"sla": "gold", "reopened": false}',
      },
      {
          "ticket_id":
              1002,
          "region":
              "AMER",
          "priority":
              "LOW",
          "score":
              0.20,
          "source_system":
              "zendesk",
          "summary":
              "User cannot reset password; the reset email never arrives.",
          "metadata":
              '{"sla": "silver", "reopened": true}',
      },
      {
          "ticket_id":
              1003,
          "region":
              "APAC",
          "priority":
              "MEDIUM",
          "score":
              0.55,
          "source_system":
              "zendesk",
          "summary":
              "Dashboard widgets render blank after the latest browser update.",
          "metadata":
              None,
      },
      {
          "ticket_id":
              1004,
          "region":
              "EMEA",
          "priority":
              "HIGH",
          "score":
              0.91,
          "source_system":
              "zendesk",
          "summary":
              "API returns 500 on the bulk endpoint when payloads exceed 10 megabytes.",
          "metadata":
              '{"sla": "gold", "reopened": false}',
      },
  ]


@pytest.fixture
def wide_ctx(wide_ddl_dict, wide_reference) -> GenerationContext:
  return GenerationContext(
      table_schema=TableSchema.model_validate(wide_ddl_dict),
      reference_rows=wide_reference,
      reference_digest="wide-digest",
      pipeline_run_id="b2-wide-run",
  )


@pytest.fixture
def wide_client(wide_reference) -> FakeModelClient:
  # canned mode: the free-text hook's generate_json calls cycle these dicts;
  # _extract_values pulls their string fields into the pool.
  return FakeModelClient(responses=wide_reference)


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


def test_engine_is_registered():
  assert get_engine("b2_library") is B2LibraryEngine


# ---------------------------------------------------------------------------
# Column profiling — the O(1) fit step (FASTGEN spine).
# ---------------------------------------------------------------------------


def test_profile_classifies_column_kinds(wide_ctx):
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  assert profiles["ticket_id"].kind is ColumnKind.NUMERIC
  assert profiles["region"].kind is ColumnKind.CATEGORICAL
  assert profiles["priority"].kind is ColumnKind.CATEGORICAL
  assert profiles["score"].kind is ColumnKind.NUMERIC
  assert profiles["source_system"].kind is ColumnKind.CONSTANT
  assert profiles["summary"].kind is ColumnKind.FREE_TEXT  # long prose
  assert profiles["metadata"].kind is ColumnKind.FREE_TEXT  # JSON


def test_profile_records_observed_bounds(wide_ctx):
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  score = profiles["score"]
  assert score.minimum == pytest.approx(0.20)
  assert score.maximum == pytest.approx(0.91)
  assert profiles["source_system"].constant_value == "zendesk"


# ---------------------------------------------------------------------------
# Post-hoc fidelity enforcement (constant / range / enum).
# ---------------------------------------------------------------------------


def test_enforce_constant_always_copies():
  p = ColumnProfile(
      name="src",
      bq_type="STRING",
      kind=ColumnKind.CONSTANT,
      nullable=False,
      constant_value="zendesk",
  )
  assert enforce_value(p, "something else") == "zendesk"
  assert enforce_value(p, None) == "zendesk"


def test_enforce_numeric_clips_to_range():
  p = ColumnProfile(
      name="score",
      bq_type="FLOAT64",
      kind=ColumnKind.NUMERIC,
      nullable=False,
      minimum=0.2,
      maximum=0.91,
  )
  assert enforce_value(p, 5.0) == pytest.approx(0.91)  # above max → clamp
  assert enforce_value(p, -3.0) == pytest.approx(0.2)  # below min → clamp
  assert enforce_value(p, 0.5) == pytest.approx(0.5)  # in range → kept


def test_enforce_integer_rounds_and_clips():
  p = ColumnProfile(
      name="id",
      bq_type="INT64",
      kind=ColumnKind.NUMERIC,
      nullable=False,
      minimum=1.0,
      maximum=10.0,
      is_integer=True,
  )
  out = enforce_value(p, 7.6)
  assert out == 8 and isinstance(out, int)
  assert enforce_value(p, 99.0) == 10


def test_enforce_enum_snaps_unknown_to_known():
  p = ColumnProfile(
      name="region",
      bq_type="STRING",
      kind=ColumnKind.CATEGORICAL,
      nullable=False,
      categories=("EMEA", "AMER", "APAC"),
      weights=(0.5, 0.25, 0.25),
  )
  assert enforce_value(p, "EMEA") == "EMEA"  # known → kept
  assert enforce_value(p, "ANTARCTICA") == "EMEA"  # unknown → snapped


# ---------------------------------------------------------------------------
# The free-text LLM hook (charter AC #4) — exercised by ≥1 column,
# respects cfg.similarity.
# ---------------------------------------------------------------------------


def test_similarity_to_temperature_monotone():
  # similarity high → low temperature (mimic); low → high temperature.
  assert similarity_to_temperature(1.0) < similarity_to_temperature(0.0)
  assert similarity_to_temperature(0.5) == pytest.approx(0.7, abs=0.05)


def test_free_text_hook_calls_model_client(wide_ctx, wide_client):
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  hook = FreeTextHook(wide_client)
  rng = np.random.default_rng(7)
  out = hook.sample(profiles["summary"], 5, GenerationConfig(seed=7), rng)
  assert len(out) == 5
  assert wide_client.call_count >= 1  # the LLM hook was invoked
  # all non-null values are strings drawn from the pool.
  assert all(v is None or isinstance(v, str) for v in out)


def test_free_text_hook_temperature_tracks_similarity(wide_ctx):
  """High similarity ⇒ low LLM temperature passed to the client."""
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  captured: list[float] = []

  class _RecordingClient:
    call_count = 0

    def generate_json(self,
                      prompt,
                      json_schema,
                      *,
                      max_tokens=2048,
                      temperature=0.7,
                      n=1,
                      seed=None,
                      top_p=None,
                      top_k=None):
      self.call_count += 1
      captured.append(temperature)
      return [{"values": ["alpha", "beta", "gamma"]}]

  hook = FreeTextHook(_RecordingClient())
  rng = np.random.default_rng(1)
  hook.sample(profiles["summary"], 3, GenerationConfig(seed=1, similarity=1.0),
              rng)
  hook.sample(profiles["summary"], 3, GenerationConfig(seed=2, similarity=0.0),
              rng)
  assert captured[0] < captured[1]  # sim=1.0 colder than sim=0.0


def test_engine_exercises_free_text_columns(wide_ctx, wide_client):
  """End-to-end: a fixture column (summary, metadata) is patched by the
    free-text hook, not the statistical backend (charter AC #4)."""
  engine = B2LibraryEngine(use_sdgx=False)
  engine.setup(wide_client, wide_ctx)
  out = list(engine.generate_batch(6, GenerationConfig(seed=11)))
  assert len(out) > 0
  assert wide_client.call_count >= 1  # the LLM hook ran for free-text cols
  for rec in out:
    assert isinstance(rec.summary, str) and rec.summary  # filled, non-empty


# ---------------------------------------------------------------------------
# Fidelity by construction, end-to-end through the engine.
# ---------------------------------------------------------------------------


def test_generated_rows_respect_constant_and_range(wide_ctx, wide_client):
  engine = B2LibraryEngine(use_sdgx=False)
  engine.setup(wide_client, wide_ctx)
  out = list(engine.generate_batch(20, GenerationConfig(seed=3)))
  assert len(out) > 0
  for rec in out:
    assert rec.source_system == "zendesk"  # constant preserved
    assert 0.20 <= rec.score <= 0.91  # numeric clipped to range
    assert rec.region in {"EMEA", "AMER", "APAC"}  # enum kept in support
    assert rec.priority in {"HIGH", "LOW", "MEDIUM"}


def test_similarity_zero_widens_diversity(wide_ctx, wide_client):
  """similarity→0 should not collapse categoricals to a single value."""
  engine = B2LibraryEngine(use_sdgx=False)
  engine.setup(wide_client, wide_ctx)
  out = list(
      engine.generate_batch(40, GenerationConfig(seed=5, similarity=0.0)))
  regions = {rec.region for rec in out}
  assert len(regions) >= 2  # diversity preserved at low similarity


# ---------------------------------------------------------------------------
# Pickling across the Beam worker boundary (charter / spec §4 step 1).
# ---------------------------------------------------------------------------


def test_fitted_engine_pickles(wide_ctx, wide_client):
  engine = B2LibraryEngine(use_sdgx=False)
  engine.setup(wide_client, wide_ctx)
  blob = pickle.dumps(engine)
  revived = pickle.loads(blob)
  out = list(revived.generate_batch(3, GenerationConfig(seed=9)))
  assert len(out) > 0
  assert all(isinstance(r, GeneratedRecord) for r in out)


# ---------------------------------------------------------------------------
# The empirical backend in isolation — deterministic, in-support.
# ---------------------------------------------------------------------------


def test_empirical_backend_is_deterministic(wide_ctx):
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  b1 = EmpiricalBackend()
  b1.fit(wide_ctx.reference_rows, profiles)
  b2 = EmpiricalBackend()
  b2.fit(wide_ctx.reference_rows, profiles)
  a = b1.sample_columns(10, np.random.default_rng(42), temperature=1.0)
  b = b2.sample_columns(10, np.random.default_rng(42), temperature=1.0)
  assert a == b
  # Free-text columns are NOT produced by the backend.
  assert "summary" not in a and "metadata" not in a


def test_empirical_backend_temperature_zero_collapses_to_mode(wide_ctx):
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  backend = EmpiricalBackend()
  backend.fit(wide_ctx.reference_rows, profiles)
  cols = backend.sample_columns(30, np.random.default_rng(0), temperature=0.0)
  # priority modal value is HIGH (appears twice); temp=0 ⇒ all HIGH.
  assert set(cols["priority"]) == {"HIGH"}


# ---------------------------------------------------------------------------
# Lifecycle edge cases beyond the shared contract.
# ---------------------------------------------------------------------------


def test_setup_idempotent_does_not_refit(wide_ctx, wide_client):
  engine = B2LibraryEngine(use_sdgx=False)
  engine.setup(wide_client, wide_ctx)
  backend_before = engine._backend
  engine.setup(wide_client, wide_ctx)  # second call — no-op
  assert engine._backend is backend_before


def test_empty_reference_yields_nothing(wide_ddl_dict, wide_client):
  ctx = GenerationContext(
      table_schema=TableSchema.model_validate(wide_ddl_dict),
      reference_rows=[],
  )
  engine = B2LibraryEngine(use_sdgx=False)
  engine.setup(wide_client, ctx)
  assert list(engine.generate_batch(5, GenerationConfig(seed=1))) == []
