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
"""Rendering stored evaluations: one run, two runs compared, the
catalogue.

    sdfb-eval report     render_markdown / render_json(Evaluation)
    sdfb-eval compare    compare(a, b) → render_compare_markdown / _json
    sdfb-eval catalogue  render_catalogue_markdown(Catalogue)

A report leads with the registry's latest event (status, scores, the
headline counts) and then explains every FAIL: each failing metric id
once, with the catalogue's own text — what it measures, what a bad value
means, its pitfalls — above the rows that failed, so a reader never has
to look an id up. WARN rows, the reasons behind not_evaluated rows and
the run's warnings follow. The JSON form carries the same, plus every
metric row, for agents and the E2E bundle.

Thresholds are read from the stored rows, never assumed: a run under
`--thresholds_uri` grades and stores against its own (Ruling R93-6). The
header says which thresholds graded the run (the registry's
`evaluation_params.thresholds_uri` / `thresholds_digest`), a failing
metric shows the warn and fail its rows carry — and the packaged
catalogue's beside them when they differ — and a comparison of two runs
graded against different thresholds says so.

A comparison is noise-aware. Rows are joined on (table, metric, column,
second column, edge) and the delta B - A is judged against what the two
rows say about their own sampling noise:

    both rows carry                   the delta is "≈" (within noise) when
    ────────────────────────────────  ─────────────────────────────────────
    noise_floor (scalar methods)      |Δ| <= sqrt(floor_A² + floor_B²): the
                                      two estimates' noise is independent
    ci_low and ci_high (interval      the two intervals overlap
      methods)
    neither                           never: the delta is reported with
                                      "no noise floor" beside it

Otherwise the delta reads `worse` or `better` by the catalogue's
direction (`changed` for a metric with no target to move towards).
Statuses are compared as stored; two runs graded by different catalogue
versions are flagged, not re-graded.

Run-vs-run drift is the population stability index (`stats.distances.
psi`, counts with half-a-row smoothing) between the two runs' `histogram`
profiles of the same table, column and side — only when both carry the
same `edges_digest`: the same digest means the same bin edges, so the
counts line up bin for bin. Different digests (a re-planned grid) are
"not comparable"; nothing is re-binned or interpolated. The bands are
the catalogue's own `column.psi` thresholds.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sdfb_evaluation.catalogue import Catalogue, Metric, load_catalogue
from sdfb_evaluation.report.store import Evaluation
from sdfb_evaluation.scoring import FAMILIES, headline_counts, is_aggregate
from sdfb_evaluation.stats.distances import psi

__all__ = [
    "compare",
    "render_catalogue_markdown",
    "render_compare_json",
    "render_compare_markdown",
    "render_json",
    "render_markdown",
]

WITHIN_NOISE = "≈"
NOT_COMPARABLE = "not comparable"
_DASH = "—"
_SCORES = ("overall", *FAMILIES)
_COUNTS = ("total", "pass", "warn", "fail", "info", "not_evaluated")
_HISTOGRAM = "histogram"
_PSI_ID = "column.psi"
_REASONS_SHOWN = 20
_SIGNIFICANT = 4
_STATUS_ORDER = {"fail": 0, "warn": 1, "pass": 2, "info": 3, "not_evaluated": 4}
_DESCRIBED = ("title", "level", "family", "direction", "threshold_warn",
              "threshold_fail", "purpose", "interpretation", "pitfalls")

RowKey = tuple[str, str, str | None, str | None, str | None]


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------
def _num(value: Any) -> str:
  if value is None:
    return _DASH
  if isinstance(value, bool):
    return str(value).lower()
  if isinstance(value, int):
    return f"{value:,}"
  if isinstance(value, float):
    return f"{value:.{_SIGNIFICANT}g}" if math.isfinite(value) else _DASH
  return str(value)


def _cell(value: Any) -> str:
  """`value` as one markdown table cell (no pipe, no line break)."""
  text = _num(value) if not isinstance(value, str) else value
  return " ".join(text.replace("|", "\\|").split()) or _DASH


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
  lines = [
      "| " + " | ".join(headers) + " |",
      "|" + "|".join("---" for _ in headers) + "|"
  ]
  lines.extend("| " + " | ".join(_cell(v) for v in row) + " |" for row in rows)
  return lines


def _prose(text: str) -> str:
  return " ".join(text.split())


def _code(text: Any) -> str:
  return f"`{text}`"


def _scope(row: Mapping[str, Any]) -> str:
  """What a metric row is about, inside its table."""
  if row.get("edge"):
    return str(row["edge"])
  first, second = row.get("column_name"), row.get("column_name_2")
  if first and second:
    return f"{first} & {second}"
  return str(first) if first else "(table)"


def _row_key(row: Mapping[str, Any]) -> RowKey:
  return (str(row.get("table_name")), str(row.get("metric_id")),
          row.get("column_name"), row.get("column_name_2"), row.get("edge"))


def _sort_key(row: Mapping[str, Any]) -> tuple[str, ...]:
  return tuple("" if part is None else str(part) for part in _row_key(row))


def _interval(row: Mapping[str, Any]) -> str:
  low, high = row.get("ci_low"), row.get("ci_high")
  if low is None and high is None:
    return _DASH
  return f"[{_num(low)}, {_num(high)}]"


def _graded_against(metric: Metric,
                    rows: Sequence[Mapping[str, Any]]) -> tuple[Any, Any]:
  """The (warn, fail) the stored `rows` of `metric` were graded against:
  the ones they carry (one evaluation grades a metric against one pair),
  the catalogue's when there is no row."""
  if not rows:
    return metric.warn, metric.fail
  return rows[0].get("threshold_warn"), rows[0].get("threshold_fail")


