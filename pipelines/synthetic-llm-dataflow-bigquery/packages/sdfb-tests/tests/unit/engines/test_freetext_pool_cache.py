"""Pool-cache key regression tests (2026-07-20 b2 E2E BLOCKER).

Production defect: ``FreeTextHook._pool_for`` cached the bounded value pool
keyed ``(profile.name, cfg.seed, round(cfg.similarity, 4))``, but
``GenerateRecordsDoFn.process`` derives a fresh ``cfg.seed`` per batch
(``derive_batch_seed(run_id, batch_id)`` — deliberate anti-replay design).
The cache therefore never hit across batches: every one of ~63 batches in
the 2026-07-20 run rebuilt every FREE_TEXT column's pool via a fresh LLM
call (63x cost), and under ``strict_freetext`` each rebuild was a fresh
chance for ``FreeTextEmptyYieldError`` to kill the whole batch (~60/63
batches failed, job FAILED).

The fix drops ``seed`` from the cache key: the pool is built genuinely once
per worker per ``(column, similarity)`` (the FASTGEN O(1) guarantee).
Batch-to-batch value diversity comes from the per-batch-seeded
with-replacement *draw* in ``sample()`` (the ``rng`` argument), not from
rebuilding the pool.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=broad-exception-caught,redefined-outer-name,unused-argument

from __future__ import annotations

import threading

import numpy as np
import pytest
from sdfb_core.contracts import TableSchema
from sdfb_core.engines.b2_library.fidelity import profile_table
from sdfb_core.engines.b2_library.freetext import FreeTextHook
from sdfb_core.engines.base import GenerationConfig


@pytest.fixture
def wide_ctx_schema() -> TableSchema:
  return TableSchema.model_validate({
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
              "name": "summary",
              "type": "STRING",
              "mode": "REQUIRED"
          },
      ],
      "primary_keys": ["ticket_id"],
  })


@pytest.fixture
def wide_reference() -> list[dict]:
  return [
      {
          "ticket_id":
              1001,
          "region":
              "EMEA",
          "summary":
              "Customer reports the export job hangs at 90 percent for large tables.",
      },
      {
          "ticket_id":
              1002,
          "region":
              "AMER",
          "summary":
              "User cannot reset password; the reset email never arrives.",
      },
      {
          "ticket_id":
              1003,
          "region":
              "APAC",
          "summary":
              "Dashboard widgets render blank after the latest browser update.",
      },
  ]


class _CountingClient:
  """Fake ``ModelClient`` that counts pool-build calls and returns a fresh
    batch of distinct novel values each call, so successive builds (if the
    cache wrongly misses) are distinguishable from a single cached build."""

  def __init__(self) -> None:
    self.calls = 0

  def generate_json(self, *a, **k):
    self.calls += 1
    return [{"values": [f"novel-call{self.calls}-{i}" for i in range(32)]}]


class _RaiseThenSucceedClient:
  """Raises on the first pool-build call, succeeds on the second — models
    a transient LLM failure that must NOT poison the cache."""

  def __init__(self) -> None:
    self.calls = 0

  def generate_json(self, *a, **k):
    self.calls += 1
    if self.calls == 1:
      raise RuntimeError("transient boom")
    return [{"values": [f"novel-{i}" for i in range(32)]}]


def test_pool_built_once_across_batch_seeds(wide_ctx_schema, wide_reference):
  """Two GenerationConfigs identical except for cfg.seed (mirroring two
    Beam batches with derive_batch_seed-derived seeds) must share ONE pool
    build — the E2E defect was a fresh LLM call per batch."""
  profiles = profile_table(wide_ctx_schema, wide_reference)
  client = _CountingClient()
  hook = FreeTextHook(client)

  # similarity=0.0 puts all sampling mass on the LLM-generated pool (see
  # `_blend_pools`), so draws are attributable to a specific pool build.
  cfg1 = GenerationConfig(seed=1, similarity=0.0)
  cfg2 = GenerationConfig(seed=2, similarity=0.0)
  rng1 = np.random.default_rng(1)
  rng2 = np.random.default_rng(2)

  out1 = hook.sample(profiles["summary"], 5, cfg1, rng1)
  out2 = hook.sample(profiles["summary"], 5, cfg2, rng2)

  assert client.calls == 1, "pool must be built once, not once per batch seed"
  assert out1 and out2

  # Both draws must come from the SAME underlying pool (all "novel-call1-*").
  def _from_pool(values):
    return all(v is None or v.startswith("novel-call1-") for v in values)

  assert _from_pool(out1)
  assert _from_pool(out2)


def test_draws_differ_across_batches_from_same_pool(wide_ctx_schema,
                                                    wide_reference):
  """Diversity comes from the per-batch-seeded draw, not from rebuilding
    the pool: different rng seeds over the same cached pool must yield
    different sampled value lists."""
  profiles = profile_table(wide_ctx_schema, wide_reference)
  client = _CountingClient()
  hook = FreeTextHook(client)

  cfg = GenerationConfig(seed=1)
  out1 = hook.sample(profiles["summary"], 20, cfg, np.random.default_rng(1))
  out2 = hook.sample(profiles["summary"], 20, cfg, np.random.default_rng(2))

  assert client.calls == 1, "second draw must reuse the cached pool"
  assert out1 != out2, "different rng draws over the same pool should differ"


def test_failed_strict_build_not_cached_retries_next_batch(
    wide_ctx_schema, wide_reference):
  """A strict-mode pool build that fails (exception) must NOT populate the
    cache — the next batch's call retries the build rather than reusing a
    poisoned/absent entry forever."""
  profiles = profile_table(wide_ctx_schema, wide_reference)
  client = _RaiseThenSucceedClient()
  hook = FreeTextHook(client, strict=True)

  cfg = GenerationConfig(seed=1)
  with pytest.raises(RuntimeError, match="transient boom"):
    hook.sample(profiles["summary"], 5, cfg, np.random.default_rng(1))

  # Second batch (different seed) retries and succeeds.
  out = hook.sample(profiles["summary"], 5, GenerationConfig(seed=2),
                    np.random.default_rng(2))
  assert out
  assert client.calls == 2, "exactly two pool-build calls: failed + retried"


class _UndersizedClient:
  """Fake ``ModelClient`` that always yields the SAME small handful of
    novel values (fewer than ``pool_size``) — models a column where the LLM
    genuinely can't fill the bounded pool, but what it did yield is real,
    novel generation (not a fallback) and must stay cacheable."""

  def __init__(self, n_values: int = 5) -> None:
    self.calls = 0
    self._n_values = n_values

  def generate_json(self, *a, **k):
    self.calls += 1
    return [{"values": [f"novel-{i}" for i in range(self._n_values)]}]


def test_nonstrict_fallback_not_cached_retries_and_recovers(
    wide_ctx_schema, wide_reference):
  """Non-strict mode: a transient LLM failure on the first build must fall
    back to exemplars for the CURRENT batch without poisoning the cache —
    the next batch retries the build and, once it succeeds, that genuine
    pool (not the fallback) is what gets cached."""
  profiles = profile_table(wide_ctx_schema, wide_reference)
  client = _RaiseThenSucceedClient()
  hook = FreeTextHook(client, strict=False)

  # similarity=0.0 => all sampling mass goes to the LLM-side pool passed
  # into `_blend_pools` (the exemplar fallback on the first call, the
  # genuine novel pool on the second) — makes the source attributable.
  cfg = GenerationConfig(seed=1, similarity=0.0)

  out1 = hook.sample(profiles["summary"], 5, cfg, np.random.default_rng(1))
  assert client.calls == 1, "first call attempts exactly one (failed) build"
  ref_pool = set(profiles["summary"].text_pool)
  assert all(v is None or v in ref_pool for v in out1), (
      "degraded first batch must be exemplar-derived, not novel")

  out2 = hook.sample(
      profiles["summary"],
      5,
      GenerationConfig(seed=2, similarity=0.0),
      np.random.default_rng(2),
  )
  assert client.calls == 2, "fallback must not be cached: second call retries the build"
  assert all(v is None or v.startswith("novel-") for v in out2), (
      "recovered second batch must draw from the genuine novel pool")

  out3 = hook.sample(
      profiles["summary"],
      5,
      GenerationConfig(seed=3, similarity=0.0),
      np.random.default_rng(3),
  )
  assert client.calls == 2, "third call must hit the cache from the genuine build"
  assert all(v is None or v.startswith("novel-") for v in out3)


def test_undersized_pool_still_cached(wide_ctx_schema, wide_reference):
  """A genuine pool that never reaches ``pool_size`` (but is non-empty) is
    still real LLM generation — it must be cached like a full pool, not
    treated as a fallback."""
  profiles = profile_table(wide_ctx_schema, wide_reference)
  client = _UndersizedClient(n_values=5)
  hook = FreeTextHook(client)

  cfg = GenerationConfig(seed=1, similarity=0.0)
  out1 = hook.sample(profiles["summary"], 5, cfg, np.random.default_rng(1))
  calls_after_first = client.calls
  assert calls_after_first > 0
  assert all(v is None or v.startswith("novel-") for v in out1)

  out2 = hook.sample(
      profiles["summary"],
      5,
      GenerationConfig(seed=2, similarity=0.0),
      np.random.default_rng(2),
  )
  assert client.calls == calls_after_first, (
      "undersized-but-genuine pool must be cached: no rebuild on the second batch"
  )
  assert all(v is None or v.startswith("novel-") for v in out2)


class _SlowFirstBuildClient:
  """Fake ``ModelClient`` whose first ``generate_json`` call blocks on a
    ``threading.Event`` until released — simulates a slow LLM pool build so
    a concurrent second thread reliably contends on ``FreeTextHook._lock``
    instead of racing past a fast, already-finished build."""

  def __init__(self) -> None:
    self.calls = 0
    self._count_lock = threading.Lock()
    self.entered_first_call = threading.Event()
    self.release_first_call = threading.Event()

  def generate_json(self, *a, **k):
    with self._count_lock:
      self.calls += 1
      is_first = self.calls == 1
    if is_first:
      self.entered_first_call.set()
      # Bounded wait keeps the test deterministic even if the release
      # signal is somehow missed.
      self.release_first_call.wait(timeout=5.0)
    return [{"values": [f"novel-{i}" for i in range(32)]}]


class _SeedRecordingClient:
  """Fake ``ModelClient`` that records the ``seed`` kwarg passed to each
    pool-build call — lets a test assert which seed actually reached the LLM
    call, independent of the batch-level ``cfg.seed``."""

  def __init__(self) -> None:
    self.calls = 0
    self.seeds_seen: list[int | None] = []

  def generate_json(self, *a, seed=None, **k):
    self.calls += 1
    self.seeds_seen.append(seed)
    return [{"values": [f"novel-{i}" for i in range(32)]}]


def test_pool_build_uses_batch_independent_seed(wide_ctx_schema,
                                                wide_reference):
  """The pool-build LLM call must use ``cfg.engine_specific["pool_seed"]``,
    NOT ``cfg.seed`` — two batches with different ``cfg.seed`` but the same
    ``pool_seed`` (mirroring two Beam batches under an explicit ``--seed``,
    per ``GenerateRecordsDoFn.process``) must share one build, and that
    build's recorded seed must be the shared ``pool_seed``, not either
    batch's own seed."""
  profiles = profile_table(wide_ctx_schema, wide_reference)
  client = _SeedRecordingClient()
  hook = FreeTextHook(client)

  cfg1 = GenerationConfig(
      seed=42, similarity=0.0, engine_specific={"pool_seed": 7})
  cfg2 = GenerationConfig(
      seed=43, similarity=0.0, engine_specific={"pool_seed": 7})

  hook.sample(profiles["summary"], 5, cfg1, np.random.default_rng(1))
  hook.sample(profiles["summary"], 5, cfg2, np.random.default_rng(2))

  assert client.calls == 1, "pool must be built once across batches sharing pool_seed"
  assert client.seeds_seen == [
      7
  ], ("pool-build seed must be the batch-independent pool_seed, not cfg.seed (42/43)"
     )


