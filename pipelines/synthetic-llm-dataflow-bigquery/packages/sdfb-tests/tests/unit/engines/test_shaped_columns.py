"""Shaped STRING columns bypass the LLM free-text pool (both engines).

2026-07-17 E2E defects this locks down:

  - B.1: date-shaped STRING columns (COL_044/COL_034/COL_061) went to the
    LLM pool; plausible generated dates collide with the dense source
    keyspace → memorization_flags CRITICAL despite zero exemplar echo.
    They must profile as TEMPORAL and range-sample, rendered back to the
    observed string format.
  - B.2: the 24-char upper-hex identifier COL_001 went to the LLM pool;
    qwen3-4b echoed the shown exemplars on all escalation attempts →
    novel=0 on 55/63 batches, 872 rows dead. Fixed-alphabet identifiers
    must be generated format-preservingly, no LLM involved.
  - B.1: identifier-shaped columns (ID_COL, UUID-shaped) likewise skip the
    LLM pool and generate per-row (a 32-value pool over 1000 rows collapses
    distinctness).
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=redefined-outer-name,unused-argument,use-implicit-booleaness-not-comparison

from __future__ import annotations

import random
import re
from datetime import date, timedelta

import numpy as np
import pytest
from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationConfig, GenerationContext
from sdfb_core.engines.b1_rag import B1RagEngine, HashingEmbedder
from sdfb_core.engines.b1_rag.profile import ColumnKind, profile_columns
from sdfb_core.engines.b2_library.fidelity import profile_table
from sdfb_core.engines.b2_library.freetext import FreeTextHook


def _hex_upper_ids(n: int, length: int = 24, seed: int = 7) -> list[str]:
  rng = random.Random(seed)
  return [
      "".join(rng.choice("0123456789ABCDEF")
              for _ in range(length))
      for _ in range(n)
  ]


@pytest.fixture
def shaped_schema() -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.contracts"
      },
      "schema": [
          {
              "name": "id",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "event_date",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "device_key",
              "type": "STRING",
              "mode": "REQUIRED"
          },
      ],
      "primary_keys": ["id"],
  })


@pytest.fixture
def shaped_rows() -> list[dict]:
  keys = _hex_upper_ids(80)
  return [{
      "id": i,
      "event_date": (date(2023, 1, 1) + timedelta(days=i * 3)).isoformat(),
      "device_key": keys[i],
  } for i in range(80)]


@pytest.fixture
def shaped_ctx(shaped_schema, shaped_rows) -> GenerationContext:
  return GenerationContext(
      table_schema=shaped_schema,
      reference_rows=shaped_rows,
      reference_digest="shaped-digest",
      pipeline_run_id="shaped-run",
  )


class _RecordingClient:
  """Records calls; returns a values array (never expected to be hit)."""

  def __init__(self):
    self.calls: list[dict] = []

  def generate_json(self, prompt, json_schema, **kwargs):
    self.calls.append({"prompt": prompt, **kwargs})
    return [{"values": ["Should never be needed"]}]


# ---------------------------------------------------------------------------
# B.1 profiling — shaped strings leave the FREE_TEXT/LLM route
# ---------------------------------------------------------------------------


def test_b1_profiles_date_shaped_string_as_temporal(shaped_schema, shaped_rows):
  profiles = profile_columns(shaped_schema, shaped_rows)
  prof = profiles["event_date"]
  assert prof.kind is ColumnKind.TEMPORAL
  assert prof.temporal_format == "%Y-%m-%d"
  assert prof.numeric_min is not None and prof.numeric_max is not None


def test_b1_profiles_hex_identifier_with_shape(shaped_schema, shaped_rows):
  profiles = profile_columns(shaped_schema, shaped_rows)
  prof = profiles["device_key"]
  assert prof.kind is ColumnKind.FREE_TEXT
  assert prof.identifier_shape is not None


def test_b1_low_cardinality_date_strings_stay_categorical(shaped_schema):
  # 5 distinct load dates over 80 rows — enum in disguise, not TEMPORAL.
  rows = [{
      "id": i,
      "event_date": (date(2023, 1, 1) + timedelta(days=i % 5)).isoformat(),
      "device_key": _hex_upper_ids(80)[i],
  } for i in range(80)]
  profiles = profile_columns(shaped_schema, rows)
  assert profiles["event_date"].kind is ColumnKind.CATEGORICAL


# ---------------------------------------------------------------------------
# B.1 generation — no LLM call, format-true output
# ---------------------------------------------------------------------------


def test_b1_shaped_columns_never_call_the_llm(shaped_ctx):
  client = _RecordingClient()
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(client, shaped_ctx)
  assert client.calls == []


def test_b1_date_shaped_column_samples_novel_in_range_strings(
    shaped_ctx, shaped_rows):
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(_RecordingClient(), shaped_ctx)
  out = list(engine.generate_batch(200, GenerationConfig(seed=13)))
  assert out
  observed = {r["event_date"] for r in shaped_rows}
  values = [r.event_date for r in out]
  assert all(isinstance(v, str) for v in values)
  assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in values)
  assert min(values) >= min(observed)
  assert max(values) <= max(observed)
  novel_share = sum(v not in observed for v in values) / len(values)
  assert novel_share >= 0.5


def test_b1_identifier_column_generates_per_row_distinct(
    shaped_ctx, shaped_rows):
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(_RecordingClient(), shaped_ctx)
  out = list(engine.generate_batch(200, GenerationConfig(seed=13)))
  assert out
  values = [r.device_key for r in out]
  assert all(re.fullmatch(r"[0-9A-F]{24}", v) for v in values)
  # Per-row generation, not a 32-value pool sampled with replacement.
  assert len(set(values)) == len(values)
  # Never a reference value (the whole point — keyspace is 16^24).
  assert not set(values) & {r["device_key"] for r in shaped_rows}


def test_b1_identifier_generation_is_seed_reproducible(shaped_ctx):
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(_RecordingClient(), shaped_ctx)
  a = [
      r.device_key for r in engine.generate_batch(20, GenerationConfig(seed=5))
  ]
  b = [
      r.device_key for r in engine.generate_batch(20, GenerationConfig(seed=5))
  ]
  assert a == b


# ---------------------------------------------------------------------------
# B.2 — identifier-shaped columns bypass the LLM hook
# ---------------------------------------------------------------------------


def test_b2_profiles_hex_identifier_with_shape(shaped_schema, shaped_rows):
  profiles = profile_table(shaped_schema, shaped_rows)
  assert profiles["device_key"].identifier_shape is not None


def test_b2_identifier_column_skips_llm_and_preserves_format(
    shaped_schema, shaped_rows):
  profiles = profile_table(shaped_schema, shaped_rows)
  client = _RecordingClient()
  hook = FreeTextHook(client)
  rng = np.random.default_rng(3)
  values = hook.sample(profiles["device_key"], 100, GenerationConfig(seed=3),
                       rng)
  assert client.calls == []
  assert all(re.fullmatch(r"[0-9A-F]{24}", v) for v in values)
  assert len(set(values)) == len(values)
  assert not set(values) & {r["device_key"] for r in shaped_rows}


def test_b2_prose_columns_still_use_the_llm(shaped_schema):
  # A genuinely free-text column must keep the LLM path.
  rows = [{
      "id": i,
      "event_date": f"Long descriptive prose entry number {i} about nothing.",
      "device_key": f"Another distinct prose line {i} with plenty of words.",
  } for i in range(80)]
  profiles = profile_table(shaped_schema, rows)
  assert profiles["device_key"].identifier_shape is None
  client = _RecordingClient()
  hook = FreeTextHook(client)
  rng = np.random.default_rng(3)
  hook.sample(profiles["device_key"], 5, GenerationConfig(seed=3), rng)
  assert client.calls, "prose columns must still reach the LLM pool"
