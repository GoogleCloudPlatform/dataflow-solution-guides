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
"""Tests for `sdfb_evaluation.beam.pipeline` (Task 26) beyond the
acceptance: the runner defaults (a local run is in process, never on
Prism) and side-input cache sizing, the prepare step's degradations (on
a refusal only), per-table failure isolation — with and without the
failure side input —, profile rows whose edges persist as computed, and
the label key made once and kept out of the job graph.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import base64
import bz2
import dataclasses
import hashlib
import itertools
import json
import math
import random
import sys
import zlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import apache_beam as beam
import numpy as np
import pytest
from apache_beam.internal import pickler
from apache_beam.options.pipeline_options import (
    DebugOptions,
    PipelineOptions,
    SetupOptions,
    StandardOptions,
    WorkerOptions,
)
from apache_beam.portability import common_urns
from apache_beam.portability.api import beam_runner_api_pb2
from apache_beam.testing.test_pipeline import TestPipeline as BeamTestPipeline

from sdfb_evaluation.beam import census as census_module
from sdfb_evaluation.beam import dense as dense_module
from sdfb_evaluation.beam import pipeline as pipeline_module
from sdfb_evaluation.beam.assemble import stable_floats
from sdfb_evaluation.beam.io import InMemorySources, LocalJsonSinks
from sdfb_evaluation.beam.membership import TABLE_ERRORS
from sdfb_evaluation.beam.pipeline import (
    MIN_CACHE_MB,
    build_evaluation_pipeline,
    pipeline_options_defaults,
    prepare_evaluation,
    side_input_bytes,
)
from sdfb_evaluation.beam.relational import (
    COGROUP,
    SIDE_INPUT,
    SIDE_INPUT_MAX_KEYS,
    plan_edges,
)
from sdfb_evaluation.context.bq import BqApiError
from sdfb_evaluation.context.plan import SAMPLE_MODULUS, PrepareStatement
from sdfb_evaluation.context.scope import pin_source, sampled_read
from sdfb_evaluation.report.render import compare
from sdfb_evaluation.report.store import read_local
from sdfb_evaluation.scoring import is_aggregate
from sdfb_evaluation.types import Side

from .acceptance_data import (
    ITEMS_FIELDS,
    ORDER_EDGE,
    ORDERS_FIELDS,
    USER_EDGE,
    USERS_FIELDS,
    evaluation_plan,
    launch_rows,
    table_plan,
)
from .relational_data import external_table

LABEL_KEY = b"pipeline-test-label-key-0123456789"
_NOW = "2026-09-30T00:00:00Z"


@pytest.fixture(scope="module", name="small")
def fixture_small() -> dict[str, dict[str, list[dict]]]:
  source = launch_rows(seed=5, id_base=100_000)
  good = launch_rows(seed=6, id_base=500_000)
  return {
      side: {
          name: rows[:120] for name, rows in by_table.items()
      } for side, by_table in (("source", source), ("synthetic", good))
  }


def _tables(small: Mapping[str, Mapping[str, list]], **panels: int) -> list:
  source, synthetic = small["source"], small["synthetic"]
  return [
      table_plan(
          "users",
          USERS_FIELDS,
          source["users"],
          synthetic["users"],
          pk=("id",),
          identity=("email",),
          role="root",
          panel_rows=panels.get("users", 0)),
      table_plan(
          "orders",
          ORDERS_FIELDS,
          source["orders"],
          synthetic["orders"],
          pk=("order_id",),
          edges=(USER_EDGE,),
          role="driven",
          panel_rows=panels.get("orders", 0)),
      table_plan(
          "order_items",
          ITEMS_FIELDS,
          source["order_items"],
          synthetic["order_items"],
          pk=("id",),
          edges=(ORDER_EDGE,),
          role="driven",
          panel_rows=panels.get("order_items", 0)),
  ]


# --------------------------------------------------------------------------
# runner defaults
# --------------------------------------------------------------------------
def _options(plan: Any) -> PipelineOptions:
  """A local run's options: the evaluator's defaults and an explicit,
  empty command line — `PipelineOptions(**defaults)` alone also parses
  `sys.argv`, which under pytest is pytest's."""
  return PipelineOptions([], **pipeline_options_defaults("DirectRunner", plan))


_SHOWN = ("table_name", "metric_id", "column_name", "edge", "status", "value",
          "detail")


def _describe(rows: Sequence[Mapping[str, Any]]) -> str:
  """What each metric row measures, its status, value and detail, for an
  assertion message."""
  return "; ".join(json.dumps({key: row[key] for key in _SHOWN}) for row in rows)


def test_runner_defaults(small):
  direct = pipeline_options_defaults("DirectRunner")
  assert direct == {
      "runner": "FnApiRunner",  # in process: DirectRunner would go to Prism
      "save_main_session": False,
      "max_cache_memory_usage_mb": MIN_CACHE_MB,
  }
  dataflow = pipeline_options_defaults("DataflowRunner")
  assert dataflow["runner"] == "DataflowRunner"
  assert dataflow["experiments"] == ["upload_graph"]
  options = PipelineOptions([], **dataflow)
  assert options.view_as(SetupOptions).save_main_session is False
  assert options.view_as(WorkerOptions).max_cache_memory_usage_mb >= 512
  assert "enable_data_sampling" not in (
      options.view_as(DebugOptions).experiments or [])
  plan = evaluation_plan(
      _tables(small), evaluation_id="ev_opts", label_key_uri=None)
  assert pipeline_options_defaults(
      "DataflowRunner", plan
  )["max_cache_memory_usage_mb"] == MIN_CACHE_MB  # a small plan: the floor
  with pytest.raises(ValueError, match="runner"):
    pipeline_options_defaults("")


