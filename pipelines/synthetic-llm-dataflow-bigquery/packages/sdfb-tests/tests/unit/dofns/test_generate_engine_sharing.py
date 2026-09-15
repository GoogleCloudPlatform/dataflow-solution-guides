"""One engine per (process, run, table), shared by every bundle thread
(ADR 0034).

The 2026-08-29 R6 pair ran 32 `GenerateRecordsDoFn` instances per table
(4 workers x 8 harness threads) and EVERY instance built its own engine:
its own chunk-store read, FAISS index, pool-store reads, source-domain
fetches and FK key-pool fit — 2,940 thread-seconds of setup for C_TABLE
(p50 75 s, max 444 s) serialized on process-level single-flight locks,
while the same engine state was already sitting in the sibling threads.
A B.2 run pays the CTGAN fit and the lazy pool ladders per instance too.

The registry hands the FIRST DoFn's engine to every sibling with the same
(engine, run_id, landing table, reference digest) key, refcounts the
holders, and tears the engine down only when the last one releases it —
so the single-DoFn lifecycle (`engine_setup` → `engine_teardown` →
`client_teardown`) is unchanged.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=broad-exception-caught,import-outside-toplevel,missing-class-docstring,protected-access,redefined-outer-name,unused-argument

from __future__ import annotations

import threading
from typing import ClassVar

import pytest
from sdfb_beam.dofns import generate as generate_mod
from sdfb_beam.dofns.generate import GenerateRecordsDoFn
from sdfb_beam.handlers.fake_client import FakeModelClient
from sdfb_core.engines import GenerationContext


class _Record:

  def __init__(self, i: int) -> None:
    self._i = i

  def model_dump(self, mode: str = "python") -> dict:
    return {"i": self._i}


class _CountingEngine:
  instances: ClassVar[int] = 0
  setups: ClassVar[int] = 0
  teardowns: ClassVar[int] = 0
  fail_first_setup: ClassVar[bool] = False
  setup_delay_s: ClassVar[float] = 0.0

  def __init__(self) -> None:
    type(self).instances += 1
    self._ready = False

  def setup(self, model_client, ctx) -> None:
    if type(self).fail_first_setup:
      type(self).fail_first_setup = False
      raise RuntimeError("boom on first setup")
    if type(self).setup_delay_s:
      import time

      time.sleep(type(self).setup_delay_s)
    type(self).setups += 1
    self._ready = True

  def generate_batch(self, n, cfg):
    assert self._ready, "generate_batch on a torn-down engine"
    for i in range(n):
      yield _Record(i)

  def teardown(self) -> None:
    type(self).teardowns += 1
    self._ready = False


@pytest.fixture
def counting_engine(monkeypatch):
  _CountingEngine.instances = 0
  _CountingEngine.setups = 0
  _CountingEngine.teardowns = 0
  _CountingEngine.fail_first_setup = False
  _CountingEngine.setup_delay_s = 0.0
  monkeypatch.setattr(generate_mod, "get_engine", lambda _name: _CountingEngine)
  generate_mod._reset_engine_registry()
  yield _CountingEngine
  generate_mod._reset_engine_registry()


def _ctx(schema, run_id="run-1", landing="p.land.t", digest="d1"):
  return GenerationContext(
      table_schema=schema,
      embedder_uri="",
      pipeline_run_id=run_id,
      landing_table=landing,
      reference_digest=digest,
  )


def _dofn(ctx, expect_fk_side=False):
  return GenerateRecordsDoFn(
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=[{
          "x": 1
      }]),
      ctx=ctx,
      expect_fk_side=expect_fk_side,
  )


def test_dofns_of_the_same_run_and_table_share_one_engine(
    counting_engine, customers_schema):
  d1, d2 = _dofn(_ctx(customers_schema)), _dofn(_ctx(customers_schema))
  d1.setup()
  d2.setup()
  assert counting_engine.setups == 1
  assert counting_engine.instances == 1
  assert d1._engine is d2._engine
  # Both DoFns generate from the shared engine.
  assert len(list(d1.process({"n": 3, "batch_id": 0}))) == 3
  assert len(list(d2.process({"n": 2, "batch_id": 1}))) == 2


def test_different_runs_get_their_own_engines(counting_engine,
                                              customers_schema):
  _dofn(_ctx(customers_schema, run_id="run-1")).setup()
  _dofn(_ctx(customers_schema, run_id="run-2")).setup()
  assert counting_engine.setups == 2


def test_different_tables_in_one_run_get_their_own_engines(
    counting_engine, customers_schema):
  """ADR 0030: two tables' generate DoFns can live in one worker process."""
  _dofn(_ctx(customers_schema, landing="p.land.parent")).setup()
  _dofn(_ctx(customers_schema, landing="p.land.child")).setup()
  assert counting_engine.setups == 2


