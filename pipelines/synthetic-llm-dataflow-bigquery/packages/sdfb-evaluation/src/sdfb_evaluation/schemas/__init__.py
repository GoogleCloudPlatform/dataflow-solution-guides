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
"""The frozen BigQuery contract for the evaluation registry.

Four tables — `evaluation_data_history` (one append-only event row per
RUNNING/FINAL transition, D7), `evaluation_metrics` (tidy per-metric rows,
with a `baseline_value` floor per D4 and a `noise_floor` per D5),
`evaluation_profiles` (histograms/quantiles/etc., literal only under the D6
policy) and `evaluation_row_flags` (per-row privacy flags, keyed source-key
hashes) — plus the two convenience views layered on the registry.

This is a TypeScript GUI's data contract: every field carries a
`description`, field names are frozen exactly as declared here, and the
`.schema.json` / `.sql` files are the single source both `bq mk` and the
GUI's zod-type generator read. They live inside this package, never under
`config/bq_schema/` (that tree belongs to the main DSG unit's Terraform).

REF: https://docs.cloud.google.com/bigquery/docs/schemas#creating_a_JSON_schema_file
REF: https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/query-syntax#qualify_clause
"""

from __future__ import annotations

import json
import re
from importlib import resources
from typing import Any

TABLES: tuple[str, ...] = (
    "evaluation_data_history",
    "evaluation_metrics",
    "evaluation_profiles",
    "evaluation_row_flags",
)

# table -> (partition field, DAY | MONTH, expiration in DAYS or None = never).
PARTITIONING: dict[str, tuple[str, str, int | None]] = {
    "evaluation_data_history": ("recorded_at", "DAY", None),
    "evaluation_metrics": ("evaluated_at", "MONTH", None),
    "evaluation_profiles": ("evaluated_at", "MONTH", None),
    # 180-day retention: row-level privacy flags are the most sensitive
    # rows the registry writes.
    "evaluation_row_flags": ("evaluated_at", "MONTH", 180),
}

# table -> clustering columns, in `bq mk --clustering_fields` order.
CLUSTERING: dict[str, tuple[str, ...]] = {
    "evaluation_data_history":
        ("relationship_model", "engine", "evaluation_id"),
    "evaluation_metrics": ("table_name", "metric_id", "evaluation_id"),
    "evaluation_profiles": ("table_name", "profile_kind", "evaluation_id"),
    "evaluation_row_flags": ("table_name", "check", "evaluation_id"),
}

_PACKAGE = "sdfb_evaluation.schemas"
_SECONDS_PER_DAY = 86400
# A `CREATE OR REPLACE VIEW ... ;` statement, comments and blank lines
# outside it discarded.
_VIEW_STATEMENT = re.compile(r"CREATE OR REPLACE VIEW.*?;", re.DOTALL)


def _read(name: str) -> str:
  """Read a packaged resource under `schemas/` as text."""
  return resources.files(_PACKAGE).joinpath(name).read_text(encoding="utf-8")


def _schema_path(table: str) -> str:
  """Filesystem path to `table`'s packaged schema file, for `bq mk --schema`."""
  return str(resources.files(_PACKAGE) / f"{table}.schema.json")


def load_schema(table: str) -> list[dict[str, Any]]:
  """`table`'s BigQuery JSON schema, in column order.

  Each entry is a `{name, type, mode, description, fields?}` dict; a
  `RECORD` field nests its sub-fields under `fields`.
  """
  fields: list[dict[str, Any]] = json.loads(_read(f"{table}.schema.json"))
  return fields


def field_names(table: str) -> tuple[str, ...]:
  """Top-level column names of `table`, in schema order."""
  return tuple(field["name"] for field in load_schema(table))


def required_fields(table: str) -> tuple[str, ...]:
  """Top-level column names of `table` whose mode is REQUIRED."""
  return tuple(field["name"]
               for field in load_schema(table)
               if field.get("mode") == "REQUIRED")