@pytest.mark.parametrize("name", [
    "DirectRunner", "directrunner", "Direct", "SwitchingDirectRunner",
    "TestDirectRunner", "apache_beam.runners.direct.direct_runner.DirectRunner"
])
def test_a_local_run_is_in_process(small, name):
  """Every name Beam resolves to its DirectRunner — which hands a batch
  pipeline to Prism — becomes the in-process FnApiRunner, and the
  pipeline built from those options is accepted."""
  plan = evaluation_plan(
      _tables(small)[:1], evaluation_id="ev_local", label_key_uri=None)
  defaults = pipeline_options_defaults(name, plan)
  assert defaults["runner"] == "FnApiRunner"
  p = beam.Pipeline(options=PipelineOptions([], **defaults))
  assert type(p.runner).__name__ == "FnApiRunner"
  out = build_evaluation_pipeline(
      p, plan, sources=InMemorySources({}), sinks=LocalJsonSinks("unused"))
  assert set(out) == {"metrics", "profiles", "flags", "registry", "failures"}


_ROUTED = sorted(pipeline_module._PRISM_ROUTED)  # pylint: disable=protected-access  # the whole set


@pytest.mark.parametrize("key", _ROUTED)
def test_every_prism_routed_name_defaults_to_a_runner_the_pipeline_accepts(
    small, key):
  """The two functions agree: a name the defaults reroute is never a name
  the pipeline then refuses (`TestDirectRunner` was routed by Beam to
  Prism but not by the defaults)."""
  plan = evaluation_plan(
      _tables(small)[:1], evaluation_id="ev_agree", label_key_uri=None)
  for spelling in (key, key + "runner"):
    defaults = pipeline_options_defaults(spelling, plan)
    assert defaults["runner"] == "FnApiRunner", spelling
    p = beam.Pipeline(options=PipelineOptions([], **defaults))
    build_evaluation_pipeline(
        p, plan, sources=InMemorySources({}), sinks=LocalJsonSinks("unused"))


def test_tests_default_to_the_in_process_runner():
  """`tests/conftest.py`: a pipeline a test of this package builds without
  naming a runner is in process too, not on Prism."""
  assert type(beam.Pipeline().runner).__name__ == "FnApiRunner"
  assert type(BeamTestPipeline().runner).__name__ == "FnApiRunner"


@pytest.mark.parametrize("name", [
    "PrismRunner", "prism",
    "apache_beam.runners.portability.prism_runner.PrismRunner"
])
def test_the_defaults_refuse_prism(name):
  with pytest.raises(ValueError, match="Prism"):
    pipeline_options_defaults(name)


@pytest.mark.parametrize("runner", [
    "DirectRunner", "SwitchingDirectRunner", "TestDirectRunner", "PrismRunner"
])
def test_the_pipeline_refuses_a_runner_backed_by_prism(small, runner):
  """Prism starts a step before its batch side input is complete, so a
  failed table's rows could be published as evaluated: a pipeline whose
  runner Beam backs with Prism is refused when the graph is built, with
  the way out — never run to produce rows that may be wrong."""
  plan = evaluation_plan(
      _tables(small)[:1], evaluation_id="ev_prism", label_key_uri=None)
  p = beam.Pipeline(options=PipelineOptions([], runner=runner))
  with pytest.raises(ValueError, match="Prism") as raised:
    build_evaluation_pipeline(
        p, plan, sources=InMemorySources({}), sinks=LocalJsonSinks("unused"))
  message = str(raised.value)
  assert "pipeline_options_defaults" in message and "FnApiRunner" in message
  assert not p.transforms_stack[0].parts  # nothing was added to the graph


def test_pytest_arguments_never_reach_beam(small, monkeypatch):
  """`PipelineOptions(**kwargs)` without `flags` parses `sys.argv`; the
  helpers pass an empty command line, so neither a flag Beam knows
  (`--runner`) nor one it does not (`--tb=long`) can enter a run."""
  monkeypatch.setattr(sys, "argv", [
      "pytest", "tests/x.py::test_y", "-q", "--tb=long", "--runner=PrismRunner"
  ])
  plan = evaluation_plan(
      _tables(small)[:1], evaluation_id="ev_argv", label_key_uri=None)
  options = _options(plan)
  assert options.view_as(StandardOptions).runner == "FnApiRunner"
  everything = options.get_all_options(retain_unknown_options=True)
  assert "tb" not in everything and "q" not in everything
  leaky = PipelineOptions(**pipeline_options_defaults("DirectRunner", plan))
  assert "tb" in leaky.get_all_options(retain_unknown_options=True)


def test_cache_is_the_sum_of_the_broadcast_sets(small):
  plan = evaluation_plan(
      _tables(small), evaluation_id="ev_cache", label_key_uri=None)
  base = side_input_bytes(plan)
  users = plan.tables[0]
  big = dataclasses.replace(
      users, rows_source=30_000_000, rows_synthetic=9_000_000)
  bigger = dataclasses.replace(plan, tables=(big, *plan.tables[1:]))
  # users is the orders edge's parent: its synthetic side (9 M <= 10 M
  # rows) is a side-input key set, its source side (30 M) a CoGroupByKey,
  # and 30 M source rows no longer fit membership's full-source sets —
  # so only the 9 M-key set is added (less the small plan's own sets)
  grown = side_input_bytes(bigger) - base
  assert 9_000_000 * 8 - 10_000 <= grown <= 9_000_000 * 8
  large = dataclasses.replace(
      plan,
      tables=tuple(
          dataclasses.replace(
              t, rows_source=9_000_000, rows_synthetic=9_000_000)
          for t in plan.tables))
  # two 9 M-key parent sets (72 MB each) and three full-source membership
  # sets (9 M rows x 2 arrays x 8 B = 144 MB each): past the 512 MB floor
  wanted = math.ceil(side_input_bytes(large) * 1.25 / 2**20)
  assert wanted > MIN_CACHE_MB
  assert pipeline_options_defaults("DataflowRunner",
                                   large)["max_cache_memory_usage_mb"] == wanted


