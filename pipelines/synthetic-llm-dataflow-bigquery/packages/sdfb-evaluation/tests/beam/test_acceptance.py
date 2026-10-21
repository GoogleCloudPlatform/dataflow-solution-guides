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
"""The planted-defect acceptance of the whole evaluator (Task 26): the
composed pipeline (`beam.pipeline.build_evaluation_pipeline`) on the
local runner (FnApiRunner) with `LocalJsonSinks`, over the invented thelook-shaped
launch of `acceptance_data`.

    good synthetic (same generator) ─► no FAIL on any fidelity, privacy
                                       or integrity row or headline
    bad synthetic (7 planted defects) ─► every defect FAILs the metrics
                                         `acceptance_data.DEFECTS` names

Defect 1's 1 % of copies FAILs the memorization lift and the exact-match
rate; the DCR holdout share it cannot move past its gate (a copy fraction
c moves the share by at most c / 2, the gate reads ci_low >= 0.60), so
that metric's FAIL is proved on a heavy copier (30 % of R copies).

The bad run is also specific: it FAILs nothing outside the scopes its
defects touch. Every row either run persists re-scores to its own status
and score, and its histogram edges match their digest (Ruling R89).

Plus the brief's lifecycle and hygiene tests: a skipped registry row for
an empty scope, every written row valid against its schema, the FINAL
registry row written only after the metric sinks' load and copy jobs,
degenerate columns never NaN, and a rerun byte-identical (the heavy
copier's run, repeated).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import apache_beam as beam
import numpy as np
import pytest
from apache_beam.options.pipeline_options import PipelineOptions

from sdfb_evaluation.beam.io import InMemorySources, LocalJsonSinks, Sinks
from sdfb_evaluation.beam.pipeline import (
    build_evaluation_pipeline,
    pipeline_options_defaults,
)
from sdfb_evaluation.catalogue import load_catalogue
from sdfb_evaluation.context.plan import EvaluationPlan
from sdfb_evaluation.scoring import MODEL_KEY, headline_counts, is_aggregate

from .acceptance_data import (
    DEFECTS,
    FIELDS_BY_TABLE,
    FakeStatsQuery,
    acceptance_plan,
    evaluation_plan,
    heavy_copies,
    launch_rows,
    plant_defects,
    rescored,
    table_plan,
    with_scope,
)
from .row_checks import check_rows

pytestmark = pytest.mark.slow  # two three-table and two one-table evaluations

LABEL_KEY = b"acceptance-label-key-0123456789ab"
_GATED_FAMILIES = ("fidelity", "privacy", "integrity")
_CATALOGUE = load_catalogue()


class Outcome(NamedTuple):
  """What one evaluation wrote, table by table."""
  plan: EvaluationPlan
  metrics: list[dict[str, Any]]
  profiles: list[dict[str, Any]]
  flags: list[dict[str, Any]]
  registry: list[dict[str, Any]]
  lines: list[str]  # the metric shards' raw NDJSON lines


def _key_file(directory: Path) -> str:
  path = directory / "label.key"
  path.write_bytes(LABEL_KEY)
  return str(path)


def evaluate(plan: EvaluationPlan,
             rows: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
             out_dir: Path,
             *,
             stats_query: Any = None,
             sinks: Sinks | None = None) -> Outcome:
  """One evaluation on the local runner (FnApiRunner) with the runner
  defaults;
  `rows` maps side → table → rows."""
  sources = InMemorySources({
      (name, side): table_rows for side, by_table in rows.items()
      for name, table_rows in by_table.items()
  })
  sinks = sinks or LocalJsonSinks(str(out_dir))
  options = PipelineOptions([],
                            **pipeline_options_defaults("DirectRunner", plan))
  with beam.Pipeline(options=options) as p:
    build_evaluation_pipeline(
        p, plan, sources=sources, sinks=sinks, stats_query=stats_query)
  assert isinstance(sinks, LocalJsonSinks)
  lines = sorted(
      line for path in sorted(out_dir.glob("evaluation_metrics/*.jsonl"))
      for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
  return Outcome(plan, sinks.read_rows("evaluation_metrics"),
                 sinks.read_rows("evaluation_profiles"),
                 sinks.read_rows("evaluation_row_flags"),
                 sinks.read_rows("evaluation_data_history"), lines)


@pytest.fixture(scope="module", name="launch")
def fixture_launch() -> dict[str, dict[str, list[dict]]]:
  source = launch_rows(seed=11, id_base=100_000)
  good = launch_rows(seed=23, id_base=500_000)
  return {"source": source, "good": good, "bad": plant_defects(source, good)}


def _run(launch: Mapping[str, Any], which: str, directory: Path) -> Outcome:
  key = _key_file(directory)
  plan = acceptance_plan(
      launch["source"],
      launch[which],
      evaluation_id=f"ev_acc_{which}",
      label_key_uri=key)
  return evaluate(
      plan, {
          "source": launch["source"],
          "synthetic": launch[which]
      },
      directory / "out",
      stats_query=FakeStatsQuery(plan))


@pytest.fixture(scope="module", name="good_run")
def fixture_good_run(launch, tmp_path_factory) -> Outcome:
  return _run(launch, "good", tmp_path_factory.mktemp("good"))


@pytest.fixture(scope="module", name="bad_run")
def fixture_bad_run(launch, tmp_path_factory) -> Outcome:
  return _run(launch, "bad", tmp_path_factory.mktemp("bad"))


def _rows_for(outcome: Outcome, table: str, metric_id: str,
              column: str | tuple[str, str] | None) -> list[dict[str, Any]]:
  found = [
      row for row in outcome.metrics
      if row["table_name"] == table and row["metric_id"] == metric_id
  ]
  if column is None:
    return found
  if isinstance(column, tuple):  # a pair, in either order
    return [
        row for row in found
        if {row["column_name"], row["column_name_2"]} == set(column)
    ]
  return [
      row for row in found
      if column in (row["column_name"], row["column_name_2"])
  ]


def _describe(rows: Sequence[Mapping[str, Any]]) -> str:
  """One line per row, for an assertion message."""
  lines = []
  for row in rows:
    scope = "/".join(
        str(row[k])
        for k in ("table_name", "metric_id", "column_name", "column_name_2"))
    status, value, detail = row["status"], row["value"], row["detail"]
    ci = (row["ci_low"], row["ci_high"])
    lines.append(f"{scope}: {status} value={value} ci={ci} "
                 f"detail={json.dumps(detail)[:200]}")
  return "; ".join(lines)


# --------------------------------------------------------------------------
# the acceptance
# --------------------------------------------------------------------------
def test_good_synthetic_has_no_fail_on_gated_headlines(good_run):
  """A faithful generator FAILs nothing in the fidelity, privacy and
  integrity families: no measured row, no table or model headline."""
  failed = [
      row for row in good_run.metrics
      if row["family"] in _GATED_FAMILIES and row["status"] == "fail"
  ]
  assert not failed, _describe(failed)
  for _, table, column, metric_ids in DEFECTS:
    for metric_id in metric_ids:
      rows = _rows_for(good_run, table, metric_id, column)
      assert rows, (table, metric_id, column)
      assert all(r["status"] in ("pass", "warn") for r in rows), _describe(rows)
  headlines = [
      row for row in good_run.metrics
      if is_aggregate(row["metric_id"]) and row["family"] in _GATED_FAMILIES
  ]
  assert {row["table_name"] for row in headlines
         } == {"users", "orders", "order_items", MODEL_KEY}
  assert all(row["status"] != "fail" for row in headlines), _describe(headlines)
  final = good_run.registry[-1]
  assert final["event"] == "FINAL" and final["status"] == "SUCCEEDED"


@pytest.mark.parametrize(
    "defect", DEFECTS, ids=[d[0].split(" ", 1)[0] for d in DEFECTS])
def test_bad_synthetic_fails_every_planted_defect(bad_run, defect):
  _, table, column, metric_ids = defect
  for metric_id in metric_ids:
    rows = _rows_for(bad_run, table, metric_id, column)
    assert rows, (table, metric_id, column)
    assert all(r["status"] == "fail" for r in rows), _describe(rows)


def _one(outcome: Outcome, table: str, metric_id: str) -> dict[str, Any]:
  [row] = _rows_for(outcome, table, metric_id, None)
  return row


def test_one_percent_copies_raise_the_holdout_share(good_run, bad_run):
  """Defect 1 at the brief's 1 % (plus defect 2's 0.5 %) moves the DCR
  holdout share up, but a copy fraction c moves it by at most c / 2
  (the copies' nearest record is in R; the rest split evenly), so 1.5 %
  of copies cannot bring its CI lower bound to the 0.60 gate: the exact
  copies are the lift's and the exact-match rate's to catch (both FAIL,
  above), and the share's FAIL is proved at its design effect size in
  `test_heavy_memorization_fails_the_holdout_share`."""
  good = _one(good_run, "users", "row.dcr_train_holdout_share")
  bad = _one(bad_run, "users", "row.dcr_train_holdout_share")
  assert bad["value"] > good["value"]
  assert bad["value"] <= 0.5 + 0.015 / 2 + 4 * bad["detail"]["se_perm"]
  assert good["status"] == "pass"


def _heavy(launch: Mapping[str, Any], directory: Path) -> Outcome:
  """The heavy copier (30 % of the users rows carry R records): users
  alone, with the full R/H panel."""
  source = launch["source"]
  users = heavy_copies(source["users"], launch["good"]["users"])
  table = table_plan(
      "users",
      _fields("users"),
      source["users"],
      users,
      pk=("id",),
      identity=("email",),
      panel_rows=2000)
  plan = evaluation_plan([table],
                         evaluation_id="ev_acc_heavy",
                         label_key_uri=_key_file(directory))
  return evaluate(
      plan, {
          "source": {
              "users": source["users"]
          },
          "synthetic": {
              "users": users
          }
      },
      directory / "out",
      stats_query=FakeStatsQuery(plan))


@pytest.fixture(scope="module", name="heavy_run")
def fixture_heavy_run(launch, tmp_path_factory) -> Outcome:
  return _heavy(launch, tmp_path_factory.mktemp("heavy"))


def test_heavy_memorization_fails_the_holdout_share(heavy_run):
  share = _one(heavy_run, "users", "row.dcr_train_holdout_share")
  assert share["status"] == "fail", _describe([share])
  assert share["ci_low"] >= 0.6
  for metric_id in ("row.memorization_lift", "row.exact_match_rate_nonkey"):
    row = _one(heavy_run, "users", metric_id)
    assert row["status"] == "fail", _describe([row])


# Where the seven defects may FAIL a row; every other scope of the bad run
# is untouched and must not (specificity, M11):
#
#   users        1-2 copy whole R / E records under fresh keys: privacy
#                rows only (the row-level ones and the copied rare values
#                of its fields); every marginal and every pair is kept
#   orders       3 amount, 6 status, 7 delivery_note: those columns, the
#                pairs holding one of them, and the table-level fidelity
#                detectors (detection, pMSE, the correlation summaries)
#   order_items  4 cost against sale_price, the marginal kept: the pairs
#                holding cost and the table-level fidelity rows — never a
#                column's own rows; 5 the orphan rate of its edge
_ORDERS_DEFECT_COLUMNS = frozenset({"amount", "status", "delivery_note"})


def _planted(row: Mapping[str, Any]) -> bool:
  """Whether a planted defect explains a FAIL on `row`'s scope."""
  table, level, family = row["table_name"], row["level"], row["family"]
  columns = {row["column_name"], row["column_name_2"]}
  detectors = level == "table" and family == "fidelity"
  if table == "users":
    return family == "privacy" and level in ("row", "field")
  if table == "orders":
    cells = level in ("field", "column", "pair")
    return detectors or (cells and bool(columns & _ORDERS_DEFECT_COLUMNS))
  if table == "order_items":
    return (detectors or (level == "pair" and "cost" in columns) or
            row["metric_id"] == "relationship.orphan_rate")
  return False


def test_bad_run_fails_only_what_it_planted(bad_run):
  """Specificity: every FAIL of the bad run sits on a scope a planted
  defect touches, in every family; and a roll-up FAILs only for a table
  and family in which a measured row did."""
  measured = [r for r in bad_run.metrics if not is_aggregate(r["metric_id"])]
  failed = [r for r in measured if r["status"] == "fail"]
  stray = [r for r in failed if not _planted(r)]
  assert not stray, _describe(stray)
  hit = {(r["table_name"], r["family"]) for r in failed}
  for row in bad_run.metrics:
    if not is_aggregate(row["metric_id"]) or row["status"] != "fail":
      continue
    table, family = row["table_name"], row["family"]
    assert any(
        table in (MODEL_KEY, t) and family in ("overall", f)
        for t, f in hit), _describe([row])


# Named scopes no defect touches — (table, a column, a pair or an edge
# label): each is evaluated (it PASSes rows), so its lack of a FAIL above
# is not the lack of a row.
_UNTOUCHED: tuple[tuple[str, str | tuple[str, str]], ...] = (
    ("users", "age"),
    ("users", "city"),
    ("users", "created_at"),
    ("users", ("age", "created_at")),
    ("users", ("country", "traffic_source")),
    ("orders", "num_of_item"),
    ("orders", "created_at"),
    ("orders", ("num_of_item", "created_at")),
    ("orders", "orders(user_id) -> users(id)"),
    ("order_items", "sale_price"),
    ("order_items", "cost"),  # permuted across rows: its marginal is kept
    ("order_items", "status"),
    ("order_items", "created_at"),
    ("order_items", ("sale_price", "created_at")),
)


@pytest.mark.parametrize(
    "untouched", _UNTOUCHED, ids=[f"{t}-{s}" for t, s in _UNTOUCHED])
def test_bad_run_passes_the_scopes_it_did_not_touch(bad_run, untouched):
  table, scope = untouched
  rows = [r for r in bad_run.metrics if r["table_name"] == table]
  if isinstance(scope, tuple):
    rows = [
        r for r in rows if r["level"] == "pair" and
        {r["column_name"], r["column_name_2"]} == set(scope)
    ]
  elif "->" in scope:
    rows = [r for r in rows if r["edge"] == scope]
  else:
    rows = [
        r for r in rows
        if r["level"] in ("field", "column") and r["column_name"] == scope
    ]
  if table == "users":  # defects 1-2's own consequence: copied field values
    rows = [r for r in rows if r["family"] != "privacy"]
  assert sum(r["status"] == "pass" for r in rows) >= 3, untouched
  failed = [r for r in rows if r["status"] == "fail"]
  assert not failed, _describe(failed)


def test_every_persisted_row_rescores_to_its_status(good_run, bad_run):
  """Status and score are graded on the values as persisted (I1): any
  written metric row — measured or roll-up — scored again from its own
  fields gives the same status, score, value and detail."""
  statuses: set[str] = set()
  for outcome in (good_run, bad_run):
    for row in outcome.metrics:
      again = rescored(row)
      assert (again["status"], again["score"], again["value"],
              again["detail"]) == (row["status"], row["score"], row["value"],
                                   row["detail"]), _describe([row])
      statuses.add(row["status"])
  assert statuses == {"pass", "warn", "fail", "info", "not_evaluated"}


def test_registry_scores_are_the_persisted_rollup_rows(good_run, bad_run):
  for outcome in (good_run, bad_run):
    [final] = outcome.registry
    by_id = {
        (r["table_name"], r["metric_id"]): r["value"]
        for r in outcome.metrics
        if is_aggregate(r["metric_id"])
    }
    for family in ("overall", "fidelity", "privacy", "integrity", "diversity"):
      assert final[f"{family}_score"] == by_id[(MODEL_KEY,
                                                f"model.{family}_score")]
    for entry in final["tables"]:
      assert entry["table_score"] == by_id[(entry["name"],
                                            "table.overall_score")]


def _digest(edges: Sequence[float], unit: str = "") -> str:
  payload = unit.encode() + np.asarray(edges, dtype="<f8").tobytes()
  return hashlib.blake2b(payload, digest_size=16).hexdigest()


def test_persisted_edges_are_the_digested_edges(good_run, bad_run):
  """Histogram edges persist as computed (I2): strictly increasing, and
  they hash to the row's own `edges_digest` (dense: the unit and the
  edges in the column's own unit, a timestamp's as whole microseconds;
  nearest-neighbour distances: the edges). `test_pipeline` runs the
  narrow ranges nine significant digits would merge."""
  checked: dict[str, int] = {}
  for outcome in (good_run, bad_run):
    for row in outcome.profiles:
      kind, payload = row["profile_kind"], row["payload"]
      if kind not in ("histogram", "dcr_hist", "nndr_hist"):
        continue
      edges = payload["edges"]
      assert all(a < b for a, b in itertools.pairwise(edges)), row
      if kind != "histogram":
        unit, digest = kind, _digest(edges)
      elif payload["unit"] == "epoch_seconds":
        unit = "epoch_micros"
        digest = _digest(np.rint(np.asarray(edges) * 1e6), unit)
      else:
        unit, digest = "value", _digest(edges, "value")
      assert digest == row["edges_digest"], (row["column_name"], kind)
      checked[unit] = checked.get(unit, 0) + 1
  assert set(checked) == {"value", "epoch_micros", "dcr_hist", "nndr_hist"}


def test_bad_run_integrity_headline_and_registry(bad_run):
  final = bad_run.registry[-1]
  assert final["status"] == "SUCCEEDED" and final["event"] == "FINAL"
  assert final["metrics_fail"] > 0
  integrity = [
      row for row in bad_run.metrics
      if row["metric_id"] == "table.integrity_score" and
      row["table_name"] == "order_items"
  ]
  assert integrity and integrity[0]["detail"]["integrity_fail"] >= 1


# --------------------------------------------------------------------------
# the brief's other tests
# --------------------------------------------------------------------------
def test_empty_scope_writes_skipped_registry_row(launch, tmp_path):
  source = {name: rows[:40] for name, rows in launch["source"].items()}
  synthetic = {name: rows[:40] for name, rows in launch["good"].items()}
  full = acceptance_plan(
      source, synthetic, evaluation_id="ev_acc_empty", label_key_uri=None)
  tables = [
      with_scope(t, status="empty", reason="no rows in the appends window")
      for t in full.tables
  ]
  plan = dataclasses.replace(full, tables=tuple(tables))
  assert plan.skip_reason is not None
  outcome = evaluate(plan, {
      "source": source,
      "synthetic": synthetic
  }, tmp_path / "out")
  assert outcome.metrics == [] and outcome.profiles == []
  assert outcome.flags == []
  [row] = outcome.registry
  assert (row["event"], row["status"]) == ("FINAL", "SKIPPED")
  assert "empty" in row["status_reason"]
  assert row["metrics_total"] == 0 and row["metrics_fail"] == 0
  assert row["finished_at"] and row["recorded_at"] >= row["evaluated_at"]
  assert row["evaluation_params"]["label_key_mode"] == "ephemeral"
  check_rows("evaluation_data_history", outcome.registry)


def test_all_rows_validate_against_schemas(good_run, bad_run):
  for outcome in (good_run, bad_run):
    assert outcome.metrics and outcome.profiles and outcome.flags
    check_rows("evaluation_metrics", outcome.metrics)
    check_rows("evaluation_profiles", outcome.profiles)
    check_rows("evaluation_row_flags", outcome.flags)
    check_rows("evaluation_data_history", outcome.registry)
    catalogue_ids = set(_CATALOGUE.ids())
    assert {row["metric_id"] for row in outcome.metrics} <= catalogue_ids
    for row in outcome.metrics:
      if row["status"] == "not_evaluated":
        assert row["detail"] and row["detail"].get("reason"), row
    [final] = outcome.registry
    measured = [r for r in outcome.metrics if not is_aggregate(r["metric_id"])]
    counts = headline_counts(measured)
    assert final["metrics_total"] == counts["total"] == len(measured)
    assert final["metrics_info"] == counts["info"]
    assert (final["metrics_pass"] + final["metrics_warn"] +
            final["metrics_fail"] + final["metrics_info"] +
            final["metrics_not_evaluated"]) == final["metrics_total"]


class _OrderLog:
  """An append-only event log shared by the fake sinks' DoFns (the
  local runner (FnApiRunner) runs them in this process)."""

  def __init__(self, path: Path):
    self.path = str(path)

  def __call__(self, element: Any, event: str) -> Any:
    with open(self.path, "a", encoding="utf-8") as log:
      log.write(event + "\n")
    return element


class _FakeWriteResultSinks(Sinks):
  """Sinks whose metric writes return fake `WriteToBigQuery` results: a
  load-job and a copy-job PCollection, each logged when it completes;
  the registry write logs every row it receives."""

  def __init__(self, log: _OrderLog):
    self.log = log
    self.signals: dict[str, tuple[beam.PCollection, ...]] = {}

  def write(self,
            rows: beam.PCollection,
            table: str,
            *,
            label: str | None = None) -> tuple[beam.PCollection, ...]:
    label = label or f"Write[{table}]"
    if table == "evaluation_data_history":
      logged = rows | f"{label}/Log" >> beam.Map(self.log, "registry")
      return (logged,)
    counted = rows | f"{label}/Count" >> beam.combiners.Count.Globally()
    loads = counted | f"{label}/LoadJobs" >> beam.Map(
        lambda n, t=table: (t, f"load-{t}-{n}")) | (
            f"{label}/LoadDone" >> beam.Map(self.log, f"load:{table}"))
    copies = loads | f"{label}/CopyJobs" >> beam.Map(
        lambda pair: (pair[0], f"copy-{pair[0]}")) | (
            f"{label}/CopyDone" >> beam.Map(self.log, f"copy:{table}"))
    self.signals[table] = (loads, copies)
    return (loads, copies)


def test_registry_final_after_metric_writes(launch, tmp_path):
  source = {name: rows[:300] for name, rows in launch["source"].items()}
  synthetic = {name: rows[:300] for name, rows in launch["good"].items()}
  users = table_plan(
      "users",
      _fields("users"),
      source["users"],
      synthetic["users"],
      pk=("id",),
      identity=("email",))
  plan = evaluation_plan([users],
                         evaluation_id="ev_acc_order",
                         label_key_uri=None)
  log = _OrderLog(tmp_path / "events.log")
  sinks = _FakeWriteResultSinks(log)
  options = PipelineOptions([],
                            **pipeline_options_defaults("DirectRunner", plan))
  with beam.Pipeline(options=options) as p:
    out = build_evaluation_pipeline(
        p,
        plan,
        sources=InMemorySources({
            ("users", "source"): source["users"],
            ("users", "synthetic"): synthetic["users"]
        }),
        sinks=sinks)
    final_transform = out["registry"].producer
  events = Path(log.path).read_text(encoding="utf-8").split()
  writes = [e for e in events if e != "registry"]
  assert sorted(writes) == sorted(f"{kind}:{table}" for kind in ("load", "copy")
                                  for table in ("evaluation_metrics",
                                                "evaluation_profiles",
                                                "evaluation_row_flags"))
  assert events.count("registry") == 1
  assert events[-1] == "registry", events
  # the FINAL row's step reads every load and copy signal as a side input
  side_inputs = {id(s.pvalue) for s in final_transform.side_inputs}
  for loads, copies in sinks.signals.values():
    assert id(loads) in side_inputs and id(copies) in side_inputs


def _fields(name: str) -> Sequence[Mapping[str, Any]]:
  return FIELDS_BY_TABLE[name]


_DEGENERATE_FIELDS: tuple[dict[str, str], ...] = (
    {
        "name": "id",
        "type": "INT64",
        "mode": "REQUIRED"
    },
    {
        "name": "all_null",
        "type": "FLOAT64",
        "mode": "NULLABLE"
    },
    {
        "name": "constant",
        "type": "INT64",
        "mode": "NULLABLE"
    },
    {
        "name": "blank",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "single",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "flag",
        "type": "BOOL",
        "mode": "NULLABLE"
    },
    {
        "name": "moment",
        "type": "TIMESTAMP",
        "mode": "NULLABLE"
    },
    {
        "name": "amount",
        "type": "FLOAT64",
        "mode": "NULLABLE"
    },
)


def _degenerate(n: int, base: int, *, spread: bool) -> list[dict[str, Any]]:
  moment = datetime(2025, 5, 1, 12, tzinfo=UTC)
  return [{
      "id": base + i,
      "all_null": None,
      "constant": 7,
      "blank": "" if i % 2 else "   ",
      "single": "same",
      "flag": True,
      "moment": moment,
      "amount": float(i % 3) if spread else 2.5,
  } for i in range(n)]


def _no_constant(token: str) -> Any:
  raise AssertionError(f"a {token} token in a written row")


def test_degenerate_columns_never_nan(tmp_path):
  """All-NULL, constant, all-blank, single-valued and one-sided columns,
  tiny sides and a table with no rows at all: every metric row is a
  value or `not_evaluated` with a reason — never NaN, never a crash."""
  source = _degenerate(40, 1000, spread=True)
  synthetic = _degenerate(30, 5000, spread=False)
  tiny = table_plan(
      "tiny", _DEGENERATE_FIELDS, source, synthetic, pk=("id",), panel_rows=10)
  empty = table_plan("empty", _DEGENERATE_FIELDS, [], [], pk=("id",))
  plan = evaluation_plan([tiny, empty],
                         evaluation_id="ev_acc_degenerate",
                         label_key_uri=None)
  outcome = evaluate(
      plan, {
          "source": {
              "tiny": source,
              "empty": []
          },
          "synthetic": {
              "tiny": synthetic,
              "empty": []
          }
      }, tmp_path / "out")
  assert {row["table_name"] for row in outcome.metrics} >= {"tiny", "empty"}
  empty_rows = [
      r for r in outcome.metrics
      if r["table_name"] == "empty" and not is_aggregate(r["metric_id"])
  ]
  assert empty_rows and all(r["status"] == "not_evaluated" for r in empty_rows)
  assert any(r["metric_id"] == "column.null_rate_delta" for r in empty_rows)
  numeric = ("value", "source_value", "synthetic_value", "baseline_value",
             "score", "noise_floor", "ci_low", "ci_high", "threshold_warn",
             "threshold_fail", "sample_rate")
  for row in outcome.metrics:
    for name in numeric:
      value = row[name]
      assert value is None or math.isfinite(value), (row["metric_id"], name)
    if row["status"] == "not_evaluated":
      assert row["detail"]["reason"], row
  for line in outcome.lines:
    json.loads(line, parse_constant=_no_constant)  # no NaN / ±Infinity token
  check_rows("evaluation_metrics", outcome.metrics)
  [final] = outcome.registry
  # `empty` has no reference panel (nothing to rank): the privacy block
  # is not evaluated, so the run is PARTIAL for that reason alone
  assert final["status"] == "PARTIAL"
  assert final["status_reason"] == (
      "empty: no reference panel — the reference-based privacy metrics are "
      "not evaluated")


@pytest.fixture(scope="module", name="heavy_rerun")
def fixture_heavy_rerun(launch, tmp_path_factory) -> Outcome:
  return _heavy(launch, tmp_path_factory.mktemp("heavy_again"))


def test_rerun_is_deterministic(heavy_run, heavy_rerun):
  """The same plan and rows give byte-identical metric rows (sorted: the
  shards' order is the runner's), profiles and flags.

  The pair is the heavy copier's one-table run, not a second three-table
  bad run (a third of its cost). It covers the transforms that merge
  floating point or sample — dense, census, membership, privacy with the
  full panel, the source-stats drift — every profile kind and the row
  flags. It does NOT cover the relational pass (integer counts), the
  FLOAT64 column or the multi-table roll-up:
  `test_pipeline.py::test_a_three_table_rerun_is_deterministic` reruns
  the three-table case."""
  assert heavy_run.lines == heavy_rerun.lines
  assert len(heavy_run.lines) == len(heavy_run.metrics) > 0

  def canonical(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True) for r in rows)

  assert canonical(heavy_run.profiles) == canonical(heavy_rerun.profiles)
  assert canonical(heavy_run.flags) == canonical(heavy_rerun.flags)
  assert heavy_run.registry[0]["overall_score"] == heavy_rerun.registry[0][
      "overall_score"]
  # the pair is not trivially equal: the noisy producers all wrote
  kinds = {row["profile_kind"] for row in heavy_run.profiles}
  assert kinds >= {
      "histogram", "quantiles", "moments", "corr_matrix", "contingency", "topk",
      "shape_mix", "dcr_hist", "nndr_hist", "roc_curve"
  }
  assert heavy_run.flags
  evaluated = {
      row["metric_id"]
      for row in heavy_run.metrics
      if row["status"] in ("pass", "warn", "fail")
  }
  assert evaluated >= {
      "column.smd", "pair.pearson_delta", "pair.spearman_delta",
      "table.detection_auc", "row.dcr_train_holdout_share",
      "row.memorization_lift", "column.source_stats_drift"
  }
