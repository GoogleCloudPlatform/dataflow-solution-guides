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
"""What the generation DAG measured about its own run: `validation_runs`.

The generator writes one `validation_runs` row per table it generates
(`sdfb_beam.pipeline`'s run-level summary). A relational launch with base
run id `B` names its tables' runs `B-NN-<table>`, NN the zero-padded
position in the generation order (parents first); a single-table launch
uses `B` itself (`sdfb_beam.cli.run_pipeline.plan_launch`):

    B = thelook-0913-a1b2c3
    ├── B-00-users         reference_digest, valid_count
    ├── B-01-orders        reference_digest, valid_count
    └── B-02-order_items   reference_digest, valid_count

`runs_for` returns those rows for a set of landing tables inside a time
window, with the base and the position recovered from each run id; it is
the one launch-context source that survives log retention.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sdfb_evaluation.context.jobs import iso_timestamp, parse_timestamp

__all__ = ["runs_for", "split_run_id"]

_DATASET_RE = re.compile(
    r"([a-z][a-z0-9-]{4,28}[a-z0-9])[.:]([A-Za-z0-9_]{1,1024})")
_TABLE_NAME_RE = re.compile(r"[A-Za-z0-9_$-]{1,1024}")
_INDEX_RE = re.compile(r"(?P<base>.+)-(?P<index>\d{2,})")
# created_at is stamped inside the job; the pad absorbs clock skew only.
_RUNS_WINDOW_PAD = timedelta(minutes=10)
_COLUMNS = ("run_id", "landing_table", "reference_table", "reference_digest",
            "valid_count", "num_rows_requested", "status", "created_at")


def split_run_id(run_id: str, landing_table: str) -> tuple[str, int | None]:
  """(base run id, position) of a per-table run id.

  `B-NN-<table>` (where `<table>` is `landing_table`'s short name) gives
  `(B, NN)`; anything else is a single-table run: `(run_id, None)`.
  """
  suffix = "-" + landing_table.rsplit(".", 1)[-1]
  if run_id.endswith(suffix):
    match = _INDEX_RE.fullmatch(run_id[:-len(suffix)])
    if match is not None:
      return match.group("base"), int(match.group("index"))
  return run_id, None


def runs_for(bq: Any,
             *,
             quality_dataset: str,
             landing_tables: Sequence[str],
             window: tuple[str | None, str | None],
             table_name: str = "validation_runs",
             base_run_id: str | None = None) -> list[dict]:
  """`validation_runs` rows created inside `window` for `landing_tables`
  OR, when `base_run_id` is known, for any run of that launch
  (`run_id = @base` or `STARTS_WITH(run_id, CONCAT(@base, '-'))`) — so a
  relational launch is found whole even when only its target table is
  known.

  Args:
    bq: a `Bq` (or fake).
    quality_dataset: `project.dataset` holding the generator's table.
    landing_tables: landing FQNs, bound as an array parameter.
    window: (start, end), RFC 3339; either side may be `None` (unbounded).
      Each bound is padded by ten minutes.
    table_name: the generator's `--validation_runs_table` short name.
    base_run_id: the launch's `--run_id`, when known.

  Returns:
    Rows with `run_id`, `landing_table`, `reference_table`,
    `reference_digest`, `valid_count`, `num_rows_requested`, `status`,
    `created_at` (RFC 3339), plus `base_run_id` and `table_index` from
    `split_run_id`; ordered by position, then creation time. Rows of
    OTHER launches that wrote the same tables in the window are returned
    too — the caller decides which base is its own.
  """
  match = _DATASET_RE.fullmatch(quality_dataset or "")
  if match is None:
    raise ValueError(f"expected the quality dataset as project.dataset, got "
                     f"{quality_dataset!r}")
  if _TABLE_NAME_RE.fullmatch(table_name or "") is None:
    raise ValueError(f"malformed table name {table_name!r}")
  if not landing_tables and not base_run_id:
    return []
  source = f"`{match.group(1)}.{match.group(2)}.{table_name}`"
  params: dict[str, Any] = {}
  matches = []
  if landing_tables:
    params["landing_tables"] = sorted(
        {t.replace(":", ".", 1) for t in landing_tables})
    matches.append("landing_table IN UNNEST(@landing_tables)")
  if base_run_id:
    params["base"] = base_run_id
    matches.append("run_id = @base OR STARTS_WITH(run_id, CONCAT(@base, '-'))")
  clauses = ["(" + " OR ".join(matches) + ")"]
  if window[0]:
    params["start"] = parse_timestamp(window[0]) - _RUNS_WINDOW_PAD
    clauses.append("created_at >= @start")
  if window[1]:
    params["end"] = parse_timestamp(window[1]) + _RUNS_WINDOW_PAD
    clauses.append("created_at <= @end")
  columns = ", ".join(_COLUMNS)
  where = " AND ".join(clauses)
  sql = (f"SELECT {columns}\n"
         f"FROM {source}\n"
         f"WHERE {where}\n"
         "ORDER BY created_at, run_id")
  out = []
  for row in bq.query(sql, params):
    record = {column: row.get(column) for column in _COLUMNS}
    record["created_at"] = iso_timestamp(record["created_at"])
    base, index = split_run_id(
        str(record["run_id"]), str(record["landing_table"]))
    record["base_run_id"] = base
    record["table_index"] = index
    out.append(record)
  return sorted(
      out,
      key=lambda r: (-1 if r["table_index"] is None else r["table_index"], r[
          "created_at"] or "", r["run_id"]))