def test_engine_is_torn_down_only_by_the_last_holder(counting_engine,
                                                     customers_schema):
  d1, d2 = _dofn(_ctx(customers_schema)), _dofn(_ctx(customers_schema))
  d1.setup()
  d2.setup()
  d1.teardown()
  assert counting_engine.teardowns == 0
  # d2 still generates after its sibling released the engine.
  assert len(list(d2.process({"n": 2, "batch_id": 0}))) == 2
  d2.teardown()
  assert counting_engine.teardowns == 1
  # After the last release a newcomer builds afresh — never a torn-down
  # engine out of the registry.
  d3 = _dofn(_ctx(customers_schema))
  d3.setup()
  assert counting_engine.setups == 2
  assert len(list(d3.process({"n": 1, "batch_id": 0}))) == 1


def test_failed_setup_is_not_shared(counting_engine, customers_schema):
  counting_engine.fail_first_setup = True
  with pytest.raises(RuntimeError, match="boom"):
    _dofn(_ctx(customers_schema)).setup()
  d2 = _dofn(_ctx(customers_schema))
  d2.setup()
  assert counting_engine.setups == 1
  assert len(list(d2.process({"n": 1, "batch_id": 0}))) == 1


def test_concurrent_setups_build_the_engine_once(counting_engine,
                                                 customers_schema):
  counting_engine.setup_delay_s = 0.05
  dofns = [_dofn(_ctx(customers_schema)) for _ in range(8)]
  errors: list[BaseException] = []

  def _run(d):
    try:
      d.setup()
    except BaseException as e:  # pragma: no cover - surfaced below
      errors.append(e)

  threads = [threading.Thread(target=_run, args=(d,)) for d in dofns]
  for t in threads:
    t.start()
  for t in threads:
    t.join()
  assert not errors
  assert counting_engine.setups == 1
  assert counting_engine.instances == 1
  assert len({id(d._engine) for d in dofns}) == 1


def test_deferred_fk_side_build_is_shared_too(counting_engine,
                                              customers_schema):
  """A child table defers its engine build to the first bundle (the
    parent-keys side input is only visible in process()); siblings must
    still share that engine."""
  ctx = _ctx(customers_schema, landing="p.land.child")
  d1, d2 = _dofn(ctx, expect_fk_side=True), _dofn(ctx, expect_fk_side=True)
  d1.setup()
  d2.setup()
  assert counting_engine.setups == 0  # deferred
  side = [{"cols": ["customer_id"], "keys": [(1,), (2,)]}]
  list(d1.process({"n": 2, "batch_id": 0}, side))
  list(d2.process({"n": 2, "batch_id": 1}, side))
  assert counting_engine.setups == 1
  assert d1._engine is d2._engine


def test_shared_reuse_is_logged(counting_engine, customers_schema, caplog):
  import logging

  d1, d2 = _dofn(_ctx(customers_schema)), _dofn(_ctx(customers_schema))
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    d1.setup()
    d2.setup()
  text = "\n".join(r.message for r in caplog.records)
  assert "SDFB_MILESTONE name=engine_shared" in text
  assert "holders=2" in text