def test_cache_counts_exactly_the_relational_side_input_joins(small):
  """The estimate reads the relational pass's own edge plan
  (`relational.plan_edges`: its parent resolution, its join switch and
  the sides it does not evaluate), so a parent key set is budgeted
  exactly when that pass broadcasts it (M9)."""
  plan = evaluation_plan(
      _tables(small), evaluation_id="ev_sets", label_key_uri=None)
  users = plan.tables[0]

  def planned(**changes: Any) -> Any:
    parent = dataclasses.replace(users, **changes)
    return dataclasses.replace(plan, tables=(parent, *plan.tables[1:]))

  def orders_edge(variant: Any) -> Any:
    """The synthetic side of orders → users as the relational pass plans it."""
    [spec] = [s for s, _ in plan_edges(variant.tables) if s.child == "orders"]
    return spec.synthetic

  key_set = 8 * SIDE_INPUT_MAX_KEYS
  at = planned(rows_synthetic=SIDE_INPUT_MAX_KEYS)
  past = planned(rows_synthetic=SIDE_INPUT_MAX_KEYS + 1)
  assert orders_edge(at).path == SIDE_INPUT
  assert orders_edge(past).path == COGROUP
  # one parent row past the switch: the join shuffles, the set is not held
  assert side_input_bytes(at) - side_input_bytes(past) == key_set
  # a side the pass does not evaluate (its parent cannot be read) builds
  # no key set, whatever the parent's planned row count
  unread = planned(
      rows_synthetic=SIDE_INPUT_MAX_KEYS,
      synthetic_read_table="",
      scope=dataclasses.replace(
          users.scope, read_table="", reason="tables.getData denied"))
  assert "cannot be read" in str(orders_edge(unread).reason)
  assert side_input_bytes(at) - side_input_bytes(unread) == key_set


def test_encoding_memory_errors_fail_the_bundle(small):
  """A data error on a worker makes the table not_evaluated; a
  MemoryError is transient and must fail (and retry) the bundle (M12)."""
  users = _tables(small)[0]
  fn = pipeline_module._SafeEncodeFn(users, Side.SYNTHETIC, salt="s" * 32)  # pylint: disable=protected-access  # the DoFn under test
  fn.setup()
  good = list(fn.process(small["synthetic"]["users"][:5]))
  assert len(good) == 1 and good[0].n == 5
  broken = [{
      k: v for k, v in r.items() if k != "age"
  } for r in small["synthetic"]["users"][:5]]
  [tagged] = list(fn.process(broken))
  assert tagged.tag == "failed" and "age" in tagged.value[1]

  class _Exhausted:

    def encode(self, rows: Any) -> Any:
      raise MemoryError("out of memory")

  fn._encoder = _Exhausted()  # pylint: disable=protected-access  # simulate an OOM mid-encode
  with pytest.raises(MemoryError):
    list(fn.process(small["synthetic"]["users"][:5]))
  # every other per-table data error still makes the table not_evaluated
  caught = pipeline_module._ENCODE_ERRORS  # pylint: disable=protected-access  # the rule under test
  assert set(caught) == set(TABLE_ERRORS) - {MemoryError}


# --------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------
class _FakeBq:
  """`Bq.execute`/`dry_run_bytes` over rules: a statement or dry run
  matching `refuse` raises BigQuery's error."""

  def __init__(self,
               *,
               refuse: Sequence[str] = (),
               error: Exception | None = None):
    self.refuse = tuple(refuse)
    self.error = error
    self.executed: list[str] = []
    self.dry_runs: list[str] = []

  def execute(self,
              sql: str,
              params: Mapping[str, Any],
              *,
              max_bytes: int | None = None) -> None:
    del params, max_bytes
    if any(word in sql for word in self.refuse):
      raise self.error or BqApiError(f"400 cannot run: {sql[:40]}", status=400)
    self.executed.append(sql)

  def dry_run_bytes(self, sql: str, params: Any = None) -> int:
    del params
    self.dry_runs.append(sql)
    if any(word in sql for word in self.refuse):
      raise self.error or PermissionError("403 tables.getData denied")
    return 0


def _pinned(small, *, sampled: bool):
  users = _tables(small)[0]
  pin = pin_source(
      source_table=str(users.source_table),
      job_create_time="2026-09-29T23:00:00Z",
      now=_NOW,
      time_travel_hours=168,
      temp_dataset="demo-project.synthetic_data_quality",
      evaluation_id="ev_prep")
  statements = [PrepareStatement(sql, {}) for sql in pin.prepare_sql]
  table = dataclasses.replace(
      users,
      source_pin=pin,
      source_pinned=True,
      source_read_table=pin.read_table)
  if sampled:
    keep = SAMPLE_MODULUS // 4
    sql, temp, params = sampled_read(
        pin.read_table,
        keep=keep,
        modulo=SAMPLE_MODULUS,
        salt="s" * 32,
        temp_dataset="demo-project.synthetic_data_quality",
        evaluation_id="ev_prep",
        side="src")
    statements.append(PrepareStatement(sql, params))
    table = dataclasses.replace(
        table, source_read_table=temp, sample_rate_source=keep / SAMPLE_MODULUS)
  plan = evaluation_plan([table], evaluation_id="ev_prep", label_key_uri=None)
  return dataclasses.replace(
      plan, prepare_sql=tuple(statements), salt="s" * 32), pin


def test_prepare_runs_every_statement(small):
  plan, pin = _pinned(small, sampled=True)
  bq = _FakeBq()
  prepared = prepare_evaluation(plan, bq)
  assert bq.executed == [s.sql for s in plan.prepare_sql]
  assert prepared.tables[0].source_pinned
  assert prepared.tables[0].source_pin == pin
  assert prepared.warnings == plan.warnings


