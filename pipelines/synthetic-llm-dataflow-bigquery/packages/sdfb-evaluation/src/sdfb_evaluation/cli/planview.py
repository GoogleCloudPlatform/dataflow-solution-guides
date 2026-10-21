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
"""What `sdfb-eval plan` prints: an `EvaluationPlan` as a document
(`describe_plan`, the `--format json` output) and as text
(`render_plan_text`).

Per table: its role, the landing and source tables, the scope (mode,
status, what is read), the rows each side holds and the sampling rates,
the reference panel, and per column the methods the pipeline will use —
its kind, how its value census runs (exact, or value-sampled at a rate
of the value-hash space), whether it is binned on the source's quantile
grid, and whether its values are stored literally (D6) or as hashed
labels. Then the BigQuery bytes the dry runs counted against
`--max_bytes_billed`, the predicted shuffle against `--max_shuffle_gb`,
the DDL planning already ran (Ruling R57) and the DDL a run would
execute first.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from sdfb_evaluation.context.plan import ColumnPlan, EvaluationPlan, TablePlan
from sdfb_evaluation.types import ColumnKind

__all__ = ["describe_plan", "render_plan_text"]

_NAME_WIDTH = 28
_TYPE_WIDTH = 10
_KIND_WIDTH = 12


def _method(column: ColumnPlan) -> str:
  """How the pipeline measures one column, in words."""
  if column.kind is ColumnKind.NESTED:
    return "null counts only"
  parts = []
  if column.census == "exact":
    parts.append("census exact")
  elif column.census == "value_sampled":
    parts.append(f"census value-sampled at {column.value_sample_rate:.3g}")
  else:
    parts.append("no census")
  if column.is_key:
    parts.append("key (uniqueness and integrity)")
  if column.quantiles_src:
    grid = f"binned on the {len(column.quantiles_src)}-point source grid"
    if column.atoms:
      grid += f" + {len(column.atoms)} atoms"
    parts.append(grid)
  if column.day_granularity:
    parts.append("day granularity")
  if column.dictionary is not None:
    parts.append("literal values" if column.literal_ok else "hashed labels")
  return "; ".join(parts)


def _column(column: ColumnPlan) -> dict[str, Any]:
  return {
      "name": column.name,
      "bq_type": column.bq_type,
      "kind": str(column.kind),
      "is_key": column.is_key,
      "census": column.census,
      "value_sample_rate": column.value_sample_rate,
      "literal_ok": column.literal_ok,
      "source_distinct": column.source_distinct,
      "synthetic_distinct": column.synthetic_distinct,
      "method": _method(column),
  }


def _table(table: TablePlan, unplanned: Collection[str]) -> dict[str, Any]:
  scope, panel, pin = table.scope, table.panel, table.source_pin
  names = [c.name for c in table.columns]
  paired = sorted({i for pair in table.pairs for i in pair})
  return {
      "name":
          table.name,
      "role":
          table.role,
      "landing_table":
          table.landing_table,
      "source_table":
          table.source_table,
      "run_id":
          table.run_id,
      "evaluated":
          table.evaluated,
      "skip_reason":
          table.skip_reason,
      "unplanned":
          table.landing_table in unplanned,
      "scope": {
          "mode": scope.mode,
          "status": scope.status,
          "reason": scope.reason,
          "read_table": scope.read_table,
      },
      "source": {
          "read_table": table.source_read_table,
          "pinned": table.source_pinned,
          "as_of": pin.as_of if pin is not None else None,
      },
      "rows_source":
          table.rows_source,
      "rows_synthetic":
          table.rows_synthetic,
      "rows_expected":
          scope.expected_rows,
      "sample_rate_source":
          table.sample_rate_source,
      "sample_rate_synthetic":
          table.sample_rate_synthetic,
      "panel":
          None if panel is None else {
              "reference_n": len(panel.r_rows),
              "holdout_n": len(panel.h_rows),
              "exposure_n": panel.e_n,
              "verified": panel.verified,
              "reason": panel.reason,
          },
      "columns": [_column(c) for c in table.columns],
      "pairs":
          len(table.pairs),
      "pair_columns": [names[i] for i in paired],
      "edges": [edge.label(table.name) for edge in table.edges],
      "encoding_plan_digest":
          table.encoding_plan_digest,
  }


def describe_plan(
    plan: EvaluationPlan, unplanned: Collection[str] = ()) -> dict[str, Any]:
  """`plan` as a JSON-ready document (module docstring); `unplanned`
  are the landing tables `--no_planning_snapshots` left without a
  scope."""
  launch = plan.launch
  return {
      "evaluation_id": plan.evaluation_id,
      "evaluation_key": plan.evaluation_key,
      "evaluated_at": plan.evaluated_at,
      "mode": plan.mode,
      "runner": plan.runner,
      "trigger": plan.trigger,
      "launch": {
          "generation_job_id": launch.generation_job_id,
          "job_name": launch.job_name,
          "region": launch.region,
          "base_run_id": launch.base_run_id,
          "run_ids": list(launch.run_ids),
          "params_source": launch.params_source,
          "relationship_model": launch.model_name,
          "relationships_uri": launch.relationships_uri,
          "write_disposition": launch.write_disposition,
      },
      "bq_bytes_estimate": plan.bq_bytes_estimate,
      "max_bytes_billed": plan.knobs.max_bytes_billed,
      "predicted_shuffle_gb": plan.predicted_shuffle_gb,
      "max_shuffle_gb": plan.knobs.max_shuffle_gb,
      "skip_reason": plan.skip_reason,
      "tables": [_table(t, unplanned) for t in plan.tables],
      "planning_ddl": [s.sql for s in plan.planning_ddl],
      "prepare_sql": [s.sql for s in plan.prepare_sql],
      "warnings": list(plan.warnings),
  }


def _rate(value: Any) -> str:
  return "—" if value is None else f"{float(value):.4g}"


def _count(value: Any) -> str:
  return "—" if value is None else f"{int(value):,}"


def _table_lines(table: Mapping[str, Any]) -> list[str]:
  name, role = table["name"], table["role"]
  landing, source = table["landing_table"], table["source_table"] or "—"
  lines = [f"  {name}  [{role}]  {landing}  ←  {source}"]
  scope = table["scope"]
  if role == "external":
    lines.append("    read-only parent: read as it is now, for the integrity "
                 "joins of its children only")
  elif table["unplanned"]:
    lines.append("    UNPLANNED: --no_planning_snapshots, and this "
                 "as_of_diff scope reads through a start snapshot")
  elif not table["evaluated"]:
    reason = table["skip_reason"]
    lines.append(f"    NOT EVALUATED: {reason}")
  mode, status = scope["mode"], scope["status"]
  read = scope["read_table"] or "nothing"
  lines.append(f"    scope {mode} / {status}: reads {read}")
  if scope["reason"] and table["evaluated"]:
    reason = scope["reason"]
    lines.append(f"      {reason}")
  if not table["evaluated"]:
    return lines
  src, syn, expected = (
      _count(table[k])
      for k in ("rows_source", "rows_synthetic", "rows_expected"))
  rate_src, rate_syn = (
      _rate(table[k]) for k in ("sample_rate_source", "sample_rate_synthetic"))
  pinned = "pinned" if table["source"]["pinned"] else "unpinned"
  lines.append(f"    rows: source {src} ({pinned}, sampled at {rate_src}), "
               f"synthetic {syn} (expected {expected}, sampled at {rate_syn})")
  lines.append(_panel_line(table["panel"]))
  lines.append("    columns:")
  for column in table["columns"]:
    column_name, bq_type = column["name"], column["bq_type"]
    kind, method = column["kind"], column["method"]
    lines.append(f"      {column_name:<{_NAME_WIDTH}} "
                 f"{bq_type:<{_TYPE_WIDTH}} {kind:<{_KIND_WIDTH}} {method}")
  if table["pairs"]:
    pairs, among = table["pairs"], ", ".join(table["pair_columns"])
    lines.append(f"    pairs: {pairs} among {among}")
  lines.extend(f"    edge: {edge}" for edge in table["edges"])
  return lines


def _panel_line(panel: Mapping[str, Any] | None) -> str:
  if panel is None:
    return ("    reference panel: none (the reference-based privacy metrics "
            "are not evaluated)")
  r_n, h_n, e_n = panel["reference_n"], panel["holdout_n"], panel["exposure_n"]
  verified = "verified" if panel["verified"] else "NOT verified"
  reason = panel["reason"]
  why = f" — {reason}" if reason else ""
  return (f"    reference panel: R {r_n:,}, H {h_n:,}, E {e_n:,}; "
          f"{verified}{why}")


def render_plan_text(document: Mapping[str, Any]) -> str:
  """`describe_plan`'s document as the text `sdfb-eval plan` prints."""
  launch = document["launch"]
  evaluation_id, mode = document["evaluation_id"], document["mode"]
  runner = document["runner"]
  job = launch["generation_job_id"] or "none (manual launch)"
  source, model = launch["params_source"], launch["relationship_model"] or "—"
  estimate, cap = document["bq_bytes_estimate"], document["max_bytes_billed"]
  shuffle, shuffle_cap = (document["predicted_shuffle_gb"],
                          document["max_shuffle_gb"])
  lines = [
      f"evaluation plan {evaluation_id} (planned only, nothing was run)",
      f"  mode {mode}, runner {runner}",
      f"  generation job: {job}; parameters from {source}; relationship "
      f"model: {model}",
      f"  BigQuery dry-run bytes: {estimate:,} (cap --max_bytes_billed "
      f"{cap:,})",
      f"  predicted shuffle: {shuffle:.3f} GB (cap --max_shuffle_gb "
      f"{shuffle_cap:g})", "", "tables:"
  ]
  for table in document["tables"]:
    lines.extend(_table_lines(table))
  if document["skip_reason"]:
    lines += [
        "", "nothing can be evaluated: a run of this plan writes a SKIPPED "
        "registry row and no metric"
    ]
  if document["planning_ddl"]:
    lines += [
        "", "planning already created (zero bytes billed, expires in 24 h; "
        "--no_planning_snapshots plans without them):"
    ]
    lines.extend(f"  {sql.splitlines()[0]}" for sql in document["planning_ddl"])
  if document["prepare_sql"]:
    count = len(document["prepare_sql"])
    lines += ["", f"a run would first execute {count} DDL statement(s):"]
    lines.extend(f"  {sql.splitlines()[0]}" for sql in document["prepare_sql"])
  if document["warnings"]:
    lines += ["", "warnings:"]
    lines.extend(f"  - {warning}" for warning in document["warnings"])
  return "\n".join(lines) + "\n"