def _thresholds(metric: Metric, rows: Sequence[Mapping[str, Any]]) -> str:
  parts = [metric.family, metric.level, metric.direction.replace("_", " ")]
  if metric.target is not None:
    parts.append(f"target {_num(metric.target)}")
  warn, fail = _graded_against(metric, rows)
  graded = f"fail {_num(fail)}"
  if (warn, fail) != (metric.warn, metric.fail):
    graded += (" (this run's thresholds; the packaged catalogue has warn "
               f"{_num(metric.warn)}, fail {_num(metric.fail)})")
  parts += [f"warn {_num(warn)}", graded]
  if metric.uses_ci_bound:
    parts.append("gated on its confidence bound")
  return " · ".join(parts)


def _override(row: Mapping[str, Any]) -> tuple[Any, Any]:
  """The threshold override a registry row records: (URI, digest), both
  None for a run graded by the catalogue (or stored before overrides
  were recorded)."""
  params = row.get("evaluation_params") or {}
  return params.get("thresholds_uri"), params.get("thresholds_digest")


def _graded_by(row: Mapping[str, Any], override: str) -> str:
  """Which thresholds graded the run of registry row `row`: the
  catalogue's, or `override` filled with the URI and the digest."""
  uri, digest = _override(row)
  if uri is None and digest is None:
    return "the catalogue's"
  return override.format(uri=_code(uri), digest=_code(digest))


# --------------------------------------------------------------------------
# one evaluation
# --------------------------------------------------------------------------
def _measured(evaluation: Evaluation) -> list[dict[str, Any]]:
  return [r for r in evaluation.metrics if not is_aggregate(r["metric_id"])]


def _by_status(evaluation: Evaluation, status: str) -> list[dict[str, Any]]:
  return sorted((r for r in _measured(evaluation) if r.get("status") == status),
                key=_sort_key)


def _failing_groups(
    evaluation: Evaluation, catalogue: Catalogue
) -> list[tuple[str, Metric | None, list[dict[str, Any]]]]:
  """The FAIL rows grouped by metric id, in catalogue order: (id, its
  catalogue entry, its rows). A stored evaluation may hold an id the
  packaged catalogue does not have (another catalogue version graded
  it): its entry is None and it comes last, by name."""
  failing = _by_status(evaluation, "fail")
  order = {metric_id: i for i, metric_id in enumerate(catalogue.ids())}
  ids = sorted({str(r["metric_id"]) for r in failing},
               key=lambda m: (order.get(m, len(order)), m))
  return [(metric_id, catalogue.get(metric_id) if metric_id in order else None,
           [r
            for r in failing
            if r["metric_id"] == metric_id])
          for metric_id in ids]


