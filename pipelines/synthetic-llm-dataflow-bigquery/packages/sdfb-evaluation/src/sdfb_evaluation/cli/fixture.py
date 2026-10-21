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
"""Offline fixtures: `sdfb-eval plan|run --fixture_dir DIR` (a hidden
flag) evaluates rows held in JSON files, with no call to GCP.

    DIR/fixture.json
      {"project": "demo-project",
       "params": {"engine": "b1_rag"},                  (optional)
       "tables": [{"name": "orders",
                   "schema": [{"name", "type", "mode"}, ...],
                   "pk": ["order_id"], "identity": [],
                   "edges": [{"cols": ["user_id"], "ref": "users",
                              "ref_cols": ["id"]}],
                   "panel_rows": 40}]}                  (parents first)
    DIR/orders.source.json      [ {row}, ... ]   the source rows
    DIR/orders.synthetic.json   [ {row}, ... ]   the landed rows

A cell is JSON-typed and read back as the BigQuery client types it (the
encoder hashes canonical values, so the type matters): TIMESTAMP an
aware UTC `datetime` from ISO-8601 text, DATETIME / DATE / TIME from
ISO text, NUMERIC and BIGNUMERIC a `Decimal` from text, BYTES from
base64, RECORD and JSON as they are, a REPEATED column a list.

There is no BigQuery to plan with: each table is planned from its rows
by `context.offline` (the planning SELECT's statistics, exact; the
planner's own functions on top), its role comes from the planner's
`table_roles`, and the launch is a manual one — no generation job, the
fixture's `params`. Nothing is billed, prepared or sampled; the pipeline
reads the same rows through `InMemorySources`.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sdfb_evaluation.beam.io import InMemorySources
from sdfb_evaluation.catalogue import load_catalogue
from sdfb_evaluation.context import offline
from sdfb_evaluation.context.bq import normalize_fqn
from sdfb_evaluation.context.budget import predict_shuffle_gb
from sdfb_evaluation.context.launch import LaunchContext
from sdfb_evaluation.context.plan import (
    EvaluationPlan,
    Knobs,
    TablePlan,
    table_roles,
)
from sdfb_evaluation.context.relationships import Edge
from sdfb_evaluation.types import Side
from sdfb_evaluation.version import EVALUATOR_VERSION

__all__ = ["Fixture", "FixtureTable", "load_fixture"]

MANIFEST = "fixture.json"
_TABLE_KEYS = frozenset({
    "name", "schema", "pk", "identity", "edges", "panel_rows", "landing_table",
    "source_table"
})
_EDGE_KEYS = frozenset({"cols", "ref", "ref_cols", "enforced"})


def _timestamp(text: str) -> datetime:
  return datetime.fromisoformat(text.replace("Z", "+00:00"))


_DECODERS: dict[str, Callable[[Any], Any]] = {
    "INT64": int,
    "INTEGER": int,
    "FLOAT64": float,
    "FLOAT": float,
    "NUMERIC": Decimal,
    "BIGNUMERIC": Decimal,
    "DECIMAL": Decimal,
    "BIGDECIMAL": Decimal,
    "BOOL": bool,
    "BOOLEAN": bool,
    "STRING": str,
    "GEOGRAPHY": str,
    "BYTES": base64.b64decode,
    "TIMESTAMP": _timestamp,
    "DATETIME": datetime.fromisoformat,
    "DATE": date.fromisoformat,
    "TIME": time.fromisoformat,
}


def _cell(field: Mapping[str, Any], raw: Any) -> Any:
  """One JSON cell with the BigQuery client's Python type."""
  if raw is None:
    return None
  bq_type = str(field.get("type") or "").upper()
  if str(field.get("mode") or "").upper() == "REPEATED":
    scalar = {**field, "mode": "NULLABLE"}
    return [_cell(scalar, item) for item in raw]
  decode = _DECODERS.get(bq_type)
  if decode is None:  # RECORD, JSON, a type the planner calls nested
    return raw
  try:
    return decode(raw)
  except (TypeError, ValueError, ArithmeticError) as exc:
    # the column and the type, never the value
    name = field.get("name")
    raise ValueError(f"column {name!r}: a cell is not a JSON-encoded "
                     f"{bq_type} ({type(exc).__name__})") from exc


