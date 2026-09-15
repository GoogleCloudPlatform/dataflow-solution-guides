"""WS2 §4b.2: pool target = min(num_rows, column_distinct, 512), filled by
multiple bounded calls. Closes the 28-619x oversampling of the 2026-07-19
run (3 FREE_TEXT columns capped at 32 values over 1000 rows)."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,protected-access,unused-argument

from __future__ import annotations

from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationContext
from sdfb_core.engines.b1_rag import B1RagEngine
from sdfb_core.engines.b1_rag.engine import (
    _FREE_TEXT_POOL_MAX,
    _POOL_VALUES_PER_CALL,
    _pool_llm_yield,
)
from sdfb_core.engines.b1_rag.profile import profile_columns
from sdfb_core.rag.embedding import HashingEmbedder


class _BatchClient:
  """Yields _POOL_VALUES_PER_CALL fresh values per requested choice, like
    a healthy LLM honoring `n` (vLLM parallel sampling)."""

  def __init__(self) -> None:
    self.calls = 0
    self._issued = 0

  def generate_json(self, prompt, json_schema, *, n=1, **kw):
    self.calls += 1
    out = []
    for _ in range(max(1, n)):
      base = self._issued
      self._issued += _POOL_VALUES_PER_CALL
      out.append({
          "values": [
              f"novel value {base + i}" for i in range(_POOL_VALUES_PER_CALL)
          ]
      })
    return out


def _schema_and_rows(n_rows: int = 300):
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.notes"
      },
      "schema": [
          {
              "name": "id",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "notes",
              "type": "STRING",
              "mode": "REQUIRED"
          },
      ],
      "primary_keys": ["id"],
  })
  rows = [{
      "id": i,
      "notes": f"customer reported issue number {i} with details"
  } for i in range(n_rows)]
  return schema, rows


def test_constants():
  assert _FREE_TEXT_POOL_MAX == 512
  assert _POOL_VALUES_PER_CALL == 32


def test_pool_scales_past_32_with_batched_calls():
  schema, rows = _schema_and_rows(300)
  client = _BatchClient()
  engine = B1RagEngine(embedder=HashingEmbedder(dim=32))
  ctx = GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="d",
      pipeline_run_id="pool-scale",
      num_rows=200,
      # Ladder-mechanics test: expansion off forces the pool path
      # (wave 4 skips ladders for expandable columns).
      freetext_expansion="off",
  )
  engine.setup(client, ctx)
  pool = engine._free_text_pools["notes"]
  # target = min(num_rows=200, distinct=300, 512) = 200
  assert len(pool) >= 200
  # n=4 choices/call -> per-round yield 128: 2 calls cover target=200.
  assert client.calls >= -(-200 // (_POOL_VALUES_PER_CALL * 4))
  engine.teardown()


def test_pool_target_prefers_exact_source_distinct():
  """Tier-2 exact distinct lifts a sample-starved target (ADR 0022):
    60 sample-distinct notes + source_distinct 4000 → num_rows bound (500),
    not the sample's 60. Missing/zero hints keep sample behavior."""
  schema, rows = _schema_and_rows(300)
  for i, r in enumerate(rows):
    r["notes"] = f"repeating customer note body number {i % 60} with extended details"
  engine = B1RagEngine(embedder=HashingEmbedder(dim=32))
  ctx = GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="d",
      pipeline_run_id="pool-exact",
      num_rows=500,
      source_distinct={"notes": 4000},
  )
  prof = profile_columns(schema, rows)["notes"]
  assert engine._pool_target(prof, ctx) == 500
  ctx_no_hint = ctx.model_copy(update={"source_distinct": {}})
  assert engine._pool_target(prof, ctx_no_hint) == 60
  ctx_zero = ctx.model_copy(update={"source_distinct": {"notes": 0}})
  assert engine._pool_target(prof, ctx_zero) == 60


