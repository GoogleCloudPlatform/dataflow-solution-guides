#  Copyright 2026 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""`LocalJsonSinks` as the driver uses it (Task 27): its own registry
events (`write_rows`) around a pipeline, also one that fails at run time
and leaves Beam's temp directory behind.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import json
from pathlib import Path

import apache_beam as beam
import pytest

from sdfb_evaluation.beam.io import LocalJsonSinks

from .helpers import local_pipeline

REGISTRY = "evaluation_data_history"


def _explode(row: dict) -> dict:
  raise RuntimeError(f"a worker failed on {row}")


def _rows(directory: Path) -> list[dict]:
  return [
      json.loads(line)
      for path in sorted(directory.glob("*.jsonl"))
      for line in path.read_text().splitlines()
  ]


# a failed DirectRunner pipeline lets its worker threads die noisily
@pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_the_drivers_rows_frame_a_pipeline_that_fails_at_run_time(
    tmp_path: Path):
  """A pipeline that dies after its file sink started leaves the sink's
  own `beam-temp-<prefix>-<uuid>` directory: this instance's leftover,
  not another run's, so the FAILED row can still be written."""
  sinks = LocalJsonSinks(str(tmp_path))
  sinks.write_rows([{"status": "RUNNING"}], REGISTRY, label="driver-running")
  with pytest.raises(
      RuntimeError, match="a worker failed"), local_pipeline() as p:
    final = (
        p | beam.Create([{
            "status": "SUCCEEDED"
        }]) | "Final" >> beam.Map(_explode))
    sinks.write(final, REGISTRY)
  directory = tmp_path / REGISTRY
  leftovers = [p.name for p in directory.iterdir() if p.is_dir()]
  assert len(leftovers) == 1
  assert leftovers[0].startswith("beam-temp-0002-Write_evaluation_data_history")
  path = sinks.write_rows([{
      "status": "FAILED"
  }],
                          REGISTRY,
                          label="driver-failed")
  assert Path(path).parent == directory
  assert [r["status"] for r in _rows(directory)] == ["RUNNING", "FAILED"]
  assert [r["status"] for r in sinks.read_rows(REGISTRY)
         ] == ["RUNNING", "FAILED"]


def test_another_runs_temp_directory_is_still_refused(tmp_path: Path):
  """Only THIS instance's leftovers are its own: a temp directory under
  a prefix it never wrote, or with no Beam id after it, is another
  run's."""
  sinks = LocalJsonSinks(str(tmp_path))
  sinks.write_rows([{"status": "RUNNING"}], REGISTRY, label="driver-running")
  directory = tmp_path / REGISTRY
  own = directory / ("beam-temp-0001-driver-running-" + "0a" * 16)
  own.mkdir()
  sinks.write_rows([{"status": "FAILED"}], REGISTRY, label="driver-failed")
  own.rmdir()
  for name in ("beam-temp-0009-Write_evaluation_data_history-" + "0a" * 16,
               "beam-temp-0001-driver-running-not-a-beam-id",
               "beam-temp-0001-driver-running"):
    foreign = directory / name
    foreign.mkdir()
    with pytest.raises(ValueError, match="another run"):
      sinks.write_rows([{"status": "FAILED"}], REGISTRY, label="again")
    with pytest.raises(ValueError, match="another run"):
      sinks.write(local_pipeline() | beam.Create([]), REGISTRY)
    foreign.rmdir()
