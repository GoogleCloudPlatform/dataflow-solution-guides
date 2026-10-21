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
"""What a generation job wrote to BigQuery, read from BigQuery itself.

Beam's `WriteToBigQuery` labels every load and copy job it submits with
`beam_job_id=<Dataflow job id>` when it runs on Dataflow (Beam's
`bigquery_io_metadata`), and stages FILE_LOADS through temporary tables
named `beam_bq_job_…` that it then copies into the destination.
`INFORMATION_SCHEMA.JOBS_BY_PROJECT` therefore answers "which tables did
job X commit rows to, and when", independently of any log retention:

    JOBS_BY_PROJECT ──(label beam_job_id = X, DONE, no error,
                       LOAD|COPY, destination not beam_bq_job_*)──► JobWrite
          │
          └── jobs.get(job) ── statistics.load.outputRows
                               statistics.copy.copiedRows ──► output_rows

Both reads need `roles/bigquery.resourceViewer` on the project the jobs ran
in (`bigquery.jobs.listAll` for the view, `bigquery.jobs.get` for another
principal's job); a denial is raised as `PermissionError` naming that role.

`foreign_writes` is the contamination check: every committed write into
one table inside a window by anything other than job X — another Dataflow
job, a DML statement, a manual load. The view only lists jobs run IN the
queried project; a writer billed to another project stays invisible.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sdfb_evaluation.context.bq import normalize_fqn

__all__ = [
    "JobWrite",
    "foreign_writes",
    "iso_timestamp",
    "parse_timestamp",
    "writes_by_beam_job",
]

_TEMP_TABLE_PREFIX = "beam_bq_job_"
_WRITE_TYPES = ("LOAD", "COPY")
_LOCATION_RE = re.compile(r"[A-Za-z]+(?:-[A-Za-z0-9]+)*")
_PROJECT_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
# The labelled jobs of one Dataflow job can start before its createTime is
# final and commit after its end; the pad only bounds the partition scan.
_LABEL_WINDOW_PAD = timedelta(hours=1)
_ROLE_HINT = ("roles/bigquery.resourceViewer (bigquery.jobs.listAll + "
              "bigquery.jobs.get)")

_COLUMNS = """job_id, job_type, state, error_result, creation_time, start_time,
  end_time, destination_table.project_id AS project_id,
  destination_table.dataset_id AS dataset_id,
  destination_table.table_id AS table_id"""


@dataclass(frozen=True)
class JobWrite:
  """One committed BigQuery write job into a destination table."""
  table: str  # destination FQN, project.dataset.table
  job_type: str  # LOAD | COPY (foreign writes may also be QUERY)
  start: str  # RFC 3339 UTC
  end: str  # RFC 3339 UTC — the commit time
  output_rows: int | None  # rows committed; None when not read
  job_id: str  # the BigQuery job id


def parse_timestamp(value: str | datetime) -> datetime:
  """A timezone-aware UTC datetime from RFC 3339 text (or a datetime)."""
  moment = (
      value if isinstance(value, datetime) else datetime.fromisoformat(
          str(value)))
  if moment.tzinfo is None:
    moment = moment.replace(tzinfo=UTC)
  return moment.astimezone(UTC)


def iso_timestamp(value: Any) -> str | None:
  """RFC 3339 UTC text (`…T…:…:….ffffffZ`) for a BigQuery TIMESTAMP value;
  text passes through unchanged, `None` stays `None`."""
  if value is None:
    return None
  if isinstance(value, datetime):
    return parse_timestamp(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
  return str(value)


def jobs_view(project: str, location: str) -> str:
  """The region-qualified `JOBS_BY_PROJECT` view, validated for SQL."""
  if _PROJECT_RE.fullmatch(project or "") is None:
    raise ValueError(f"malformed project id {project!r}")
  if _LOCATION_RE.fullmatch(location or "") is None:
    raise ValueError(f"malformed BigQuery location {location!r} "
                     "(expected e.g. EU, US or europe-west1)")
  return (f"`{project}`.`region-{location.lower()}`"
          ".INFORMATION_SCHEMA.JOBS_BY_PROJECT")


def _committed(row: Mapping[str, Any]) -> bool:
  return (row.get("state") == "DONE" and not row.get("error_result") and
          bool(row.get("table_id")))


def _to_write(row: Mapping[str, Any], output_rows: int | None) -> JobWrite:
  table = ".".join(
      str(row[key]) for key in ("project_id", "dataset_id", "table_id"))
  end = iso_timestamp(row.get("end_time")) or ""
  start = iso_timestamp(row.get("start_time") or
                        row.get("creation_time")) or end
  return JobWrite(
      table=table,
      job_type=str(row["job_type"]),
      start=start,
      end=end,
      output_rows=output_rows,
      job_id=str(row["job_id"]))


def _output_rows(stats: Mapping[str, Any], job_type: str) -> int | None:
  if job_type == "LOAD":
    value = (stats.get("load") or {}).get("outputRows")
  else:
    value = (stats.get("copy") or {}).get("copiedRows")
  return int(value) if value not in (None, "") else None


def _denied(exc: PermissionError, what: str, project: str) -> PermissionError:
  return PermissionError(f"{what} was denied ({exc}); grant {_ROLE_HINT} on "
                         f"project {project} to the evaluator's principal")


def writes_by_beam_job(
    bq: Any,
    *,
    location: str,
    beam_job_id: str,
    window: tuple[str | None, str | None] | None = None) -> list[JobWrite]:
  """Every table the Dataflow job `beam_job_id` committed rows to.

  Args:
    bq: a `Bq` (or fake) whose project the Beam jobs ran in.
    location: the BigQuery location the jobs ran in (`EU`, `US`, …).
    beam_job_id: the Dataflow job id (Beam's `beam_job_id` label value).
    window: the Dataflow job's (create, end) times; when given, bounds the
      view's `creation_time` partitions (padded by an hour each side).

  Returns:
    One `JobWrite` per DONE, error-free LOAD or COPY into a non-temporary
    table, ordered by commit time; `output_rows` from `jobs.get`.

  Raises:
    PermissionError: the view or `jobs.get` was denied — the message names
      `roles/bigquery.resourceViewer`.
  """
  params: dict[str, Any] = {"beam_job_id": beam_job_id}
  bound = ""
  if window is not None and window[0]:
    params["start"] = parse_timestamp(window[0]) - _LABEL_WINDOW_PAD
    if window[1]:
      params["end"] = parse_timestamp(window[1]) + _LABEL_WINDOW_PAD
      bound = "\n  AND creation_time BETWEEN @start AND @end"
    else:
      bound = "\n  AND creation_time >= @start"
  sql = (f"SELECT {_COLUMNS}\n"
         f"FROM {jobs_view(bq.project, location)}\n"
         "WHERE state = 'DONE' AND error_result IS NULL\n"
         "  AND job_type IN ('LOAD', 'COPY')\n"
         "  AND EXISTS (SELECT 1 FROM UNNEST(labels) AS l\n"
         "              WHERE l.key = 'beam_job_id' AND l.value = @beam_job_id)"
         "\n  AND NOT STARTS_WITH(destination_table.table_id, 'beam_bq_job_')"
         f"{bound}\n"
         "ORDER BY end_time, job_id")
  try:
    rows = bq.query(sql, params)
  except PermissionError as exc:
    raise _denied(
        exc, f"listing the BigQuery jobs of Dataflow job "
        f"{beam_job_id} (INFORMATION_SCHEMA.JOBS_BY_PROJECT)",
        bq.project) from exc
  kept = [
      r for r in rows if _committed(r) and r.get("job_type") in _WRITE_TYPES and
      not str(r["table_id"]).startswith(_TEMP_TABLE_PREFIX)
  ]
  writes = []
  for row in kept:
    job_id = str(row["job_id"])
    try:
      stats = bq.job_stats(job_id, location)
    except PermissionError as exc:
      raise _denied(exc, f"reading BigQuery job {job_id} (jobs.get)",
                    bq.project) from exc
    writes.append(_to_write(row, _output_rows(stats, str(row["job_type"]))))
  return sorted(writes, key=lambda w: (w.end, w.job_id))


def foreign_writes(bq: Any,
                   *,
                   location: str,
                   table: str,
                   window: tuple[str | None, str | None],
                   exclude_job: str,
                   max_bytes: int | None = None) -> list[JobWrite]:
  """Writes into `table` committed inside `window` by anything but the
  Dataflow job `exclude_job` (any job type, labelled or not).

  An open window (`end` None) runs to now. `output_rows` is not read
  (`None`): the check is whether anyone else wrote, not how much.
  `max_bytes` caps the bytes billed (the evaluation's budget).

  Raises:
    ValueError: the window has no start, or `table` is not a strict
      `project.dataset.table`.
    PermissionError: the view was denied (names the role to grant).
  """
  if not window[0]:
    raise ValueError("foreign_writes needs a window start")
  project_id, dataset_id, table_id = normalize_fqn(table).split(".")
  params: dict[str, Any] = {
      "project_id": project_id,
      "dataset_id": dataset_id,
      "table_id": table_id,
      "start": parse_timestamp(window[0]),
      "exclude_job": exclude_job,
  }
  created_before = ""
  if window[1]:
    params["end"] = parse_timestamp(window[1])
    end = "@end"
    created_before = "  AND creation_time <= @end\n"
  else:
    end = "CURRENT_TIMESTAMP()"
  sql = (f"SELECT {_COLUMNS}\n"
         f"FROM {jobs_view(bq.project, location)}\n"
         "WHERE state = 'DONE' AND error_result IS NULL\n"
         "  AND destination_table.project_id = @project_id\n"
         "  AND destination_table.dataset_id = @dataset_id\n"
         "  AND destination_table.table_id = @table_id\n"
         "  AND creation_time >= TIMESTAMP_SUB(@start, INTERVAL 1 DAY)\n"
         f"{created_before}"
         f"  AND end_time BETWEEN @start AND {end}\n"
         "  AND IFNULL((SELECT l.value FROM UNNEST(labels) AS l\n"
         "              WHERE l.key = 'beam_job_id'), '') != @exclude_job\n"
         "ORDER BY end_time, job_id")
  try:
    rows = bq.query(sql, params, max_bytes=max_bytes)
  except PermissionError as exc:
    raise _denied(
        exc, f"listing the writers of {table} "
        "(INFORMATION_SCHEMA.JOBS_BY_PROJECT)", bq.project) from exc
  writes = [_to_write(r, None) for r in rows if _committed(r)]
  return sorted(writes, key=lambda w: (w.end, w.job_id))