def test_pool_target_respects_column_distinct():
  schema, rows = _schema_and_rows(300)
  # 60 distinct long notes values → target = min(num_rows=500, 60, 512) = 60.
  # (If b1's profiler routes this fixture to CATEGORICAL instead of
  # FREE_TEXT, raise the distinct count / mean length until it profiles
  # FREE_TEXT — the assertions below must run unconditionally.)
  for i, r in enumerate(rows):
    r["notes"] = f"repeating customer note body number {i % 60} with extended details"
  client = _BatchClient()
  engine = B1RagEngine(embedder=HashingEmbedder(dim=32))
  ctx = GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="d",
      pipeline_run_id="pool-cap",
      num_rows=500,
  )
  engine.setup(client, ctx)
  pool = engine._free_text_pools["notes"]
  assert len(pool) <= 60  # distinct bound, not 512
  assert len(pool) >= _POOL_VALUES_PER_CALL  # scaled past the old 32 cap
  engine.teardown()


def _free_text_prof():
  from sdfb_core.engines.b1_rag.profile import ColumnKind, ColumnProfile

  return ColumnProfile(
      name="notes",
      bq_type="STRING",
      kind=ColumnKind.FREE_TEXT,
      nullable=False,
      null_fraction=0.0,
  )


def test_pool_llm_yield_empty_yield_stops_after_full_ladder():
  """An all-empty yield used to ride the full 2*ceil(target/32)=32-call
    budget (2026-07-23 E2E: ~30 min of T4 setup across 2 such columns).
    Every escalation level must run once; after that, attempts that add no
    novel values are pure waste and the ladder must stop."""

  class _EmptyClient:

    def __init__(self) -> None:
      self.calls = 0

    def generate_json(self, prompt, json_schema, **kw):
      self.calls += 1
      return [{"values": []}]

  from sdfb_core.engines.base import escalating_sampling

  client = _EmptyClient()
  y = _pool_llm_yield(client, "p", {}, _free_text_prof(), [], target=512)
  assert y.pool == []
  assert client.calls == len(escalating_sampling())  # 3, not 32


def test_pool_llm_yield_stops_when_novel_yield_stagnates():
  """2026-07-23 E2E COL_052: 32 attempts parsed 1035 values for a
    257-value pool — after the early attempts the model only re-emitted
    duplicates/echoes. Consecutive low-novelty attempts (after the ladder is
    exhausted) must end the loop, keeping whatever the early attempts won."""

  class _StagnantClient:

    def __init__(self) -> None:
      self.calls = 0

    def generate_json(self, prompt, json_schema, **kw):
      self.calls += 1
      return [{"values": [f"same value {i}" for i in range(8)]}]

  client = _StagnantClient()
  y = _pool_llm_yield(client, "p", {}, _free_text_prof(), [], target=512)
  assert y.pool == [f"same value {i}" for i in range(8)]  # early yield kept
  assert client.calls <= 6  # ladder (3) + stagnation window, not 32


def test_pool_llm_yield_healthy_client_still_reaches_target():
  """The stagnation exit must not fire while the pool is actually growing."""
  from sdfb_core.engines.b1_rag.engine import _POOL_PARALLEL_CHOICES

  client = _BatchClient()
  y = _pool_llm_yield(client, "p", {}, _free_text_prof(), [], target=512)
  assert len(y.pool) >= 512
  # 4 full-yield calls at 128 values/round (was 16 at 32/round).
  assert client.calls == -(-512 //
                           (_POOL_VALUES_PER_CALL * _POOL_PARALLEL_CHOICES))


# --- n-choice batched pool calls (2026-07-25 perf fix) ---------------------


class _RecordingArrayClient:
  """Returns `n` distinct {"values": [...]} dicts per call and records
    the `n` each call requested."""

  def __init__(self,
               values_per_choice: int = 32,
               novel_per_call: int | None = None):
    self.calls: list[int] = []
    self._values_per_choice = values_per_choice
    # When set, ONLY this many values across the whole call are novel;
    # the rest repeat a fixed token (stagnation/cap scenarios).
    self._novel_per_call = novel_per_call
    self._counter = 0

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
    self.calls.append(n)
    out = []
    for choice in range(n):
      values = []
      for i in range(self._values_per_choice):
        if self._novel_per_call is not None and (
            choice * self._values_per_choice + i >= self._novel_per_call):
          values.append("dup-fixed-token")
        else:
          self._counter += 1
          values.append(f"novel-{self._counter:05d}")
      out.append({"values": values})
    return out