def _header(evaluation: Evaluation) -> list[str]:
  row = evaluation.latest
  status, reason = str(row.get("status")), row.get("status_reason")
  lines = [f"# Evaluation `{evaluation.evaluation_id}` — {status}", ""]
  if reason:
    lines += [f"> {_prose(str(reason))}", ""]
  if evaluation.final is None:
    lines += [
        "> No FINAL event is recorded: the evaluation is still running, or "
        "its job died after launch.", ""
    ]
  counts = headline_counts(_measured(evaluation))
  how = " / ".join(str(row.get(k)) for k in ("mode", "runner", "trigger"))
  versions = " / ".join(
      str(row.get(k)) for k in ("evaluator_version", "catalogue_version"))
  facts = [
      ("Evaluated at", row.get("evaluated_at")),
      ("Finished at", row.get("finished_at")),
      ("Mode / runner / trigger", how),
      ("Generation job", row.get("generation_job_id")),
      ("Run ids", ", ".join(row.get("run_ids") or ()) or None),
      ("Relationship model", row.get("relationship_model")),
      ("Evaluator / catalogue", versions),
      ("Thresholds", _graded_by(row, "overridden by {uri} (digest {digest})")),
      ("Read from", evaluation.origin),
  ]
  facts += [
      (f"{name.title()} score", row.get(f"{name}_score")) for name in _SCORES
  ]
  facts.append(
      ("Metrics", ", ".join(f"{counts[key]:,} {key}" for key in _COUNTS)))
  return lines + _table(("", ""), facts) + [""]


def _tables_section(evaluation: Evaluation) -> list[str]:
  entries = evaluation.latest.get("tables") or []
  if not entries:
    return []
  rows = [
      (e.get("name"), e.get("role"),
       " / ".join(str(e.get(k)) for k in ("scope_mode", "scope_status")),
       e.get("rows_source"), e.get("rows_synthetic"), e.get("rows_expected"),
       e.get("reference_verified"), e.get("table_score")) for e in entries
  ]
  return [
      "## Tables", "", *_table(
          ("table", "role", "scope", "rows source", "rows synthetic",
           "rows expected", "reference verified", "score"), rows), ""
  ]


def _explained(metric_id: str, metric: Metric | None, catalogue: Catalogue,
               rows: Sequence[Mapping[str, Any]]) -> list[str]:
  """The heading of one failing metric with the catalogue's own text
  and the thresholds its `rows` were graded against — or, for an id the
  packaged catalogue lacks, a line saying so."""
  if metric is None:
    return [
        f"### `{metric_id}` — not in the packaged catalogue", "",
        f"*The packaged catalogue ({catalogue.version}) has no entry for "
        "this id: the evaluation was graded by another catalogue version "
        "(the registry row names it). Its rows follow as stored.*", ""
    ]
  return [
      f"### `{metric.id}` — {metric.title}", "",
      f"*{_thresholds(metric, rows)}*", "",
      f"- **Measures:** {_prose(metric.purpose)}",
      f"- **A bad value means:** {_prose(metric.interpretation.bad)}",
      f"- **A good value:** {_prose(metric.interpretation.good)}",
      f"- **Pitfalls:** {_prose(metric.pitfalls)}", ""
  ]


def _failing_section(evaluation: Evaluation, catalogue: Catalogue) -> list[str]:
  groups = _failing_groups(evaluation, catalogue)
  total = sum(len(rows) for _, _, rows in groups)
  lines = [f"## Failing metrics ({total} rows, {len(groups)} metrics)", ""]
  if not groups:
    return [*lines, "No metric is at FAIL.", ""]
  for metric_id, metric, rows in groups:
    lines += _explained(metric_id, metric, catalogue, rows)
    lines += _table(
        ("table", "scope", "value", "noise floor", "CI", "baseline", "source",
         "synthetic", "n source", "n synthetic", "method"),
        [(r.get("table_name"), _scope(r), r.get("value"), r.get("noise_floor"),
          _interval(r), r.get("baseline_value"), r.get("source_value"),
          r.get("synthetic_value"), r.get("n_source"), r.get("n_synthetic"),
          r.get("method")) for r in rows])
    lines.append("")
  return lines


def _warn_section(evaluation: Evaluation) -> list[str]:
  rows = _by_status(evaluation, "warn")
  if not rows:
    return []
  return [
      f"## Metrics at WARN ({len(rows)} rows)", "", *_table(
          ("metric", "table", "scope", "value", "warn at", "fail at"),
          [(_code(r["metric_id"]), r.get("table_name"), _scope(r),
            r.get("value"), r.get("threshold_warn"), r.get("threshold_fail"))
           for r in rows]), ""
  ]