def test_a_refused_pin_reads_the_source_unpinned(small):
  plan, _ = _pinned(small, sampled=False)
  prepared = prepare_evaluation(plan, _FakeBq(refuse=("CREATE SNAPSHOT",)))
  table = prepared.tables[0]
  assert table.source_read_table == table.source_table
  assert not table.source_pinned and table.source_pin is not None
  assert not table.source_pin.pinned
  assert table.registry_entry()["source_snapshot_ts"] is None
  assert any("read unpinned" in w for w in prepared.warnings)
  assert prepared.prepare_sql == ()


def test_a_refused_pin_resamples_the_live_source(small):
  plan, pin = _pinned(small, sampled=True)
  bq = _FakeBq(refuse=("CREATE SNAPSHOT",))
  prepared = prepare_evaluation(plan, bq)
  table = prepared.tables[0]
  assert len(bq.executed) == 1
  sample = bq.executed[0]
  assert f"FROM `{table.source_table}`" in sample
  assert pin.read_table not in sample
  assert sample.startswith(f"CREATE TABLE `{table.source_read_table}`")
  assert table.source_read_table != plan.tables[0].source_read_table
  assert table.sample_rate_source == plan.tables[0].sample_rate_source
  assert f"`{table.source_read_table}`" in prepared.prepare_sql[0].sql


@pytest.mark.parametrize(
    "error",
    [
        BqApiError("400 cannot clone across regions", status=400),
        BqApiError("403 not allowed", status=403),
        BqApiError("404 not found", status=404),
        PermissionError("403 snapshot denied"),  # the wrapper's 403
        LookupError("404 dataset not found in location"),  # and its 404
    ])
def test_a_pin_refusal_degrades(small, error):
  plan, _ = _pinned(small, sampled=False)
  bq = _FakeBq(refuse=("CREATE SNAPSHOT",), error=error)
  prepared = prepare_evaluation(plan, bq)
  assert not prepared.tables[0].source_pinned
  assert any(str(error) in w for w in prepared.warnings)


@pytest.mark.parametrize("error", [
    BqApiError("503 backend error", status=503),
    BqApiError("500 internal error", status=500),
    BqApiError("429 rate limited", status=429),
    BqApiError("409 concurrent job", status=409),
    BqApiError("transport failure", status=None),
])
def test_a_transient_pin_failure_raises(small, error):
  """Only a refusal degrades a pin; a transient or unknown error fails
  the preparation instead of silently switching the table to its live
  source (M8)."""
  plan, _ = _pinned(small, sampled=False)
  bq = _FakeBq(refuse=("CREATE SNAPSHOT",), error=error)
  with pytest.raises(BqApiError) as raised:
    prepare_evaluation(plan, bq)
  assert raised.value is error


def test_other_prepare_failures_raise(small):
  plan, _ = _pinned(small, sampled=True)
  with pytest.raises(BqApiError):
    prepare_evaluation(plan, _FakeBq(refuse=("FARM_FINGERPRINT",)))


def test_an_unreadable_read_only_parent_loses_that_side(small):
  products = external_table("products", ({
      "name": "id",
      "type": "INT64",
      "mode": "REQUIRED"
  },))
  plan = evaluation_plan([*_tables(small), products],
                         evaluation_id="ev_ext",
                         label_key_uri=None)
  bq = _FakeBq(refuse=(str(products.source_read_table),))
  prepared = prepare_evaluation(plan, bq)
  parent = prepared.tables[-1]
  assert parent.source_read_table == ""
  assert parent.scope.read_table == products.scope.read_table
  assert any("source twin" in w for w in prepared.warnings)
  bq = _FakeBq(refuse=(products.landing_table,))
  parent = prepare_evaluation(plan, bq).tables[-1]
  assert parent.scope.read_table == "" and parent.synthetic_read_table == ""
  assert "cannot be read" in str(parent.scope.reason)


@pytest.mark.parametrize("error", [
    BqApiError("503 backend error", status=503),
    BqApiError("429 rate limited", status=429),
])
def test_a_transient_preflight_failure_raises(small, error):
  """The read-only parent's readability check drops a side only when
  BigQuery refuses the read; a transient error fails the preparation
  instead of silently losing the side's metrics (M8)."""
  products = external_table("products", ({
      "name": "id",
      "type": "INT64",
      "mode": "REQUIRED"
  },))
  plan = evaluation_plan([*_tables(small), products],
                         evaluation_id="ev_ext",
                         label_key_uri=None)
  bq = _FakeBq(refuse=(str(products.source_read_table),), error=error)
  with pytest.raises(BqApiError):
    prepare_evaluation(plan, bq)
  refused = BqApiError("400 not a table", status=400)
  bq = _FakeBq(refuse=(str(products.source_read_table),), error=refused)
  assert prepare_evaluation(plan, bq).tables[-1].source_read_table == ""


# --------------------------------------------------------------------------
# per-table failure isolation (local runner, FnApiRunner)
# --------------------------------------------------------------------------
def _run(plan, rows_by: Mapping[tuple[str, str], list], out: Path,
         **kwargs: Any) -> LocalJsonSinks:
  sinks = LocalJsonSinks(str(out))
  with beam.Pipeline(options=_options(plan)) as p:
    build_evaluation_pipeline(
        p, plan, sources=InMemorySources(rows_by), sinks=sinks, **kwargs)
  return sinks


def _canonical(rows: Sequence[Mapping[str, Any]]) -> list[str]:
  return sorted(json.dumps(r, sort_keys=True) for r in rows)


