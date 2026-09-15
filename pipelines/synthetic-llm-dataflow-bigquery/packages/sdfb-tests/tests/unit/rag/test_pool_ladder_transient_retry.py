"""A ladder thread that loses the vLLM fit race retries in-process.

2026-08-25 R6 1M run (job 2026-08-25_12_05_08): `A_TABLE/BuildFreeTextPools`
ran four ladders on a thread pool. Thread 1 entered the shared client's
setup at 19:24:36, measured an unfittable card five times and raised
`ModelLenUnfittableError` at 19:27:27 — sixty seconds before a sibling
embedder released 3.6 GiB and thread 2's spawn succeeded (19:28:27,
`vllm_ready` 19:30:29). Threads 2-4 then built their pools (564-685 s of
T4 work); `_build_free_text_pools` collected every future and re-raised
thread 1's error at 19:36:02, failing the bundle. Dataflow retried it:
re-embed (155 s), store fetches, and only then the missing 70 s ladder —
eight minutes of wall time for a column whose client had been healthy
since 19:30.

A transient client failure inside ONE ladder thread must be retried
in-process once the siblings have landed — never fail the bundle while
the client is usable.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=protected-access,unused-argument

from __future__ import annotations

import logging
import threading

import pytest
from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationContext
from sdfb_core.engines.b1_rag import B1RagEngine
from sdfb_core.engines.b1_rag.engine import _POOL_VALUES_PER_CALL
from sdfb_core.engines.base import ModelClientTransientError
from sdfb_core.rag.embedding import HashingEmbedder

_COLUMNS = ("memo_a", "memo_b", "memo_c")


class _FlakyClient:
  """The first `generate_json` call in the process raises the given
    error (the thread that lost the fit race); every later call yields
    fresh values — the server a sibling spawned meanwhile."""

  def __init__(self, error: Exception, *, fail_calls: int = 1) -> None:
    self._error = error
    self._fail_calls = fail_calls
    self._lock = threading.Lock()
    self.calls = 0
    self._issued = 0

  def generate_json(self, prompt, json_schema, *, n=1, **kw):
    with self._lock:
      self.calls += 1
      if self.calls <= self._fail_calls:
        raise self._error
      out = []
      for _ in range(max(1, n)):
        base = self._issued
        self._issued += _POOL_VALUES_PER_CALL
        out.append({
            "values": [
                f"fresh memo {base + i}" for i in range(_POOL_VALUES_PER_CALL)
            ]
        })
      return out


def _ctx(digest: str, **update) -> GenerationContext:
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.memos"
      },
      "schema": [{
          "name": c,
          "type": "STRING",
          "mode": "REQUIRED"
      } for c in _COLUMNS],
  })
  rows = [{
      c: f"{c} narrative line {i} about a transfer" for c in _COLUMNS
  } for i in range(120)]
  return GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest=digest,
      pipeline_run_id=f"run-{digest}",
      num_rows=64,
      # Ladder-mechanics test: expansion off forces the pool path.
      freetext_expansion="off",
      **update,
  )


def test_transient_failure_in_one_ladder_thread_is_retried_in_process(
    caplog,) -> None:
  client = _FlakyClient(ModelClientTransientError("card unfittable"))
  engine = B1RagEngine(embedder=HashingEmbedder(dim=32))
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    engine.setup(client, _ctx("transient-once"))
  # Every column landed — the losing thread's column was rebuilt once
  # the siblings finished, against the now-usable client.
  for c in _COLUMNS:
    assert engine._free_text_pools.get(c), c
  text = "\n".join(r.message for r in caplog.records)
  assert "name=freetext_pool_ladder_retried" in text
  assert "error=ModelClientTransientError" in text
  engine.teardown()


def test_non_transient_ladder_failure_still_fails_the_setup() -> None:
  # A real defect (bad JSON schema, auth) must keep surfacing as before
  # under strict_freetext — the retry is for the client-not-ready race.
  client = _FlakyClient(RuntimeError("schema rejected by the server"))
  engine = B1RagEngine(embedder=HashingEmbedder(dim=32))
  with pytest.raises(RuntimeError, match="schema rejected"):
    engine.setup(client, _ctx("non-transient", strict_freetext=True))


def test_transient_failure_that_persists_on_retry_still_raises() -> None:
  # Bounded: one in-process retry per column. If the client is still not
  # usable after the siblings landed, the bundle-retry path takes over —
  # in LAX mode too: a client that never answered is not "the LLM yielded
  # nothing", and must never degrade into the exemplar-folding fallback
  # (the 2026-07-10 root cause: setup() never ran → universal memorization).
  client = _FlakyClient(
      ModelClientTransientError("still unfittable"), fail_calls=10)
  engine = B1RagEngine(embedder=HashingEmbedder(dim=32))
  with pytest.raises(ModelClientTransientError):
    engine.setup(client, _ctx("transient-persists"))