def _reason(row: Mapping[str, Any]) -> str:
  detail = row.get("detail")
  reason = detail.get("reason") if isinstance(detail, Mapping) else None
  return _prose(str(reason)) if reason else "(no reason recorded)"


def _not_evaluated_section(evaluation: Evaluation) -> list[str]:
  rows = _by_status(evaluation, "not_evaluated")
  if not rows:
    return []
  reasons = Counter(_reason(r) for r in rows)
  shown = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
  lines = [
      f"## Not evaluated ({len(rows)} rows, {len(reasons)} reasons)", "",
      *_table(("rows", "reason"),
              [(count, reason) for reason, count in shown[:_REASONS_SHOWN]])
  ]
  if len(shown) > _REASONS_SHOWN:
    lines.append(f"\n… and {len(shown) - _REASONS_SHOWN} more reasons "
                 "(`--format json` has every row).")
  return [*lines, ""]


def _warnings_section(evaluation: Evaluation) -> list[str]:
  warnings = evaluation.latest.get("warnings") or []
  if not warnings:
    return []
  return [
      f"## Run warnings ({len(warnings)})", "",
      *(f"- {_prose(str(w))}" for w in warnings), ""
  ]


def render_markdown(evaluation: Evaluation,
                    catalogue: Catalogue | None = None) -> str:
  """The evaluation as a markdown report (module docstring)."""
  catalogue = catalogue or load_catalogue()
  lines = [
      *_header(evaluation),
      *_tables_section(evaluation),
      *_failing_section(evaluation, catalogue),
      *_warn_section(evaluation),
      *_not_evaluated_section(evaluation),
      *_warnings_section(evaluation),
  ]
  return "\n".join(lines).rstrip() + "\n"


def _dump(payload: Mapping[str, Any]) -> str:
  return json.dumps(
      payload, indent=2, sort_keys=True, ensure_ascii=False,
      allow_nan=False) + "\n"


def _failing_entry(metric_id: str, metric: Metric | None,
                   rows: list[dict[str, Any]]) -> dict[str, Any]:
  """One failing metric of the JSON report: the catalogue's text (every
  field None for an id the packaged catalogue lacks), the thresholds its
  rows were graded against, and its rows."""
  if metric is None:
    described: dict[str, Any] = dict.fromkeys(_DESCRIBED)
  else:
    warn, fail = _graded_against(metric, rows)
    described = {
        "title": metric.title,
        "level": metric.level,
        "family": metric.family,
        "direction": metric.direction,
        "threshold_warn": warn,
        "threshold_fail": fail,
        "purpose": _prose(metric.purpose),
        "interpretation": {
            "good": _prose(metric.interpretation.good),
            "bad": _prose(metric.interpretation.bad),
        },
        "pitfalls": _prose(metric.pitfalls),
    }
  return {"metric_id": metric_id, **described, "rows": rows}


def render_json(evaluation: Evaluation,
                catalogue: Catalogue | None = None) -> str:
  """The evaluation as one JSON document: the latest registry event,
  every event, the headline counts, each failing metric with its
  catalogue text, and every metric row."""
  catalogue = catalogue or load_catalogue()
  failing = [
      _failing_entry(metric_id, metric, rows)
      for metric_id, metric, rows in _failing_groups(evaluation, catalogue)
  ]
  return _dump({
      "evaluation_id": evaluation.evaluation_id,
      "origin": evaluation.origin,
      "status": evaluation.latest.get("status"),
      "evaluation": evaluation.latest,
      "events": evaluation.events,
      "counts": headline_counts(_measured(evaluation)),
      "failing": failing,
      "metrics": sorted(evaluation.metrics, key=_sort_key),
  })


# --------------------------------------------------------------------------
# two evaluations
# --------------------------------------------------------------------------
def _finite(value: Any) -> float | None:
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    return None
  return float(value) if math.isfinite(value) else None


def _noise(a: Mapping[str, Any], b: Mapping[str, Any],
           delta: float) -> tuple[bool | None, str]:
  """(within noise?, what it was judged against); None: no noise
  information on both rows."""
  floor_a, floor_b = _finite(a.get("noise_floor")), _finite(
      b.get("noise_floor"))
  if floor_a is not None and floor_b is not None:
    floor = math.hypot(floor_a, floor_b)
    return abs(delta) <= floor, f"floor {_num(floor)}"
  bounds = [
      _finite(row.get(key)) for row in (a, b) for key in ("ci_low", "ci_high")
  ]
  if all(bound is not None for bound in bounds):
    low_a, high_a, low_b, high_b = (float(x) for x in bounds if x is not None)
    return max(low_a, low_b) <= min(high_a, high_b), "CI overlap"
  return None, "no noise floor"