def bq_mk_commands(project: str,
                   dataset: str = "synthetic_data_quality") -> list[str]:
  """`bq mk --table` commands provisioning all four tables, in `TABLES` order.

  Partitioning and clustering come from `PARTITIONING` / `CLUSTERING`;
  `PARTITIONING`'s expiration is in days, `bq mk --time_partitioning_expiration`
  wants seconds.
  """
  commands = []
  for table in TABLES:
    field, partition_type, expiration_days = PARTITIONING[table]
    parts = [
        "bq mk --table",
        f"--schema {_schema_path(table)}",
        f"--time_partitioning_type {partition_type}",
        f"--time_partitioning_field {field}",
    ]
    if expiration_days is not None:
      parts.append(
          f"--time_partitioning_expiration {expiration_days * _SECONDS_PER_DAY}"
      )
    clustering = CLUSTERING.get(table)
    if clustering:
      separator = ","
      parts.append(f"--clustering_fields {separator.join(clustering)}")
    parts.append(f"{project}:{dataset}.{table}")
    commands.append(" ".join(parts))
  return commands


def _column_ddl(field: dict[str, Any], *, nested: bool = False) -> str:
  """One field as a DDL column (or STRUCT field) definition: the name
  between backticks (never a keyword clash), a RECORD as a STRUCT of its
  sub-fields, REPEATED as an ARRAY, REQUIRED as `NOT NULL`, and the
  description as `OPTIONS(description=…)` (a JSON string literal is a
  valid GoogleSQL one)."""
  kind = str(field["type"])
  if kind == "RECORD":
    members = ", ".join(
        _column_ddl(sub, nested=True) for sub in field["fields"])
    kind = f"STRUCT<{members}>"
  mode = field.get("mode")
  if mode == "REPEATED":
    kind = f"ARRAY<{kind}>"
  name = field["name"]
  parts = [f"`{name}`", kind]
  if mode == "REQUIRED":
    parts.append("NOT NULL")
  description = field.get("description")
  if description and not nested:
    parts.append(f"OPTIONS(description={json.dumps(description)})")
  return " ".join(parts)


def table_ddl(project: str,
              dataset: str = "synthetic_data_quality") -> list[str]:
  """`CREATE TABLE IF NOT EXISTS` statements for all four tables, in
  `TABLES` order — the same tables `bq_mk_commands` provisions (schema,
  partitioning, clustering, partition expiry), as DDL a BigQuery client
  can run (`sdfb-eval schemas --apply`). An existing table is kept as it
  is: never replaced, never altered. Every identifier is backtick-quoted;
  sub-field descriptions of a RECORD stay in the `.schema.json` only.
  """
  statements = []
  for table in TABLES:
    field, partition_type, expiration_days = PARTITIONING[table]
    columns = ",\n  ".join(_column_ddl(f) for f in load_schema(table))
    partition = (f"DATE(`{field}`)" if partition_type == "DAY" else
                 f"TIMESTAMP_TRUNC(`{field}`, {partition_type})")
    lines = [
        f"CREATE TABLE IF NOT EXISTS `{project}.{dataset}.{table}` (",
        f"  {columns}", ")", f"PARTITION BY {partition}"
    ]
    clustering = CLUSTERING.get(table)
    if clustering:
      lines.append("CLUSTER BY " + ", ".join(f"`{c}`" for c in clustering))
    if expiration_days is not None:
      lines.append(f"OPTIONS(partition_expiration_days={expiration_days})")
    statements.append("\n".join(lines))
  return statements


def view_sql(project: str, dataset: str) -> list[str]:
  """`views.sql`'s `CREATE OR REPLACE VIEW` statements, rendered for `project.dataset`."""
  rendered = _read("views.sql").replace("{project}",
                                        project).replace("{dataset}", dataset)
  return [statement.strip() for statement in _VIEW_STATEMENT.findall(rendered)]
