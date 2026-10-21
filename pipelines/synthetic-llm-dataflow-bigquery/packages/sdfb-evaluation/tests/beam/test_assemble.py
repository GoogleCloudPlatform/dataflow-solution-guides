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
"""Tests for `sdfb_evaluation.beam.assemble` (Task 26): the registry
lifecycle rows, the run status, the metric rows the transforms do not own
(row count, source-stats drift, a failed table's rows), producer-CI
validation and the roll-ups — pure functions, no pipeline.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import random
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest

from sdfb_evaluation import schemas
from sdfb_evaluation.beam import assemble
from sdfb_evaluation.beam.assemble import (
    ColumnStats,
    DriftSpec,
    RegistryContext,
    RowContext,
    TableStats,
    aggregate_metrics,
    checked_ci,
    drift_metrics,
    evaluation_status,
    failed_row,
    failed_table_metrics,
    final_event,
    final_row,
    finish_time,
    guarded,
    metric_row,
    plan_problems,
    read_source_stats,
    row_count_metric,
    running_row,
    skipped_row,
    source_stats_sql,
    stable_floats,
    stable_metric,
    stable_profile,
    summarize_rows,
)
from sdfb_evaluation.beam.dense import DenseProfile, DenseSpec
from sdfb_evaluation.beam.encode import BatchEncoder
from sdfb_evaluation.catalogue import load_catalogue
from sdfb_evaluation.context.bq import BqApiError
from sdfb_evaluation.context.plan import PrepareStatement
from sdfb_evaluation.scoring import MODEL_KEY, is_aggregate
from sdfb_evaluation.types import MetricValue, ProfileValue

from .acceptance_data import (
    EVALUATED_AT,
    SALT,
    STATS_TABLE,
    USERS_FIELDS,
    FakeStatsQuery,
    evaluation_plan,
    rescored,
    scope_of,
    stats_rows,
    table_plan,
    users_rows,
    with_scope,
)

_CATALOGUE = load_catalogue()
_NOW = datetime(2026, 9, 30, 1, 2, 3, tzinfo=UTC)


@pytest.fixture(scope="module", name="users")
def fixture_users() -> tuple[list[dict], list[dict]]:
  return users_rows(600, 1, 100_000), users_rows(500, 2, 500_000)


def _users_plan(source: Sequence[Mapping[str, Any]],
                synthetic: Sequence[Mapping[str, Any]], **kwargs: Any):
  return table_plan(
      "users",
      USERS_FIELDS,
      source,
      synthetic,
      pk=("id",),
      identity=("email",),
      panel_rows=kwargs.pop("panel_rows", 100),
      **kwargs)


def _plan(users, **kwargs: Any):
  source, synthetic = users
  return evaluation_plan([_users_plan(source, synthetic)],
                         evaluation_id="ev_unit",
                         label_key_uri=kwargs.pop("label_key_uri", None),
                         **kwargs)


def _check_registry(row: Mapping[str, Any]) -> None:
  fields = schemas.load_schema("evaluation_data_history")
  assert list(row) == [f["name"] for f in fields]
  for field in fields:
    if field.get("mode") == "REQUIRED":
      assert row[field["name"]] is not None, field["name"]
  json.dumps(row, allow_nan=False)


# --------------------------------------------------------------------------
# the registry lifecycle
# --------------------------------------------------------------------------
def test_running_row_is_the_seed_as_a_running_event(users):
  plan = _plan(users, label_key_uri="/keys/label.key")
  row = running_row(plan)
  _check_registry(row)
  assert (row["event"], row["status"]) == ("RUNNING", "RUNNING")
  assert row["finished_at"] is None and row["metrics_total"] is None
  assert row["recorded_at"] == row["evaluated_at"] == EVALUATED_AT
  assert row["evaluation_params"]["label_key_mode"] == "operator"
  assert row["evaluation_params"]["planning_ddl"] == []
  assert "/keys/label.key" not in json.dumps(row)  # the mode, never the URI


def test_ephemeral_key_mode_and_planning_ddl_recorded(users):
  snapshot = PrepareStatement(
      "CREATE SNAPSHOT TABLE `demo-project.tmp.start` CLONE `a.b.c`", {})
  plan = dataclasses.replace(_plan(users), planning_ddl=(snapshot,))
  params = running_row(plan)["evaluation_params"]
  assert params["label_key_mode"] == "ephemeral"
  assert params["planning_ddl"] == [snapshot.sql]


def test_final_row_carries_scores_counts_and_table_scores(users):
  plan = _plan(users)
  counts = {
      "pass": 5,
      "warn": 1,
      "fail": 2,
      "info": 1,
      "not_evaluated": 3,
      "total": 12
  }
  scores = {
      "fidelity": 0.9,
      "privacy": 0.8,
      "integrity": 1.0,
      "diversity": None,
      "overall": 0.9
  }
  finished = finish_time(plan.evaluated_at, _NOW)
  row = final_row(
      plan,
      scores,
      counts, {"users": {
          "table_score": 0.77
      }},
      finished_at=finished,
      status="SUCCEEDED_WITH_WARNINGS",
      status_reason="1 warning(s): see warnings",
      warnings=("extra note",))
  _check_registry(row)
  assert (row["event"], row["status"]) == ("FINAL", "SUCCEEDED_WITH_WARNINGS")
  assert row["recorded_at"] == row["finished_at"] == finished
  assert row["recorded_at"] > running_row(plan)["recorded_at"]
  assert row["overall_score"] == 0.9 and row["diversity_score"] is None
  assert row["metrics_total"] == 12 and row["metrics_info"] == 1
  assert row["tables"][0]["table_score"] == 0.77
  assert "extra note" in row["warnings"]
  with pytest.raises(ValueError, match="status"):
    final_event(
        running_row(plan),
        scores=scores,
        counts=counts,
        per_table={},
        finished_at=finished,
        status="RUNNING",
        status_reason=None)


def test_finish_time_is_strictly_after_the_running_event():
  early = datetime(2020, 1, 1, tzinfo=UTC)
  assert finish_time(EVALUATED_AT, early) == "2026-09-30T00:00:00.000001Z"
  assert finish_time(EVALUATED_AT, _NOW) == "2026-09-30T01:02:03.000000Z"


def test_failed_row_names_the_error_and_what_planning_created(users):
  plan = _plan(users)

  class _PlanningError(RuntimeError):
    planning_ddl = (PrepareStatement("CREATE SNAPSHOT TABLE `x.y.z`", {}),)

  row = failed_row(plan, _PlanningError("boom " + "x" * 5000), now=_NOW)
  _check_registry(row)
  assert (row["event"], row["status"]) == ("FINAL", "FAILED")
  assert row["status_reason"].startswith("_PlanningError: boom")
  assert len(row["status_reason"]) <= 1000
  assert row["metrics_total"] is None and row["overall_score"] is None
  assert any("CREATE SNAPSHOT TABLE" in w for w in row["warnings"])
  assert row["recorded_at"] > row["evaluated_at"]


def test_skipped_row_only_for_a_skipped_plan(users):
  plan = _plan(users)
  with pytest.raises(ValueError, match="not skipped"):
    skipped_row(plan, finished_at=EVALUATED_AT)
  skipped = dataclasses.replace(
      plan,
      tables=tuple(
          with_scope(t, status="empty", reason="nothing written")
          for t in plan.tables))
  row = skipped_row(skipped, finished_at="2026-09-30T00:00:01.000000Z")
  _check_registry(row)
  assert (row["event"], row["status"]) == ("FINAL", "SKIPPED")
  assert row["metrics_total"] == 0
  assert row["evaluation_params"]["label_key_mode"] == "ephemeral"


# --------------------------------------------------------------------------
# the run status
# --------------------------------------------------------------------------
def test_status_rules():
  assert evaluation_status([], []) == ("SUCCEEDED", None)
  status, reason = evaluation_status([], ["scope contaminated"])
  assert status == "SUCCEEDED_WITH_WARNINGS" and "1 warning" in reason
  status, reason = evaluation_status(["orders: not evaluated — x"], ["w"])
  assert status == "PARTIAL" and reason.startswith("orders")
  status, reason = evaluation_status(["orders: not evaluated — x"], ["w"],
                                     none_evaluated=True)
  assert status == "FAILED"
  assert reason == ("no launch table could be evaluated: orders: not "
                    "evaluated — x")


def test_plan_problems_name_each_partial_condition(users):
  plan = _plan(users)
  assert not plan_problems(plan)
  table = plan.tables[0]
  panel = table.panel
  assert panel is not None
  variants = {
      "not evaluated":
          with_scope(table, status="empty", reason="no rows"),
      "count_mismatch":
          dataclasses.replace(
              table,
              scope=dataclasses.replace(
                  table.scope, status="count_mismatch", reason="3 vs 4")),
      "no reference panel":
          dataclasses.replace(table, panel=None),
      "reference not verified":
          dataclasses.replace(
              table,
              panel=dataclasses.replace(
                  panel, verified=False, reason="digest mismatch")),
  }
  for words, variant in variants.items():
    problems = plan_problems(dataclasses.replace(plan, tables=(variant,)))
    assert len(problems) == 1
    assert words in problems[0] and problems[0].startswith("users")
  ok_but_warned = dataclasses.replace(
      table,
      scope=dataclasses.replace(
          table.scope, status="contaminated", reason="foreign writer"))
  assert not plan_problems(dataclasses.replace(plan, tables=(ok_but_warned,)))


def test_every_launch_table_failing_is_a_failed_run(users):
  """No launch table evaluated (none plan-skipped, all failed in the
  pipeline): the FINAL row is FAILED, the reasons in warnings (M4)."""
  plan = _plan(users)
  context = RegistryContext.from_plan(plan)
  row = context.final(
      summarize_rows([]), {"users": "the source side cannot be read"},
      finished_at="2026-09-30T00:00:05.000000Z")
  _check_registry(row)
  assert row["status"] == "FAILED" and row["event"] == "FINAL"
  assert row["status_reason"].startswith(
      "no launch table could be evaluated: users: not evaluated — the "
      "source side cannot be read")
  assert any("cannot be read" in w for w in row["warnings"])
  assert row["metrics_total"] == 0 and row["overall_score"] is None
  # a plan-skipped table is not one the pipeline could have evaluated:
  # with the only other table failing, nothing was evaluated at all
  skipped = dataclasses.replace(
      with_scope(plan.tables[0], status="empty", reason="no rows"),
      name="people")
  both = dataclasses.replace(plan, tables=(*plan.tables, skipped))
  row = RegistryContext.from_plan(both).final(
      summarize_rows([]), {"users": "the source side cannot be read"},
      finished_at="2026-09-30T00:00:05.000000Z")
  assert row["status"] == "FAILED"
  assert "people: not evaluated" in row["status_reason"]


def test_registry_scores_are_the_persisted_rollup_values(users):
  """The registry's scores go through the rounding the roll-up rows get,
  so `overall_score` equals the persisted `model.overall_score` (M6)."""
  plan = _plan(users)
  raw = 0.12345678912345
  scores = {
      family: raw
      for family in ("fidelity", "privacy", "integrity", "diversity", "overall")
  }
  row = final_row(
      plan,
      scores, {"total": 0}, {"users": {
          "table_score": raw
      }},
      finished_at="2026-09-30T00:00:05.000000Z",
      status="SUCCEEDED",
      status_reason=None)
  context = RowContext("e1", EVALUATED_AT, {}, {})
  persisted = metric_row(
      MetricValue("model.overall_score", MODEL_KEY, raw), context)["value"]
  assert row["overall_score"] == persisted == 0.123456789
  assert row["integrity_score"] == row["fidelity_score"] == persisted
  assert row["tables"][0]["table_score"] == persisted
  # each score as ITS roll-up row is rounded (an integrity-family value
  # keeps no absolute floor, the others do)
  tiny = 1 / 3e10
  row = final_row(
      plan, {family: tiny for family in scores}, {"total": 0}, {},
      finished_at="2026-09-30T00:00:05.000000Z",
      status="SUCCEEDED",
      status_reason=None)
  for family in scores:
    rollup = MetricValue(f"model.{family}_score", MODEL_KEY, tiny)
    assert row[f"{family}_score"] == metric_row(rollup, context)["value"]
  assert row["integrity_score"] > 0 and row["overall_score"] == 0.0


def test_registry_context_marks_pipeline_failures_partial(users):
  source, synthetic = users
  plan = _plan(users)
  twin = dataclasses.replace(
      _users_plan(source, synthetic),
      name="people",
      landing_table="demo-project.thelook_synthetic.people")
  plan = dataclasses.replace(plan, tables=(*plan.tables, twin))
  context = RegistryContext.from_plan(plan)
  summary = summarize_rows([])
  row = context.final(
      summary, {"users": "the synthetic rows could not be encoded"},
      finished_at="2026-09-30T00:00:05.000000Z")
  _check_registry(row)
  assert row["status"] == "PARTIAL"
  assert "users: not evaluated" in row["status_reason"]
  assert any("could not be encoded" in w for w in row["warnings"])
  clean = context.final(summary, {}, finished_at="2026-09-30T00:00:05.000000Z")
  assert clean["status"] == "SUCCEEDED" and clean["metrics_total"] == 0


# --------------------------------------------------------------------------
# metric rows
# --------------------------------------------------------------------------
def test_stable_floats_hide_merge_noise_only():
  """Pairs of values two DirectRunner reruns of the acceptance wrote (a
  temporal SMD, a Spearman baseline, an epoch-micros mean) collapse; real
  differences and non-floats are kept."""
  for a, b in ((0.004734302797007421, 0.00473430279699401),
               (7.631888443659918e-06,
                7.63188844377094e-06), (1739676154630672.8, 1739676154630673.0),
               (0.00042778981969227026, 0.0004277898196924923)):
    assert stable_floats(a) == stable_floats(b)
  assert stable_floats(0.123456789123) == 0.123456789
  assert stable_floats(0.1234567) == 0.1234567
  assert stable_floats(4e-11) == 0.0  # below the 1e-10 floor
  for noise in (-4e-11, -0.0):  # never a signed zero: "-0.0" != "0.0"
    assert json.dumps(stable_floats(noise)) == "0.0"
  assert stable_floats(1e-5) == 1e-5  # the smallest catalogue threshold
  assert stable_floats({"a": [0.1 + 0.2, 3, None, "x", True]}) == {
      "a": [0.3, 3, None, "x", True]
  }
  assert stable_floats(float("inf")) == float("inf")
  assert stable_floats(-2.5000000001234) == -2.50000000


def test_reversed_producer_ci_is_treated_as_missing():
  """A reversed interval (ci_low > ci_high) is a producer defect: it is
  dropped before scoring (the noise check then says it had no CI, and a
  CI-gated metric has nothing to gate on) and flagged with the reported
  bounds (M3)."""
  reversed_ci = MetricValue(
      "column.null_rate_delta",
      "users",
      0.3,
      column="age",
      ci_low=0.4,
      ci_high=0.1,
      source_value=0.0,
      synthetic_value=0.3)
  flagged = checked_ci(reversed_ci)
  assert flagged.ci_low is None and flagged.ci_high is None
  assert flagged.detail["ci_invalid"].startswith("reversed interval")
  assert flagged.detail["ci_reported"] == [0.4, 0.1]
  context = RowContext("e1", EVALUATED_AT, {"users": ("a.b.users", None)}, {})
  row = metric_row(reversed_ci, context)  # metric_row checks it itself
  assert row == metric_row(flagged, context)
  assert row["status"] == "fail"
  assert row["ci_low"] is None and row["ci_high"] is None
  assert row["detail"]["noise_check"] == "unavailable"
  assert row["detail"]["ci_reported"] == [0.4, 0.1]
  # a proper interval covering the reference reads this FAIL as noise; a
  # reversed one is no evidence either way, so the status stands and the
  # row says the check was unavailable
  covering = dataclasses.replace(reversed_ci, ci_low=0.0, ci_high=0.4)
  assert metric_row(covering, context)["status"] == "pass"
  lift = MetricValue(
      "row.memorization_lift", "users", 3.0, ci_low=6.0, ci_high=2.0)
  row = metric_row(lift, context)
  assert row["status"] == "not_evaluated" and row["score"] is None
  assert "ci_low missing" in row["detail"]["reason"]
  assert row["detail"]["ci_invalid"].startswith("reversed interval")
  fine = dataclasses.replace(reversed_ci, ci_low=0.1, ci_high=0.4)
  assert checked_ci(fine) is fine
  open_ended = dataclasses.replace(reversed_ci, ci_low=5.0, ci_high=None)
  assert checked_ci(open_ended) is open_ended


@pytest.mark.parametrize(
    ("metric_id", "raw", "persisted", "status"),
    [
        # the review's probes: either side of a fail threshold
        ("column.null_rate_delta", 0.05 - 1e-12, 0.05, "fail"),
        ("column.null_rate_delta", 0.05 + 1e-12, 0.05, "fail"),
        # raw values short of a threshold by more than scoring's 1e-9
        # relative tolerance that persist AS the threshold: the raw value
        # reads the milder status, the persisted one the stricter
        ("column.null_rate_delta", 0.02 - 4e-11, 0.02, "warn"),
        ("column.ks", 0.2 - 4e-10, 0.2, "fail"),
        ("column.ks", 0.1 - 4e-11, 0.1, "warn"),
    ])
def test_status_is_graded_on_the_persisted_value(metric_id, raw, persisted,
                                                 status):
  """A value within a rounding step of a threshold persists as the
  threshold, and its status and score are the ones the persisted value
  reads (inclusive: at the threshold is past it), not the raw one's
  (I1)."""
  context = RowContext("e1", EVALUATED_AT, {}, {})
  mv = MetricValue(
      metric_id,
      "users",
      raw,
      column="age",
      noise_floor=0.001 + 1e-13,
      ci_low=raw - 0.01 + 1e-13,
      ci_high=raw + 0.01 - 1e-13)
  row = metric_row(mv, context)
  assert row["value"] == persisted and row["status"] == status
  metric = _CATALOGUE.get(metric_id)
  assert persisted in (metric.warn, metric.fail)
  again = rescored(row)
  assert (again["status"], again["score"], again["value"],
          again["detail"]) == (row["status"], row["score"], row["value"],
                               row["detail"])


def _near(rng: random.Random, anchor: float) -> float:
  """`anchor` displaced by nothing, by merge noise or by a real amount."""
  step = rng.choice((0.0, 1e-13, 1e-12, 4e-11, 3e-10, 1e-9, 1e-6, 1e-3, 0.03))
  return anchor + rng.choice((-1, 1)) * step * rng.uniform(0.5, 1.0)


def _probe(rng: random.Random, metric: Any) -> MetricValue:
  """A measurement of `metric` near one of the values its status turns
  on, with every field scoring can read."""
  anchors = [
      a for a in (metric.warn, metric.fail, metric.target, 0.0, 1.0)
      if a is not None
  ]
  value = _near(rng, rng.choice(anchors))
  width = rng.choice((0.0, 1e-12, 1e-4, 0.02, 0.5))
  detail: dict[str, Any] = {}
  if metric.id == "table.pmse_ratio":
    detail["ceiling"] = _near(rng, rng.choice((metric.fail, 50.0)))
  if metric.level == "relationship":
    detail["enforced"] = rng.random() < 0.8
  return MetricValue(
      metric.id,
      "users",
      value,
      column="age" if metric.level in ("field", "column", "pair") else None,
      column_2="city" if metric.level == "pair" else None,
      edge="users(a) -> users(b)" if metric.level == "relationship" else None,
      source_value=_near(rng, rng.choice(anchors)),
      synthetic_value=_near(rng, value),
      baseline_value=rng.choice((None, _near(rng, 0.0))),
      noise_floor=rng.choice((None, 0.0, 1e-12, 0.01, 0.2)),
      ci_low=rng.choice((None, _near(rng, value - width))),
      ci_high=rng.choice((None, _near(rng, value + width))),
      column_kind=rng.choice((None, "text", "numeric", "categorical")),
      detail=detail)


def test_any_persisted_row_rescores_to_itself():
  """The property behind I1, over every catalogue metric: a row as it is
  persisted (through JSON), scored again from its own fields, gives the
  same status, score, value and detail — including values a rounding
  step away from a threshold and bounds that noise reverses."""
  rng = random.Random(26)
  context = RowContext("e1", EVALUATED_AT, {}, {})
  seen: dict[str, int] = {}
  for metric in _CATALOGUE.metrics:
    for _ in range(60):
      mv = _probe(rng, metric)
      row = json.loads(json.dumps(metric_row(mv, context), allow_nan=False))
      again = rescored(row)
      assert (again["status"], again["score"], again["value"],
              again["detail"]) == (row["status"], row["score"], row["value"],
                                   row["detail"]), (mv, row)
      seen[row["status"]] = seen.get(row["status"], 0) + 1
  assert all(
      seen.get(status, 0) > 50
      for status in ("pass", "warn", "fail", "info", "not_evaluated")), seen


def test_an_infinite_value_rescores_through_its_nonfinite_note():
  """JSON has no infinity: an infinite gated value or bound persists NULL
  with `detail.nonfinite`, which is what a re-scoring reads it back
  from."""
  context = RowContext("e1", EVALUATED_AT, {}, {})
  lift = MetricValue("row.memorization_lift", "users", 7.0, ci_low=float("inf"))
  psi = MetricValue("column.psi", "users", float("inf"), column="age")
  for mv, field in ((lift, "ci_low"), (psi, "value")):
    row = json.loads(json.dumps(metric_row(mv, context), allow_nan=False))
    assert row["status"] == "fail" and row[field] is None
    assert row["detail"]["nonfinite"] == "+inf" and row["score"] == 0.0
    again = rescored(row)
    assert (again["status"], again["score"], again["value"],
            again["detail"]) == ("fail", 0.0, row["value"], row["detail"])


def test_one_orphan_in_3e10_persists_and_fails():
  """No absolute floor for an integrity metric or a count-derived rate:
  a single orphan in 3e10 child rows stays > 0 and FAILs (I1)."""
  context = RowContext("e1", EVALUATED_AT, {}, {})
  orphans = MetricValue(
      "relationship.orphan_rate",
      "orders",
      1 / 3e10,
      edge="e",
      detail={
          "enforced": True,
          "orphans": 1
      })
  row = metric_row(orphans, context)
  assert row["value"] == 3.33333333e-11 and row["status"] == "fail"
  assert row["score"] == 0.0
  again = rescored(row)
  assert (again["status"], again["score"]) == ("fail", 0.0)
  # the other zero-tolerance keys, a count-derived rate of another family
  # and an integrity value that is not a share: none is floored to 0
  for metric_id in ("table.pk_duplicate_rate", "table.identity_duplicate_rate"):
    row = metric_row(MetricValue(metric_id, "orders", 2 / 3e10), context)
    assert row["value"] == 6.66666667e-11 and row["status"] == "fail"
  for metric_id in ("row.exact_match_rate", "relationship.orphan_rate_source",
                    "column.source_stats_drift"):
    mv = MetricValue(metric_id, "orders", 1 / 3e10, column="a", edge="e")
    assert metric_row(mv, context)["value"] == 3.33333333e-11, metric_id
  # the rate's other fields and its detail are count-derived too
  rate = MetricValue(
      "row.exact_match_rate",
      "orders",
      1 / 3e10,
      ci_low=1 / 7e11,
      synthetic_value=1 / 3e10,
      detail={"rate_reference": 1 / 3e10})
  row = metric_row(rate, context)
  assert row["ci_low"] > 0 and row["synthetic_value"] == 3.33333333e-11
  assert row["detail"]["rate_reference"] == 3.33333333e-11
  # a distance keeps the 1e-10 floor: merge noise around 0 is 0
  noise = MetricValue(
      "pair.pearson_delta", "orders", 3e-17, column="a", column_2="b")
  assert metric_row(noise, context)["value"] == 0.0


def test_stable_metric_rounds_every_gating_field():
  mv = MetricValue(
      "table.pmse_ratio",
      "users",
      2.99999999999,
      source_value=0.1234567891234,
      noise_floor=0.0333333333333,
      ci_low=1.00000000004,
      ci_high=5.1234567891,
      baseline_value=1.1111111111111,
      detail={
          "ceiling": 9.99999999999,
          "k": 7
      })
  out = stable_metric(mv)
  assert out.value == 3.0 and out.detail["ceiling"] == 10.0
  assert out.detail["k"] == 7 and out.ci_low == 1.0
  assert out.source_value == 0.123456789 and out.noise_floor == 0.0333333333
  assert out.ci_high == 5.12345679 and out.baseline_value == 1.11111111
  assert stable_metric(out) == out  # a persisted value is a fixed point
  # numpy values (a producer's arrays) are rounded like Python floats
  numpy_detail = dataclasses.replace(
      mv,
      value=np.float64(2.99999999999),
      detail={
          "ceiling": np.float32(10.0),
          "curve": np.array([0.1234567891234, 1e-17])
      })
  out = stable_metric(numpy_detail)
  assert out.value == 3.0 and not isinstance(out.value, np.floating)
  assert out.detail == {"ceiling": 10.0, "curve": [0.123456789, 0.0]}
  empty = MetricValue.not_evaluated("table.pmse_ratio", "users", "no rows")
  assert stable_metric(empty) == empty


def test_profile_rounding_keeps_edges_quantiles_and_counts():
  """Only moment-derived payload values are rounded: edges, quantiles and
  counts are deterministic and persist as computed, so edges 3 s apart at
  an epoch-seconds offset stay strictly increasing (I2)."""
  edges = [1_758_000_400.0 + 3 * k for k in range(6)]
  histogram = ProfileValue(
      table="users",
      profile_kind="histogram",
      side="source",
      column="created_at",
      payload={
          "edges": edges,
          "counts": [1, 2, 3, 4, 5, 6, 7],
          "below_mass": 0.1,
          "unit": "epoch_seconds"
      },
      edges_digest="d" * 32)
  assert stable_profile(histogram) is histogram
  quantiles = dataclasses.replace(
      histogram,
      profile_kind="quantiles",
      payload={
          "probs": [0.1, 0.5],
          "values": [1_758_000_401.123456, 1_758_000_402.654321]
      })
  assert stable_profile(quantiles) is quantiles
  moments = dataclasses.replace(
      histogram,
      profile_kind="moments",
      payload={
          "n": 30,
          "mean": 1_758_000_406.0000001,
          "std": 3.14159265358979,
          "skewness": 1e-17,
          "kurtosis_excess": -1.2000000000001,
          "min": 1_758_000_400.5,
          "zeros": 0
      })
  out = stable_profile(moments).payload
  assert out["std"] == 3.14159265 and out["skewness"] == 0.0
  assert out["kurtosis_excess"] == -1.2 and out["min"] == 1_758_000_400.5
  assert out["n"] == 30 and out["zeros"] == 0
  assert out["mean"] == 1_758_000_410.0  # a moment: 9 significant digits
  assert moments.payload["mean"] == 1_758_000_406.0000001  # not mutated
  corr = dataclasses.replace(
      histogram,
      profile_kind="corr_matrix",
      payload={
          "columns": ["a", "b"],
          "values": [[1.0, 0.12345678912], [0.12345678912, 1.0]],
          "n": 9
      })
  assert stable_profile(corr).payload["values"][0][1] == 0.123456789
  # every other kind persists as computed: narrow edges at a large offset
  # (ids near 1e12, distances near 0) and count ratios
  for kind, payload in (
      ("histogram", {
          "edges": [1e12 + k for k in range(4)],
          "counts": [1, 1, 1, 1, 1]
      }),
      ("dcr_hist", {
          "edges": [0.0, 1e-12, 2e-12],
          "counts": [3, 4],
          "n": 7
      }),
      ("topk", {
          "items": [{
              "label": "a",
              "count": 1,
              "share": 1 / 3e10
          }]
      }),
  ):
    kept = dataclasses.replace(histogram, profile_kind=kind, payload=payload)
    assert stable_profile(kept) is kept


def test_roc_payload_restates_its_metric_row_rounded_alike():
  auc, lo, hi = 0.71234567891234, 0.61234567891234, 0.81234567891234
  points = [[0.1234567891234, 0.9876543219876]]
  context = RowContext("e1", EVALUATED_AT,
                       {"orders": ("p.d.orders", "p.s.orders")}, {})
  metric = metric_row(
      MetricValue("table.detection_auc", "orders", auc, ci_low=lo, ci_high=hi),
      context)
  roc = ProfileValue(
      "orders",
      "roc_curve",
      "both",
      payload={
          "points": points,
          "auc": auc,
          "ci_low": lo,
          "ci_high": hi
      },
      n=10)
  payload = stable_profile(roc).payload
  assert (payload["auc"], payload["ci_low"],
          payload["ci_high"]) == (metric["value"], metric["ci_low"],
                                  metric["ci_high"])
  assert payload["auc"] != auc
  assert payload["points"] == points


def test_metric_row_passes_the_edge_enforcement():
  context = RowContext("e1", EVALUATED_AT,
                       {"orders": ("p.d.orders", "p.s.orders")}, {})
  orphans = MetricValue(
      "relationship.orphan_rate",
      "orders",
      0.02,
      edge="orders(user_id) -> users(id)",
      detail={"enforced": False})
  row = metric_row(orphans, context)
  assert row["status"] == "info"
  assert (row["landing_table"], row["source_table"]) == ("p.d.orders",
                                                         "p.s.orders")
  enforced = dataclasses.replace(orphans, detail={"enforced": True})
  assert metric_row(enforced, context)["status"] == "fail"


def test_guard_rewrites_a_failed_tables_rows_keeping_their_scope():
  mv = MetricValue(
      "relationship.orphan_rate",
      "orders",
      0.5,
      edge="e",
      ci_low=0.1,
      detail={
          "enforced": False,
          "orphans": 3
      })
  assert guarded(mv, {"users": "x"}) is mv
  rewritten = guarded(mv, {"orders": "encode failed"})
  assert rewritten.value is None and rewritten.ci_low is None
  assert rewritten.edge == "e"
  assert dict(rewritten.detail) == {
      "reason": "encode failed",
      "enforced": False
  }
  noted = dataclasses.replace(mv, detail={"rows_withheld": "why", "n": 3})
  assert dict(guarded(noted, {
      "orders": "encode failed"
  }).detail) == {
      "reason": "encode failed",
      "rows_withheld": "why"
  }


def test_failed_table_rows_survive_a_malformed_plan(users):
  """A plan malformed enough to fail the driver checks (a pair index
  past the columns, paired with a numeric column so the pair metrics
  reach for it) still yields rows: the table-level ones (M10)."""
  age = [f["name"] for f in USERS_FIELDS].index("age")
  table = dataclasses.replace(_users_plan(*users), pairs=((age, 99),))
  rows = failed_table_metrics(table, "malformed plan")
  assert rows and all(r.column is None for r in rows)
  assert all(r.value is None for r in rows)
  assert all(r.detail["reason"] == "malformed plan" for r in rows)
  assert {r.metric_id for r in rows
         } >= {"table.detection_auc", "row.memorization_lift"}
  levels = {_CATALOGUE.get(r.metric_id).level for r in rows}
  assert levels == {"row", "table"}
  # the rows say what is missing and why (nothing is dropped silently)
  for row in rows:
    assert "IndexError" in row.detail["rows_withheld"]
    assert "column and pair" in row.detail["rows_withheld"]
  whole = failed_table_metrics(_users_plan(*users), "malformed plan")
  assert {r.metric_id for r in rows
         } == {r.metric_id for r in whole if r.column is None}
  assert not any("rows_withheld" in r.detail for r in whole)


def test_failed_table_rows_cover_every_owned_id_by_kind(users):
  table = _users_plan(*users)
  rows = failed_table_metrics(table, "unreadable")
  assert rows and all(r.value is None for r in rows)
  assert all(r.detail["reason"] == "unreadable" for r in rows)
  ids = {r.metric_id for r in rows}
  assert "column.null_rate_delta" in ids and "table.detection_auc" in ids
  assert "row.memorization_lift" in ids
  assert not any(i.startswith("relationship.") for i in ids)
  assert "column.source_stats_drift" in ids
  assert "table.row_count_ratio" not in ids  # the plan's own row
  by_column = {(r.metric_id, r.column) for r in rows}
  assert ("column.ks", "age") in by_column
  assert ("column.ks", "email") not in by_column  # not a numeric kind
  pairs = [r for r in rows if r.metric_id == "pair.pearson_delta"]
  assert all(r.column and r.column_2 for r in pairs)
  kinds = {c.name: str(c.kind) for c in table.columns}
  for row in rows:
    metric = _CATALOGUE.get(row.metric_id)
    if row.column is not None:
      assert kinds[row.column] in metric.kinds


def test_row_count_ratio_prefers_valid_count(users):
  table = _users_plan(*users)
  row = row_count_metric(table)
  assert row.value == 1.0 and row.detail["expected_from"] == "valid_count"
  written = dataclasses.replace(
      table,
      scope=dataclasses.replace(
          table.scope, expected_rows=None, written_rows=1000))
  row = row_count_metric(written)
  assert row.value == len(users[1]) / 1000
  assert row.detail["expected_from"] == "written_rows"
  unknown = dataclasses.replace(
      table, scope=scope_of(table.landing_table, expected=None))
  assert row_count_metric(unknown).value is None
  uncounted = dataclasses.replace(table, rows_synthetic=None)
  assert "not counted" in row_count_metric(uncounted).detail["reason"]


# --------------------------------------------------------------------------
# roll-ups
# --------------------------------------------------------------------------
def _row(table: str,
         metric_id: str,
         score: float | None,
         *,
         column: str | None = None,
         column_2: str | None = None,
         status: str = "pass") -> dict[str, Any]:
  metric = _CATALOGUE.get(metric_id)
  return {
      "table_name": table,
      "level": metric.level,
      "family": metric.family,
      "metric_id": metric_id,
      "column_name": column,
      "column_name_2": column_2,
      "edge": None,
      "score": score,
      "status": status,
  }


def test_rollups_average_per_unit_and_ignore_row_order():
  rows = [
      _row("users", "column.ks", 1.0, column="age"),
      _row("users", "column.jsd", 0.0, column="age"),
      _row("users", "column.ks", 1.0, column="created_at"),
      _row("users", "pair.pearson_delta", 0.5, column="a", column_2="b"),
      _row("users", "pair.spearman_delta", 1.0, column="a", column_2="b"),
      _row("users", "pair.pearson_delta", 0.0, column="a", column_2="c"),
      _row("users", "table.pk_duplicate_rate", 0.0, status="fail"),
      _row("users", "table.fidelity_score", 0.1),  # an aggregate: ignored
  ]
  summary = summarize_rows(rows)
  assert summary.shapes["users"] == pytest.approx((0.5 + 1.0) / 2)
  assert summary.trends["users"] == pytest.approx((0.75 + 0.0) / 2)
  assert summary.counts["total"] == 7 and summary.counts["fail"] == 1
  shuffled = list(rows)
  random.Random(3).shuffle(shuffled)
  assert summarize_rows(shuffled) == summary
  out = aggregate_metrics(summary, {"users": "d" * 32})
  by_id = {(m.table, m.metric_id): m for m in out}
  assert by_id[("users",
                "table.column_shape_score")].value == pytest.approx(0.75)
  integrity = by_id[("users", "table.integrity_score")]
  assert integrity.value == 0.0 and integrity.detail["integrity_fail"] == 1
  assert by_id[(MODEL_KEY,
                "model.integrity_score")].detail["integrity_fail"] == 1
  privacy = by_id[("users", "table.privacy_score")]
  assert privacy.value is None and privacy.detail["reason"]
  assert all(is_aggregate(m.metric_id) for m in out)
  assert {m.metric_id for m in out} >= {
      "table.overall_score", "table.pair_trend_score", "model.overall_score"
  }


def test_rollup_rows_score_as_their_own_value():
  summary = summarize_rows([_row("users", "column.ks", 0.9, column="age")])
  context = RowContext("e1", EVALUATED_AT, {"users": ("a.b.users", None)}, {})
  rows = [metric_row(m, context) for m in aggregate_metrics(summary, {})]
  fidelity = next(r for r in rows if r["metric_id"] == "table.fidelity_score")
  assert fidelity["score"] == fidelity["value"] == 0.9
  model = next(r for r in rows if r["metric_id"] == "model.overall_score")
  assert model["table_name"] == MODEL_KEY and model["landing_table"] is None


# --------------------------------------------------------------------------
# source_stats_drift (R62)
# --------------------------------------------------------------------------
class _Query:
  """A fake `Bq.query` returning `rows` (or raising `error`); `calls`
  records every call."""

  def __init__(self,
               rows: Sequence[Mapping[str, Any]] = (),
               error: Exception | None = None):
    self.rows = list(rows)
    self.error = error
    self.calls: list[tuple[str, dict, dict]] = []

  def __call__(self, sql: str, params: Mapping[str, Any],
               **kwargs: Any) -> list[dict]:
    self.calls.append((sql, dict(params), kwargs))
    if self.error is not None:
      raise self.error
    return list(self.rows)


def test_stats_are_read_with_the_tables_digest_and_tier(users):
  plan = _plan(users)
  table = plan.tables[0]
  assert table.panel is not None
  rows = stats_rows(table, USERS_FIELDS, table.panel.r_rows)
  query = _Query(rows)
  stats = read_source_stats(plan, query)["users"]
  assert len(query.calls) == 1
  sql, params, kwargs = query.calls[0]
  assert sql == source_stats_sql(
      "demo-project.synthetic_rag.source_table_stats")
  assert params == {
      "table_fqn": table.source_table,
      "reference_digest": table.reference_digest,
      "tier": "sample"
  }
  assert kwargs["max_bytes"] == plan.budget.max_bytes_billed
  assert stats.reason is None and stats.tier == "sample"
  age = stats.columns["age"]
  assert len(age.deciles) == 9  # the ends (exact extremes) are never kept
  assert set(stats.columns) == {f["name"] for f in USERS_FIELDS}
  assert "`distinct`" in sql and "@reference_digest" in sql


def test_stats_read_the_latest_row_of_the_launch_tier(users):
  """An older row and another tier's row for the same (table, digest)
  are never read: the driver asks for the launch's tier, and the query —
  the SQL's clauses, which the fake answers row by row — keeps that tier
  (a row without one counts as `sample`) and the latest row per column
  (M12)."""
  plan = _plan(users)
  table = plan.tables[0]
  assert table.panel is not None
  sql = source_stats_sql(STATS_TABLE)
  assert "IFNULL(stats_tier, 'sample') = @tier" in sql
  assert "PARTITION BY `column` ORDER BY computed_at DESC) = 1" in sql
  query = FakeStatsQuery(plan)
  base = {
      row["column"]: row for row in query(
          source_stats_sql(STATS_TABLE), {
              "table_fqn": table.source_table,
              "reference_digest": table.reference_digest,
              "tier": "sample"
          })
  }
  query.add([
      {
          **base["age"], "null_fraction": 0.9,
          "computed_at": "2026-09-01T00:00:00Z"
      },
      {
          **base["gender"], "null_fraction": 0.5,
          "computed_at": "2026-09-29T02:00:00Z"
      },
      {
          **base["city"], "null_fraction": 0.7,
          "stats_tier": "exact",
          "computed_at": "2026-09-30T00:00:00Z"
      },
      {
          **base["country"], "null_fraction": 0.3,
          "stats_tier": None,
          "computed_at": "2026-09-29T03:00:00Z"
      },
  ])
  stats = read_source_stats(plan, query)["users"]
  assert stats.columns["age"].null_fraction == base["age"]["null_fraction"]
  assert stats.columns["gender"].null_fraction == 0.5
  assert stats.columns["city"].null_fraction == base["city"]["null_fraction"]
  assert stats.columns["country"].null_fraction == 0.3  # legacy: sample
  params = {**plan.launch.params, "source_stats": "exact"}
  exact = dataclasses.replace(
      plan, launch=dataclasses.replace(plan.launch, params=params))
  stats = read_source_stats(exact, query)["users"]
  assert set(stats.columns) == {"city"}  # the one exact-tier row
  assert stats.columns["city"].null_fraction == 0.7
  assert query.calls[-1][1]["tier"] == "exact"


@pytest.mark.parametrize(("params", "query", "words"), [
    ({
        "source_stats": "off"
    }, _Query(), "--source_stats=off"),
    ({}, _Query(), "no --source_stats tier"),
    ({
        "source_stats": "exact"
    }, _Query(), "no --source_stats_table"),
    (None, None, "no BigQuery reader"),
    (None, _Query(error=PermissionError("403 denied")), "could not be read"),
    (None, _Query(error=LookupError("404 not found")), "could not be read"),
    (None, _Query(error=BqApiError("400 invalid query", status=400)),
     "could not be read"),
    (None, _Query(), "no source_table_stats row"),
])
def test_missing_stats_have_a_reason(users, params, query, words):
  plan = _plan(users)
  if params is not None:
    launch = dataclasses.replace(plan.launch, params=params)
    plan = dataclasses.replace(plan, launch=launch)
  stats = read_source_stats(plan, query)["users"]
  assert stats.reason is not None and words in stats.reason
  assert not stats.columns


@pytest.mark.parametrize("error", [
    BqApiError("503 backend error", status=503),
    BqApiError("429 rate limit exceeded", status=429),
    BqApiError("409 conflict", status=409),
    BqApiError("the connection was reset"),
])
def test_a_transient_stats_read_error_is_raised(users, error):
  """The final review's M2: only a read BigQuery REFUSES degrades the
  drift check (`context.bq.is_refusal`, as pins and pools do). An error a
  retry may remove is raised — the run fails instead of publishing a
  drift check that is missing by accident."""
  with pytest.raises(BqApiError) as caught:
    read_source_stats(_plan(users), _Query(error=error))
  assert caught.value is error


def test_stats_need_a_reference_digest(users):
  plan = _plan(users)
  table = dataclasses.replace(plan.tables[0], reference_digest=None)
  stats = read_source_stats(
      dataclasses.replace(plan, tables=(table,)), _Query())["users"]
  assert "reference digest" in str(stats.reason)


def _source_profile(table, rows: Sequence[Mapping[str, Any]]) -> DenseProfile:
  spec = DenseSpec.from_table(table)
  encoder = BatchEncoder.from_table(table, "source", salt=SALT)
  profile = DenseProfile.empty(spec, "source")
  for start in range(0, len(rows), 211):
    batch = encoder.encode(rows[start:start + 211])
    profile = profile.merge(DenseProfile.from_batch(spec, batch))
  return profile


def _drift(users, stats: TableStats, *, rows=None) -> dict[str, MetricValue]:
  source, synthetic = users
  table = _users_plan(source, synthetic)
  profile = None if rows == [] else _source_profile(table, rows or source)
  out = drift_metrics(DriftSpec.from_table(table), profile, stats)
  return {str(mv.column): mv for mv in out}


def _stats(users, tier: str = "sample", **columns: ColumnStats) -> TableStats:
  source, synthetic = users
  table = _users_plan(source, synthetic)
  rows = stats_rows(table, USERS_FIELDS, source, stats_tier=tier)
  query = _Query(rows)
  plan = evaluation_plan([table], evaluation_id="ev_unit", label_key_uri=None)
  launch = dataclasses.replace(
      plan.launch, params={
          **plan.launch.params, "source_stats": tier
      })
  stats = read_source_stats(dataclasses.replace(plan, launch=launch),
                            query)["users"]
  return dataclasses.replace(stats, columns={**stats.columns, **columns})


def test_drift_of_the_same_source_is_zero(users):
  out = _drift(users, _stats(users))
  assert set(out) == {f["name"] for f in USERS_FIELDS}
  for mv in out.values():
    assert mv.value is not None and mv.value <= 1e-3, (mv.column, mv.detail)
  assert "decile_cdf_delta" in out["age"].detail
  assert out["age"].detail["tier"] == "sample"
  assert "distinct" in out["age"].detail  # not compared on the sample tier
  text = json.dumps([dict(mv.detail) for mv in out.values()])
  for row in users[0][:50]:
    assert str(row["street_address"]) not in text


def test_drift_catches_stale_nulls_and_deciles(users):
  base = _stats(users)
  age = base.columns["age"]
  shifted = ColumnStats(
      null_fraction=age.null_fraction,
      distinct=age.distinct,
      deciles=tuple(d + 20 for d in age.deciles))
  nulls = ColumnStats(null_fraction=0.4, distinct=None)
  out = _drift(
      users,
      dataclasses.replace(
          base, columns={
              **base.columns, "age": shifted,
              "gender": nulls
          }))
  metric = _CATALOGUE.get("column.source_stats_drift")
  assert out["age"].value >= metric.fail
  assert out["gender"].value == pytest.approx(0.4)
  context = RowContext("e1", EVALUATED_AT, {}, {})
  assert metric_row(out["age"], context)["status"] == "fail"
  assert metric_row(out["age"], context)["score"] is None  # R26


def test_drift_compares_distinct_on_the_exact_tier(users):
  base = _stats(users, tier="exact")
  city = base.columns["city"]
  source, synthetic = users
  planned = {
      c.name: c for c in _users_plan(source, synthetic).columns
  }["city"].source_distinct
  assert planned is not None
  off = dataclasses.replace(city, distinct=planned * 2)
  out = _drift(users,
               dataclasses.replace(base, columns={
                   **base.columns, "city": off
               }))
  assert out["city"].detail["distinct_rel_delta"] == pytest.approx(0.5)
  assert "distinct_rel_delta" in out["country"].detail


def test_drift_not_evaluated_with_reasons(users):
  none = _drift(users, TableStats(tier="off", reason="stats off"))
  assert all(mv.value is None and mv.detail["reason"] == "stats off"
             for mv in none.values())
  base = _stats(users)
  missing = dict(base.columns)
  del missing["age"]
  out = _drift(users, dataclasses.replace(base, columns=missing))
  assert "no source_table_stats row for column age" in out["age"].detail[
      "reason"]
  empty = _drift(users, base, rows=[])
  assert all(
      mv.detail["reason"] == "no source rows read" for mv in empty.values())


def test_cdf_interval_matches_the_empirical_cdf():
  """The drift's [F⁻(x), F(x)] against a brute-force count, on integers
  (every value an edge: exact) and between edges (interpolated)."""
  rng = np.random.default_rng(5)
  values = rng.integers(0, 40, 3000).astype(float)
  edges = np.unique(values)[::2]  # every other value is an edge
  right = np.array([np.sum(values <= edges[0])] + [
      np.sum((values > lo) & (values <= hi))
      for lo, hi in itertools.pairwise(edges)
  ] + [np.sum(values > edges[-1])])
  left = np.array([np.sum(values < edges[0])] + [
      np.sum((values >= lo) & (values < hi))
      for lo, hi in itertools.pairwise(edges)
  ] + [np.sum(values >= edges[-1])])
  n = values.size
  for x in edges:
    lo, hi = assemble._cdf_interval(edges, right, left, float(x))  # pylint: disable=protected-access  # the drift's own CDF
    assert lo == pytest.approx(np.sum(values < x) / n)
    assert hi == pytest.approx(np.sum(values <= x) / n)
  lo, hi = assemble._cdf_interval(edges, right, left, float(edges[3]) + 0.5)  # pylint: disable=protected-access  # between edges
  assert lo == hi
  assert np.sum(values <= edges[3]) / n <= lo <= np.sum(values < edges[4]) / n