def _direction(metric: Metric | None, value_a: float, value_b: float) -> str:
  """`worse`, `better` or `changed`, by the catalogue's direction."""
  if metric is None or value_a == value_b:
    return "changed"
  if metric.direction == "lower_better":
    return "worse" if value_b > value_a else "better"
  if metric.direction == "higher_better":
    return "worse" if value_b < value_a else "better"
  if metric.target is None:
    return "changed"
  return ("worse" if abs(value_b -
                         metric.target) > abs(value_a -
                                              metric.target) else "better")


def _delta(key: RowKey, a: Mapping[str, Any], b: Mapping[str, Any],
           metric: Metric | None) -> dict[str, Any]:
  value_a, value_b = _finite(a.get("value")), _finite(b.get("value"))
  entry: dict[str, Any] = {
      "table": key[0],
      "metric_id": key[1],
      "scope": _scope(a),
      "a": value_a,
      "b": value_b,
      "status_a": a.get("status"),
      "status_b": b.get("status"),
      "delta": None,
      "noise": None,
      "verdict": "n/a",
  }
  if value_a is None or value_b is None:
    return entry
  delta = value_b - value_a
  within, basis = _noise(a, b, delta)
  entry.update(delta=delta, noise=basis)
  if delta == 0:
    entry["verdict"] = "="
  elif within:
    entry["verdict"] = WITHIN_NOISE
  else:
    entry["verdict"] = _direction(metric, value_a, value_b)
  return entry


def _metric_deltas(a: Evaluation, b: Evaluation,
                   catalogue: Catalogue) -> list[dict[str, Any]]:
  rows_a = {_row_key(r): r for r in _measured(a)}
  rows_b = {_row_key(r): r for r in _measured(b)}
  known = set(catalogue.ids())
  deltas = []
  for key in sorted(
      set(rows_a) & set(rows_b),
      key=lambda k: tuple("" if p is None else p for p in k)):
    metric = catalogue.get(key[1]) if key[1] in known else None
    deltas.append(_delta(key, rows_a[key], rows_b[key], metric))
  return deltas


def _psi_band(value: float, catalogue: Catalogue) -> str:
  known = _PSI_ID in catalogue.ids()
  warn = catalogue.get(_PSI_ID).warn if known else None
  fail = catalogue.get(_PSI_ID).fail if known else None
  if fail is not None and value >= fail:
    return "large shift"
  if warn is not None and value >= warn:
    return "moderate shift"
  return "stable"


def _histograms(
    evaluation: Evaluation
) -> dict[tuple[str, str | None, str], Mapping[str, Any]]:
  return {
      (str(p.get("table_name")), p.get("column_name"), str(p.get("side"))): p
      for p in evaluation.profiles
      if p.get("profile_kind") == _HISTOGRAM
  }


def _counts_of(profile: Mapping[str, Any]) -> list[float] | None:
  payload = profile.get("payload")
  counts = payload.get("counts") if isinstance(payload, Mapping) else None
  if not isinstance(counts, list) or not counts:
    return None
  return [float(c) for c in counts]


def _drift_entry(profile_a: Mapping[str, Any], profile_b: Mapping[str, Any],
                 catalogue: Catalogue) -> dict[str, Any]:
  digest_a, digest_b = profile_a.get("edges_digest"), profile_b.get(
      "edges_digest")
  entry: dict[str, Any] = {
      "psi": None,
      "edges_digest_a": digest_a,
      "edges_digest_b": digest_b,
  }
  counts_a, counts_b = _counts_of(profile_a), _counts_of(profile_b)
  if not digest_a or not digest_b or digest_a != digest_b:
    entry["note"] = (f"{NOT_COMPARABLE}: the bin edges differ (edges_digest "
                     f"{str(digest_a)[:8]} vs {str(digest_b)[:8]})")
  elif counts_a is None or counts_b is None or len(counts_a) != len(counts_b):
    entry["note"] = (f"{NOT_COMPARABLE}: a histogram has no counts, or the "
                     "two differ in length")
  else:
    value = psi(counts_a, counts_b)
    entry["psi"] = value
    entry["note"] = (f"{NOT_COMPARABLE}: an empty histogram"
                     if value is None else _psi_band(value, catalogue))
  return entry


