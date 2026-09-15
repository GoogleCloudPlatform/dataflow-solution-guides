"""Column-parallel free-text pool ladders (2026-07-25 perf fix).

The 2026-07-24 12:46 E2E built 3 columns' pools strictly sequentially
(717s + 505s + 330s). The ladders are independent per column and vLLM's
continuous batching absorbs concurrent requests, so they run in a bounded
thread pool. Seed-example retrieval keeps using the embedder SEQUENTIALLY
(HF fast tokenizers are not thread-safe — "Already borrowed").
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=protected-access,unused-argument

from __future__ import annotations

import threading
import time

import pytest
from sdfb_core.contracts import TableSchema
from sdfb_core.engines.b1_rag.engine import B1RagEngine
from sdfb_core.engines.base import FreeTextEmptyYieldError, GenerationContext

_COLS = ["col_a", "col_b", "col_c"]


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
  # 60 distinct multi-word values per column -> FREE_TEXT, no identifier
  # shape, no relaxed template (whitespace).
  return [{
      c: f"{c} reference prose value number {i}" for c in _COLS
  } for i in range(60)]


def _ctx(**overrides) -> GenerationContext:
  defaults = dict(
      table_schema=_schema(),
      reference_rows=_rows(),
      pipeline_run_id="run-par-1",
      num_rows=40,
  )
  defaults.update(overrides)
  return GenerationContext(**defaults)


class _ConcurrencyProbeClient:
  """Prompt-keyed deterministic values; records max in-flight calls."""

  def __init__(self):
    self._lock = threading.Lock()
    self._inflight = 0
    self.max_inflight = 0
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
    with self._lock:
      self._inflight += 1
      self.call_count += 1
      self.max_inflight = max(self.max_inflight, self._inflight)
    time.sleep(0.05)  # long enough for the other ladders to enter
    col = next((c for c in _COLS if f"'{c}'" in prompt), "unknown")
    out = [{
        "values": [f"{col}-gen-{choice}-{i}" for i in range(32)]
    } for choice in range(n)]
    with self._lock:
      self._inflight -= 1
    return out


def test_ladders_run_concurrently_and_pools_stay_per_column():
  client = _ConcurrencyProbeClient()
  engine = B1RagEngine()
  engine.setup(client, _ctx())
  assert client.max_inflight >= 2, "expected overlapping ladder calls"
  for c in _COLS:
    pool = engine._free_text_pools[c]
    assert pool and all(v.startswith(f"{c}-gen-") for v in pool), (
        "columns must not receive each other's values")


class _OneColumnFailsClient(_ConcurrencyProbeClient):
  """col_b parrots (parsed>0, novel=0) -> strict raise; others succeed."""

  def generate_json(self, prompt, json_schema, **kw):
    if "'col_b'" in prompt:
      with self._lock:
        self.call_count += 1
      return [{"values": ["col_b reference prose value number 0"]}]
    return super().generate_json(prompt, json_schema, **kw)


def test_strict_failure_on_one_column_still_finishes_siblings():
  client = _OneColumnFailsClient()
  engine = B1RagEngine()
  with pytest.raises(FreeTextEmptyYieldError):
    engine.setup(client, _ctx(strict_freetext=True))
  # Siblings must have completed their ladders (their calls happened) —
  # the pool cache turns those completed builds into retry savings.
  assert client.call_count > 0
