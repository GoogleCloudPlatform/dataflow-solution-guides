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
"""What the composed pipeline writes, as pure functions: the registry's
lifecycle rows, the run status, the metric rows the transforms do not own
and the roll-up rows.

The registry is append-only events (D7):

    driver ──► running_row(plan)            event RUNNING, status RUNNING
                  │  (DirectRunner/ClientLoadSinks: loaded before the
                  │   pipeline; Dataflow: written before it is launched)
    pipeline ─► metric / profile / flag sinks ──► load AND copy jobs done
                  │                      (their job-id PCollections, read
                  ▼                       as AsIter side inputs)
               final_event(...)          event FINAL, recorded after them
    driver ──► failed_row(plan, exc)     event FINAL, status FAILED, on any
                                         exception (then it re-raises)

    status                   when
    ───────────────────────  ─────────────────────────────────────────────
    SKIPPED                  no launch table can be evaluated (the plan's
                             `skip_reason`; `registry_seed`'s FINAL row)
    FAILED                   the driver caught an exception (`failed_row`:
                             no counts, no scores); or the pipeline ran
                             but could evaluate NONE of the launch tables
                             the plan meant it to (each failed on the
                             driver or on a worker): the FINAL row keeps
                             its counts — every row not_evaluated — and
                             names each table's reason in `warnings`
    PARTIAL                  a launch table was not evaluated (skipped at
                             planning, unreadable or failing in the
                             pipeline) while another was, a scope count
                             mismatch, or a reference panel absent or
                             unverified (its privacy block not evaluated)
    SUCCEEDED_WITH_WARNINGS  otherwise, with warnings (contaminated,
                             expired or unknown scopes, fallbacks, …)
    SUCCEEDED                otherwise

PARTIAL means a launch table or a whole metric block (the privacy panel)
was not evaluated for a plan/driver-level reason, or — with every table
evaluated — a scope `count_mismatch` (the table above lists both): the plan skipped the
table or has no verified panel for it, the driver could not set the
table up, or the pipeline's own encode step failed its rows (the whole
table then, by `guarded`). Transform-internal not_evaluated rows — a
table or metric that Membership, Privacy or Relational could not compute
and reported itself, as `not_evaluated` rows with the reason — show in
the `metrics_not_evaluated` count only and do not change the status.

A status describes the RUN, never the data: a FAIL metric leaves it
SUCCEEDED (the counts and scores carry the verdict). Every row records
`evaluation_params.label_key_mode` (operator | ephemeral, R64; never the
key or its URI) and `evaluation_params.planning_ddl` (the as_of_diff
start snapshots planning created, R57/R58; the plan's warnings name them
too). A FINAL row's `recorded_at` is its finish time, strictly after the
RUNNING row's (`evaluation_latest` orders by it).

Metric rows the transforms do not own:

    table.row_count_ratio       n_syn / n_expected per evaluated table
                                (the scope's Σ valid_count, else the
                                job's own committed output rows)
    column.source_stats_drift   the generator's source_table_stats row vs
                                the evaluator's dense SOURCE profile
                                (Ruling R62), below
    a failed table's rows       every catalogue id the transforms own and
                                the drift, per applicable column / pair,
                                not_evaluated with the failure (driver-side
                                failures; `guarded` rewrites a worker
                                failure's rows)
    roll-ups                    table.{family}_score, table.overall_score,
                                table.column_shape_score,
                                table.pair_trend_score and model.* from the
                                scored rows (`scoring.aggregate_scores`,
                                Ruling R11); `headline_counts` leaves them
                                out (R43)

Determinism, and what a row is graded on (Ruling R89). The same plan
and rows give byte-identical metric and profile rows. Integer counts
merge exactly, but Moments and co-moments merge in floating point in the
order the runner chooses (bundles, `BatchElements`' timing-sized batches,
hot-key fan-out), so a rerun differs in the last ulps: up to about 1e-11
relative where a small delta cancels two near-equal correlations
(measured on the acceptance run). What is written is therefore rounded —
and rounded FIRST, so that nothing is graded on a value the row does not
hold:

    MetricValue ─► stable_metric ─► checked_ci ─► scoring.to_metric_row
                   (round what is    (a reversed    (status and score read
                    persisted)        interval is    the rounded fields;
                                      no interval)   nothing rounds after)

    stable_metric rounds   value, source_value, synthetic_value,
                           baseline_value, noise_floor, ci_low, ci_high,
                           sample_rate and every float of `detail`
                           (`detail.ceiling` gates table.pmse_ratio)
    to                     `STABLE_DIGITS` = 9 significant digits
    and never finer than   `STABLE_FLOOR` = 1e-10 absolute (every
                           catalogue threshold is 1e-5 or more), so the
                           merge noise around an exact 0 is 0 — EXCEPT an
                           integrity-family metric and a count-derived
                           rate (value_kind share or count), which keep
                           no absolute floor: a count over a count is
                           exact and has no merge noise to hide, and one
                           orphan in 3e10 rows must persist as > 0 beside
                           its FAIL

A persisted row scored again from its own fields gives the same status
and score when its auxiliary fields are finite (a non-finite float is
stored as null AFTER scoring, so such a row re-scores from less than
it was scored on); the acceptance re-scores every row it writes; `score` is the
score function of the rounded fields, written as computed. The registry's
`overall_score`, `{family}_score` and `tables[].table_score` are rounded
as their roll-up rows are (`model.*`, `table.overall_score`), so the two
agree. A value whose merge noise straddles a rounding boundary can still
differ between reruns — and with it, now consistently, its status when
the boundary is a threshold — with a probability of about the noise over
the step (1e-6 relative noise … 1e-4 for the micro-epoch means), not the
ulp noise of every value.

Profile payloads (`stable_profile`) round only what the moments make:
`moments.{mean, std, skewness, kurtosis_excess}` and `corr_matrix.values`.
Histogram edges are the plan's grid, quantiles and bounds are read off
exact counts on that grid, counts and count ratios are exact: they
persist as computed, so edges stay strictly increasing at any offset
(epoch seconds, ids near 1e12) and still hash to the row's
`edges_digest`.

`checked_ci` treats a producer's reversed interval (ci_low > ci_high, a
producer defect) as no interval at all before scoring: the bounds are
dropped (reported in `detail.ci_reported`) and `detail.ci_invalid` says
why, so the D5 coverage test answers `noise_check: unavailable` instead
of reading the reversed bounds, and a metric that gates on its bound is
`not_evaluated` instead of gated on a bound that is wrong (Task 14
review). A relationship row passes its edge's `detail["enforced"]` to
`to_metric_row` (Ruling R42).

source_stats_drift (Ruling R62). The driver reads the generator's rows
for (source table, reference digest, tier) once, behind an injectable
query (`read_source_stats`), keeps only the NULL fraction, the distinct
count and the nine inner deciles per column (never a top value or an
extreme), and each column's row compares them with the evaluator's own
dense profile of the source:

    component      compared with                        when
    ─────────────  ───────────────────────────────────  ──────────────────
    null           |p_null(stats) - p_null(source)|     always
    deciles        max_i dist(i/10, [F⁻(d_i), F(d_i)])  numeric columns the
                   — where the evaluator's exact source  generator gave
                   CDF puts the generator's i-th decile  deciles and the
                                                         plan gave a grid
    distinct       |d_stats - d_src| / max(d_stats,     tier `exact` only
                   d_src), d_src the plan's source      (a `sample` tier's
                   distinct less the empty string       distinct is bounded
                                                        by its sample)

The value is the largest available component (the catalogue's formula).
With no stats (tier off, no stats table, no reference digest, no row, a
read BigQuery refused) every column is not_evaluated with the reason; a
read error a retry may remove fails the run instead (`read_source_stats`).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import functools
import json
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np

from sdfb_evaluation.beam import census, dense, membership, privacy
from sdfb_evaluation.beam.dense import DenseProfile, DenseSpec
from sdfb_evaluation.beam.label_key import label_key_mode
from sdfb_evaluation.catalogue import load_catalogue
from sdfb_evaluation.context.bq import (
    BqApiError,
    is_refusal,
    normalize_fqn,
    quote_fqn,
)
from sdfb_evaluation.schemas import load_schema
from sdfb_evaluation.scoring import (
    FAMILIES,
    INTEGRITY_FAIL,
    MODEL_KEY,
    OVERALL,
    Thresholds,
    aggregate_scores,
    headline_counts,
    is_aggregate,
    to_metric_row,
)
from sdfb_evaluation.types import ColumnKind, Method, MetricValue, ProfileValue

if TYPE_CHECKING:
  from sdfb_evaluation.context.plan import EvaluationPlan, TablePlan

__all__ = [
    "REGISTRY_STATUSES",
    "ColumnStats",
    "DriftSpec",
    "RegistryContext",
    "RowContext",
    "RowSummary",
    "StatsQuery",
    "TableStats",
    "aggregate_metrics",
    "checked_ci",
    "drift_metrics",
    "evaluation_status",
    "failed_row",
    "failed_table_metrics",
    "final_event",
    "final_row",
    "finish_time",
    "guarded",
    "metric_row",
    "plan_problems",
    "read_source_stats",
    "row_count_metric",
    "running_row",
    "skipped_row",
    "source_stats_sql",
    "stable_floats",
    "stable_metric",
    "stable_profile",
    "summarize_rows",
]

REGISTRY_STATUSES = ("SUCCEEDED", "SUCCEEDED_WITH_WARNINGS", "PARTIAL",
                     "SKIPPED", "FAILED")
_REGISTRY = "evaluation_data_history"
_REASON_CHARS = 1000
_DRIFT_ID = "column.source_stats_drift"
_ROW_COUNT_ID = "table.row_count_ratio"
_NO_ROWS = "no source rows read"
_DECILES = tuple(i / 10 for i in range(1, 10))
_STATS_ERRORS = (BqApiError, PermissionError, LookupError, ValueError,
                 TypeError, KeyError)
# The catalogue ids the transforms own (a failed table's rows cover them).
_TRANSFORM_IDS = frozenset(
    (*dense.OWNED_METRIC_IDS, *census.OWNED_METRIC_IDS,
     *membership.OWNED_METRIC_IDS, *privacy.OWNED_METRIC_IDS))
_ROW_LEVELS = ("row", "table")
STABLE_DIGITS = 9
STABLE_FLOOR = 1e-10
_FLOOR_PLACES = 10  # decimal places of STABLE_FLOOR
# Exact by construction, so never floored: the integrity family and the
# count-derived rates (a count over a count, or a count).
_EXACT_FAMILY = "integrity"
_EXACT_KINDS = frozenset({"share", "count"})
# What `stable_metric` rounds of a MetricValue besides its detail: every
# float `to_metric_row` grades on or writes.
_MEASURED = ("value", "source_value", "synthetic_value", "baseline_value",
             "noise_floor", "ci_low", "ci_high", "sample_rate")
# The payload values a profile computes from floating-point (co-)moments.
_MOMENT_FIELDS: Mapping[str, tuple[str, ...]] = {
    "moments": ("mean", "std", "skewness", "kurtosis_excess"),
    "corr_matrix": ("values",),
}
# The `roc_curve` payload restates the `table.detection_auc` metric's
# value and interval; they round exactly as that row does (the curve's
# points are edges and stay as computed).
_ROC_METRIC = "table.detection_auc"
_ROC_RESTATED = ("auc", "ci_low", "ci_high")
_MODEL_OVERALL, _TABLE_OVERALL = "model.overall_score", "table.overall_score"
# What listing a malformed table plan's columns and pairs can raise.
_PLAN_ERRORS = (IndexError, KeyError, TypeError, ValueError)
_WITHHELD_CHARS = 300
# The detail a failed table's rewritten row keeps: the edge's enforcement
# (R42) and a failed table's note on the rows it could not list.
_GUARD_KEEPS = ("enforced", "rows_withheld")
_CELL_LEVELS = ("field", "column")
_TABLE_ROLLUPS = tuple(f"table.{family}_score" for family in FAMILIES)
_MODEL_ROLLUPS = tuple(f"model.{family}_score" for family in FAMILIES)

StatsQuery = Callable[..., Sequence[Mapping[str, Any]]]


# --------------------------------------------------------------------------
# times
# --------------------------------------------------------------------------
def _rfc3339(moment: datetime) -> str:
  return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(text: str) -> datetime:
  moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
  return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def finish_time(evaluated_at: str, now: datetime | None = None) -> str:
  """A FINAL event's time: `now`, but strictly after `evaluated_at` (the
  RUNNING row's `recorded_at`) whatever the clocks say, so the latest
  event is always the FINAL one."""
  moment = now or datetime.now(UTC)
  floor = _parse(evaluated_at) + timedelta(microseconds=1)
  return _rfc3339(max(moment, floor))


# --------------------------------------------------------------------------
# registry rows
# --------------------------------------------------------------------------
def _ordered(row: Mapping[str, Any]) -> dict[str, Any]:
  names = [str(f["name"]) for f in load_schema(_REGISTRY)]
  if set(row) != set(names):
    raise AssertionError(f"registry row keys differ from the schema: missing "
                         f"{sorted(set(names) - set(row))}, extra "
                         f"{sorted(set(row) - set(names))}")
  return {name: row[name] for name in names}


def _seed(plan: EvaluationPlan) -> dict[str, Any]:
  """`plan.registry_seed()` with the evaluation params only the composed
  run knows: the label-key mode and the planning DDL."""
  row = plan.registry_seed()
  row["evaluation_params"] = {
      **row["evaluation_params"],
      "label_key_mode":
          label_key_mode(plan.label_key_uri),
      "planning_ddl": [statement.sql for statement in plan.planning_ddl],
  }
  return row


def running_row(plan: EvaluationPlan) -> dict[str, Any]:
  """The RUNNING event, in schema order (the driver writes it first)."""
  row = _seed(plan)
  row.update(
      event="RUNNING",
      status="RUNNING",
      status_reason=None,
      finished_at=None,
      recorded_at=plan.evaluated_at,
      metrics_total=None,
      metrics_pass=None,
      metrics_warn=None,
      metrics_fail=None,
      metrics_not_evaluated=None,
      metrics_info=None)
  return _ordered(row)


def skipped_row(plan: EvaluationPlan, *, finished_at: str) -> dict[str, Any]:
  """The FINAL SKIPPED event of a plan with nothing to evaluate
  (`registry_seed`'s own, stamped at `finished_at`)."""
  if plan.skip_reason is None:
    raise ValueError("the plan has tables to evaluate: it is not skipped")
  row = _seed(plan)
  row.update(recorded_at=finished_at, finished_at=finished_at)
  return _ordered(row)


def final_event(
    seed: Mapping[str, Any],
    *,
    scores: Mapping[str, Any],
    counts: Mapping[str, int],
    per_table: Mapping[str, Mapping[str, Any]],
    finished_at: str,
    status: str,
    status_reason: str | None,
    warnings: Sequence[str] = ()) -> dict[str, Any]:
  """The FINAL event from a RUNNING row (`running_row`): the model's
  family scores, the headline counts (R37), each table's `table_score`
  (`per_table[name]["table_score"]`) and any warnings the run added.
  Every score is rounded as its roll-up row is (`model.overall_score`,
  `model.{family}_score`, `table.overall_score`), so the registry and
  `evaluation_metrics` hold the same number.

  Raises:
    ValueError: a status that is not a FINAL status.
  """
  if status not in REGISTRY_STATUSES:
    raise ValueError(f"status {status!r}: expected one of "
                     f"{list(REGISTRY_STATUSES)}")
  row = dict(seed)
  row.update(
      recorded_at=finished_at,
      finished_at=finished_at,
      event="FINAL",
      status=status,
      status_reason=status_reason,
      overall_score=_stable_as(_MODEL_OVERALL, scores.get(OVERALL)),
      metrics_total=counts.get("total", 0),
      metrics_pass=counts.get("pass", 0),
      metrics_warn=counts.get("warn", 0),
      metrics_fail=counts.get("fail", 0),
      metrics_not_evaluated=counts.get("not_evaluated", 0),
      metrics_info=counts.get("info", 0))
  for family, metric_id in zip(FAMILIES, _MODEL_ROLLUPS, strict=True):
    row[f"{family}_score"] = _stable_as(metric_id, scores.get(family))
  row["tables"] = [{
      **entry, "table_score":
          _stable_as(
              _TABLE_OVERALL,
              per_table.get(str(entry["name"]), {}).get("table_score",
                                                        entry["table_score"]))
  } for entry in seed["tables"]]
  row["warnings"] = list(dict.fromkeys([*seed["warnings"], *warnings]))
  return _ordered(row)


def final_row(
    plan: EvaluationPlan,
    scores: Mapping[str, Any],
    counts: Mapping[str, int],
    per_table: Mapping[str, Mapping[str, Any]],
    *,
    finished_at: str,
    status: str,
    status_reason: str | None,
    warnings: Sequence[str] = ()) -> dict[str, Any]:
  """The FINAL event of `plan` (`final_event` over `running_row(plan)`)."""
  return final_event(
      running_row(plan),
      scores=scores,
      counts=counts,
      per_table=per_table,
      finished_at=finished_at,
      status=status,
      status_reason=status_reason,
      warnings=warnings)


def failed_row(plan: EvaluationPlan,
               exc: BaseException,
               *,
               now: datetime | None = None) -> dict[str, Any]:
  """The FINAL FAILED event the driver appends when anything raised: the
  error class and message as the reason (bounded; this package's messages
  name tables, columns and types, never values or keys), no counts and no
  scores. An error carrying `planning_ddl` (R58) names what planning had
  created in the warnings."""
  row = running_row(plan)
  finished = finish_time(plan.evaluated_at, now)
  created = [s.sql for s in getattr(exc, "planning_ddl", ()) or ()]
  row.update(
      recorded_at=finished,
      finished_at=finished,
      event="FINAL",
      status="FAILED",
      status_reason=f"{type(exc).__name__}: {exc}"[:_REASON_CHARS])
  if created:
    row["warnings"] = [
        *row["warnings"],
        *(f"planning created {sql}" for sql in created),
    ]
  return _ordered(row)


# --------------------------------------------------------------------------
# the run status
# --------------------------------------------------------------------------
def plan_problems(plan: EvaluationPlan) -> list[str]:
  """What the plan already knows makes the run PARTIAL (module docstring),
  one line per launch table."""
  problems = []
  for table in plan.tables:
    if table.role == "external":
      continue
    if table.skip_reason is not None:
      problems.append(f"{table.name}: not evaluated — {table.skip_reason}")
    elif table.scope.status == "count_mismatch":
      problems.append(f"{table.name}: scope count_mismatch — "
                      f"{table.scope.reason}")
    elif table.panel is None:
      problems.append(f"{table.name}: no reference panel — the "
                      "reference-based privacy metrics are not evaluated")
    elif not table.panel.verified:
      why = table.panel.reason or "digest mismatch"
      problems.append(f"{table.name}: reference not verified — {why}")
  return problems


def evaluation_status(problems: Sequence[str],
                      warnings: Sequence[str],
                      *,
                      none_evaluated: bool = False) -> tuple[str, str | None]:
  """(status, status_reason) of a run that finished (module docstring).
  `none_evaluated`: the pipeline could evaluate none of the launch tables
  the plan meant it to — the run FAILED, `problems` saying why."""
  if none_evaluated:
    reason = "no launch table could be evaluated: " + "; ".join(problems)
    return "FAILED", reason[:_REASON_CHARS]
  if problems:
    return "PARTIAL", "; ".join(problems)[:_REASON_CHARS]
  if warnings:
    return ("SUCCEEDED_WITH_WARNINGS",
            f"{len(warnings)} warning(s): see warnings")
  return "SUCCEEDED", None


# --------------------------------------------------------------------------
# metric rows
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RowContext:
  """What turns a `MetricValue` into an `evaluation_metrics` row (slim:
  pickled into the pipeline): the run's ids and each table's landing and
  source table. `thresholds` are the run's overrides of the catalogue's
  (`scoring.to_metric_row`, Ruling R93-6), None for none."""
  evaluation_id: str
  evaluated_at: str
  tables: Mapping[str, tuple[str | None, str | None]]
  digests: Mapping[str, str]
  thresholds: Thresholds | None = None

  @classmethod
  def from_plan(cls,
                plan: EvaluationPlan,
                *,
                thresholds: Thresholds | None = None) -> RowContext:
    return cls(
        evaluation_id=plan.evaluation_id,
        evaluated_at=plan.evaluated_at,
        tables={t.name: (t.landing_table, t.source_table) for t in plan.tables},
        digests={t.name: t.encoding_plan_digest for t in plan.tables},
        thresholds=thresholds)


def _stable(x: float, *, floor: bool = True) -> float:
  """`x` to `STABLE_DIGITS` significant digits and — with `floor` —
  never finer than `STABLE_FLOOR` (module docstring, Determinism)."""
  if not math.isfinite(x):
    return x
  if x == 0:
    return 0.0  # -0.0 too: a sign of noise must not reach the row
  places = STABLE_DIGITS - 1 - math.floor(math.log10(abs(x)))
  value = float(round(x, min(places, _FLOOR_PLACES) if floor else places))
  return value if value != 0 else 0.0


def stable_floats(obj: Any, *, floor: bool = True) -> Any:
  """`obj` with every float `_stable`, recursively (dicts, lists and
  tuples; numpy floats and arrays as Python values). `floor=False` keeps
  no absolute floor (an exact, count-derived value)."""
  if isinstance(obj, (float, np.floating)):
    return _stable(float(obj), floor=floor)
  if isinstance(obj, np.ndarray):
    return stable_floats(obj.tolist(), floor=floor)
  if isinstance(obj, Mapping):
    return {key: stable_floats(value, floor=floor) for key, value in obj.items()}
  if isinstance(obj, (list, tuple)):
    return [stable_floats(item, floor=floor) for item in obj]
  return obj


@functools.cache
def _exact_ids() -> frozenset[str]:
  """The catalogue ids whose values are exact by construction (the
  integrity family, the count-derived rates): rounded with no floor."""
  return frozenset(
      metric.id
      for metric in load_catalogue().metrics
      if metric.family == _EXACT_FAMILY or metric.value_kind in _EXACT_KINDS)


def _stable_as(metric_id: str, value: Any) -> float | None:
  """`value` as a `metric_id` row persists it (None stays None)."""
  if value is None:
    return None
  return _stable(float(value), floor=metric_id not in _exact_ids())


def stable_metric(mv: MetricValue) -> MetricValue:
  """`mv` holding exactly what its row persists: every measured float
  and every float of its detail rounded (module docstring) BEFORE it is
  scored, so status and score are graded on the persisted values. An
  integrity-family metric and a count-derived rate keep no absolute
  floor: a nonzero one never rounds to 0."""
  floor = mv.metric_id not in _exact_ids()
  rounded: dict[str, Any] = {
      name: _stable(float(value), floor=floor)
      for name in _MEASURED
      if (value := getattr(mv, name)) is not None
  }
  rounded["detail"] = stable_floats(mv.detail, floor=floor)
  return dataclasses.replace(mv, **rounded)


def stable_profile(pv: ProfileValue) -> ProfileValue:
  """`pv` with its moment-derived payload values rounded
  (`_MOMENT_FIELDS`: the only ones merge order can move) and the
  `roc_curve` payload's restated `auc`, `ci_low`, `ci_high`, rounded as
  the `table.detection_auc` row is. Everything
  else — edges, quantiles, bounds, counts, count ratios — persists as
  computed, so edges stay strictly increasing and match `edges_digest`."""
  rounded = {
      name: stable_floats(pv.payload[name])
      for name in _MOMENT_FIELDS.get(pv.profile_kind, ())
      if name in pv.payload
  }
  if pv.profile_kind == "roc_curve":
    rounded.update({
        name: _stable_as(_ROC_METRIC, pv.payload[name])
        for name in _ROC_RESTATED
        if name in pv.payload
    })
  if not rounded:
    return pv
  return dataclasses.replace(pv, payload={**pv.payload, **rounded})


def metric_row(mv: MetricValue, context: RowContext) -> dict[str, Any]:
  """The `evaluation_metrics` row of `mv`: rounded to what is persisted
  (`stable_metric`), a reversed interval dropped (`checked_ci`), THEN
  scored by `scoring.to_metric_row` with the run's ids and the table's
  landing and source — so the row's status and score are the ones its own
  fields give. A relationship row passes its edge's `enforced` (R42)."""
  landing, source = context.tables.get(mv.table, (None, None))
  persisted = checked_ci(stable_metric(mv))
  enforced = persisted.detail.get("enforced", True)
  return to_metric_row(
      persisted,
      evaluation_id=context.evaluation_id,
      evaluated_at=context.evaluated_at,
      landing_table=landing,
      source_table=source,
      enforced=enforced if isinstance(enforced, bool) else True,
      thresholds=context.thresholds)


def checked_ci(mv: MetricValue) -> MetricValue:
  """`mv`, or — its interval reversed (both bounds finite, ci_low >
  ci_high: a producer defect) — `mv` with no interval: the bounds are
  dropped, reported in `detail.ci_reported`, and `detail.ci_invalid`
  says why. Scoring then treats the row as one without an interval (the
  noise check is unavailable; a metric gating on its bound is
  not_evaluated) instead of reading bounds that are wrong."""
  low, high = mv.ci_low, mv.ci_high
  if (low is None or high is None or not math.isfinite(low) or
      not math.isfinite(high) or low <= high):
    return mv
  return dataclasses.replace(
      mv,
      ci_low=None,
      ci_high=None,
      detail={
          **mv.detail,
          "ci_invalid": ("reversed interval (ci_low > ci_high): a producer "
                         "defect; scored as a row without an interval"),
          "ci_reported": [low, high],
      })


def _not_evaluated(mv: MetricValue, reason: str) -> MetricValue:
  detail: dict[str, Any] = {"reason": reason}
  detail.update((k, mv.detail[k]) for k in _GUARD_KEEPS if k in mv.detail)
  return MetricValue(
      metric_id=mv.metric_id,
      table=mv.table,
      value=None,
      column=mv.column,
      column_2=mv.column_2,
      edge=mv.edge,
      column_kind=mv.column_kind,
      encoding_plan_digest=mv.encoding_plan_digest,
      detail=detail)


def guarded(mv: MetricValue, failures: Mapping[str, str]) -> MetricValue:
  """`mv`, or — its table failed in the pipeline — the same metric and
  scope `not_evaluated` with the failure: rows computed from part of a
  table's batches are never published. The rewritten row keeps its
  edge's `enforced` and a `rows_withheld` note (`_GUARD_KEEPS`)."""
  reason = failures.get(mv.table)
  if reason is None:
    return mv
  return _not_evaluated(mv, reason)


def _failed_rows(table: TablePlan, reason: str,
                 withheld: str | None) -> list[MetricValue]:
  """`failed_table_metrics`' rows; with `withheld` only the row- and
  table-level ones, each carrying it in `detail.rows_withheld`."""
  out: list[MetricValue] = []
  columns = table.columns
  for metric in load_catalogue().metrics:
    if metric.id not in _TRANSFORM_IDS and metric.id != _DRIFT_ID:
      continue
    scope: dict[str, Any] = {"encoding_plan_digest": table.encoding_plan_digest}
    if metric.level in _ROW_LEVELS:
      row = MetricValue.not_evaluated(metric.id, table.name, reason, **scope)
      if withheld is not None:
        row = dataclasses.replace(
            row, detail={
                **row.detail, "rows_withheld": withheld
            })
      out.append(row)
    elif withheld is not None:
      continue
    elif metric.level in _CELL_LEVELS:
      out.extend(
          MetricValue.not_evaluated(
              metric.id,
              table.name,
              reason,
              column=c.name,
              column_kind=str(c.kind),
              **scope) for c in columns if str(c.kind) in metric.kinds)
    elif metric.level == "pair":
      out.extend(
          MetricValue.not_evaluated(
              metric.id,
              table.name,
              reason,
              column=columns[i].name,
              column_2=columns[j].name,
              **scope)
          for i, j in table.pairs
          if str(columns[i].kind) in metric.kinds and
          str(columns[j].kind) in metric.kinds)
  return out


def failed_table_metrics(table: TablePlan, reason: str) -> list[MetricValue]:
  """A table that failed on the driver (unreadable, unplannable): every
  catalogue id the transforms own and `column.source_stats_drift`, per
  applicable column (by kind) or planned pair, or once for a row/table
  id, `not_evaluated` with `reason`. Its edges' rows come from the
  relational pass and its row-count row from the plan, like any
  table's.

  Never raises on a plan too malformed to list its columns or pairs (a
  pair index past the columns — the kind of plan that failed the table
  in the first place): the row- and table-level rows are returned alone,
  each saying in `detail.rows_withheld` that the column and pair rows
  could not be listed and why."""
  try:
    return _failed_rows(table, reason, None)
  except _PLAN_ERRORS as exc:
    withheld = (
        "the column and pair rows of this table could not be listed "
        f"from its plan ({type(exc).__name__}: {exc})")[:_WITHHELD_CHARS]
    return _failed_rows(table, reason, withheld)


def row_count_metric(table: TablePlan) -> MetricValue:
  """`table.row_count_ratio`: the rows the scope holds over the rows the
  run promised (Σ valid_count, else the job's own committed output rows)."""
  n = table.rows_synthetic
  expected, basis = table.scope.expected_rows, "valid_count"
  if not expected:
    expected, basis = table.scope.written_rows, "written_rows"
  digest = table.encoding_plan_digest
  if n is None:
    return MetricValue.not_evaluated(
        _ROW_COUNT_ID,
        table.name,
        "the synthetic side was not counted",
        encoding_plan_digest=digest)
  if not expected:
    return MetricValue.not_evaluated(
        _ROW_COUNT_ID,
        table.name,
        "no expected row count: validation_runs gave no valid_count and the "
        "job's committed output rows are unknown",
        n_synthetic=n,
        encoding_plan_digest=digest)
  return MetricValue(
      metric_id=_ROW_COUNT_ID,
      table=table.name,
      value=n / expected,
      n_synthetic=n,
      encoding_plan_digest=digest,
      detail={
          "rows_synthetic": n,
          "rows_expected": expected,
          "expected_from": basis
      })


# --------------------------------------------------------------------------
# roll-ups
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RowSummary:
  """What the scored rows add up to: `scoring.aggregate_scores`, the
  headline counts and the SDMetrics-style column-shape / pair-trend
  means per table."""
  scores: Mapping[str, Mapping[str, Any]]
  counts: Mapping[str, int]
  shapes: Mapping[str, float | None]
  trends: Mapping[str, float | None]


# the fields of a row the roll-ups read (a CombineFn holds only these)
SUMMARY_FIELDS = ("table_name", "level", "family", "metric_id", "column_name",
                  "column_name_2", "edge", "score", "status")


def _fmean(values: Iterable[float]) -> float | None:
  present = list(values)
  return math.fsum(present) / len(present) if present else None


def _unit_mean(units: Mapping[Any, list[float]]) -> float | None:
  return _fmean(m for m in (_fmean(u) for u in units.values()) if m is not None)


def summarize_rows(rows: Iterable[Mapping[str, Any]]) -> RowSummary:
  """The roll-ups of `to_metric_row` rows (aggregate ids ignored). Every
  mean is an exact `fsum`, so the result never depends on row order."""
  measured = [row for row in rows if not is_aggregate(row["metric_id"])]
  shapes: dict[str, dict[str | None,
                         list[float]]] = defaultdict(lambda: defaultdict(list))
  trends: dict[str, dict[tuple[Any, Any],
                         list[float]]] = defaultdict(lambda: defaultdict(list))
  for row in measured:
    score = row.get("score")
    if score is None or not math.isfinite(score):
      continue
    if row["level"] in _CELL_LEVELS and row["family"] == "fidelity":
      shapes[row["table_name"]][row.get("column_name")].append(float(score))
    elif row["level"] == "pair":
      key = (row.get("column_name"), row.get("column_name_2"))
      trends[row["table_name"]][key].append(float(score))
  scores = aggregate_scores(measured)
  tables = [name for name in scores if name != MODEL_KEY]
  return RowSummary(
      scores=scores,
      counts=headline_counts(measured),
      shapes={name: _unit_mean(shapes.get(name, {})) for name in tables},
      trends={name: _unit_mean(trends.get(name, {})) for name in tables})


def _rollup(metric_id: str,
            table: str,
            value: float | None,
            digest: str | None,
            reason: str,
            detail: Mapping[str, Any] | None = None) -> MetricValue:
  if value is None:
    return MetricValue.not_evaluated(
        metric_id, table, reason, encoding_plan_digest=digest)
  return MetricValue(
      metric_id=metric_id,
      table=table,
      value=float(value),
      encoding_plan_digest=digest,
      detail=dict(detail or {}))


def aggregate_metrics(summary: RowSummary,
                      digests: Mapping[str, str]) -> list[MetricValue]:
  """The roll-up rows (`table.*_score`, `model.*`) of a run: a value, or
  `not_evaluated` when no scored row feeds it. A family score's row
  carries the integrity FAIL badge count (R39) on `*.integrity_score`."""
  out: list[MetricValue] = []
  for table, scores in summary.scores.items():
    model = table == MODEL_KEY
    digest = None if model else digests.get(table)
    ids = _MODEL_ROLLUPS if model else _TABLE_ROLLUPS
    for family, metric_id in zip(FAMILIES, ids, strict=True):
      badge = ({
          INTEGRITY_FAIL: scores[INTEGRITY_FAIL]
      } if family == "integrity" else None)
      out.append(
          _rollup(metric_id, table, scores[family], digest,
                  f"no scored {family} metric", badge))
    overall = _MODEL_OVERALL if model else _TABLE_OVERALL
    out.append(
        _rollup(overall, table, scores[OVERALL], digest,
                "no family score to average"))
    if not model:
      out.append(
          _rollup("table.column_shape_score", table, summary.shapes[table],
                  digest, "no scored column-level fidelity metric"))
      out.append(
          _rollup("table.pair_trend_score", table, summary.trends[table],
                  digest, "no scored pair metric (no planned pair)"))
  return out


@dataclass(frozen=True)
class RegistryContext:
  """What the pipeline's FINAL step needs (slim, pickled): the RUNNING
  row it completes, the PARTIAL conditions and warnings the plan knows,
  and the launch tables the plan means the pipeline to evaluate."""
  seed: Mapping[str, Any]
  problems: tuple[str, ...]
  warnings: tuple[str, ...]
  evaluated: tuple[str, ...] = ()

  @classmethod
  def from_plan(cls, plan: EvaluationPlan) -> RegistryContext:
    return cls(
        seed=running_row(plan),
        problems=tuple(plan_problems(plan)),
        warnings=tuple(plan.warnings),
        evaluated=tuple(t.name for t in plan.tables if t.evaluated))

  def final(self, summary: RowSummary, failures: Mapping[str, str], *,
            finished_at: str) -> dict[str, Any]:
    """The FINAL row once every metric write is committed; `failures`
    are the tables the pipeline could not evaluate. When they are every
    table it was meant to evaluate, the run FAILED (module docstring):
    the reasons lead `status_reason` and are in `warnings`."""
    failed = [
        f"{table}: not evaluated — {reason}"
        for table, reason in sorted(failures.items())
    ]
    none_evaluated = bool(self.evaluated) and all(
        name in failures for name in self.evaluated)
    problems = [*self.problems, *failed]
    if none_evaluated:  # the failures lead the reason
      problems = [*failed, *self.problems]
    status, reason = evaluation_status(
        problems, [*self.warnings, *failed], none_evaluated=none_evaluated)
    per_table = {
        table: {
            "table_score": scores.get(OVERALL)
        } for table, scores in summary.scores.items() if table != MODEL_KEY
    }
    return final_event(
        self.seed,
        scores=summary.scores.get(MODEL_KEY, {}),
        counts=summary.counts,
        per_table=per_table,
        finished_at=finished_at,
        status=status,
        status_reason=reason,
        warnings=failed)


# --------------------------------------------------------------------------
# source_stats_drift (Ruling R62)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ColumnStats:
  """One column of the generator's source_table_stats row, slimmed to
  what the drift compares (no top value, no extreme)."""
  null_fraction: float | None
  distinct: int | None
  deciles: tuple[float, ...] = ()  # the nine inner deciles, p10 … p90


@dataclass(frozen=True)
class TableStats:
  """The generator's stats of one table, or why there are none."""
  tier: str | None
  columns: Mapping[str, ColumnStats] = field(default_factory=dict)
  reason: str | None = None


def source_stats_sql(stats_table: str) -> str:
  """The latest source_table_stats row per column for one (source table,
  reference digest, tier); a legacy row without a tier counts as
  `sample`.

  Raises:
    ValueError: `stats_table` is not a strict `project.dataset.table`.
  """
  return ("SELECT `column`, null_fraction, `distinct`, stats, stats_tier, "
          "sample_rows, profiler_version, computed_at "
          f"FROM {quote_fqn(normalize_fqn(stats_table))} "
          "WHERE table_fqn = @table_fqn "
          "AND reference_digest = @reference_digest "
          "AND IFNULL(stats_tier, 'sample') = @tier "
          "QUALIFY ROW_NUMBER() OVER (PARTITION BY `column` "
          "ORDER BY computed_at DESC) = 1")


def _finite(value: Any) -> float | None:
  if value is None or isinstance(value, bool):
    return None
  try:
    number = float(value)
  except (TypeError, ValueError):
    return None
  return number if math.isfinite(number) else None


def _inner_deciles(stats: Any) -> tuple[float, ...]:
  """The nine inner deciles of a stats JSON's 11-point `deciles`, or ()
  (the ends are the sample's exact extremes: never read)."""
  if isinstance(stats, str):
    try:
      stats = json.loads(stats)
    except ValueError:
      return ()
  deciles = stats.get("deciles") if isinstance(stats, Mapping) else None
  if not isinstance(deciles, list) or len(deciles) != len(_DECILES) + 2:
    return ()
  inner = [_finite(d) for d in deciles[1:-1]]
  if any(d is None for d in inner):
    return ()
  return tuple(float(d) for d in inner if d is not None)


def _column_stats(row: Mapping[str, Any]) -> ColumnStats:
  distinct = row.get("distinct")
  return ColumnStats(
      null_fraction=_finite(row.get("null_fraction")),
      distinct=(int(distinct) if isinstance(distinct, int) and
                not isinstance(distinct, bool) else None),
      deciles=_inner_deciles(row.get("stats")))


def _no_stats_reason(table: TablePlan, tier: str | None,
                     stats_table: str) -> str | None:
  if tier is None:
    return "the launch records no --source_stats tier"
  if tier == "off":
    return ("the generation ran with --source_stats=off: it wrote no "
            "source_table_stats")
  if not stats_table:
    return ("the launch named no --source_stats_table: the generator's "
            "stats never reached BigQuery")
  if not table.source_table:
    return "the table has no source table"
  if not table.reference_digest:
    return "no reference digest is recorded for this table (validation_runs)"
  return None


def read_source_stats(plan: EvaluationPlan,
                      query: StatsQuery | None) -> dict[str, TableStats]:
  """The generator's source_table_stats of every evaluated table, read on
  the driver through `query` (`Bq.query`'s shape: `query(sql, params,
  max_bytes=…)`; a fake in tests). A table without stats gets the reason,
  as does one whose read BigQuery REFUSES (`context.bq.is_refusal`) or
  whose stats rows are malformed.

  Raises:
    BqApiError: a read failed for a reason a retry may remove (a 5xx, a
      429, no status): the run fails instead of publishing a drift check
      that is missing by accident, as for pins and pools (Ruling R113).
  """
  params = plan.launch.params
  tier_raw = params.get("source_stats")
  tier = str(tier_raw).strip().lower() if tier_raw not in (None, "") else None
  stats_table = str(params.get("source_stats_table") or "")
  out: dict[str, TableStats] = {}
  for table in plan.tables:
    if not table.evaluated:
      continue
    reason = _no_stats_reason(table, tier, stats_table)
    if reason is not None or query is None:
      out[table.name] = TableStats(
          tier=tier,
          reason=reason or
          "no BigQuery reader in this run: source_table_stats not read")
      continue
    try:
      rows = query(
          source_stats_sql(stats_table), {
              "table_fqn": normalize_fqn(str(table.source_table)),
              "reference_digest": table.reference_digest,
              "tier": tier,
          },
          max_bytes=plan.budget.max_bytes_billed)
      columns = {str(row["column"]): _column_stats(row) for row in rows}
    except _STATS_ERRORS as exc:
      if isinstance(exc, BqApiError) and not is_refusal(exc):
        raise
      out[table.name] = TableStats(
          tier=tier,
          reason=(f"source_table_stats could not be read "
                  f"({type(exc).__name__}: {exc})")[:300])
      continue
    if not columns:
      out[table.name] = TableStats(
          tier=tier,
          reason=(f"no source_table_stats row for {table.source_table} at "
                  f"this reference digest and tier {tier}"))
      continue
    out[table.name] = TableStats(tier=tier, columns=columns)
  return out


@dataclass(frozen=True, eq=False)
class _DriftColumn:
  name: str
  kind: str
  j: int
  grid: int | None  # index into the dense spec's grids
  edges: np.ndarray | None  # that grid's union edges
  string: int | None  # index into the dense spec's strings
  source_distinct: int | None


@dataclass(frozen=True, eq=False)
class DriftSpec:
  """What the drift reads of one table's plan: per non-nested column its
  dense slots and union edges (slim, pickled)."""
  table: str
  digest: str
  sample_rate: float | None
  columns: tuple[_DriftColumn, ...]

  @classmethod
  def from_table(cls, table: TablePlan) -> DriftSpec:
    spec = DenseSpec.from_table(table)
    grids = {g.j: (gi, g.union) for gi, g in enumerate(spec.grids)}
    strings = {s.j: si for si, s in enumerate(spec.strings)}
    columns = []
    for j, column in enumerate(table.columns):
      if column.kind is ColumnKind.NESTED:
        continue
      gi, edges = grids.get(j, (None, None))
      columns.append(
          _DriftColumn(
              name=column.name,
              kind=str(column.kind),
              j=j,
              grid=gi,
              edges=edges,
              string=strings.get(j),
              source_distinct=column.source_distinct))
    rate = table.sample_rate_source
    return cls(
        table=table.name,
        digest=table.encoding_plan_digest,
        sample_rate=rate if rate is not None and rate < 1 else None,
        columns=tuple(columns))


def _cdf_interval(edges: np.ndarray, right: np.ndarray, left: np.ndarray,
                  x: float) -> tuple[float, float]:
  """[F⁻(x), F(x)] of the source from its right- and left-closed union
  counts (unnormalised): exact at an edge, linear inside a bin, and the
  whole unknown range beyond the first or last edge."""
  total = float(right.sum())
  le = np.cumsum(right)[:-1] / total  # count(x <= e_k) / N
  lt = np.cumsum(left)[:-1] / total  # count(x < e_k) / N
  k = int(np.searchsorted(edges, x, side="left"))
  if k < edges.size and edges[k] == x:
    return float(lt[k]), float(le[k])
  if k == 0:
    return 0.0, float(lt[0])
  if k == edges.size:
    return float(le[-1]), 1.0
  lo_edge, hi_edge = float(edges[k - 1]), float(edges[k])
  inside = (x - lo_edge) / (hi_edge - lo_edge)
  value = float(le[k - 1] + inside * (lt[k] - le[k - 1]))
  return value, value


def _decile_delta(column: _DriftColumn, profile: DenseProfile,
                  deciles: Sequence[float]) -> float | None:
  if column.grid is None or column.edges is None or not deciles:
    return None
  right, left = profile.union[column.grid], profile.union_left[column.grid]
  if column.edges.size == 0 or right.sum() <= 0:
    return None
  worst = 0.0
  for p, d in zip(_DECILES, deciles, strict=True):
    lo, hi = _cdf_interval(column.edges, right, left, d)
    worst = max(worst, 0.0 if lo <= p <= hi else min(abs(p - lo), abs(p - hi)))
  return worst


def _distinct_delta(column: _DriftColumn, profile: DenseProfile,
                    stats: ColumnStats) -> float | None:
  if stats.distinct is None or column.source_distinct is None:
    return None
  mine = column.source_distinct
  if column.string is not None and profile.str_empty[column.string] > 0:
    mine -= 1  # the generator counts non-empty values only
  top = max(stats.distinct, mine, 1)
  return abs(stats.distinct - mine) / top


def drift_metrics(spec: DriftSpec, profile: DenseProfile | None,
                  stats: TableStats) -> list[MetricValue]:
  """One `column.source_stats_drift` row per non-nested column (module
  docstring): the largest component, the components in detail, never a
  source value."""
  out = []
  rows = 0 if profile is None else profile.rows
  for column in spec.columns:
    scope: dict[str, Any] = {
        "column": column.name,
        "column_kind": column.kind,
        "encoding_plan_digest": spec.digest,
    }
    entry = stats.columns.get(column.name)
    reason = stats.reason
    if reason is None and entry is None:
      reason = f"no source_table_stats row for column {column.name}"
    if reason is None and (profile is None or rows <= 0):
      reason = _NO_ROWS
    if reason is not None or profile is None or entry is None:
      out.append(
          MetricValue.not_evaluated(_DRIFT_ID, spec.table, str(reason),
                                    **scope))
      continue
    parts: dict[str, float] = {}
    if entry.null_fraction is not None:
      mine = float(profile.nulls[column.j]) / rows
      parts["null_fraction_delta"] = abs(entry.null_fraction - mine)
    decile = _decile_delta(column, profile, entry.deciles)
    if decile is not None:
      parts["decile_cdf_delta"] = decile
    notes: dict[str, Any] = {"tier": stats.tier}
    if stats.tier == "exact":
      distinct = _distinct_delta(column, profile, entry)
      if distinct is not None:
        parts["distinct_rel_delta"] = distinct
    else:
      notes["distinct"] = ("not compared: the sample tier's distinct count "
                           "is bounded by its sample")
    if not parts:
      out.append(
          MetricValue.not_evaluated(
              _DRIFT_ID, spec.table,
              "the stats row holds nothing comparable for this column",
              **scope))
      continue
    detail = {
        **{
            k: round(v, 12) for k, v in parts.items()
        }, "compared": sorted(parts),
        **notes
    }
    sampled = spec.sample_rate is not None
    out.append(
        MetricValue(
            metric_id=_DRIFT_ID,
            table=spec.table,
            value=round(max(parts.values()), 12),
            n_source=rows,
            method=Method.SAMPLE if sampled else Method.EXACT,
            sample_rate=spec.sample_rate,
            detail=detail,
            **scope))
  return out
