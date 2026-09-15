"""The pool-build branch (WS5 T7).

Runs the ladder once, in its own DAG branch, and writes rows that a later
run reads back instead of re-inferring.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,missing-class-docstring,protected-access,unused-argument

from __future__ import annotations

import apache_beam as beam
import pytest
from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.util import assert_that, equal_to
from sdfb_beam.dofns.pools import BuildFreeTextPoolsDoFn
from sdfb_beam.pools.store import row_to_pool
from sdfb_core.contracts import TableSchema
from sdfb_core.engines.b1_rag.engine import clear_free_text_pool_cache
from sdfb_core.engines.base import GenerationContext
from sdfb_core.pools import FreeTextPool, InMemoryFreeTextPoolStore

_COLS = ["col_a", "col_b"]
_DIGEST = "digest-branch"
_MODEL = "gs://m/qwen3/v1"


@pytest.fixture(autouse=True)
def _fresh_cache():
  clear_free_text_pool_cache()
  yield
  clear_free_text_pool_cache()


def _ctx(**overrides) -> GenerationContext:
  defaults = dict(
      table_schema=TableSchema.model_validate({
          "table_info": {
              "table_id": "demo.t"
          },
          "schema": [{
              "name": c,
              "type": "STRING",
              "mode": "REQUIRED"
          } for c in _COLS],
      }),
      reference_rows=[{
          c: f"{c} reference prose value number {i}" for c in _COLS
      } for i in range(60)],
      reference_digest=_DIGEST,
      model_uri=_MODEL,
      pipeline_run_id="run-branch",
      num_rows=40,
  )
  defaults.update(overrides)
  return GenerationContext(**defaults)


class _StubClient:

  def __init__(self):
    self.call_count = 0

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
    col = next((c for c in _COLS if f"'{c}'" in prompt), "unknown")
    return [{
        "values": [f"{col}-gen-{choice}-{i}" for i in range(32)]
    } for choice in range(n)]


def test_branch_emits_one_row_per_free_text_column():
  with TestPipeline() as p:
    columns = (
        p
        | beam.Create([None])
        | beam.ParDo(BuildFreeTextPoolsDoFn("b1_rag", _StubClient(), _ctx()))
        | beam.Map(lambda r: r["column"]))
    assert_that(columns, equal_to(_COLS))


def test_emitted_rows_carry_the_run_identity():
  dofn = BuildFreeTextPoolsDoFn("b1_rag", _StubClient(), _ctx())
  dofn.setup()
  rows = list(dofn.process(None))
  assert rows
  for row in rows:
    assert row["reference_digest"] == _DIGEST
    assert row["model_uri"] == _MODEL
    assert row["values"], "an empty pool must never be written"


def test_emitted_rows_round_trip_back_into_pools():
  """What the branch writes is exactly what the read path consumes."""
  dofn = BuildFreeTextPoolsDoFn("b1_rag", _StubClient(), _ctx())
  dofn.setup()
  pools = [row_to_pool(r) for r in dofn.process(None)]
  assert all(isinstance(p, FreeTextPool) for p in pools)
  store = InMemoryFreeTextPoolStore(pools)
  assert store.exists(_DIGEST, _MODEL) is True
  assert {p.column for p in store.fetch(_DIGEST, _MODEL)} == set(_COLS)


def test_branch_ignores_an_attached_store_so_it_cannot_short_circuit_itself():
  """If the branch read its own output it would emit nothing on a re-run
    and the artifact would never be refreshed."""
  prefilled = InMemoryFreeTextPoolStore([
      FreeTextPool(
          reference_digest=_DIGEST,
          model_uri=_MODEL,
          column=c,
          target=2,
          values=("stale-1", "stale-2"),
      ) for c in _COLS
  ])
  client = _StubClient()
  dofn = BuildFreeTextPoolsDoFn(
      "b1_rag", client,
      _ctx(pool_store=prefilled, freetext_pools_table="p.d.t"))
  dofn.setup()
  rows = list(dofn.process(None))
  assert client.call_count > 0, "branch must build, not read"
  assert all("stale" not in v for r in rows for v in r["values"])


def test_dag_gains_the_branch_and_gate_only_when_a_store_is_passed():
  """None store => DAG unchanged, exactly like rag_chunks (WS5 §2). A
    store => the branch appears AND Generate's input is gated on its output
    (AwaitFreeTextPools) — the R1/R3 cold runs proved that an ungated
    Generate rebuilds every pool concurrently with the branch. The branch
    writes the store itself, so no WriteFreeTextPools sink exists anymore."""
  from sdfb_beam.pipeline import PipelineConfig, build_pipeline

  def _labels(freetext_pools_store):
    cfg = PipelineConfig(
        table_schema=_ctx().table_schema,
        engine_name="b1_rag",
        model_client=_StubClient(),
        num_rows=4,
        batch_size=4,
        run_id="r1",
        model_uri=_MODEL,
        freetext_pools_table="p.d.freetext_pools",
    )
    with TestPipeline() as p:
      build_pipeline(
          p,
          reference_rows=_ctx().reference_rows,
          config=cfg,
          landing_sink=beam.Map(lambda x: x),
          dlq_sink=beam.Map(lambda x: x),
          freetext_pools_store=freetext_pools_store,
      )
      return {str(t.full_label) for t in p.transforms_stack[0].parts}

  without = _labels(None)
  assert not any("BuildFreeTextPools" in lbl for lbl in without)
  assert not any("AwaitFreeTextPools" in lbl for lbl in without)

  with_store = _labels(InMemoryFreeTextPoolStore())
  assert any("BuildFreeTextPools" in lbl for lbl in with_store)
  assert any("AwaitFreeTextPools" in lbl for lbl in with_store)
  assert not any("WriteFreeTextPools" in lbl for lbl in with_store)


# ---------------------------------------------------------------------------
# 2026-07-28 R1 crash: the pool branch handed the engine the RAW gs://
# embedder_uri. GenerateRecordsDoFn warm-pulls it to /local-ssd/embedder and
# rewrites the ctx BEFORE engine.setup(); BuildFreeTextPoolsDoFn skipped that
# localization, so BgeEmbedder passed the gs:// URI to
# AutoTokenizer.from_pretrained, which treats it as a HuggingFace repo id ->
# HFValidationError -> 4 bundle retries -> job FAILED.
# ---------------------------------------------------------------------------
def test_pool_branch_localizes_a_gcs_embedder_before_engine_setup(monkeypatch):
  from sdfb_beam.dofns import localize as localize_mod
  from sdfb_beam.dofns import pools as pools_mod

  pulled = {}
  monkeypatch.setattr(
      localize_mod,
      "localize_gcs_prefix",
      lambda uri, dest: pulled.setdefault("dir", "/local-ssd/embedder-test"),
  )

  seen = {}

  class _SpyEngine:

    def setup(self, client, ctx):
      seen["embedder_uri"] = ctx.embedder_uri
      self._free_text_pools = {}

    def teardown(self):
      pass

  monkeypatch.setattr(pools_mod, "get_engine", lambda name: _SpyEngine)

  ctx = _ctx(embedder_uri="gs://bucket/synthetic/models/embedders/bge/v1")
  dofn = pools_mod.BuildFreeTextPoolsDoFn("b1_rag", _StubClient(), ctx)
  dofn.setup()
  assert seen["embedder_uri"] == "/local-ssd/embedder-test", (
      "engine must never see a gs:// embedder_uri")


def test_pool_branch_leaves_a_local_embedder_uri_untouched(monkeypatch):
  from sdfb_beam.dofns import pools as pools_mod

  seen = {}

  class _SpyEngine:

    def setup(self, client, ctx):
      seen["embedder_uri"] = ctx.embedder_uri
      self._free_text_pools = {}

    def teardown(self):
      pass

  monkeypatch.setattr(pools_mod, "get_engine", lambda name: _SpyEngine)
  ctx = _ctx(embedder_uri="/local-ssd/embedder")
  pools_mod.BuildFreeTextPoolsDoFn("b1_rag", _StubClient(), ctx).setup()
  assert seen["embedder_uri"] == "/local-ssd/embedder"


# ---------------------------------------------------------------------------
# 2026-07-29 four-run postmortem: the branch writes its own rows (blocking)
# so the AwaitFreeTextPools gate downstream can only open once a Generate
# setup's store fetch will hit. R1/R3 evidence for why the gate exists: with
# WriteFreeTextPools as a sibling sink, Generate raced the branch and every
# cold pool was built TWICE on one T4 (R3: 2x 13 ladders, 2,005 s + 2,026 s).
# ---------------------------------------------------------------------------
def test_branch_writes_rows_through_the_store_before_emitting():
  written: dict = {}

  class _SpyStore:

    def write_rows(self, rows):
      written["rows"] = list(rows)

  dofn = BuildFreeTextPoolsDoFn(
      "b1_rag", _StubClient(), _ctx(), store=_SpyStore())
  dofn.setup()
  rows = list(dofn.process(None))
  assert rows
  assert written["rows"] == rows, "the store write must cover every emitted row"


def test_a_store_write_failure_is_loud_but_never_fatal():
  """Pools are an optimisation: a dead store must not kill the bundle,
    and rows must still be emitted so the DAG gate opens and Generate can
    fall back to building pools itself."""

  class _BrokenStore:

    def write_rows(self, rows):
      raise RuntimeError("bq down")

  dofn = BuildFreeTextPoolsDoFn(
      "b1_rag", _StubClient(), _ctx(), store=_BrokenStore())
  dofn.setup()
  rows = list(dofn.process(None))
  assert rows, "rows must still flow on a failed store write"


def test_rows_record_the_real_build_info_not_defaults():
  """2026-07-29 postmortem, freetext_pools screenshots: every stored row
    had attempts=0 / stagnated=false, and `target` was silently set to the
    ACHIEVED size (COL_047: stored target=386 for a 512-target build
    that ran 8 attempts and ended undersized). The engine must record per-
    column build info and the branch must persist it."""

  class _ParrotClient:
    """Always the same 5 values — the ladder can never reach target."""

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
      return [{"values": [f"fixed-{i}" for i in range(5)]} for _ in range(n)]

  dofn = BuildFreeTextPoolsDoFn("b1_rag", _ParrotClient(), _ctx())
  dofn.setup()
  rows = list(dofn.process(None))
  assert rows
  info = dofn._engine._pool_build_info
  for row in rows:
    col_info = info[row["column"]]
    assert row["attempts"] == col_info["attempts"] >= 1
    assert row["target"] == col_info["target"] >= 1
    assert row["stagnated"] == col_info["stagnated"]


class _DomainStore:
  """Fake `SourceValueStore`: the LLM's first few generations for col_a
    "already exist" in the full source domain."""

  def __init__(self) -> None:
    self.calls: list[str] = []
    self.domain = frozenset(
        f"col_a-gen-{c}-{i}" for c in range(4) for i in range(8))

  def fetch_distinct(self, column: str) -> frozenset[str] | None:
    self.calls.append(column)
    return self.domain


def test_branch_attaches_source_value_store_and_rejects_domain_values():
  """The build branch is where the full-domain rejection must engage
    (2026-08-05 B_TABLE R1: pools memorized 33-99% of 10 columns because
    only the profiled sample was rejected against)."""
  store = _DomainStore()
  dofn = BuildFreeTextPoolsDoFn(
      "b1_rag", _StubClient(), _ctx(), source_value_store=store)
  dofn.setup()
  rows = list(dofn.process(None))
  assert "col_a" in store.calls
  emitted = {v for r in rows for v in r["values"]}
  assert emitted, "pools still build from the novel values"
  assert not emitted & store.domain


def test_branch_does_not_cry_store_absent(caplog):
  """The branch blanks its own store BY DESIGN (self-read guard); the
    `freetext_pool_store_absent` WARNING is reserved for runs where nobody
    passed --freetext_pools_table. Two E2E reports (2026-08-05, 2026-08-07)
    misread the branch's own blank as a store outage / setup retry."""
  import logging

  dofn = BuildFreeTextPoolsDoFn("b1_rag", _StubClient(), _ctx())
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    dofn.setup()
  text = "\n".join(r.message for r in caplog.records)
  assert "freetext_pool_store_absent" not in text
