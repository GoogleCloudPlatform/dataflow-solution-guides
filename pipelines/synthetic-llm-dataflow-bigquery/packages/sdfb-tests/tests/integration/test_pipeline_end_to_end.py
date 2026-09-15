"""End-to-end DirectRunner test for the synthesis pipeline.

Satisfies M1 §8 acceptance #1: pipeline runs end-to-end on DirectRunner
with `FakeModelClient` against a fixture `_ddl.json` (no GPU, no GCP).
Acceptance #2 (DLQ populated on invalid records) is covered by the
unit tests on `ValidateRecordDoFn` and `PanderaValidateBatchDoFn`.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=invalid-name

from __future__ import annotations

import json
from pathlib import Path

import apache_beam as beam
import pytest
from apache_beam.options.pipeline_options import PipelineOptions
from sdfb_beam.io.local_sinks import WriteToJsonLines
from sdfb_beam.pipeline import PipelineConfig, build_pipeline
from sdfb_core.codegen import derive_record_model

# Importing this module registers `MinimalEngine` under "minimal" — the
# Beam DoFn looks it up via `get_engine()` in `setup()`.
from sdfb_tests.fakes import FakeModelClient


def _read_jsonl(directory: Path) -> list[dict]:
  out: list[dict] = []
  for f in sorted(directory.glob("*.jsonl")):
    for line in f.read_text().splitlines():
      if line.strip():
        out.append(json.loads(line))
  return out


@pytest.mark.integration
def test_end_to_end_customers_happy_path(tmp_path, customers_schema,
                                         customers_reference):
  """Pipeline runs end-to-end; valid records land; no exceptions."""
  landing_dir = tmp_path / "landing"
  dlq_dir = tmp_path / "dlq"
  landing_dir.mkdir()
  dlq_dir.mkdir()

  config = PipelineConfig(
      table_schema=customers_schema,
      engine_name="minimal",
      model_client=FakeModelClient(reference_pool=customers_reference),
      num_rows=20,
      batch_size=5,
      similarity=0.7,
      seed=42,
      run_id="test-customers-happy",
  )

  options = PipelineOptions(["--runner=DirectRunner"])
  with beam.Pipeline(options=options) as p:
    result = build_pipeline(
        p,
        reference_rows=customers_reference,
        config=config,
        landing_sink=WriteToJsonLines(
            str(landing_dir / "landing"), num_shards=1),
        dlq_sink=WriteToJsonLines(str(dlq_dir / "dlq"), num_shards=1),
    )
    assert result["reference_digest"]
    assert len(result["reference_digest"]) == 64  # sha-256 hex
    assert result["run_id"] == "test-customers-happy"

  landing = _read_jsonl(landing_dir)
  dlq = _read_jsonl(dlq_dir)

  # All valid records must round-trip the Pydantic model.
  Record = derive_record_model(customers_schema)
  for record in landing:
    Record.model_validate(record)

  # Conservation: every yielded record either landed or DLQ'd.
  # The MinimalEngine never yields more than num_rows (ABC contract).
  total = len(landing) + len(dlq)
  assert 0 < total <= config.num_rows, (
      f"expected 0 < landing+dlq ({total}) <= num_rows ({config.num_rows})")
  assert len(landing) > 0, "engine should yield at least one valid record"

  # DLQ entries (if any) must have the canonical error envelope.
  for entry in dlq:
    assert {"raw_record", "error_type", "rule_id", "stage"} <= set(entry.keys())
    assert entry["stage"] == "pre_write"
    assert entry["error_type"] in {
        "pydantic", "pandera", "engine", "uniqueness"
    }


@pytest.mark.integration
def test_end_to_end_uses_reference_digest(tmp_path, customers_schema,
                                          customers_reference):
  """Two runs with the same reference rows produce the same digest."""
  digests: list[str] = []
  for run_id in ("run-a", "run-b"):
    landing_dir = tmp_path / run_id / "landing"
    dlq_dir = tmp_path / run_id / "dlq"
    landing_dir.mkdir(parents=True)
    dlq_dir.mkdir(parents=True)

    config = PipelineConfig(
        table_schema=customers_schema,
        engine_name="minimal",
        model_client=FakeModelClient(reference_pool=customers_reference),
        num_rows=5,
        batch_size=5,
        seed=1,
        run_id=run_id,
    )

    options = PipelineOptions(["--runner=DirectRunner"])
    with beam.Pipeline(options=options) as p:
      result = build_pipeline(
          p,
          reference_rows=customers_reference,
          config=config,
          landing_sink=WriteToJsonLines(str(landing_dir / "landing")),
          dlq_sink=WriteToJsonLines(str(dlq_dir / "dlq")),
      )
      digests.append(result["reference_digest"])

  assert digests[0] == digests[1]