def test_concurrent_sample_builds_pool_exactly_once(wide_ctx_schema,
                                                    wide_reference):
  """Two threads calling ``sample()`` concurrently on a fresh hook must
    serialize on the pool build: exactly ONE ``generate_json`` call, not one
    per thread (the check-then-act race this fix closes)."""
  profiles = profile_table(wide_ctx_schema, wide_reference)
  client = _SlowFirstBuildClient()
  hook = FreeTextHook(client)
  cfg = GenerationConfig(seed=1, similarity=0.0)

  results: dict[str, list] = {}
  errors: list[BaseException] = []

  def _call_a():
    try:
      results["a"] = hook.sample(profiles["summary"], 5, cfg,
                                 np.random.default_rng(1))
    except BaseException as e:
      errors.append(e)

  def _call_b():
    try:
      results["b"] = hook.sample(profiles["summary"], 5, cfg,
                                 np.random.default_rng(2))
    except BaseException as e:
      errors.append(e)

  thread_a = threading.Thread(target=_call_a)
  thread_b = threading.Thread(target=_call_b)

  thread_a.start()
  # Only start B once A is inside its (slow) build call — guarantees B
  # contends on the lock rather than racing the fast-path cache read.
  assert client.entered_first_call.wait(
      timeout=5.0), "thread A never entered its build"
  thread_b.start()

  client.release_first_call.set()
  thread_a.join(timeout=5.0)
  thread_b.join(timeout=5.0)

  assert not errors, f"unexpected errors from worker threads: {errors}"
  assert client.calls == 1, "exactly one pool build must happen under concurrency"
  assert results["a"] and results["b"]
