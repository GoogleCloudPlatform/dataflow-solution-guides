"""DirectRunner tests for the §12 validation_runs subgraph + BLOCKER gate."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=arguments-renamed,unused-argument

from __future__ import annotations

import json
from typing import ClassVar

import apache_beam as beam
import pytest
from apache_beam.options.pipeline_options import PipelineOptions
from sdfb_beam.io.local_sinks import WriteToJsonLines
from sdfb_beam.pipeline import PipelineConfig, _BlockerGateDoFn, build_pipeline
from sdfb_core.codegen import derive_record_model
from sdfb_core.engines import register_engine
from sdfb_core.engines.base import GenerationEngine
from sdfb_core.validation import STATUS_FAILED_BLOCKER, STATUS_PASSED, Thresholds
from sdfb_tests.fakes import FakeModelClient  # also registers MinimalEngine as "minimal"


class _PartialFailureEngine(GenerationEngine):
  """Test-only engine: batches whose derived seed is in ``FAIL_SEEDS``
    raise (an uncaught engine crash, routed to the ``engine_failure`` DLQ
    tag by `GenerateRecordsDoFn`); every other batch emits ``n`` rows with
    a unique ``customer_id`` per row so full-row uniqueness never masks the
    valid count.

    With ``PipelineConfig.seed=0`` the derived per-batch seed equals the
    batch_id (``base_seed + batch_id``, see `GenerateRecordsDoFn.process`),
    so ``FAIL_SEEDS`` selects batches deterministically regardless of
    DirectRunner execution order.
    """

  name = "partial_failure"
  FAIL_SEEDS: ClassVar[set[int]] = {0, 1}

  def setup(self, model_client, ctx):
    self._ctx = ctx
    self._record_model = derive_record_model(ctx.table_schema)

  def generate_batch(self, n, cfg):
    if cfg.seed in self.FAIL_SEEDS:
      raise RuntimeError("synthetic engine failure")
    template = self._ctx.reference_rows[0]
    for i in range(n):
      row = dict(template)
      row["customer_id"] = cfg.seed * 1000 + i
      yield self._record_model.model_validate(row)

  def teardown(self):
    self._ctx = None
    self._record_model = None


register_engine("partial_failure", _PartialFailureEngine)


class _WriteResultLike:
  """Minimal stand-in for `apache_beam.io.gcp.bigquery.WriteResult` —
    only the attribute `build_pipeline`'s gate wiring probes for."""

  def __init__(self, destination_load_jobid_pairs):
    self.destination_load_jobid_pairs = destination_load_jobid_pairs


class _FakeBQFileLoadsSink(beam.PTransform):
  """Test double for `WriteToBigQuery(method=FILE_LOADS)`: writes the
    summary row through (identity `Map`, result discarded) and returns an
    object exposing `.destination_load_jobid_pairs` as a real PCollection —
    exactly the shape `build_pipeline` probes via
    ``getattr(write_result, "destination_load_jobid_pairs", None)`` to
    decide whether to wire the `AsIter` ordering edge in front of the
    BLOCKER gate (see pipeline.py ~line 232 / `_BlockerGateDoFn`).
    """

  def expand(self, pcoll):  # type: ignore[override]
    _ = pcoll | "FakeBQWrite" >> beam.Map(lambda row: row)
    load_jobs = pcoll.pipeline | "FakeLoadJobIds" >> beam.Create(
        [("proj.ds.validation_runs", "job-1")])
    return _WriteResultLike(load_jobs)


def _read_jsonl(directory):
  out = []
  for f in sorted(directory.glob("*.jsonl")):
    for line in f.read_text().splitlines():
      if line.strip():
        out.append(json.loads(line))
  return out


@pytest.mark.integration
def test_validation_run_row_written(tmp_path, customers_schema,
                                    customers_reference):
  """A single validation_runs row lands with status PASSED on a clean run."""
  landing, dlq, vr = (tmp_path / d for d in ("landing", "dlq", "vr"))
  for d in (landing, dlq, vr):
    d.mkdir()

  config = PipelineConfig(
      table_schema=customers_schema,
      engine_name="minimal",
      model_client=FakeModelClient(reference_pool=customers_reference),
      num_rows=20,
      batch_size=5,
      seed=42,
      run_id="vr-test",
      reference_table="proj.ds.src",
      landing_table="proj.ds.landing",
      thresholds=Thresholds(env="dev", blocker_failure_ratio=0.20),
  )

  options = PipelineOptions(["--runner=DirectRunner"])
  with beam.Pipeline(options=options) as p:
    build_pipeline(
        p,
        reference_rows=customers_reference,
        config=config,
        landing_sink=WriteToJsonLines(str(landing / "l"), num_shards=1),
        dlq_sink=WriteToJsonLines(str(dlq / "d"), num_shards=1),
        validation_runs_sink=WriteToJsonLines(str(vr / "v"), num_shards=1),
    )

  rows = _read_jsonl(vr)
  assert len(rows) == 1
  row = rows[0]
  assert row["run_id"] == "vr-test"
  assert row["status"] == STATUS_PASSED
  assert row["valid_count"] > 0
  assert row["engine"] == "minimal"
  assert row["reference_table"] == "proj.ds.src"
  assert row["env"] == "dev"
  assert len(row["reference_digest"]) == 64
  assert isinstance(json.loads(row["dlq_by_rule"]), dict)