def _free_text_profile_with_observed():
  from sdfb_core.engines.b1_rag.profile import ColumnKind, ColumnProfile

  return ColumnProfile(
      name="col_a",
      bq_type="STRING",
      kind=ColumnKind.FREE_TEXT,
      nullable=False,
      null_fraction=0.0,
      observed_values=tuple(f"observed value number {i}" for i in range(600)),
      text_examples=("observed value number 0", "observed value number 1"),
  )


def test_pool_call_requests_parallel_choices():
  from sdfb_core.engines.b1_rag import engine as b1_engine

  client = _RecordingArrayClient()
  y = _pool_llm_yield(
      client, "p", {}, _free_text_profile_with_observed(), ["seed"], target=512)
  assert client.calls, "expected at least one LLM call"
  assert all(n == b1_engine._POOL_PARALLEL_CHOICES for n in client.calls)
  assert len(y.pool) >= 512
  # 4 choices x 32 values = 128 novel per call -> target hit in 4 calls.
  assert y.attempts == 4


def test_pool_call_budget_scales_with_choices():
  # Every call yields exactly 5 novel values (>= _POOL_STAGNATION_MIN_NOVEL,
  # so the stagnation exit never fires) -> the loop must stop at the
  # SCALED cap: max(len(levels), 2*ceil(512/(32*4))) = 8, not 32.
  client = _RecordingArrayClient(novel_per_call=5)
  y = _pool_llm_yield(
      client, "p", {}, _free_text_profile_with_observed(), ["seed"], target=512)
  assert y.attempts == 8


class _FakeSourceValueStore:
  """`fetch_distinct(column)` → the column's FULL source domain."""

  def __init__(self, values: dict[str, frozenset[str]]) -> None:
    self._values = values

  def fetch_distinct(self, column: str) -> frozenset[str] | None:
    return self._values.get(column)


def test_pool_target_takes_the_source_filter_cardinality_when_exact_stats_are_absent(
    caplog,):
  # 2026-08-25/26 R6 runs: A_COL_015 (95% empty) showed 94 distinct in
  # the 10k reference sample while the source filter the SAME setup
  # fetched a moment later held 4,022 — and the target stayed 94 because
  # the Tier-2 exact count (ADR 0022, --source_stats=exact) was absent.
  # The filter's size IS the exact cardinality, already paid for.
  import logging

  schema, _ = _schema_and_rows(1)
  # 94 distinct substantive values over 300 rows (a sparse column).
  rows = [{"id": i, "notes": f"sparse ref {i % 94:04d}"} for i in range(300)]
  client = _BatchClient()
  engine = B1RagEngine(embedder=HashingEmbedder(dim=32))
  ctx = GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="d-source-filter-card",
      pipeline_run_id="pool-target-filter",
      num_rows=1_000_000,
      freetext_expansion="off",
      source_value_store=_FakeSourceValueStore(
          {"notes": frozenset(f"src ref {i:05d}" for i in range(4022))}),
  )
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    engine.setup(client, ctx)
  # target = min(num_rows=1M, source distinct=4022, cap=512) = 512, not 94.
  assert len(engine._free_text_pools["notes"]) == _FREE_TEXT_POOL_MAX
  text = "\n".join(r.message for r in caplog.records)
  assert "name=freetext_pool_built" in text
  assert f"target={_FREE_TEXT_POOL_MAX}" in text
  engine.teardown()


def test_pool_target_prefers_the_exact_stats_count_over_the_filter():
  # Tier-2 exact stats stay authoritative when present (ADR 0022).
  schema, rows = _schema_and_rows(300)
  engine = B1RagEngine(embedder=HashingEmbedder(dim=32))
  ctx = GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="d-exact-wins",
      pipeline_run_id="pool-target-exact",
      num_rows=1_000_000,
      source_distinct={"notes": 40},
      freetext_expansion="off",
  )
  engine._ctx = ctx
  prof = profile_columns(schema, rows)["notes"]
  # exact stats (40) beat the filter (4022)
  assert engine._pool_target(prof, ctx, source_cardinality=4022) == 40
  no_exact = ctx.model_copy(update={"source_distinct": {}})
  # the filter (4022 → cap 512) beats the sample distinct (300)
  assert engine._pool_target(prof, no_exact, source_cardinality=4022) == 512
  # neither: the sample distinct remains the stand-in
  assert engine._pool_target(prof, no_exact, source_cardinality=0) == 300
