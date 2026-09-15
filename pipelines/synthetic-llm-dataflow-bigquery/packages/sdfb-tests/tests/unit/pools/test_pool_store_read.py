"""Pools come from the store when present — no LLM call at all (WS5 T6).

Resolution order is store -> process cache -> build. The store is
cross-PROCESS and authoritative; _POOL_CACHE stays as the intra-process
tier that survives setup() retries within one worker.

2026-07-26 1M E2E: every autoscale wave started a fresh worker process with
an empty _POOL_CACHE and rebuilt all three pools. 108 rebuilds, 21 cache
hits, all 21 inside a single two-minute window in one process.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=missing-class-docstring,protected-access,unused-argument

from __future__ import annotations

import pytest
from sdfb_core.contracts import TableSchema
from sdfb_core.engines.b1_rag.engine import B1RagEngine, clear_free_text_pool_cache
from sdfb_core.engines.base import GenerationContext
from sdfb_core.observability import parse_milestone
from sdfb_core.pools import FreeTextPool, InMemoryFreeTextPoolStore

_COLS = ["col_a", "col_b"]
_DIGEST = "digest-ws5"
_MODEL = "gs://m/qwen3/v1"


@pytest.fixture(autouse=True)
def _fresh_cache():
  clear_free_text_pool_cache()
  yield
  clear_free_text_pool_cache()


def _schema() -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.t"
      },
      "schema": [{
          "name": c,
          "type": "STRING",
          "mode": "REQUIRED"
      } for c in _COLS],
  })


def _rows() -> list[dict]:
  return [{
      c: f"{c} reference prose value number {i}" for c in _COLS
  } for i in range(60)]


def _ctx(**overrides) -> GenerationContext:
  defaults = dict(
      table_schema=_schema(),
      reference_rows=_rows(),
      reference_digest=_DIGEST,
      model_uri=_MODEL,
      pipeline_run_id="run-ws5",
      num_rows=40,
  )
  defaults.update(overrides)
  return GenerationContext(**defaults)


class _CountingClient:

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


def _stored(column: str, **kw) -> FreeTextPool:
  return FreeTextPool(
      reference_digest=kw.pop("digest", _DIGEST),
      model_uri=kw.pop("model", _MODEL),
      column=column,
      target=kw.pop("target", 8),
      values=kw.pop("values", tuple(f"{column}-stored-{i}" for i in range(8))),
      stagnated=kw.pop("stagnated", False),
      attempts=kw.pop("attempts", 3),
  )


def _milestone_names(caplog) -> list[str]:
  return [
      m["name"] for m in (parse_milestone(r.getMessage())
                          for r in caplog.records) if m
  ]


def test_populated_store_short_circuits_every_ladder(caplog):
  store = InMemoryFreeTextPoolStore([_stored(c) for c in _COLS])
  client = _CountingClient()
  engine = B1RagEngine()
  with caplog.at_level("INFO"):
    engine.setup(client, _ctx(pool_store=store))
  assert client.call_count == 0, "stored pools must skip all LLM calls"
  assert engine._free_text_pools["col_a"] == list(_stored("col_a").values)
  assert "freetext_pool_store_hit" in _milestone_names(caplog)


def test_store_miss_falls_through_to_building(caplog):
  client = _CountingClient()
  engine = B1RagEngine()
  with caplog.at_level("INFO"):
    engine.setup(client, _ctx(pool_store=InMemoryFreeTextPoolStore()))
  assert client.call_count > 0
  assert engine._free_text_pools["col_a"]
  assert "freetext_pool_store_miss" in _milestone_names(caplog)


def test_partial_store_only_builds_the_missing_column():
  """A store holding col_a must not cost col_a any LLM calls, while col_b
    still builds normally."""
  store = InMemoryFreeTextPoolStore([_stored("col_a")])
  client = _CountingClient()
  engine = B1RagEngine()
  engine.setup(client, _ctx(pool_store=store))
  assert engine._free_text_pools["col_a"] == list(_stored("col_a").values)
  assert engine._free_text_pools["col_b"]
  assert client.call_count > 0


def test_pools_for_a_different_model_are_ignored():
  """The same reference sample under a different LLM is a different pool."""
  store = InMemoryFreeTextPoolStore(
      [_stored(c, model="gs://m/other/v1") for c in _COLS])
  client = _CountingClient()
  engine = B1RagEngine()
  engine.setup(client, _ctx(pool_store=store))
  assert client.call_count > 0


def test_a_store_outage_does_not_fail_the_run(caplog):
  """The store is an optimisation, not a dependency."""

  class _BrokenStore:

    def fetch(self, reference_digest, model_uri):
      raise RuntimeError("bigquery unavailable")

    def exists(self, reference_digest, model_uri):
      raise RuntimeError("bigquery unavailable")

  client = _CountingClient()
  engine = B1RagEngine()
  with caplog.at_level("INFO"):
    engine.setup(client, _ctx(pool_store=_BrokenStore()))
  assert engine._free_text_pools["col_a"]
  assert "freetext_pool_store_error" in _milestone_names(caplog)


def test_no_store_behaves_exactly_as_before():
  client = _CountingClient()
  engine = B1RagEngine()
  engine.setup(client, _ctx())
  assert client.call_count > 0
  assert engine._free_text_pools["col_a"]


def test_empty_stored_values_are_not_trusted():
  """A row with an empty values array must not silently produce an empty
    pool — build instead."""
  store = InMemoryFreeTextPoolStore([_stored(c, values=()) for c in _COLS])
  client = _CountingClient()
  engine = B1RagEngine()
  engine.setup(client, _ctx(pool_store=store))
  assert client.call_count > 0
