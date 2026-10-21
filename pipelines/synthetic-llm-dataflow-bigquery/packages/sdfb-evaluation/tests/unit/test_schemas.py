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
"""The frozen BigQuery contract: four schemas + two views (Task 2).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import pytest

from sdfb_evaluation import schemas

_BQ = {
    "STRING", "INT64", "FLOAT64", "BOOL", "TIMESTAMP", "JSON", "RECORD", "DATE"
}


@pytest.mark.parametrize("table", schemas.TABLES)
def test_schema_valid(table):
  fields = schemas.load_schema(table)
  names = [f["name"] for f in fields]
  assert len(names) == len(set(names))
  for f in fields:
    assert f["type"] in _BQ and f.get("description"), (table, f)
    assert f.get("mode", "NULLABLE") in {"NULLABLE", "REQUIRED", "REPEATED"}


@pytest.mark.parametrize("table", schemas.TABLES)
def test_keyed(table):
  assert "evaluation_id" in schemas.required_fields(table)


def test_registry_gui_filters_and_lifecycle():
  names = set(schemas.field_names("evaluation_data_history"))
  for col in ("event", "status", "recorded_at", "evaluation_key",
              "generation_job_id", "run_ids", "relationship_model", "engine",
              "llm_model_uri", "embedder_id", "seed", "similarity",
              "retrieval_method", "reference_rows_limit", "source_stats_tier",
              "profiler_version", "generation_params", "evaluation_params",
              "overall_score", "tables", "params_source"):
    assert col in names, col


def test_metrics_tidy_with_baseline_and_noise():
  names = set(schemas.field_names("evaluation_metrics"))
  for col in ("level", "family", "metric_id", "metric_version", "value",
              "baseline_value", "score", "status", "noise_floor",
              "noise_floor_method", "ci_low", "ci_high", "method",
              "encoding_plan_digest", "detail"):
    assert col in names, col


def test_bq_mk_and_views():
  cmds = schemas.bq_mk_commands("proj")
  assert len(cmds) == 4
  assert any("evaluation_row_flags" in c and
             "--time_partitioning_expiration 15552000" in c for c in cmds)
  views = schemas.view_sql("proj", "synthetic_data_quality")
  assert any(
      "evaluation_latest" in v and "QUALIFY ROW_NUMBER()" in v for v in views)


def test_latest_per_job_excludes_null_generation_job_id():
  # A NULL generation_job_id (cli/agent/manual evaluations with no
  # Dataflow job behind them) must not be folded into one ROW_NUMBER()
  # partition, which would silently drop all but one such evaluation from
  # this view. Those evaluations stay visible in evaluation_latest.
  views = schemas.view_sql("proj", "synthetic_data_quality")
  per_job = next(v for v in views if "evaluation_latest_per_job" in v)
  assert "event = 'FINAL'" in per_job
  assert "generation_job_id IS NOT NULL" in per_job
