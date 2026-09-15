"""dofn_setup_retry milestone (2026-07-24 16:35 E2E fix).

That run's setup() crashed twice and Dataflow silently retried — the job
landed PASSED with dlq_count=0 and ZERO trace of ~35 min of retry cost.
A retry after an in-process setup failure now logs a WARNING milestone
and bumps a Beam counter (committed only by the surviving bundle, which
is exactly the invisible case).
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=protected-access,unused-argument

from __future__ import annotations

import pytest
from sdfb_beam.dofns import generate as generate_mod
from sdfb_beam.dofns.generate import GenerateRecordsDoFn
from sdfb_beam.handlers.fake_client import FakeModelClient
from sdfb_core.contracts import TableSchema
from sdfb_core.engines import register_engine
from sdfb_core.engines.base import GenerationContext, GenerationEngine
from sdfb_core.observability import parse_milestone


class BoomOnceEngine(GenerationEngine):
  """Fails setup() a class-controlled number of times, then succeeds."""

  name = "boom_once"
  fail_remaining = 0  # tests set this

  def setup(self, model_client, ctx):
    if BoomOnceEngine.fail_remaining > 0:
      BoomOnceEngine.fail_remaining -= 1
      raise RuntimeError("synthetic setup failure")

  def generate_batch(self, n, cfg):
    return iter(())

  def teardown(self):
    pass


register_engine("boom_once", BoomOnceEngine)


@pytest.fixture(autouse=True)
def _fresh_state():
  generate_mod._reset_setup_failures()
  BoomOnceEngine.fail_remaining = 0
  yield
  generate_mod._reset_setup_failures()


def _ctx() -> GenerationContext:
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.t"
      },
      "schema": [{
          "name": "c",
          "type": "STRING",
          "mode": "REQUIRED"
      }],
  })
  return GenerationContext(table_schema=schema, pipeline_run_id="run-retry-1")


def _dofn() -> GenerateRecordsDoFn:
  return GenerateRecordsDoFn(
      engine_name="boom_once",
      model_client=FakeModelClient(reference_pool=[{
          "c": "x"
      }]),
      ctx=_ctx(),
  )


def _milestones(caplog) -> list[dict]:
  return [
      m for m in (parse_milestone(r.getMessage()) for r in caplog.records) if m
  ]


def test_retry_after_failure_emits_milestone(caplog):
  BoomOnceEngine.fail_remaining = 1
  with pytest.raises(RuntimeError):
    _dofn().setup()
  with caplog.at_level("WARNING"):
    _dofn().setup()  # fresh DoFn, same process — the Dataflow retry shape
  retries = [m for m in _milestones(caplog) if m["name"] == "dofn_setup_retry"]
  assert retries and retries[0]["attempt"] == "2"
  assert retries[0]["engine"] == "boom_once"


def test_parallel_clean_setups_emit_nothing(caplog):
  with caplog.at_level("WARNING"):
    _dofn().setup()
    _dofn().setup()  # second clean instance = normal Beam parallelism
  assert not [m for m in _milestones(caplog) if m["name"] == "dofn_setup_retry"]


def test_second_failure_bumps_attempt_number(caplog):
  BoomOnceEngine.fail_remaining = 2
  with pytest.raises(RuntimeError):
    _dofn().setup()
  with caplog.at_level("WARNING"), pytest.raises(RuntimeError):
    _dofn().setup()
  with caplog.at_level("WARNING"):
    _dofn().setup()
  attempts = [
      m["attempt"]
      for m in _milestones(caplog)
      if m["name"] == "dofn_setup_retry"
  ]
  assert attempts == ["2", "3"]