def _profile_drift(a: Evaluation, b: Evaluation,
                   catalogue: Catalogue) -> dict[str, Any]:
  hist_a, hist_b = _histograms(a), _histograms(b)
  shared = sorted(
      set(hist_a) & set(hist_b),
      key=lambda k: tuple("" if p is None else p for p in k))
  rows = [{
      "table": key[0],
      "column": key[1],
      "side": key[2],
      **_drift_entry(hist_a[key], hist_b[key], catalogue)
  } for key in shared]
  return {
      "rows": rows,
      "only_a": len(set(hist_a) - set(hist_b)),
      "only_b": len(set(hist_b) - set(hist_a)),
  }


def _comparability(a: Evaluation, b: Evaluation) -> list[str]:
  first, second = a.latest, b.latest
  notes = []
  for field, why in (
      ("catalogue_version", "statuses and scores come from different "
       "thresholds"),
      ("evaluator_version", "the metric code differs"),
      ("mode", "one run read samples, the other every row"),
  ):
    if first.get(field) != second.get(field):
      notes.append(f"{field} differs ({first.get(field)} vs "
                   f"{second.get(field)}): {why}")
  if _override(first) != _override(second):
    graded = " vs ".join(
        _graded_by(row, "{uri}, digest {digest}") for row in (first, second))
    notes.append(f"thresholds differ ({graded}): statuses and scores were "
                 "graded against different thresholds")
  if first.get("generation_job_id") != second.get("generation_job_id"):
    notes.append("the two runs evaluate different generation jobs")
  elif first.get("evaluation_key") == second.get("evaluation_key"):
    notes.append("same evaluation_key: a re-run of the same evaluation")
  return notes


def compare(a: Evaluation,
            b: Evaluation,
            catalogue: Catalogue | None = None) -> dict[str, Any]:
  """The comparison of `b` against `a` (module docstring), as a
  JSON-ready document: `summary` (status, scores and counts of each),
  `notes` (what limits the comparison), `metrics` (one entry per shared
  metric row, its delta judged against noise) and `profiles` (PSI per
  shared histogram, or why it is not comparable)."""
  catalogue = catalogue or load_catalogue()
  summary = {}
  for label, evaluation in (("a", a), ("b", b)):
    row = evaluation.latest
    summary[label] = {
        "evaluation_id": evaluation.evaluation_id,
        "status": row.get("status"),
        "evaluated_at": row.get("evaluated_at"),
        "scores": {
            name: row.get(f"{name}_score") for name in _SCORES
        },
        "counts": headline_counts(_measured(evaluation)),
    }
  shared = set(map(_row_key, _measured(a))) & set(map(_row_key, _measured(b)))
  return {
      "summary": summary,
      "notes": _comparability(a, b),
      "metrics": _metric_deltas(a, b, catalogue),
      "metrics_only_a": len(_measured(a)) - len(shared),
      "metrics_only_b": len(_measured(b)) - len(shared),
      "profiles": _profile_drift(a, b, catalogue),
  }


def render_compare_json(comparison: Mapping[str, Any]) -> str:
  """`compare`'s document as JSON text."""
  return _dump(comparison)


def _transition(entry: Mapping[str, Any]) -> str:
  before, after = entry["status_a"], entry["status_b"]
  return f"{before} → {after}"


def _moved(entry: Mapping[str, Any]) -> bool:
  return (entry["verdict"] != "=" or entry["status_a"]
          != entry["status_b"]) and not (entry["verdict"] == "n/a" and
                                         entry["status_a"] == entry["status_b"])


def _compare_summary(summary: Mapping[str, Any]) -> list[str]:
  first, second = summary["a"], summary["b"]

  def delta(x: Any, y: Any) -> Any:
    return None if x is None or y is None else y - x

  rows: list[tuple[Any, ...]] = [
      ("status", first["status"], second["status"], None),
      ("evaluated at", first["evaluated_at"], second["evaluated_at"], None),
  ]
  rows += [(f"{name} score", first["scores"][name], second["scores"][name],
            delta(first["scores"][name], second["scores"][name]))
           for name in _SCORES]
  rows += [(f"metrics {key}", first["counts"][key], second["counts"][key],
            delta(first["counts"][key], second["counts"][key]))
           for key in _COUNTS]
  return _table(("", "A", "B", "B - A"), rows)


