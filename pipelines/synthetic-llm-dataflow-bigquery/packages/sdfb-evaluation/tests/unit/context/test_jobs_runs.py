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
"""What a generation job wrote (`context.jobs`) and what it recorded in
`validation_runs` (`context.runs`)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sdfb_evaluation.context.jobs import (
    JobWrite,
    foreign_writes,
    writes_by_beam_job,
)
from sdfb_evaluation.context.runs import runs_for, split_run_id

JOB_ID = "2026-09-13_06_10_16-9000000000000000017"
WINDOW = ("2026-09-13T13:10:16.512345Z", "2026-09-13T14:02:41.118204Z")
DS = "demo-project.thelook_synthetic"
STEP = "sdfbthelook0913a1b2c3"


def test_writes_by_beam_job_filters_temp_tables_and_errors(fake_bq):
  bq = fake_bq()
  writes = writes_by_beam_job(bq, location="EU", beam_job_id=JOB_ID)
  assert writes == [
      JobWrite(
          table=f"{DS}.users",
          job_type="LOAD",
          start="2026-09-13T13:40:55.100000Z",
          end="2026-09-13T13:41:10.200000Z",
          output_rows=5000,
          job_id=f"beam_bq_job_LOAD_{STEP}_LOAD_STEP_101_c0ffee01"),
      JobWrite(
          table=f"{DS}.orders",
          job_type="COPY",
          start="2026-09-13T13:49:21.000000Z",
          end="2026-09-13T13:49:30.500000Z",
          output_rows=18250,
          job_id=f"beam_bq_job_COPY_{STEP}_COPY_STEP_202_ab12cd34_0"),
      JobWrite(
          table=f"{DS}.order_items",
          job_type="LOAD",
          start="2026-09-13T13:58:20.000000Z",
          end="2026-09-13T13:58:40.750000Z",
          output_rows=44710,
          job_id=f"beam_bq_job_LOAD_{STEP}_LOAD_STEP_303_e5f6a7b8"),
      JobWrite(
          table="demo-project.synthetic_data_quality.validation_runs",
          job_type="LOAD",
          start="2026-09-13T13:59:30.000000Z",
          end="2026-09-13T13:59:41.000000Z",
          output_rows=3,
          job_id=f"beam_bq_job_LOAD_{STEP}_LOAD_STEP_900_1a2b3c4d"),
  ]
  # The temp-table load, the errored load, the RUNNING load and the QUERY
  # never reach jobs.get.
  assert [call[0] for call in bq.stats_calls] == [w.job_id for w in writes]
  assert all(loc == "EU" for _, loc in bq.stats_calls)
  # Server-side, the same filters are parameterised SQL.
  [(sql, params)] = bq.queries
  assert "`demo-project`.`region-eu`.INFORMATION_SCHEMA.JOBS_BY_PROJECT" in sql
  assert "state = 'DONE'" in sql and "error_result IS NULL" in sql
  assert "job_type IN ('LOAD', 'COPY')" in sql
  assert "l.key = 'beam_job_id' AND l.value = @beam_job_id" in sql
  assert "STARTS_WITH(destination_table.table_id, 'beam_bq_job_')" in sql
  assert JOB_ID not in sql
  assert params == {"beam_job_id": JOB_ID}


def test_writes_by_beam_job_window_bounds_the_partition_scan(fake_bq):
  bq = fake_bq()
  writes_by_beam_job(bq, location="EU", beam_job_id=JOB_ID, window=WINDOW)
  [(sql, params)] = bq.queries
  assert "creation_time BETWEEN @start AND @end" in sql
  assert params["start"] == datetime(
      2026, 9, 13, 12, 10, 16, 512345, tzinfo=UTC)
  assert params["end"] == datetime(2026, 9, 13, 15, 2, 41, 118204, tzinfo=UTC)


def test_writes_by_beam_job_permission_denied_has_role_hint(fake_bq):
  bq = fake_bq()
  bq.failures["JOBS_BY_PROJECT"] = PermissionError("Access Denied: listAll")
  with pytest.raises(
      PermissionError, match=r"roles/bigquery\.resourceViewer") as info:
    writes_by_beam_job(bq, location="EU", beam_job_id=JOB_ID)
  assert "Access Denied: listAll" in str(info.value)

  bq = fake_bq()
  bq.stats_failure = PermissionError("Access Denied: jobs.get")
  with pytest.raises(PermissionError, match=r"bigquery\.resourceViewer"):
    writes_by_beam_job(bq, location="EU", beam_job_id=JOB_ID)


@pytest.mark.parametrize("location", ["eu`; DROP", "EU--", "", "europe west1"])
def test_writes_by_beam_job_rejects_unsafe_location(fake_bq, location):
  with pytest.raises(ValueError):
    writes_by_beam_job(fake_bq(), location=location, beam_job_id=JOB_ID)


def test_foreign_writes_excludes_own_job_and_reads_all_writers(fake_bq):
  foreign_rows = [{
      "job_id": "bquxjob_1a2b3c4d_manual_insert",
      "job_type": "QUERY",
      "state": "DONE",
      "error_result": None,
      "creation_time": "2026-09-13T13:44:00.000000Z",
      "start_time": "2026-09-13T13:44:00.000000Z",
      "end_time": "2026-09-13T13:44:02.000000Z",
      "project_id": "demo-project",
      "dataset_id": "thelook_synthetic",
      "table_id": "orders",
  }]
  bq = fake_bq(responses=[("@exclude_job", foreign_rows)])
  found = foreign_writes(
      bq,
      location="EU",
      table=f"{DS}.orders",
      window=WINDOW,
      exclude_job=JOB_ID)
  assert found == [
      JobWrite(
          table=f"{DS}.orders",
          job_type="QUERY",
          start="2026-09-13T13:44:00.000000Z",
          end="2026-09-13T13:44:02.000000Z",
          output_rows=None,
          job_id="bquxjob_1a2b3c4d_manual_insert")
  ]
  [(sql, params)] = bq.queries
  assert "destination_table.dataset_id = @dataset_id" in sql
  assert "end_time BETWEEN @start AND @end" in sql
  assert "creation_time <= @end" in sql
  assert params["exclude_job"] == JOB_ID
  assert params["table_id"] == "orders"
  assert params["start"] == datetime(
      2026, 9, 13, 13, 10, 16, 512345, tzinfo=UTC)


def test_foreign_writes_open_window_runs_to_now(fake_bq):
  bq = fake_bq(responses=[("@exclude_job", [])])
  assert foreign_writes(
      bq,
      location="EU",
      table=f"{DS}.orders",
      window=(WINDOW[0], None),
      exclude_job=JOB_ID) == []
  [(sql, params)] = bq.queries
  assert "CURRENT_TIMESTAMP()" in sql and "end" not in params
  assert "creation_time <= @end" not in sql


def test_foreign_writes_needs_a_window_start(fake_bq):
  with pytest.raises(ValueError, match="window"):
    foreign_writes(
        fake_bq(),
        location="EU",
        table=f"{DS}.orders",
        window=(None, WINDOW[1]),
        exclude_job=JOB_ID)


@pytest.mark.parametrize("run_id,table,expected", [
    ("thelook-0913-a1b2c3-01-orders", f"{DS}.orders",
     ("thelook-0913-a1b2c3", 1)),
    ("thelook-0913-a1b2c3-02-order_items", f"{DS}.order_items",
     ("thelook-0913-a1b2c3", 2)),
    ("thelook-0913-a1b2c3-112-order_items", f"{DS}.order_items",
     ("thelook-0913-a1b2c3", 112)),
    ("thelook-0913-a1b2c3", f"{DS}.orders", ("thelook-0913-a1b2c3", None)),
    ("thelook-0913-a1b2c3-01-orders", f"{DS}.users",
     ("thelook-0913-a1b2c3-01-orders", None)),
])
def test_split_run_id(run_id, table, expected):
  assert split_run_id(run_id, table) == expected


def test_runs_for_orders_by_table_index_and_parameterises(fake_bq):
  bq = fake_bq()
  tables = [f"{DS}.order_items", f"{DS}.users", f"{DS}.orders"]
  rows = runs_for(
      bq,
      quality_dataset="demo-project.synthetic_data_quality",
      landing_tables=tables,
      window=WINDOW)
  assert [(r["run_id"], r["table_index"]) for r in rows] == [
      ("thelook-0913-a1b2c3-00-users", 0),
      ("thelook-0913-a1b2c3-01-orders", 1),
      ("thelook-0913-ffee00-01-orders", 1),
      ("thelook-0913-a1b2c3-02-order_items", 2),
  ]
  assert rows[0]["base_run_id"] == "thelook-0913-a1b2c3"
  assert rows[0]["valid_count"] == 5000
  assert len(rows[0]["reference_digest"]) == 64
  [(sql, params)] = bq.queries
  assert "`demo-project.synthetic_data_quality.validation_runs`" in sql
  assert "landing_table IN UNNEST(@landing_tables)" in sql
  assert params["landing_tables"] == sorted(tables)


@pytest.mark.parametrize("dataset", [
    "synthetic_data_quality",
    "demo-project.synthetic_data_quality`; --",
    "demo-project.synthetic_data_quality.validation_runs",
])
def test_runs_for_rejects_bad_dataset(fake_bq, dataset):
  with pytest.raises(ValueError):
    runs_for(
        fake_bq(),
        quality_dataset=dataset,
        landing_tables=[f"{DS}.users"],
        window=WINDOW)


def test_runs_for_without_tables_queries_nothing(fake_bq):
  bq = fake_bq()
  assert runs_for(
      bq,
      quality_dataset="demo-project.synthetic_data_quality",
      landing_tables=[],
      window=WINDOW) == []
  assert bq.queries == []


def test_runs_for_matches_the_launch_by_run_id_base(fake_bq):
  bq = fake_bq()
  rows = runs_for(
      bq,
      quality_dataset="demo-project.synthetic_data_quality",
      landing_tables=[f"{DS}.order_items"],
      window=WINDOW,
      base_run_id="thelook-0913-a1b2c3")
  assert [r["run_id"] for r in rows] == [
      "thelook-0913-a1b2c3-00-users",
      "thelook-0913-a1b2c3-01-orders",
      "thelook-0913-a1b2c3-02-order_items",
  ]
  [(sql, params)] = bq.queries
  assert ("(landing_table IN UNNEST(@landing_tables) OR run_id = @base OR "
          "STARTS_WITH(run_id, CONCAT(@base, '-')))") in sql
  assert params["base"] == "thelook-0913-a1b2c3"


def test_runs_for_by_base_alone(fake_bq):
  bq = fake_bq()
  rows = runs_for(
      bq,
      quality_dataset="demo-project.synthetic_data_quality",
      landing_tables=[],
      window=(None, None),
      base_run_id="thelook-0913-ffee00")
  assert [r["run_id"] for r in rows] == ["thelook-0913-ffee00-01-orders"]
  [(sql, params)] = bq.queries
  assert "landing_tables" not in params and "@landing_tables" not in sql
  assert "created_at" not in sql.split("WHERE", 1)[1].split("ORDER")[0]
