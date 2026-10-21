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
"""Test tables for the Beam pass: the all-types fixture read as the
BigQuery client reads it, the same rows as a Storage Read API (Arrow)
DIRECT_READ yields them, and hand-built `TablePlan`s.

Nothing here is real data: invented thelook-shaped rows, `demo-project`,
e-mails at `example.com`.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa

from sdfb_evaluation.context.plan import ColumnPlan, TablePlan
from sdfb_evaluation.context.reference import Panel
from sdfb_evaluation.context.relationships import Edge
from sdfb_evaluation.context.scope import ScopePlan
from sdfb_evaluation.types import ColumnKind

PROJECT = "demo-project"
DATASET = "thelook_synthetic"
SOURCE_DATASET = "thelook_source"
_FIXTURE = Path(__file__).parents[1] / "fixtures" / "beam" / "all_types.json"


def all_types() -> dict[str, Any]:
  """The raw all-types fixture (schema, keys, REST-typed rows)."""
  fixture: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
  return fixture


def _float(value: Any) -> float | None:
  return None if value is None else float(value)


def _timestamp(text: str) -> datetime:
  return datetime.fromisoformat(text.replace("Z", "+00:00"))


_CLIENT = {
    "INT64": int,
    "FLOAT64": _float,
    "NUMERIC": Decimal,
    "BIGNUMERIC": Decimal,
    "BOOL": bool,
    "STRING": str,
    "BYTES": base64.b64decode,
    "TIMESTAMP": _timestamp,
    "DATETIME": datetime.fromisoformat,
    "DATE": date.fromisoformat,
    "TIME": time.fromisoformat,
    "GEOGRAPHY": str,
}


def client_value(field: Mapping[str, Any], raw: Any) -> Any:
  """One REST-typed cell as the BigQuery client returns it
  (`google.cloud.bigquery._helpers.CellDataParser`): TIMESTAMP an aware
  UTC `datetime`, DATETIME a naive one, (BIG)NUMERIC a `Decimal`, BYTES
  `bytes`, JSON parsed, RECORD a dict of typed sub-fields (untyped when
  the fixture declares none), REPEATED a list."""
  if raw is None:
    return None
  bq_type = field["type"]
  if field.get("mode") == "REPEATED":
    scalar = {**field, "mode": "NULLABLE"}
    return [client_value(scalar, item) for item in raw]
  if bq_type == "RECORD" and "fields" in field:
    return {f["name"]: client_value(f, raw[f["name"]]) for f in field["fields"]}
  if bq_type in ("RECORD", "JSON"):
    return raw
  return _CLIENT[bq_type](raw)


def client_rows(fixture: Mapping[str, Any] | None = None) -> list[dict]:
  """The fixture's rows with the BigQuery client's Python types."""
  fixture = fixture or all_types()
  fields = fixture["schema"]
  return [{
      f["name"]: client_value(f, row[f["name"]]) for f in fields
  } for row in fixture["rows"]]


def _arrow_type(field: Mapping[str, Any]) -> pa.DataType | None:
  """The Storage Read API's Arrow type for a BigQuery field (None: let
  pyarrow infer it, for a RECORD the fixture declares no sub-fields for)."""
  if field["type"] == "RECORD" and "fields" in field:
    return pa.struct([(f["name"], _arrow_type(f)) for f in field["fields"]])
  types: dict[str, pa.DataType] = {
      "INT64": pa.int64(),
      "FLOAT64": pa.float64(),
      "NUMERIC": pa.decimal128(38, 9),
      "BIGNUMERIC": pa.decimal256(76, 38),
      "BOOL": pa.bool_(),
      "STRING": pa.string(),
      "BYTES": pa.binary(),
      "TIMESTAMP": pa.timestamp("us", tz="UTC"),
      "DATETIME": pa.timestamp("us"),
      "DATE": pa.date32(),
      "TIME": pa.time64("us"),
      "GEOGRAPHY": pa.string(),
      "JSON": pa.string(),
  }
  scalar = types.get(field["type"])
  if field.get("mode") == "REPEATED" and scalar is not None:
    return pa.list_(scalar)
  return scalar


def _arrow_cell(field: Mapping[str, Any], value: Any) -> Any:
  if field["type"] == "JSON" and value is not None:
    return json.dumps(value)  # the Storage Read API sends JSON as text
  return value


def arrow_rows(fields: Sequence[Mapping[str, Any]],
               rows: Sequence[Mapping[str, Any]]) -> list[dict]:
  """`rows` (client types) as Beam 2.76's DIRECT_READ with
  `use_native_datetime=True` yields them: one Arrow record batch in the
  Storage Read API's types, each cell turned back into Python by
  `.as_py()` (`_CustomBigQueryStorageStreamSource.read_arrow`)."""
  arrays = [
      pa.array([_arrow_cell(f, row[f["name"]]) for row in rows],
               type=_arrow_type(f)) for f in fields
  ]
  names = [f["name"] for f in fields]
  batch = pa.RecordBatch.from_arrays(arrays, names=names)
  return [{
      name: column[i].as_py()
      for name, column in zip(names, batch.columns, strict=True)
  }
          for i in range(batch.num_rows)]


def column_plans(fixture: Mapping[str, Any] | None = None) -> list[ColumnPlan]:
  """`ColumnPlan`s for the fixture's schema, kinds as the fixture says."""
  fixture = fixture or all_types()
  keys = set(fixture["pk"]) | {c for e in fixture["edges"] for c in e["cols"]}
  return [
      ColumnPlan(
          name=f["name"],
          bq_type=f["type"],
          mode=f["mode"],
          kind=ColumnKind(f["kind"]),
          is_key=f["name"] in keys,
          day_granularity=f["type"] == "DATE") for f in fixture["schema"]
  ]


