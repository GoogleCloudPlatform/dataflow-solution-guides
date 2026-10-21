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
"""Doubles shared by the CLI tests: an in-memory BigQuery that also
records the driver's own writes, an `Env` over it, and a validator of a
row against its packaged table schema. Nothing here is real data
(`demo-project`, invented thelook rows).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import itertools
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions
from beam.acceptance_data import (
    ORDERS_FIELDS,
    USERS_FIELDS,
    items_rows,
    orders_rows,
    users_rows,
)
from beam.row_checks import check_rows
from unit.context.conftest import FakeResponse
from unit.context.plan_fakes import PlanBq, thelook_rows

from sdfb_evaluation.beam.assemble import final_row, finish_time
from sdfb_evaluation.beam.io import InMemorySources
from sdfb_evaluation.beam.pipeline import pipeline_options_defaults
from sdfb_evaluation.cli.driver import Env
from sdfb_evaluation.schemas import field_names, load_schema
from sdfb_evaluation.scoring import to_metric_row
from sdfb_evaluation.types import MetricValue

NOW = datetime(2026, 9, 14, 8, 0, 0, tzinfo=UTC)  # plan_fakes.NOW
REGISTRY = "evaluation_data_history"
FIXTURE_PROJECT = "demo-project"
_STAND_IN = "stand_in_rows"  # `tiny_pipeline(fast=True)`'s rows, on its pipeline
_FIXTURE_TABLES: dict[str, dict[str, Any]] = {
    "users": {
        "name": "users",
        "schema": list(USERS_FIELDS),
        "pk": ["id"],
        "identity": ["email"],
    },
    "orders": {
        "name": "orders",
        "schema": list(ORDERS_FIELDS),
        "pk": ["order_id"],
        "edges": [{
            "cols": ["user_id"],
            "ref": "users",
            "ref_cols": ["id"]
        }],
    },
}


def fixture_rows(users: int, seed: int,
                 id_base: int) -> dict[str, list[dict[str, Any]]]:
  """The acceptance generator's three tables for `users` invented
  users (deterministic in `seed`)."""
  people = users_rows(users, seed, id_base)
  orders = orders_rows(people, seed + 1, 10 * id_base)
  return {
      "users": people,
      "orders": orders,
      "order_items": items_rows(orders, seed + 2, 100 * id_base),
  }


def _json_cell(value: Any) -> Any:
  if isinstance(value, (datetime, date)):
    return value.isoformat()
  raise TypeError(type(value).__name__)


def write_fixture(directory: Path,
                  source: Mapping[str, Sequence[dict]],
                  synthetic: Mapping[str, Sequence[dict]],
                  *,
                  tables: Sequence[str] = ("users", "orders"),
                  panel: int = 0,
                  specs: Mapping[str, Mapping[str, Any]] | None = None) -> Path:
  """A `--fixture_dir` directory of `tables`, their rows JSON-typed,
  `panel` reference rows each. `specs` are the tables' manifest entries
  (default: the acceptance generator's `users` and `orders`)."""
  directory.mkdir()
  specs = specs or _FIXTURE_TABLES
  manifest = {
      "project": FIXTURE_PROJECT,
      "params": {
          "engine": "b1_rag"
      },
      "tables": [{
          **specs[name], "panel_rows": panel
      } for name in tables],
  }
  (directory / "fixture.json").write_text(json.dumps(manifest))
  for name in tables:
    for side, rows in (("source", source), ("synthetic", synthetic)):
      (directory / f"{name}.{side}.json").write_text(
          json.dumps(rows[name], default=_json_cell))
  return directory


class RecordingBq(PlanBq):
  """`PlanBq` (the planner's BigQuery over invented thelook rows) that
  also takes the driver's writes: `loads` records every `load_json`,
  `canned` answers a query by SQL substring before the planner's rules,
  `query_failures` raises for one instead, and `load_failures` raises
  instead of loading a table."""

  def __init__(self, **kwargs: Any):
    super().__init__(source=thelook_rows(1), landing=thelook_rows(2), **kwargs)
    self.loads: list[tuple[str, list[dict]]] = []
    self.canned: list[tuple[str, Sequence[Mapping[str, Any]]]] = []
    self.query_failures: dict[str, BaseException] = {}
    self.load_failures: dict[str, BaseException] = {}

  def load_json(self, fqn: str, rows: Sequence[Mapping[str, Any]],
                schema: list[dict]) -> str:
    table = fqn.rsplit(".", 1)[1]
    assert schema == load_schema(table), table
    if table in self.load_failures:
      raise self.load_failures[table]
    self.events.append(("load", fqn))
    self.loads.append((fqn, [dict(row) for row in rows]))
    return f"load_{len(self.loads)}"

  def query(self,
            sql: str,
            params: Mapping[str, Any] | None = None,
            *,
            max_bytes: int | None = None) -> list[dict]:
    for needle, exc in self.query_failures.items():
      if needle in sql:
        self.queries.append((sql, dict(params or {})))
        raise exc
    for needle, rows in self.canned:
      if needle in sql:
        self.queries.append((sql, dict(params or {})))
        return [dict(row) for row in rows]
    return super().query(sql, params, max_bytes=max_bytes)

  def registry_rows(self) -> list[dict]:
    """Every registry row the driver loaded, in order."""
    return [
        row for fqn, rows in self.loads if fqn.endswith(f".{REGISTRY}")
        for row in rows
    ]


def local_pipeline() -> beam.Pipeline:
  """A pipeline for a test that runs one itself, on the runner the
  evaluator runs a local evaluation on: the options are the evaluator's
  defaults for `DirectRunner` over an explicit, empty argument list — so
  never Prism (what Beam's own `DirectRunner` hands a batch pipeline to)
  and never `sys.argv`."""
  defaults = pipeline_options_defaults("DirectRunner")
  return beam.Pipeline(options=PipelineOptions([], **defaults))


def thelook_sources() -> InMemorySources:
  """The rows `RecordingBq` plans, as the pipeline's sources."""
  rows_by = {}
  for side, seed in (("source", 1), ("synthetic", 2)):
    for name, rows in thelook_rows(seed).items():
      rows_by[(name, side)] = rows
  return InMemorySources(rows_by)


class FakeResult:
  """A local runner's pipeline result whose state the test controls:
  `state` is what the runner says now; `wait_until_finish` raises
  `error` (leaving `state` as it is) or moves to `final` and returns it.
  It has no job id: the run cannot outlive the driver."""

  def __init__(self,
               state: str | Sequence[str] = "RUNNING",
               *,
               final: str | None = "DONE",
               error: BaseException | None = None):
    self._states = [state] if isinstance(state, str) else list(state)
    self.final = final
    self.error = error
    self.waits = 0
    self.cancels = 0

  @property
  def state(self) -> str:
    """What the runner says now. Given several states, each read takes
    the next one and the last one stays: a job that moves on between
    two looks at it."""
    if len(self._states) > 1:
      return self._states.pop(0)
    return self._states[0]

  @state.setter
  def state(self, value: str) -> None:
    self._states = [value]

  def wait_until_finish(self) -> str | None:
    self.waits += 1
    seen = self.state  # the runner's own last look, as its polling takes one
    if self.error is not None:
      raise self.error
    self.state = self.final or seen
    return self.final

  def cancel(self) -> None:
    self.cancels += 1


class FakeJob(FakeResult):
  """`FakeResult` of a submitted job (Dataflow): it has a job id and
  goes on running whatever happens to the driver."""

  def __init__(self, job: str = "2026-09-14_01_00_00-777", **kwargs: Any):
    super().__init__(**kwargs)
    self.job = job

  def job_id(self) -> str:
    return self.job


class JobOnlySession:
  """Dataflow `jobs.get` over the recorded job, recording every call; a
  Cloud Logging call (a POST) fails the test."""

  def __init__(self, job: dict | None):
    self.job = job
    self.calls: list[tuple[str, str]] = []
    self.headers: dict[str, str] = {}

  def get(self, url: str, params=None, timeout=None):
    del params, timeout
    self.calls.append(("GET", url))
    if self.job is None:
      return FakeResponse(404, {"error": {"message": "not found"}})
    if self.job == "denied":
      return FakeResponse(403, {"error": {"message": "denied"}})
    return FakeResponse(200, self.job)

  def post(self, url: str, *args, **kwargs):
    del args, kwargs
    self.calls.append(("POST", url))
    raise AssertionError(f"Cloud Logging was called: {url}")


def make_env(bq: Any,
             *,
             submit: Callable[[Any], Any] | None = None,
             now: datetime = NOW,
             **overrides: Any) -> Env:
  """An `Env` over `bq`: a fixed clock, counting id tokens, in-memory
  sources and — unless `submit` says otherwise — a pipeline that is
  built, never run, and reported DONE by a local `FakeResult`."""
  counter = itertools.count(1)
  env = Env(
      make_bq=lambda project: bq,
      session_factory=None,
      now=lambda: now,
      token=lambda: f"{next(counter):08x}",
      make_sources=thelook_sources,
      submit=submit or (lambda pipeline: FakeResult()))
  return dataclasses.replace(env, **overrides)


def check_row(table: str,
              row: Mapping[str, Any],
              *,
              ordered: bool = True) -> None:
  """`row` is exactly `table`'s schema (the acceptance suite's validator:
  the field names, every value of its field's type, no REQUIRED field
  NULL, no non-finite float) and — `ordered`, a row the driver built —
  its keys are in schema order (a local NDJSON line sorts them)."""
  if ordered:
    assert list(row) == list(field_names(table)), table
  check_rows(table, [row])


def ks_row(plan: Any, value: float, thresholds: Any = None) -> dict[str, Any]:
  """One `evaluation_metrics` row of `plan`: `column.ks` of
  orders.num_of_item at `value`, graded by the catalogue or the run's
  `thresholds` (no noise floor, so the value alone decides)."""
  return to_metric_row(
      MetricValue(
          metric_id="column.ks",
          table="orders",
          value=value,
          column="num_of_item",
          column_kind="numeric",
          n_source=240,
          n_synthetic=240),
      evaluation_id=plan.evaluation_id,
      evaluated_at=plan.evaluated_at,
      landing_table=None,
      source_table=None,
      thresholds=thresholds)


def write_stand_in(pipeline: Any) -> FakeResult:
  """`Env.submit` for a `tiny_pipeline(fast=True)`: the stand-in's rows
  go through the run's own sinks now, no Beam run, and the run is DONE."""
  for sinks, table, rows in getattr(pipeline, _STAND_IN, ()):
    sinks.write_rows(rows, table, label="stand-in")
  return FakeResult()


def tiny_pipeline(*,
                  counts: Mapping[str, int] | None = None,
                  status: str = "SUCCEEDED",
                  reason: str | None = None,
                  write: bool = True,
                  ks: Sequence[float] = (),
                  fast: bool = False) -> Callable[..., dict]:
  """A stand-in for `build_evaluation_pipeline` that writes only a FINAL
  registry row with `counts` through the run's own sinks (and one
  `ks_row` per value of `ks`, graded with the `thresholds` the driver
  hands over): the driver's whole path, in a pipeline that takes half a
  second. `built` on the returned function records each call's (plan,
  keyword arguments); `write=False` builds an empty pipeline (for a run
  whose pipeline is never executed). `fast=True` leaves Beam out: the
  same rows reach the same sinks when the test's `Env.submit` is
  `write_stand_in` (a test about the driver, not about the runner)."""
  built: list[tuple[Any, dict[str, Any]]] = []

  def build(p: beam.Pipeline, plan: Any, **kwargs: Any) -> dict:
    built.append((plan, kwargs))
    if not write:
      return {}
    row = final_row(
        plan, {
            "overall": 0.75,
            "fidelity": 0.75
        },
        dict(counts or {
            "total": 1,
            "pass": 1
        }), {},
        finished_at=finish_time(plan.evaluated_at),
        status=status,
        status_reason=reason)
    rows = {REGISTRY: [row]}
    if ks:
      graded = [ks_row(plan, v, kwargs.get("thresholds")) for v in ks]
      rows["evaluation_metrics"] = graded
    sinks = kwargs["sinks"]
    if fast:
      setattr(p, _STAND_IN, [(sinks, t, found) for t, found in rows.items()])
      return {}
    written = {}
    for table, found in rows.items():
      written[table] = p | table >> beam.Create(found)
      sinks.write(written[table], table)
    return {"registry": written[REGISTRY]}

  build.built = built  # type: ignore[attr-defined]
  return build