def test_a_three_table_rerun_is_deterministic(small, tmp_path):
  """The module's three-table fixture, run twice: the relational pass, a
  FLOAT64 column and the multi-table roll-up the one-table acceptance
  rerun does not cover. Metric, profile and flag rows are byte-identical
  (sorted: the shards' order is the runner's); the registry row differs
  only in its timestamps."""
  key = tmp_path / "label.key"  # an ephemeral key would differ per run
  key.write_bytes(LABEL_KEY)
  plan = evaluation_plan(
      _tables(small), evaluation_id="ev_rerun", label_key_uri=str(key))
  rows_by = {
      (name, side): rows for side, by_table in small.items()
      for name, rows in by_table.items()
  }
  first = _run(plan, rows_by, tmp_path / "one")
  again = _run(plan, rows_by, tmp_path / "two")
  for table in ("evaluation_metrics", "evaluation_profiles",
                "evaluation_row_flags"):
    rows = first.read_rows(table)
    assert rows, table
    assert _canonical(rows) == _canonical(again.read_rows(table)), table
  [one], [two] = (first.read_rows("evaluation_data_history"),
                  again.read_rows("evaluation_data_history"))
  stamps = ("recorded_at", "finished_at")
  assert {
      k: v for k, v in one.items() if k not in stamps
  } == {
      k: v for k, v in two.items() if k not in stamps
  }


def test_a_failing_table_is_not_evaluated_and_the_run_continues(
    small, tmp_path):
  """orders' synthetic rows lack a plan column (the encoder raises on a
  worker); order_items' source cannot be read (the driver raises): both
  are not_evaluated with the reason, users is evaluated, the run is
  PARTIAL."""
  plan = evaluation_plan(
      _tables(small), evaluation_id="ev_isolated", label_key_uri=None)
  broken = [{
      k: v for k, v in row.items() if k != "status"
  } for row in small["synthetic"]["orders"]]
  rows_by = {
      ("users", "source"): small["source"]["users"],
      ("users", "synthetic"): small["synthetic"]["users"],
      ("orders", "source"): small["source"]["orders"],
      ("orders", "synthetic"): broken,
      ("order_items", "synthetic"): small["synthetic"]["order_items"],
  }
  sinks = _run(plan, rows_by, tmp_path / "out")
  metrics = [
      r for r in sinks.read_rows("evaluation_metrics")
      if not is_aggregate(r["metric_id"])
  ]
  by_table: dict[str, list[dict]] = {}
  for row in metrics:
    by_table.setdefault(row["table_name"], []).append(row)
  assert any(r["status"] == "pass" for r in by_table["users"])
  for row in by_table["orders"]:
    assert row["status"] == "not_evaluated", row
    assert "could not be encoded" in row["detail"]["reason"]
    assert "status" in row["detail"]["reason"]  # the column, never a value
  for row in by_table["order_items"]:
    assert row["status"] == "not_evaluated", row
    assert "could not be set up" in row["detail"]["reason"]
  items_ids = {r["metric_id"] for r in by_table["order_items"]}
  assert {
      "column.ks", "row.memorization_lift", "table.detection_auc",
      "relationship.orphan_rate"
  } <= items_ids
  for table in ("evaluation_profiles", "evaluation_row_flags"):
    names = {r["table_name"] for r in sinks.read_rows(table)}
    assert not names & {"orders", "order_items"}, table
  registry = sinks.read_rows("evaluation_data_history")
  assert len(registry) == 1
  final = registry[0]
  assert final["status"] == "PARTIAL"
  assert "orders: not evaluated" in final["status_reason"]
  assert "order_items: not evaluated" in final["status_reason"]


def test_a_malformed_table_plan_is_not_evaluated_and_the_run_continues(
    small, tmp_path):
  """A plan too malformed to build a table's specs (a pair naming a
  column the table does not have) fails that table on the driver — and
  listing its not_evaluated rows must not fail the job in turn: its
  table-level rows are written, the other table is evaluated (M10)."""
  users, orders = _tables(small)[:2]
  items = [f["name"] for f in ORDERS_FIELDS].index("num_of_item")
  broken = dataclasses.replace(orders, pairs=((items, 99),))
  plan = evaluation_plan([users, broken],
                         evaluation_id="ev_malformed",
                         label_key_uri=None)
  rows_by = {
      (name, side): small[side][name] for name in ("users", "orders")
      for side in ("source", "synthetic")
  }
  sinks = _run(plan, rows_by, tmp_path / "out")
  metrics = [
      r for r in sinks.read_rows("evaluation_metrics")
      if not is_aggregate(r["metric_id"])
  ]
  assert any(
      r["table_name"] == "users" and r["status"] == "pass" for r in metrics)
  failed = [r for r in metrics if r["table_name"] == "orders"]
  assert failed and all(r["status"] == "not_evaluated" for r in failed)
  assert all("could not be set up" in r["detail"]["reason"] for r in failed)
  owned = [r for r in failed if r["level"] in ("row", "table")]
  assert {
      "row.memorization_lift", "table.detection_auc", "table.row_count_ratio"
  } <= {r["metric_id"] for r in owned}
  withheld = [r for r in owned if "rows_withheld" in r["detail"]]
  assert withheld and not any(r["column_name"] for r in withheld)
  [final] = sinks.read_rows("evaluation_data_history")
  assert final["status"] == "PARTIAL"
  assert "orders: not evaluated" in final["status_reason"]