def scope(landing: str, read_table: str) -> ScopePlan:
  return ScopePlan(
      landing_table=landing,
      mode="table",
      status="ok",
      reason=None,
      read_table=read_table,
      prepare_sql=(),
      window=(None, None),
      expected_rows=None,
      read_expr=f"`{read_table}`")


def table_plan(name: str,
               columns: Sequence[ColumnPlan],
               *,
               pk: Sequence[str] = (),
               identity: Sequence[str] = (),
               edges: Sequence[Edge] = (),
               panel: Panel | None = None,
               rows_source: int | None = None,
               rows_synthetic: int | None = None) -> TablePlan:
  """A launch table on invented thelook datasets."""
  landing = f"{PROJECT}.{DATASET}.{name}"
  source = f"{PROJECT}.{SOURCE_DATASET}.{name}"
  return TablePlan(
      name=name,
      landing_table=landing,
      source_table=source,
      run_id=f"run-00-{name}",
      role="isolated",
      pk=tuple(pk),
      identity=tuple(identity),
      edges=tuple(edges),
      columns=tuple(columns),
      scope=scope(landing, landing),
      source_read_table=source,
      source_pinned=False,
      panel=panel,
      pairs=(),
      encoding_plan_digest="0" * 32,
      synthetic_read_table=landing,
      rows_source=rows_source,
      rows_synthetic=rows_synthetic,
      sample_rate_source=1.0 if rows_source is not None else None,
      sample_rate_synthetic=1.0 if rows_synthetic is not None else None)


def fixture_edges(fixture: Mapping[str, Any] | None = None) -> tuple[Edge, ...]:
  fixture = fixture or all_types()
  return tuple(
      Edge(cols=tuple(e["cols"]), ref=e["ref"], ref_cols=tuple(e["ref_cols"]))
      for e in fixture["edges"])


def all_types_plan(*, panel: Panel | None = None, **kwargs: Any) -> TablePlan:
  """The all-types fixture's `TablePlan` (pk, identity and FK edge)."""
  fixture = all_types()
  return table_plan(
      fixture["table"],
      column_plans(fixture),
      pk=fixture["pk"],
      identity=fixture["identity"],
      edges=fixture_edges(fixture),
      panel=panel,
      **kwargs)


def make_panel(r_rows: Sequence[dict], h_rows: Sequence[dict]) -> Panel:
  return Panel(
      r_rows=list(r_rows),
      h_rows=list(h_rows),
      e_n=min(len(r_rows), 1024),
      he_n=min(len(h_rows), 1024),
      digest="d" * 64,
      verified=True,
      expected_digest="d" * 64)