@pytest.mark.integration
def test_blocker_gate_fails_pipeline():
  """The gate DoFn raises (failing the job) on a FAILED_BLOCKER summary row."""
  failed_row = {
      "status": "FAILED_BLOCKER",
      "run_id": "x",
      "blocker_count": 5,
      "observed_blocker_ratio": 0.5,
      "blocker_failure_ratio": 0.05,
      "env": "dev",
  }
  options = PipelineOptions(["--runner=DirectRunner"])
  # DirectRunner wraps the DoFn raise, so match broadly (B017).
  with pytest.raises(Exception), beam.Pipeline(  # noqa: B017
      options=options) as p:
    _ = p | beam.Create([failed_row]) | beam.ParDo(_BlockerGateDoFn())


@pytest.mark.integration
def test_blocker_gate_waits_on_bq_write_result(tmp_path, customers_schema,
                                               customers_reference):
  """A WriteResult-shaped `validation_runs_sink` (the real FILE_LOADS
    shape) wires the AsIter ordering edge and the run still completes
    cleanly when no blocker trips — proving the wiring is valid Beam graph,
    not just that the PDone-shaped fallback (`WriteToJsonLines`) works."""
  landing, dlq = (tmp_path / d for d in ("landing", "dlq"))
  for d in (landing, dlq):
    d.mkdir()

  config = PipelineConfig(
      table_schema=customers_schema,
      engine_name="minimal",
      model_client=FakeModelClient(reference_pool=customers_reference),
      num_rows=20,
      batch_size=5,
      seed=42,
      run_id="vr-write-result-pass",
      reference_table="proj.ds.src",
      landing_table="proj.ds.landing",
      thresholds=Thresholds(env="dev", blocker_failure_ratio=0.20),
  )

  options = PipelineOptions(["--runner=DirectRunner"])
  with beam.Pipeline(options=options) as p:
    result = build_pipeline(
        p,
        reference_rows=customers_reference,
        config=config,
        landing_sink=WriteToJsonLines(str(landing / "l"), num_shards=1),
        dlq_sink=WriteToJsonLines(str(dlq / "d"), num_shards=1),
        validation_runs_sink=_FakeBQFileLoadsSink(),
    )
  assert result["run_id"] == "vr-write-result-pass"


@pytest.mark.integration
def test_blocker_gate_still_raises_with_write_result_sink(
    tmp_path, customers_schema, customers_reference):
  """Regression guard for the AsIter wiring itself: a WriteResult-shaped
    sink must not swallow or delay the gate's raise — the job still fails
    on a blocker breach (2026-07-20 b2 E2E: the gate must fail the JOB)."""
  landing, dlq = (tmp_path / d for d in ("landing", "dlq"))
  for d in (landing, dlq):
    d.mkdir()

  config = PipelineConfig(
      table_schema=customers_schema,
      engine_name="partial_failure",
      model_client=FakeModelClient(reference_pool=customers_reference),
      num_rows=64,
      batch_size=16,
      seed=0,
      run_id="vr-write-result-blocker",
      reference_table="proj.ds.src",
      landing_table="proj.ds.landing",
      thresholds=Thresholds(env="dev", blocker_failure_ratio=0.20),
  )

  options = PipelineOptions(["--runner=DirectRunner"])
  # DirectRunner wraps the DoFn raise, so match broadly (B017).
  with pytest.raises(Exception), beam.Pipeline(  # noqa: B017
      options=options) as p:
    build_pipeline(
        p,
        reference_rows=customers_reference,
        config=config,
        landing_sink=WriteToJsonLines(str(landing / "l"), num_shards=1),
        dlq_sink=WriteToJsonLines(str(dlq / "d"), num_shards=1),
        validation_runs_sink=_FakeBQFileLoadsSink(),
    )


@pytest.mark.integration
def test_engine_failure_envelopes_weighted_by_lost_batch_size(
    tmp_path, customers_schema, customers_reference):
  """Regression: a crashed batch loses `n` rows, not one DLQ envelope.

    2 failed batches of 16 (`engine_failure`, batch_size=16) + 2 successful
    batches of 16 valid rows = 64 rows requested. Weighted correctly this is
    blocker_count=32 / total=64 = observed 0.5, which trips the dev
    threshold of 0.20. Counting envelopes 1-for-1 (the pre-fix behavior)
    would score observed=2/64≈0.03 and wrongly PASS.
    """
  landing, dlq, vr = (tmp_path / d for d in ("landing", "dlq", "vr"))
  for d in (landing, dlq, vr):
    d.mkdir()

  config = PipelineConfig(
      table_schema=customers_schema,
      engine_name="partial_failure",
      model_client=FakeModelClient(reference_pool=customers_reference),
      num_rows=64,
      batch_size=16,
      seed=0,
      run_id="vr-weighted-engine-failure",
      reference_table="proj.ds.src",
      landing_table="proj.ds.landing",
      thresholds=Thresholds(env="dev", blocker_failure_ratio=0.20),
      # Assert on the summary arithmetic in isolation from the gate raise
      # (covered separately by test_blocker_gate_fails_pipeline).
      fail_on_blocker=False,
  )

  options = PipelineOptions(["--runner=DirectRunner"])
  with beam.Pipeline(options=options) as p:
    build_pipeline(
        p,
        reference_rows=customers_reference,
        config=config,
        landing_sink=WriteToJsonLines(str(landing / "l"), num_shards=1),
        dlq_sink=WriteToJsonLines(str(dlq / "d"), num_shards=1),
        validation_runs_sink=WriteToJsonLines(str(vr / "v"), num_shards=1),
    )

  rows = _read_jsonl(vr)
  assert len(rows) == 1
  row = rows[0]
  assert row["valid_count"] == 32
  assert row["dlq_by_rule"] and json.loads(row["dlq_by_rule"]) == {
      "engine_failure": 32
  }
  assert row["blocker_count"] == 32
  assert row["dlq_count"] == 32
  assert row["observed_blocker_ratio"] == pytest.approx(0.5)
  assert row["status"] == STATUS_FAILED_BLOCKER