def test_every_table_failing_still_writes_rows_and_a_final_row(small, tmp_path):
  """No table reaches a transform (no side can be read): every launch
  table is not_evaluated with the reason, the roll-ups say why they are
  empty, and the FINAL row is FAILED with the reasons in warnings (M4)."""
  plan = evaluation_plan(
      _tables(small)[:2], evaluation_id="ev_none", label_key_uri=None)
  sinks = _run(plan, {}, tmp_path / "out")
  metrics = sinks.read_rows("evaluation_metrics")
  measured = [r for r in metrics if not is_aggregate(r["metric_id"])]
  assert {r["table_name"] for r in measured} == {"users", "orders"}
  graded = [r for r in measured if r["status"] != "not_evaluated"]
  assert not graded, _describe(graded)
  assert all("cannot be read" in r["detail"]["reason"] for r in measured)
  model = [r for r in metrics if r["metric_id"] == "model.overall_score"]
  assert len(model) == 1 and model[0]["status"] == "not_evaluated"
  registry = sinks.read_rows("evaluation_data_history")
  assert len(registry) == 1
  assert registry[0]["status"] == "FAILED" and registry[0]["event"] == "FINAL"
  assert registry[0]["status_reason"].startswith(
      "no launch table could be evaluated: orders: not evaluated")
  assert sum("cannot be read" in w for w in registry[0]["warnings"]) == 2
  assert registry[0]["metrics_total"] == len(measured)
  assert registry[0]["metrics_not_evaluated"] == len(measured)
  assert registry[0]["metrics_pass"] == 0
  assert registry[0]["overall_score"] is None
  rollups = [r for r in metrics if is_aggregate(r["metric_id"])]
  scored = [r for r in rollups if r["status"] != "not_evaluated"]
  assert rollups and not scored, _describe(scored)


def _never(_: Any) -> bool:
  return False


def _without_the_failure_map(monkeypatch: pytest.MonkeyPatch) -> None:
  """Make the `failures` side input arrive EMPTY at every step that reads
  it: what a runner serves when it starts a step before a side input is
  complete. Prism did exactly that to the Guard, in some runs (the root
  cause of the intermittent failure of the test above); here it is every
  run. Every other side input is left alone."""
  as_dict = beam.pvalue.AsDict

  def nothing_yet(pcoll: beam.PCollection) -> Any:
    if "FirstFailure" not in pcoll.producer.full_label:
      return as_dict(pcoll)
    return as_dict(pcoll | "NothingYet" >> beam.Filter(_never))

  monkeypatch.setattr(beam.pvalue, "AsDict", nothing_yet)


def test_driver_failures_hold_without_the_failure_side_input(
    small, tmp_path, monkeypatch):
  """Regression of the Prism race, made deterministic: with the failure
  side input empty, a table the DRIVER saw fail still has no graded row
  — its `table.row_count_ratio` and its edge's rows included —, its
  roll-ups are not scored and the FINAL row is FAILED with the reasons:
  what the driver knows is a constant of the graph, not a side input."""
  _without_the_failure_map(monkeypatch)
  plan = evaluation_plan(
      _tables(small)[:2], evaluation_id="ev_none", label_key_uri=None)
  sinks = _run(plan, {}, tmp_path / "out")
  metrics = sinks.read_rows("evaluation_metrics")
  measured = [r for r in metrics if not is_aggregate(r["metric_id"])]
  assert {r["metric_id"] for r in measured} >= {
      "table.row_count_ratio", "relationship.orphan_rate", "column.ks"
  }
  graded = [r for r in metrics if r["status"] != "not_evaluated"]
  assert not graded, _describe(graded)
  setup = [
      r for r in measured if "could not be set up" in r["detail"]["reason"]
  ]
  assert len(setup) == len(measured)  # the driver's reason, on every row
  [final] = sinks.read_rows("evaluation_data_history")
  assert final["status"] == "FAILED"
  assert final["status_reason"].startswith(
      "no launch table could be evaluated: orders: not evaluated")
  assert sum("cannot be read" in w for w in final["warnings"]) == 2
  assert final["metrics_pass"] == 0
  assert final["metrics_not_evaluated"] == len(measured)


def test_one_driver_failure_holds_without_the_failure_side_input(
    small, tmp_path, monkeypatch):
  """The same with a healthy table beside the failed one: `orders`
  cannot be read, so none of its rows is graded — not its row count, not
  the rows the relational pass computes for its edge from the readable
  parent and no child at all — while `users` is evaluated and the run is
  PARTIAL naming `orders`."""
  _without_the_failure_map(monkeypatch)
  plan = evaluation_plan(
      _tables(small)[:2], evaluation_id="ev_one", label_key_uri=None)
  rows_by = {
      ("users", side): small[side]["users"] for side in ("source", "synthetic")
  }
  sinks = _run(plan, rows_by, tmp_path / "out")
  measured = [
      r for r in sinks.read_rows("evaluation_metrics")
      if not is_aggregate(r["metric_id"])
  ]
  orders = [r for r in measured if r["table_name"] == "orders"]
  assert {
      "table.row_count_ratio", "relationship.orphan_rate",
      "relationship.parent_coverage"
  } <= {r["metric_id"] for r in orders}
  graded = [r for r in orders if r["status"] != "not_evaluated"]
  assert not graded, _describe(graded)
  assert all("could not be set up" in r["detail"]["reason"] for r in orders)
  assert any(
      r["table_name"] == "users" and r["status"] == "pass" for r in measured)
  [final] = sinks.read_rows("evaluation_data_history")
  assert final["status"] == "PARTIAL"
  assert "orders: not evaluated" in final["status_reason"]
  assert any("orders: not evaluated" in w for w in final["warnings"])


# --------------------------------------------------------------------------
# profile rows (I2)
# --------------------------------------------------------------------------
_NARROW_FIELDS: tuple[dict[str, str], ...] = (
    {
        "name": "id",
        "type": "INT64",
        "mode": "REQUIRED"
    },
    {
        "name": "seen_at",
        "type": "TIMESTAMP",
        "mode": "NULLABLE"
    },
    {
        "name": "reading",
        "type": "FLOAT64",
        "mode": "NULLABLE"
    },
)


def _narrow_rows(n: int, base: int, seed: int) -> list[dict[str, Any]]:
  """Timestamps inside one minute and readings one unit apart near 1e12:
  narrow ranges at large offsets."""
  rng = random.Random(seed)
  start = datetime(2025, 9, 16, 12, tzinfo=UTC)
  return [{
      "id": base + i,
      "seen_at": start + timedelta(milliseconds=rng.randrange(60_000)),
      "reading": 1e12 + rng.randrange(40),
  } for i in range(n)]


