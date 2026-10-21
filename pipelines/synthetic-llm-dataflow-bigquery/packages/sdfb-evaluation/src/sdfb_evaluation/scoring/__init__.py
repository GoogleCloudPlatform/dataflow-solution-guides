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
"""Catalogue-driven scoring, noise-aware status (D5) and score roll-ups.

Every rule here comes from the header of `catalogue/metrics.yaml`, which is
the semantic authority; this module only executes it.

- `score_value` applies one of the five score functions, mapping a value to
  `[0, 1]` (higher is better), or `None`.
- `status_for` assigns PASS / WARN / FAIL / INFO / NOT_EVALUATED. Its order:
  1. the gated value g is the value, or for a `uses_ci_bound` metric its CI
     bound (`ci_low` for lower_better, `ci_high` for higher_better; Rulings
     R1, R12). A missing or NaN g -> NOT_EVALUATED. A lift gates on its
     bound even when its point value is None or infinite (no events on
     either side, Ruling R38), so a clean run is not NOT_EVALUATED;
  2. `table.pmse_ratio` without `detail["ceiling"]` (`N / (k - 1)`), or with
     a ceiling below the fail threshold, -> NOT_EVALUATED: the design could
     not reach a FAIL (Ruling R33);
  3. `relationship.orphan_rate` on a documented edge (`enforced=False`) ->
     INFO, never FAIL. Only the orphan rate: fan-out metrics compare
     children per parent with the source whatever the enforcement, so they
     stay graded (Ruling R42); likewise `field.substantive_copy_rate` is
     gated only on a `text` column (free text, where the probe's rule
     came from) and INFO on every other kind (Ruling R66): numeric and
     temporal values collide with a dense source by domain size, and
     reusing a rare real category or identifier is not evidence of
     memorisation — `field.value_memorization_lift` (R vs H) is the gated
     signal there;
  4. null warn and fail -> INFO;
  5. a `target` metric reads x = |g - target|, its target being the
     catalogue's or, when that is null (`column.novelty_mass`), the row's
     `source_value` (Ruling R9); x = g otherwise;
  6. an infinite g past the bad side (+inf on lower_better and target,
     -inf on higher_better) -> FAIL with `detail["nonfinite"]` ("+inf" or
     "-inf"); on the good side it cannot be graded -> NOT_EVALUATED;
  7. warn == fail == 0 (integrity by construction): FAIL iff x > 0, else
     PASS, and the noise check is never applied;
  8. otherwise crossing is inclusive (Ruling R9): lower_better and target
     reach a threshold at x >= threshold, higher_better at x <= threshold.
     Equality is checked with a 1e-9 relative tolerance, so `|0.9 - 1.0|`
     (0.09999999999999998 in floating point) still reaches a 0.1 warn;
  9. a WARN or FAIL that sampling noise explains becomes PASS (D5: the
     brief's "past fail but below the noise floor -> PASS", applied to WARN
     alike), recorded as `detail["noise_downgraded_from"]`. The check
     follows the catalogue's `noise_floor` method (Ruling R41):
     - scalar methods (ks_two_sample, tvd_null, jsd_null, fisher_z,
       mi_bias): noise iff |g - reference| <= the row's `noise_floor`;
     - interval methods (wilson, newcombe, delong): noise iff
       [`ci_low`, `ci_high`] covers the reference;
     - rate_ratio: none beyond gating on the bound; `none`: no check.
     When the input a check needs is missing, nothing is downgraded and
     `detail["noise_check"] = "unavailable"`. The reference is the target
     (catalogue or `source_value`), else 0 for lower_better and 1 for
     higher_better (Ruling R12).
- Producer contract (Tasks 21-25): an interval-method metric puts its
  confidence interval in `ci_low`/`ci_high` (a folded interval for an
  absolute difference); a scalar-method metric puts its floor in
  `noise_floor`; a relationship metric's `table` is the CHILD table and
  its `edge` the edge label.
- A row's score is the score function on the same g its status read, and
  `None` for INFO and NOT_EVALUATED rows. A noise-downgraded row scores at
  its reference (normally 1.0; Ruling R40), so roll-ups raise no false
  alarm on small tables, while every other row keeps its raw score. An
  infinite g scores at the function's limit (0 on the bad side).
  `score: none` is the value only for aggregate ids (table.*_score,
  model.*); every other `score: none` metric scores `None` (Ruling R26).
- `to_metric_row` writes one `evaluation_metrics` row, JSON-safe;
  `to_profile_row` one `evaluation_profiles` row.
- `aggregate_scores` rolls scores up over units (Ruling R11) and
  `headline_counts` counts statuses so that they reconcile with the total.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import functools
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, NamedTuple

import numpy as np

from sdfb_evaluation.canonical import canonical_value
from sdfb_evaluation.canonical import json_safe
from sdfb_evaluation.catalogue import Catalogue
from sdfb_evaluation.catalogue import Metric
from sdfb_evaluation.catalogue import load_catalogue
from sdfb_evaluation.types import Method
from sdfb_evaluation.types import MetricValue
from sdfb_evaluation.types import ProfileValue
from sdfb_evaluation.types import Status

# The key of the model-wide entry in `aggregate_scores`' result.
MODEL_KEY = "__model__"
# The four measured families; `overall` is the roll-up family.
FAMILIES: tuple[str, ...] = ("fidelity", "privacy", "integrity", "diversity")
OVERALL = "overall"
# Per table (and model): how many zero-tolerance integrity rows (warn ==
# fail == 0: duplicate keys, orphans on enforced edges) FAILed, so a caller
# can show a FAIL badge whatever the averages say (Ruling R39).
INTEGRITY_FAIL = "integrity_fail"

SCALAR_NOISE_METHODS = frozenset(
    {"ks_two_sample", "tvd_null", "jsd_null", "fisher_z", "mi_bias"})
INTERVAL_NOISE_METHODS = frozenset({"wilson", "newcombe", "delong"})
# A run's threshold overrides (Ruling R93-6): metric id -> (warn, fail).
Thresholds = Mapping[str, tuple[float, float]]

_PMSE_ID = "table.pmse_ratio"
_ORPHAN_ID = "relationship.orphan_rate"
_COPY_RATE_ID = "field.substantive_copy_rate"
_DOMAIN_COLLISION_REASON = (
    "domain-size collision: a numeric or temporal column meets a dense "
    "source by domain size, not by copying; reported, not gated "
    "(field.value_memorization_lift is the fair test)")
_FREE_TEXT_ONLY_REASON = (
    "gated only on free text (Ruling R66): reusing a rare real category or "
    "identifier is not evidence of memorisation; reported, not gated "
    "(field.value_memorization_lift is the gated signal)")
_REL_TOL = 1e-9  # tolerance for "at the threshold" (inclusive crossing)
_UNSCORED = (Status.INFO, Status.NOT_EVALUATED)


@functools.cache
def _catalogue() -> Catalogue:
  return load_catalogue()


@functools.cache
def _zero_tolerance_ids() -> frozenset[str]:
  """The integrity metrics with warn == fail == 0, from the catalogue."""
  return frozenset(
      metric.id
      for metric in _catalogue().metrics
      if metric.family == "integrity" and metric.warn == 0 and metric.fail == 0)


def is_aggregate(metric_id: str) -> bool:
  """True for the roll-up ids: `table.*_score` and every `model.*` id."""
  return metric_id.startswith("model.") or (metric_id.startswith("table.") and
                                            metric_id.endswith("_score"))


def _clip01(x: float) -> float:
  return min(1.0, max(0.0, x))


def _band(x: float, warn: float, fail: float) -> float:
  """1 at or under `warn`, 0 at or over `fail`, linear between (lower is better).

  With `warn == fail` it is a step that scores 1 at the threshold itself,
  which is the integrity rule for the catalogue's only such metrics (0 / 0:
  PASS at exactly 0).
  """
  if x <= warn:
    return 1.0
  if x >= fail:
    return 0.0
  return (fail - x) / (fail - warn)


def score_value(  # noqa: PLR0911 — score-function dispatch, clearer flat than nested
    metric: Metric,
    value: float | None,
    *,
    target: float | None = None) -> float | None:
  """`value` mapped to `[0, 1]` (higher is better) by `metric.score_fn`.

  The five functions (catalogue header, Ruling R10): `complement` =
  clip(1 - |v| / range_hi); `linear` = 1 at or past warn on the good side,
  0 at or past fail, linear between (mirrored for higher_better);
  `ratio_to_one` = the same band on d = |v - target|; `auc` = 1 - 2 max(0,
  v - 0.5); `none` = v when 0 <= v <= 1 on an aggregate id, else `None`
  (Ruling R26: a raw distance or indicator is not a score).

  `target` is used only when the catalogue's target is null (the row's
  `source_value` for `column.novelty_mass`); without either a target
  metric has no score. For a `uses_ci_bound` metric pass the CI bound its
  status gates on, not the point estimate. `None` or NaN scores `None`; an
  infinite value scores the function's limit (0 on the bad side).

  Raises:
    ValueError: a `complement` metric without a positive range top.
  """
  if value is None or math.isnan(value):
    return None
  fn = metric.score_fn
  if fn == "none":
    in_unit = 0.0 <= value <= 1.0
    return float(value) if in_unit and is_aggregate(metric.id) else None
  if fn == "complement":
    top = metric.range[1]
    if top is None or top <= 0:
      raise ValueError(f"{metric.id}: complement needs a positive range top")
    return _clip01(1.0 - abs(value) / top)
  if fn == "auc":
    return _clip01(1.0 - 2.0 * max(0.0, value - 0.5))
  if metric.warn is None or metric.fail is None:
    return None
  if fn == "ratio_to_one" or metric.direction == "target":
    goal = metric.target if metric.target is not None else target
    if goal is None or not math.isfinite(goal):
      return None
    return _clip01(_band(abs(value - goal), metric.warn, metric.fail))
  if metric.direction == "higher_better":
    return _clip01(_band(-value, -metric.warn, -metric.fail))
  return _clip01(_band(value, metric.warn, metric.fail))


class _Assessment(NamedTuple):
  """A status, the detail keys that explain it and what the score reads."""
  status: Status
  notes: dict[str, Any] | None = None  # merged into the row's `detail`
  score_at: float | None = None  # g, or the reference after a downgrade
  target: float | None = None  # the effective target of a `target` metric


def _not_evaluated(reason: str) -> _Assessment:
  return _Assessment(Status.NOT_EVALUATED, {"reason": reason})


def _reached(x: float, threshold: float | None, higher_better: bool) -> bool:
  """Whether `x` is at or past `threshold` (inclusive, Ruling R9)."""
  if threshold is None:
    return False
  if math.isclose(x, threshold, rel_tol=_REL_TOL):
    return True
  return x <= threshold if higher_better else x >= threshold


def _noise_reference(metric: Metric, target: float | None) -> float:
  """What a value is compared with for the D5 noise check (Ruling R12)."""
  if target is not None:
    return target
  if metric.target is not None:
    return metric.target
  return 1.0 if metric.direction == "higher_better" else 0.0


def _is_noise(metric: Metric, mv: MetricValue, gated: float,
              reference: float) -> bool | None:
  """Whether sampling noise explains g (Ruling R41); None when it cannot tell.

  Returns False for the methods with no check (rate_ratio, none).
  """
  method = metric.noise_floor
  if method in SCALAR_NOISE_METHODS:
    floor = mv.noise_floor
    if floor is None or math.isnan(floor):
      return None
    return abs(gated - reference) <= floor
  if method in INTERVAL_NOISE_METHODS:
    low, high = mv.ci_low, mv.ci_high
    if low is None or high is None or math.isnan(low) or math.isnan(high):
      return None
    return low <= reference <= high
  return False


def _gated(metric: Metric, mv: MetricValue) -> tuple[str, float | None, str]:
  """Step 1: the name and value of what status reads (None or NaN when it
  cannot be read) and the reason to give in that case."""
  if not metric.uses_ci_bound:
    missing = "no value computed" if mv.value is None else "value is NaN"
    return "value", mv.value, missing
  if metric.direction == "target":
    raise ValueError(f"{metric.id}: uses_ci_bound needs a one-sided direction")
  name = "ci_high" if metric.direction == "higher_better" else "ci_low"
  bound = getattr(mv, name)
  if bound is None or math.isnan(bound):
    return name, None, f"{name} missing: {metric.id} gates on its confidence bound"
  point_missing = mv.value is None or not math.isfinite(mv.value)
  if point_missing and not math.isfinite(bound):
    return name, None, f"neither the value nor {name} is finite"
  return name, float(bound), ""


def _grade(metric: Metric, mv: MetricValue, gated: float, x: float,
           target: float | None) -> _Assessment:
  """Steps 7-9: the integrity rule, inclusive crossings, the D5 noise check."""
  if metric.warn == 0 and metric.fail == 0:
    status = Status.FAIL if x > 0 else Status.PASS
    return _Assessment(status, None, gated, target)
  higher_better = metric.direction == "higher_better"
  if _reached(x, metric.fail, higher_better):
    status = Status.FAIL
  elif _reached(x, metric.warn, higher_better):
    status = Status.WARN
  else:
    return _Assessment(Status.PASS, None, gated, target)
  reference = _noise_reference(metric, target)
  noise = _is_noise(metric, mv, gated, reference)
  if noise is None:
    return _Assessment(status, {"noise_check": "unavailable"}, gated, target)
  if noise:
    notes = {"noise_downgraded_from": status.value}
    return _Assessment(Status.PASS, notes, reference, target)
  return _Assessment(status, None, gated, target)


def _copy_rate_info_reason(mv: MetricValue) -> str | None:
  """Why a copy rate is reported as INFO, or None when it is gated: only
  a `text` column (or one with no kind given) is gated (Ruling R66)."""
  kind = mv.column_kind
  if kind is None or kind == "text":
    return None
  if kind in ("numeric", "temporal"):
    return _DOMAIN_COLLISION_REASON
  return _FREE_TEXT_ONLY_REASON


def _assess(  # noqa: PLR0911 — the status order, clearer flat than nested
    metric: Metric, mv: MetricValue, enforced: bool) -> _Assessment:
  """The full status decision, in the order the module docstring lists."""
  if mv.metric_id != metric.id:
    raise ValueError(
        f"metric value {mv.metric_id!r} scored against catalogue {metric.id!r}")
  name, gated, missing = _gated(metric, mv)
  if gated is None or math.isnan(gated):
    return _not_evaluated(str(mv.detail.get("reason") or missing))
  if metric.id == _PMSE_ID:
    ceiling = mv.detail.get("ceiling")
    if ceiling is None or math.isnan(float(ceiling)):
      return _not_evaluated("pmse ceiling missing")
    if metric.fail is not None and float(ceiling) < metric.fail:
      return _not_evaluated("ceiling below fail threshold")
  if metric.id == _ORPHAN_ID and not enforced:
    return _Assessment(
        Status.INFO,
        {"reason": "documented edge (enforced: false): reported, not gated"})
  copy_info = (
      _copy_rate_info_reason(mv) if metric.id == _COPY_RATE_ID else None)
  if copy_info is not None:
    return _Assessment(Status.INFO, {"reason": copy_info})
  if metric.warn is None and metric.fail is None:
    return _Assessment(Status.INFO)
  target = None
  x = gated
  if metric.direction == "target":
    target = metric.target if metric.target is not None else mv.source_value
    if target is None or not math.isfinite(target):
      return _not_evaluated("no target: source_value missing or non-finite")
    x = abs(gated - target)
  if math.isinf(gated):
    sign = "+inf" if gated > 0 else "-inf"
    bad_side = (
        metric.direction == "target" or
        (gated > 0) != (metric.direction == "higher_better"))
    if not bad_side:
      return _not_evaluated(f"{name} is {sign}, on the good side")
    return _Assessment(Status.FAIL, {"nonfinite": sign}, gated, target)
  return _grade(metric, mv, gated, x, target)


def status_for(metric: Metric,
               mv: MetricValue,
               *,
               enforced: bool = True) -> Status:
  """`mv`'s status under `metric`'s catalogue entry (D5; see module docstring).

  `enforced=False` marks a documented foreign-key edge: its orphan rate is
  reported as INFO, never gated (Ruling R42).

  Raises:
    ValueError: `mv.metric_id` is not `metric.id`.
  """
  return _assess(metric, mv, enforced).status


def _as_float(x: Any) -> float | None:
  return None if x is None else float(x)


def _as_int(x: Any) -> int | None:
  return None if x is None else int(x)


def _plain(obj: Any) -> Any:
  """`obj` with numpy scalars/arrays as Python values, for a JSON column."""
  if isinstance(obj, np.generic):
    return obj.item()
  if isinstance(obj, np.ndarray):
    return obj.tolist()
  if isinstance(obj, Mapping):
    return {str(key): _plain(value) for key, value in obj.items()}
  if isinstance(obj, (list, tuple)):
    return [_plain(item) for item in obj]
  return obj


def _timestamp(evaluated_at: datetime | str) -> str:
  """`evaluated_at` as UTC ISO-8601 with microseconds (naive = UTC).

  Raises:
    ValueError: a string that is not ISO-8601.
  """
  if isinstance(evaluated_at, str):
    try:
      evaluated_at = datetime.fromisoformat(evaluated_at)
    except ValueError as exc:
      raise ValueError(
          f"evaluated_at is not ISO-8601: {evaluated_at!r}") from exc
  return str(canonical_value(evaluated_at))


def to_metric_row(mv: MetricValue,
                  *,
                  evaluation_id: str,
                  evaluated_at: datetime | str,
                  landing_table: str | None,
                  source_table: str | None,
                  enforced: bool = True,
                  thresholds: Thresholds | None = None) -> dict[str, Any]:
  """One `evaluation_metrics` row: exactly its fields, in schema order.

  `level`, `family`, `metric_version`, `value_kind`, the thresholds and
  `noise_floor_method` come from the catalogue; `table`/`column`/
  `column_2` map to `table_name`/`column_name`/`column_name_2` (Ruling
  R15). A relationship row is the child table's (`mv.table`), labelled by
  `mv.edge`. `detail` gains the keys that explain the status: `reason` on
  every NOT_EVALUATED row (the producer's own when it gave one) and on a
  documented edge's INFO row, `noise_downgraded_from`, `noise_check` and
  `nonfinite` (see the module docstring). `evaluated_at` is a `datetime`
  or an ISO-8601 string, written as UTC ISO-8601 with microseconds.
  Non-finite floats become `None` (`json_safe`), so the row can go straight
  to a BigQuery load job.

  `thresholds` (`{metric id: (warn, fail)}`, Ruling R93-6) replaces the
  catalogue's two thresholds for the metrics it names, for everything
  this row derives from them: its status, its score and the
  `threshold_warn` / `threshold_fail` it stores. None: the catalogue's.

  Raises:
    KeyError: `mv.metric_id` is not in the catalogue.
    ValueError: `evaluated_at` is a string that is not ISO-8601.
  """
  metric = _catalogue().get(mv.metric_id)
  if thresholds and metric.id in thresholds:
    warn, fail = thresholds[metric.id]
    metric = dataclasses.replace(metric, warn=warn, fail=fail)
  assessment = _assess(metric, mv, enforced)
  detail = dict(mv.detail)
  detail.update(assessment.notes or {})
  score = None
  if assessment.status not in _UNSCORED:
    score = score_value(metric, assessment.score_at, target=assessment.target)
  row = {
      "evaluation_id": evaluation_id,
      "evaluated_at": _timestamp(evaluated_at),
      "table_name": mv.table,
      "landing_table": landing_table,
      "source_table": source_table,
      "level": metric.level,
      "family": metric.family,
      "metric_id": metric.id,
      "metric_version": metric.version,
      "value_kind": metric.value_kind,
      "column_name": mv.column,
      "column_name_2": mv.column_2,
      "column_kind": mv.column_kind,
      "edge": mv.edge,
      "value": _as_float(mv.value),
      "source_value": _as_float(mv.source_value),
      "synthetic_value": _as_float(mv.synthetic_value),
      "baseline_value": _as_float(mv.baseline_value),
      "score": score,
      "status": assessment.status.value,
      "threshold_warn": metric.warn,
      "threshold_fail": metric.fail,
      "noise_floor": _as_float(mv.noise_floor),
      "noise_floor_method": metric.noise_floor,
      "ci_low": _as_float(mv.ci_low),
      "ci_high": _as_float(mv.ci_high),
      "n_source": _as_int(mv.n_source),
      "n_synthetic": _as_int(mv.n_synthetic),
      "method": Method(mv.method).value,
      "sample_rate": _as_float(mv.sample_rate),
      "encoding_plan_digest": mv.encoding_plan_digest,
      "feature_set_digest": mv.feature_set_digest,
      "detail": _plain(detail) or None,
  }
  safe: dict[str, Any] = json_safe(row)
  return safe


def to_profile_row(pv: ProfileValue, *, evaluation_id: str,
                   evaluated_at: datetime | str) -> dict[str, Any]:
  """One `evaluation_profiles` row: exactly its fields, in schema order.

  The profile counterpart of `to_metric_row`: `table`/`column` map to
  `table_name`/`column_name`, numpy values in the payload become Python
  values and non-finite floats `None` (`json_safe`), so the row can go
  straight to a BigQuery load job.

  Raises:
    ValueError: `evaluated_at` is a string that is not ISO-8601.
  """
  row = {
      "evaluation_id": evaluation_id,
      "evaluated_at": _timestamp(evaluated_at),
      "table_name": pv.table,
      "column_name": pv.column,
      "edge": pv.edge,
      "profile_kind": pv.profile_kind,
      "side": pv.side,
      "n": _as_int(pv.n),
      "truncated": pv.truncated,
      "edges_digest": pv.edges_digest,
      "payload": _plain(pv.payload),
  }
  safe: dict[str, Any] = json_safe(row)
  return safe


def _mean(values: Iterable[float | None]) -> float | None:
  """The mean of the non-`None` values (`None` if there are none).

  `math.fsum` rounds the sum exactly once, so the mean does not depend on
  the order rows arrive in (a Beam combine gives no order).
  """
  present = [value for value in values if value is not None]
  return math.fsum(present) / len(present) if present else None


def _unit(row: Mapping[str, Any]) -> tuple[str, str | None]:
  """The roll-up unit a non-aggregate row belongs to (Ruling R11)."""
  level = row["level"]
  if level in ("field", "column"):
    return ("column", row.get("column_name"))
  if level == "pair":
    return ("pair", None)
  if level in ("row", "table"):
    return (level, row["metric_id"])
  if level == "relationship":
    return ("edge", row.get("edge"))
  raise ValueError(f"no roll-up unit for level {level!r}")


def aggregate_scores(
    rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
  """Family and overall scores per table and for the model (Ruling R11).

  `rows` are `to_metric_row` rows. Aggregate rows (table.*_score, model.*)
  are skipped entirely: they never feed a score and never create a table.
  A table's family score is the mean over UNITS with at least one scored
  row in that family: each column (the mean of its field and column
  scores), the pair group (one unit), each row-level metric, each
  non-aggregate table-level metric and each edge the table is the child of
  (one unit, keyed by `edge`). `None` scores are skipped and a unit with
  none left does not count. `overall` is the mean of the available family
  scores.

  Returns `{table: {fidelity, privacy, integrity, diversity, overall,
  integrity_fail}, MODEL_KEY: {...}}`, tables sorted by name. A family
  with no unit is `None`. The model's family score is the mean of the
  tables' family scores and its overall the mean of its family scores.
  `integrity_fail` counts the FAILed zero-tolerance integrity rows (warn ==
  fail == 0 in the catalogue: duplicate keys and orphans on enforced
  edges; Ruling R39), summed for the model, so a caller can show a FAIL
  badge whatever the averages say.

  Raises:
    ValueError: a non-aggregate row without a `table_name`.
  """
  units: dict[str, dict[str, dict[tuple[str, str | None], list[float]]]] = (
      defaultdict(lambda: defaultdict(lambda: defaultdict(list))))
  fails: dict[str, int] = defaultdict(int)
  tables: set[str] = set()  # every table with a measured row, scored or not
  for row in rows:
    if is_aggregate(row["metric_id"]):
      continue
    table = row["table_name"]
    if table is None:
      # Hoisted local: an inline `row['metric_id']` would nest the same
      # quote character the py3.14 pylint gate treats as inconsistent
      # with the rest of this double-quoted file (W1405).
      metric_id = row["metric_id"]
      raise ValueError(f"{metric_id} row has no table_name")
    tables.add(table)
    if (row["metric_id"] in _zero_tolerance_ids() and
        row["status"] == Status.FAIL):
      fails[table] += 1
    score = row.get("score")
    if score is not None and math.isfinite(score):
      units[table][row["family"]][_unit(row)].append(float(score))
  result: dict[str, dict[str, Any]] = {}
  for table in sorted(tables):
    scores: dict[str, Any] = {
        family: _mean(_mean(unit) for unit in units[table][family].values())
        for family in FAMILIES
    }
    scores[OVERALL] = _mean(scores[family] for family in FAMILIES)
    scores[INTEGRITY_FAIL] = fails[table]
    result[table] = scores
  model: dict[str, Any] = {
      family: _mean(scores[family] for scores in result.values())
      for family in FAMILIES
  }
  model[OVERALL] = _mean(model[family] for family in FAMILIES)
  model[INTEGRITY_FAIL] = sum(fails.values())
  result[MODEL_KEY] = model
  return result


def headline_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
  """How many measured rows have each status, plus `total`.

  Aggregate rows (table.*_score, table.column_shape_score,
  table.pair_trend_score, model.*) are excluded: they restate the measured
  rows, so counting them would double-count a run's verdicts. Every
  `Status` value is a key, zero included, so pass + warn + fail + info +
  not_evaluated always equals `total`. The keys feed the registry's
  `metrics_<key>` columns (Ruling R37).

  Raises:
    ValueError: a row's status is not a `Status` value.
  """
  counts = {status.value: 0 for status in Status}
  total = 0
  for row in rows:
    if is_aggregate(row["metric_id"]):
      continue
    status = row["status"]
    if status not in counts:
      raise ValueError(f"unknown status {status!r}")
    counts[status] += 1
    total += 1
  counts["total"] = total
  return counts