def _compare_metrics(comparison: Mapping[str, Any]) -> list[str]:
  entries = comparison["metrics"]
  moved = sorted((e for e in entries if _moved(e)),
                 key=lambda e:
                 (_STATUS_ORDER.get(str(e["status_b"]), len(_STATUS_ORDER)), e[
                     "table"], e["metric_id"], e["scope"]))
  verdicts = Counter(e["verdict"] for e in entries)
  transitions = Counter(
      _transition(e) for e in entries if e["status_a"] != e["status_b"])
  tally = ", ".join(
      f"{count} {verdict}" for verdict, count in sorted(verdicts.items()))
  lines = [
      f"## Metric deltas ({len(entries)} shared rows: {tally})", "",
      f"`{WITHIN_NOISE}` marks a delta inside the two rows' own sampling "
      "noise; identical rows are not listed.", ""
  ]
  if transitions:
    changes = ", ".join(
        f"{count} {change}" for change, count in sorted(transitions.items()))
    lines += [f"Status changes: {changes}.", ""]
  if moved:
    lines += _table(
        ("table", "metric", "scope", "A", "B", "B - A", "noise", "verdict",
         "status"),
        [(e["table"], _code(e["metric_id"]), e["scope"], e["a"], e["b"],
          e["delta"], e["noise"], e["verdict"], _transition(e)) for e in moved])
  else:
    lines.append("No shared metric row differs.")
  only = (comparison["metrics_only_a"], comparison["metrics_only_b"])
  if any(only):
    lines += [
        "", f"{only[0]} metric rows exist only in A and {only[1]} only in B "
        "(different tables, columns or pairs were planned)."
    ]
  return [*lines, ""]


def _compare_profiles(profiles: Mapping[str, Any]) -> list[str]:
  rows = profiles["rows"]
  lines = [
      f"## Profile drift, B vs A ({len(rows)} shared histograms)", "",
      "PSI between the two runs' histograms of the same column and side, "
      "only where both carry the same `edges_digest` (the same bin edges).", ""
  ]
  if rows:
    lines += _table(("table", "column", "side", "PSI", "reading"), [
        (r["table"], r["column"], r["side"], r["psi"], r["note"]) for r in rows
    ])
  else:
    lines.append("The two runs share no histogram profile.")
  only_a, only_b = profiles["only_a"], profiles["only_b"]
  if only_a or only_b:
    lines += [
        "", f"{only_a} histograms exist only in A and {only_b} only in B."
    ]
  return [*lines, ""]


def render_compare_markdown(comparison: Mapping[str, Any]) -> str:
  """`compare`'s document as markdown."""
  summary = comparison["summary"]
  first, second = (summary[k]["evaluation_id"] for k in ("a", "b"))
  lines = [
      f"# Compare `{first}` (A) → `{second}` (B)", "",
      *_compare_summary(summary), ""
  ]
  if comparison["notes"]:
    lines += [
        "## Comparability", "", *(f"- {n}" for n in comparison["notes"]), ""
    ]
  lines += _compare_metrics(comparison)
  lines += _compare_profiles(comparison["profiles"])
  return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------
# the catalogue
# --------------------------------------------------------------------------
def render_catalogue_markdown(catalogue: Catalogue | None = None) -> str:
  """The catalogue as a markdown table, one row per metric, in file
  order (the JSON form is `Catalogue.to_json`)."""
  catalogue = catalogue or load_catalogue()
  rows = [(f"`{m.id}`", m.title, m.level, m.family, ", ".join(m.kinds) or
           _DASH, m.direction, m.target, m.warn, m.fail, m.score_fn,
           m.noise_floor, m.estimator, m.version) for m in catalogue.metrics]
  levels, families = ", ".join(catalogue.levels), ", ".join(catalogue.families)
  lines = [
      f"# Metric catalogue {catalogue.version}", "",
      f"{len(catalogue.metrics)} metrics; levels: {levels}; families: "
      f"{families}.", "", *_table(
          ("id", "title", "level", "family", "kinds", "direction", "target",
           "warn", "fail", "score", "noise floor", "estimator", "version"),
          rows)
  ]
  return "\n".join(lines) + "\n"