def test_narrow_edges_at_a_large_offset_persist_as_computed(tmp_path):
  """Histogram edges closer than nine significant digits can tell apart
  (seconds apart at an epoch-seconds offset, units apart near 1e12) are
  written as computed: strictly increasing, matching the row's
  `edges_digest`; quantiles are not quantised either (I2)."""
  source, synthetic = _narrow_rows(600, 1000, 1), _narrow_rows(600, 9000, 2)
  events = table_plan("events", _NARROW_FIELDS, source, synthetic, pk=("id",))
  plan = evaluation_plan([events],
                         evaluation_id="ev_narrow",
                         label_key_uri=None)
  rows_by = {("events", "source"): source, ("events", "synthetic"): synthetic}
  profiles = _run(plan, rows_by,
                  tmp_path / "out").read_rows("evaluation_profiles")
  histograms = {
      (r["column_name"], r["side"]): r
      for r in profiles
      if r["profile_kind"] == "histogram"
  }
  assert set(histograms) == {(column, side)
                             for column in ("seen_at", "reading")
                             for side in ("source", "synthetic")}
  for (column, _), row in histograms.items():
    edges = row["payload"]["edges"]
    assert len(edges) >= 5
    assert all(a < b for a, b in itertools.pairwise(edges)), row
    # the fixture bites: nine significant digits would merge these edges
    assert len(set(stable_floats(edges))) < len(edges)
    if column == "seen_at":  # digested as whole epoch microseconds
      unit, digested = "epoch_micros", np.rint(np.asarray(edges) * 1e6)
    else:
      unit, digested = "value", np.asarray(edges)
    digest = hashlib.blake2b(
        unit.encode() + digested.astype("<f8").tobytes(),
        digest_size=16).hexdigest()
    assert digest == row["edges_digest"], column
  quantiles = [r for r in profiles if r["profile_kind"] == "quantiles"]
  assert {r["column_name"] for r in quantiles} == {"seen_at", "reading"}
  for row in quantiles:
    values = row["payload"]["values"]
    assert values == sorted(values)
    assert len(set(values)) > len(set(stable_floats(values)))
  # the moments are rounded: nine significant digits, as the metric rows
  for row in profiles:
    if row["profile_kind"] == "moments":
      payload = row["payload"]
      for name in ("mean", "std", "skewness", "kurtosis_excess"):
        assert payload[name] == stable_floats(payload[name]), (name, row)


def _salaries(n: int, seed: int, base: int) -> list[dict[str, Any]]:
  rng = random.Random(seed)
  return [{
      "id": base + i,
      "seen_at": None,
      "reading": round(rng.lognormvariate(10.5, 0.4), 2),
  } for i in range(n)]


def test_compare_reads_psi_across_two_runs_of_a_small_column(tmp_path):
  """Ruling R113 (I5): a 120-row column publishes its histogram on edges
  thinned to k source records apart. The thinning reads the source's
  counts only, so two runs over the same source — here with two
  different synthetic sides — publish the same edges under the same
  `edges_digest`, and `compare` computes their PSI."""
  source = _salaries(120, 1, 1000)
  stored = []
  for name, seed in (("one", 2), ("two", 3)):
    synthetic = _salaries(120, seed, 9000)
    table = table_plan("events", _NARROW_FIELDS, source, synthetic, pk=("id",))
    plan = evaluation_plan([table],
                           evaluation_id=f"ev_small_{name}",
                           label_key_uri=None)
    rows_by = {("events", "source"): source, ("events", "synthetic"): synthetic}
    _run(plan, rows_by, tmp_path / name)
    stored.append(read_local(str(tmp_path / name)))
  histograms = [{
      (r["column_name"], r["side"]): r
      for r in run.profiles
      if r["profile_kind"] == "histogram"
  }
                for run in stored]
  for side in ("source", "synthetic"):
    one, two = (h[("reading", side)] for h in histograms)
    edges = one["payload"]["edges"]
    assert 2 <= len(edges) <= 12 and edges == two["payload"]["edges"], side
    assert one["edges_digest"] == two["edges_digest"], side
  assert min(histograms[0][("reading", "source")]["payload"]["counts"]) >= 10
  drift = {
      (r["column"], r["side"]): r for r in compare(*stored)["profiles"]["rows"]
  }
  assert drift[("reading", "source")]["psi"] == 0  # the same source rows
  synthetic = drift[("reading", "synthetic")]
  assert synthetic["psi"] is not None and synthetic["psi"] > 0
  assert synthetic["edges_digest_a"] == synthetic["edges_digest_b"]
  assert not synthetic["note"].startswith("not comparable")


# --------------------------------------------------------------------------
# sampled mode (Ruling R72; the final review's C1)
# --------------------------------------------------------------------------
_PEOPLE_FIELDS: tuple[dict[str, str], ...] = (
    {
        "name": "id",
        "type": "INT64",
        "mode": "REQUIRED"
    },
    {
        "name": "note",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "age",
        "type": "INT64",
        "mode": "NULLABLE"
    },
)
_NOTE_WORDS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
               "hotel", "india", "juliet", "kilo", "lima", "mike", "november",
               "oscar", "papa", "quebec", "romeo", "sierra", "tango", "uniform",
               "victor", "whiskey", "xray", "yankee", "zulu")


def _people(n: int, seed: int, base: int) -> list[dict[str, Any]]:
  rng = random.Random(seed)
  return [{
      "id": base + i,
      "note": " ".join(rng.choice(_NOTE_WORDS) for _ in range(7)),
      "age": rng.randint(18, 80),
  } for i in range(n)]


def _stored(sinks: LocalJsonSinks, column: str | None) -> dict[str, dict]:
  return {
      row["metric_id"]: row
      for row in sinks.read_rows("evaluation_metrics")
      if row["column_name"] == column and not is_aggregate(row["metric_id"])
  }