def _typed_rows(fields: Sequence[Mapping[str, Any]], raw: Any,
                where: str) -> list[dict[str, Any]]:
  if not isinstance(raw, list) or not all(isinstance(r, Mapping) for r in raw):
    raise ValueError(f"{where}: expected a JSON array of row objects")
  return [{
      str(f["name"]): _cell(f, row.get(f["name"])) for f in fields
  } for row in raw]


# --------------------------------------------------------------------------
# the fixture
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FixtureTable:
  """One table of a fixture: its schema, keys and both sides' rows."""
  name: str
  fields: tuple[Mapping[str, Any], ...]
  pk: tuple[str, ...]
  identity: tuple[str, ...]
  edges: tuple[Edge, ...]
  panel_rows: int
  landing_table: str
  source_table: str
  source_rows: list[dict[str, Any]]
  synthetic_rows: list[dict[str, Any]]


def _names(spec: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
  value = spec.get(key) or ()
  if isinstance(value, str) or not all(isinstance(v, str) for v in value):
    raise ValueError(f"{where}: {key} must be a list of column names")
  return tuple(value)


def _edge(spec: Any, where: str) -> Edge:
  if not isinstance(spec, Mapping) or not set(spec) <= _EDGE_KEYS:
    raise ValueError(f"{where}: an edge is {{cols, ref, ref_cols[, "
                     "enforced]}")
  enforced = spec.get("enforced", True)
  if not isinstance(enforced, bool) or not isinstance(spec.get("ref"), str):
    raise ValueError(f"{where}: an edge needs a ref table name and a "
                     "true/false enforced")
  return Edge(
      cols=_names(spec, "cols", where),
      ref=spec["ref"],
      ref_cols=_names(spec, "ref_cols", where),
      enforced=enforced)


def _read_json(path: str) -> Any:
  try:
    with open(path, encoding="utf-8") as handle:
      return json.load(handle)
  except FileNotFoundError as exc:
    raise ValueError(f"fixture file {path} does not exist") from exc
  except json.JSONDecodeError as exc:
    raise ValueError(f"fixture file {path} is not JSON ({exc})") from exc


def _table(directory: str, project: str, spec: Any) -> FixtureTable:
  if not isinstance(spec, Mapping) or not isinstance(spec.get("name"), str):
    raise ValueError(f"{MANIFEST}: every table needs a name")
  name = spec["name"]
  where = f"{MANIFEST} table {name!r}"
  unknown = sorted(set(spec) - _TABLE_KEYS)
  if unknown:
    raise ValueError(f"{where}: unknown key(s) {unknown}; expected any of "
                     f"{sorted(_TABLE_KEYS)}")
  fields = spec.get("schema")
  if not isinstance(fields, list) or not fields or not all(
      isinstance(f, Mapping) and isinstance(f.get("name"), str)
      for f in fields):
    raise ValueError(f"{where}: schema must be a non-empty list of "
                     "{name, type, mode} fields")
  panel_rows = spec.get("panel_rows", 0)
  if isinstance(panel_rows,
                bool) or not isinstance(panel_rows, int) or panel_rows < 0:
    raise ValueError(f"{where}: panel_rows must be an int >= 0")
  sides = {
      side:
          _typed_rows(
              fields,
              _read_json(os.path.join(directory, f"{name}.{side}.json")),
              f"{name}.{side}.json")
      for side in (Side.SOURCE.value, Side.SYNTHETIC.value)
  }
  return FixtureTable(
      name=name,
      fields=tuple(fields),
      pk=_names(spec, "pk", where),
      identity=_names(spec, "identity", where),
      edges=tuple(_edge(e, where) for e in spec.get("edges") or ()),
      panel_rows=panel_rows,
      landing_table=normalize_fqn(
          spec.get("landing_table") or f"{project}.fixture_synthetic.{name}"),
      source_table=normalize_fqn(
          spec.get("source_table") or f"{project}.fixture_source.{name}"),
      source_rows=sides[Side.SOURCE.value],
      synthetic_rows=sides[Side.SYNTHETIC.value])


@dataclass(frozen=True)
class Fixture:
  """A fixture directory, loaded (module docstring)."""
  directory: str
  project: str
  params: Mapping[str, Any]
  tables: tuple[FixtureTable, ...]

  def sources(self) -> InMemorySources:
    """The pipeline's sources: every table's two sides, in memory."""
    rows_by: dict[tuple[str, Side | str], Sequence[Mapping[str, Any]]] = {}
    for table in self.tables:
      rows_by[(table.name, Side.SOURCE)] = table.source_rows
      rows_by[(table.name, Side.SYNTHETIC)] = table.synthetic_rows
    return InMemorySources(rows_by)

  def _table_plan(self, table: FixtureTable, role: str, knobs: Knobs,
                  evaluation_id: str) -> TablePlan:
    panel = (
        offline.panel_of(table.source_rows, table.panel_rows)
        if table.panel_rows else None)
    return offline.table_plan(
        table.name,
        table.fields,
        table.source_rows,
        table.synthetic_rows,
        landing_table=table.landing_table,
        source_table=table.source_table,
        budget=knobs.budget,
        pk=table.pk,
        identity=table.identity,
        edges=table.edges,
        role=role,
        panel=panel,
        pair_max_columns=knobs.pair_max_columns,
        run_id=f"{evaluation_id}-{table.name}",
        model="fixture",
        reference_digest=panel.digest if panel is not None else None,
        scope_reason="offline fixture: the landing rows as they are")

  def plan(self, *, knobs: Knobs, mode: str, trigger: str, runner: str,
           now: datetime, evaluation_id: str) -> EvaluationPlan:
    """The fixture's `EvaluationPlan` (module docstring): every table
    planned from its rows, `evaluation_key` from the tables, the mode
    and the value knobs, `salt = blake2b(evaluation_key)`."""
    roles = table_roles({t.name: t.edges for t in self.tables})
    tables = tuple(
        self._table_plan(t, roles[t.name], knobs, evaluation_id)
        for t in self.tables)
    catalogue_version = load_catalogue().version
    payload = json.dumps(
        {
            "fixture": [t.encoding_plan_digest for t in tables],
            "tables": [t.landing_table for t in tables],
            "catalogue_version": catalogue_version,
            "evaluator_version": EVALUATOR_VERSION,
            "mode": mode,
            "knobs": knobs.key_dict(),
        },
        sort_keys=True,
        separators=(",", ":"))
    key = hashlib.blake2b(payload.encode(), digest_size=16).hexdigest()
    launch = LaunchContext(
        generation_job_id=None,
        job_name=None,
        region=None,
        started_at=None,
        finished_at=None,
        base_run_id=None,
        run_ids=tuple(str(t.run_id) for t in tables),
        tables_in_order=tuple(t.landing_table for t in tables),
        reference_table=None,
        write_disposition=None,
        relationships_uri=None,
        params=dict(self.params),
        model_sha=None,
        model_name="fixture",
        adjusted_model_uri=None,
        params_source="manual",
        writes=(),
        warnings=(f"offline fixture {self.directory}: planned from its "
                  "rows, nothing read from BigQuery",))
    return EvaluationPlan(
        evaluation_id=evaluation_id,
        evaluation_key=key,
        evaluated_at=now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        salt=hashlib.blake2b(key.encode(), digest_size=16).hexdigest(),
        mode=mode,
        trigger=trigger,
        runner=runner,
        launch=launch,
        models=(),
        model_sha=None,
        tables=tables,
        knobs=knobs,
        prepare_sql=(),
        bq_bytes_estimate=0,
        predicted_shuffle_gb=predict_shuffle_gb(tables),
        warnings=launch.warnings,
        catalogue_version=catalogue_version,
        temp_dataset=knobs.temp_dataset or
        f"{self.project}.{knobs.output_dataset}")


def load_fixture(directory: str) -> Fixture:
  """Load the fixture under `directory` (module docstring).

  Raises:
    ValueError: a missing or malformed manifest or rows file, an unknown
      key, or a cell that is not its column's type (the message names
      the file, table or column, never a value).
  """
  manifest = _read_json(os.path.join(directory, MANIFEST))
  if not isinstance(manifest, Mapping):
    raise ValueError(f"{MANIFEST}: expected a JSON object")
  project = manifest.get("project") or "demo-project"
  params = manifest.get("params") or {}
  specs = manifest.get("tables")
  if not isinstance(project, str) or not isinstance(params, Mapping):
    raise ValueError(f"{MANIFEST}: project must be text and params an object")
  if not isinstance(specs, list) or not specs:
    raise ValueError(f"{MANIFEST}: tables must be a non-empty list")
  tables = tuple(_table(directory, project, spec) for spec in specs)
  names = [t.name for t in tables]
  if len(set(names)) != len(names):
    raise ValueError(f"{MANIFEST}: table names repeat ({names})")
  return Fixture(
      directory=directory, project=project, params=params, tables=tables)
