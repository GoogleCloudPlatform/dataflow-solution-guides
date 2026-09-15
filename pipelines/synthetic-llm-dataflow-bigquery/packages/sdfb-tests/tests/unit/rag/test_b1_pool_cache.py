"""Process-level free-text pool cache (2026-07-24 16:35 E2E fix).

A FreeTextEmptyYieldError out of DoFn.setup() makes Dataflow retry the
bundle: a FRESH DoFn/engine re-runs setup() in the same worker process.
Before this fix the retry rebuilt every column's pool from scratch
(~15-19 min each attempt). The cache — keyed (reference_digest, model_uri,
column, target) — hands completed pools to the retry, like the module-level
vLLM server reuse (_SERVER_REFS) already does for the server.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=missing-class-docstring,protected-access,unused-argument

from __future__ import annotations

import pytest
from sdfb_core.contracts import TableSchema
from sdfb_core.engines.b1_rag.engine import (
    B1RagEngine,
    clear_free_text_pool_cache,
)
from sdfb_core.engines.base import FreeTextEmptyYieldError, GenerationContext
from sdfb_core.observability import parse_milestone

_COLS = ["col_a", "col_b"]


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


def _ctx(digest: str = "digest-1", **overrides) -> GenerationContext:
  defaults = dict(
      table_schema=_schema(),
      reference_rows=_rows(),
      reference_digest=digest,
      model_uri="gs://m/qwen3/v1",
      pipeline_run_id="run-cache-1",
      num_rows=40,
  )
  defaults.update(overrides)
  return GenerationContext(**defaults)


class _CountingClient:

  def __init__(self):
    self.call_count = 0
    self.prompts: list[str] = []

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
    self.prompts.append(prompt)
    col = next((c for c in _COLS if f"'{c}'" in prompt), "unknown")
    return [{
        "values": [f"{col}-gen-{choice}-{i}" for i in range(32)]
    } for choice in range(n)]


def _milestone_names(caplog) -> list[str]:
  return [
      m["name"] for m in (parse_milestone(r.getMessage())
                          for r in caplog.records) if m
  ]


def test_second_engine_same_digest_reuses_pools(caplog):
  c1 = _CountingClient()
  e1 = B1RagEngine()
  e1.setup(c1, _ctx())
  assert c1.call_count > 0
  c2 = _CountingClient()
  e2 = B1RagEngine()
  with caplog.at_level("INFO"):
    e2.setup(c2, _ctx())
  assert c2.call_count == 0, "cached pools must skip all LLM calls"
  assert e2._free_text_pools == e1._free_text_pools
  assert "freetext_pool_cache_hit" in _milestone_names(caplog)


def test_different_digest_rebuilds():
  e1 = B1RagEngine()
  e1.setup(_CountingClient(), _ctx("digest-1"))
  c2 = _CountingClient()
  e2 = B1RagEngine()
  e2.setup(c2, _ctx("digest-2"))
  assert c2.call_count > 0


def test_empty_digest_disables_cache():
  e1 = B1RagEngine()
  e1.setup(_CountingClient(), _ctx(""))
  c2 = _CountingClient()
  e2 = B1RagEngine()
  e2.setup(c2, _ctx(""))
  assert c2.call_count > 0


class _ColBParrotsClient(_CountingClient):

  def generate_json(self, prompt, json_schema, **kw):
    if "'col_b'" in prompt:
      self.call_count += 1
      self.prompts.append(prompt)
      return [{"values": ["col_b reference prose value number 0"]}]
    return super().generate_json(prompt, json_schema, **kw)


def test_retry_after_strict_failure_skips_completed_sibling():
  c1 = _ColBParrotsClient()
  e1 = B1RagEngine()
  with pytest.raises(FreeTextEmptyYieldError):
    e1.setup(c1, _ctx(strict_freetext=True))
  # Retry (fresh engine, same process): col_a must come from cache.
  c2 = _ColBParrotsClient()
  e2 = B1RagEngine()
  with pytest.raises(FreeTextEmptyYieldError):
    e2.setup(c2, _ctx(strict_freetext=True))
  assert c2.prompts, "the failing column is still retried"
  assert all("'col_a'" not in p for p in c2.prompts), (
      "the retry must not rebuild the completed sibling pool")