def test_sampled_mode_never_stores_a_sample_as_exact(tmp_path):
  """The final review's probe, end to end. Half the synthetic notes are
  verbatim source notes: an exact run stores a copy rate of 0.5 and a
  novelty of 0.5, both FAIL. With the plan's source rate at 0.1 (every
  tenth source row read) the same pipeline used to store 0.046 as an
  `exact` copy rate and turn novelty into a PASS. Now what needs every
  source row is not_evaluated with the R72 reason, and every other dense
  and census row is `method` = sample with the rate, over the rows read."""
  n = 1500
  source, synthetic = _people(n, 1, 1), _people(n, 2, 100_000)
  rng = random.Random(9)
  for i in range(0, n, 2):
    synthetic[i]["note"] = source[rng.randrange(n)]["note"]
  people = table_plan(
      "people", _PEOPLE_FIELDS, source, synthetic, pk=("id",), panel_rows=150)
  runs = {}
  for name, rate in (("exact", None), ("sampled", 0.1)):
    table = dataclasses.replace(people, sample_rate_source=rate)
    plan = evaluation_plan([table],
                           evaluation_id=f"ev_{name}",
                           label_key_uri=None)
    rows_by = {
        ("people", "source"): source if rate is None else source[::10],
        ("people", "synthetic"): synthetic,
    }
    runs[name] = _run(plan, rows_by, tmp_path / name)
  exact, sampled = _stored(runs["exact"],
                           "note"), _stored(runs["sampled"], "note")
  for metric_id in ("field.substantive_copy_rate", "column.novelty_mass"):
    row = exact[metric_id]
    assert (row["value"], row["status"], row["method"]) == (0.5, "fail",
                                                            "exact"), row
    row = sampled[metric_id]
    reason = row["detail"]["reason"]
    assert row["value"] is None and row["status"] == "not_evaluated", row
    assert reason.startswith("sampled mode cannot measure"), reason
    assert "the source side is a 0.1 row sample" in reason, reason
    assert reason.endswith("run exact mode"), reason
  # every dense and census row of the sampled run: a sample, never exact
  owned = set(dense_module.OWNED_METRIC_IDS) | set(
      census_module.OWNED_METRIC_IDS)
  rows = [
      r for r in runs["sampled"].read_rows("evaluation_metrics")
      if r["metric_id"] in owned
  ]
  assert len(rows) > 30
  for row in rows:
    if row["metric_id"] == "column.distinct_ceiling_hit":  # synthetic only
      assert row["method"] == "exact" and row["sample_rate"] is None
      continue
    if row["metric_id"] == "field.type_validity":  # its value is synthetic's
      assert row["method"] == "exact" and row["sample_rate"] is None
      continue
    assert row["method"] == "sample", row
    assert row["sample_rate"] == 0.1, row
    assert row["detail"]["sample_rates"] == {"source": 0.1}, row
    assert row["n_source"] is None or row["n_source"] <= n // 10, row
  nulls = _stored(runs["sampled"], "age")["column.null_rate_delta"]
  assert (nulls["n_source"], nulls["n_synthetic"]) == (n // 10, n)
  # the exact run's rows carry no sample
  assert all(r["method"] != "sample" and r["sample_rate"] is None
             for r in runs["exact"].read_rows("evaluation_metrics")
             if r["metric_id"] in owned)
  # the full-source match rate was already withheld (membership, R72)
  assert _stored(runs["sampled"],
                 None)["row.exact_match_rate"]["status"] == "not_evaluated"
  # profiles of the sampled side say so; the synthetic side's do not
  kinds = {"histogram", "quantiles", "moments", "length_hist", "topk"}
  profiles = [
      r for r in runs["sampled"].read_rows("evaluation_profiles")
      if r["profile_kind"] in kinds
  ]
  assert {r["profile_kind"] for r in profiles} == kinds
  for row in profiles:
    if row["side"] == "source":
      assert row["payload"]["sample_rate"] == 0.1, row
    elif row["side"] == "synthetic":
      assert "sample_rate" not in row["payload"], row


# --------------------------------------------------------------------------
# the label key
# --------------------------------------------------------------------------
def _payloads(p: beam.Pipeline) -> list[bytes]:
  proto = p.to_runner_api()
  out = []
  for transform in proto.components.transforms.values():
    if transform.spec.urn != common_urns.primitives.PAR_DO.urn:
      continue
    payload = beam_runner_api_pb2.ParDoPayload.FromString(
        transform.spec.payload)
    blob = payload.do_fn.payload
    if not blob:
      continue
    pickler.loads(blob)
    raw = base64.b64decode(blob)
    for decompress in (bz2.decompress, zlib.decompress):
      try:
        raw = decompress(raw)
        break
      except (OSError, zlib.error):
        continue
    out.append(raw)
  out.append(proto.SerializeToString())
  return out


def test_label_key_made_once_and_never_in_the_graph(small, tmp_path):
  key_file = tmp_path / "label.key"
  key_file.write_bytes(LABEL_KEY)
  plan = evaluation_plan(
      _tables(small, users=40),
      evaluation_id="ev_key",
      label_key_uri=str(key_file))
  rows_by = {
      (name, side): rows for side, by_table in small.items()
      for name, rows in by_table.items()
  }
  p = beam.Pipeline(options=_options(plan))
  build_evaluation_pipeline(
      p,
      plan,
      sources=InMemorySources(rows_by),
      sinks=LocalJsonSinks(str(tmp_path / "out")))
  labels = [
      applied.full_label
      for applied in p.transforms_stack[0].parts
      if applied.full_label.startswith("LabelKey")
  ]
  assert labels == ["LabelKey"]
  for blob in _payloads(p):
    assert LABEL_KEY not in blob and LABEL_KEY.hex().encode() not in blob
