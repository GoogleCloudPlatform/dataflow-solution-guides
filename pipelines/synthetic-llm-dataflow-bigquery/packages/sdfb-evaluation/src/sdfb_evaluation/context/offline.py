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
"""Planning without BigQuery: a `TablePlan` from rows held in memory.

`context.plan.build_plan` plans from ONE aggregate SELECT per table side
(its module docstring lists the statistics). With the rows in memory —
the hidden `--fixture_dir` of the CLI, and the test suites' tables — the
same statistics are computed here, exactly instead of approximately, and
handed to the planner's own pure functions (`kinds_from_schema`,
`apply_planning`, `select_pairs`, `encoding_plan_digest`), so kinds,
grids, atoms, dictionaries, census methods and pairs are the planner's:

    planning SELECT                         here (`planning_stats`)
    ──────────────────────────────────────  ──────────────────────────────
    COUNTIF(x IS NULL), COUNTIF(TRIM = '')  counted
    APPROX_COUNT_DISTINCT(x)                exact distinct canonical values
    APPROX_QUANTILES(v, 1000), AVG,         numpy on the planning scale
      STDDEV_POP, MIN, MAX                  (`canonical.numeric_value`),
                                            quantiles by inverted CDF
    APPROX_TOP_COUNT(x, k)                  exact counts, NULL a value,
                                            ties by canonical value
    COUNTIF(TIME(x) = 00:00:00)             counted

    what `build_plan` resolves              here (`table_plan`)
    ──────────────────────────────────────  ──────────────────────────────
    the scope of the job's landing rows     the rows as they are: mode
                                            `table`, status ok
    the source pin                          none: the source rows as given
    the R/H panel (D3: the generator's      `panel_of`: R = the first n
      FARM_FINGERPRINT order)               source rows, H the next n —
                                            the caller's rows are in an
                                            order of no meaning — verified
                                            against its own digest
    sampling                                none: both sides whole

Nothing here reads, bills or creates anything.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, time
from typing import Any

import numpy as np

from sdfb_evaluation.canonical import canonical_value, numeric_value
from sdfb_evaluation.context.budget import Budget
from sdfb_evaluation.context.plan import (
    ATOM_TOP_K,
    BOOL_TYPES,
    DICTIONARY_TOP_K,
    GRID_POINTS,
    NESTED_TYPES,
    NUMERIC_TYPES,
    STRING_TYPES,
    TEMPORAL_TYPES,
    TablePlan,
    apply_planning,
    encoding_plan_digest,
    kinds_from_schema,
    select_pairs,
)
from sdfb_evaluation.context import reference
from sdfb_evaluation.context.reference import EXPOSURE_ROWS, Panel
from sdfb_evaluation.context.relationships import Edge
from sdfb_evaluation.context.scope import ScopePlan

__all__ = ["panel_of", "planning_stats", "table_plan"]

_GRID = np.linspace(0.0, 1.0, GRID_POINTS)


def _key(value: Any) -> str:
  return repr(canonical_value(value))


def _top(values: Sequence[Any], k: int) -> list[tuple[Any, int]]:
  """APPROX_TOP_COUNT, exact: most frequent first, NULL counted."""
  counts = Counter(_key(v) for v in values)
  first: dict[str, Any] = {}
  for value in values:
    first.setdefault(_key(value), value)
  ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
  return [(first[key], count) for key, count in ranked]


def _planning_scale(value: Any) -> float | None:
  reading = numeric_value(value)
  if reading is None or not math.isfinite(reading):
    return None
  return reading


def _numeric_stats(values: Sequence[Any]) -> dict[str, Any]:
  scaled = [_planning_scale(v) for v in values]
  finite = np.array([v for v in scaled if v is not None], dtype=float)
  stats: dict[str, Any] = {}
  if finite.size:
    stats.update(
        quantiles=np.quantile(finite, _GRID, method="inverted_cdf").tolist(),
        mean=float(finite.mean()),
        std=float(finite.std()),
        min=float(finite.min()),
        max=float(finite.max()))
  stats["top"] = _top(scaled, ATOM_TOP_K)
  return stats


def _column_stats(field: Mapping[str, Any], values: Sequence[Any],
                  is_key: bool) -> dict[str, Any]:
  bq_type = str(field.get("type") or "").upper()
  repeated = str(field.get("mode") or "").upper() == "REPEATED"
  if repeated or bq_type in NESTED_TYPES:  # the planner counts only NULLs
    return {"null": sum(1 for v in values if v is None or v == [])}
  present = [v for v in values if v is not None]
  stats: dict[str, Any] = {
      "null": len(values) - len(present),
      "distinct": len({_key(v) for v in present}),
  }
  if bq_type == "STRING":
    stats["empty"] = sum(1 for v in present if v.strip() == "")
  elif bq_type == "BYTES":
    stats["empty"] = sum(1 for v in present if len(v) == 0)
  if is_key:
    return stats
  if bq_type in NUMERIC_TYPES | TEMPORAL_TYPES:
    stats.update(_numeric_stats(values))
    if bq_type in ("TIMESTAMP", "DATETIME"):
      stats["midnight"] = sum(
          1 for v in present if isinstance(v, datetime) and v.time() == time(0))
  elif bq_type in BOOL_TYPES:
    stats["top"] = _top(values, DICTIONARY_TOP_K)
  elif bq_type in STRING_TYPES:
    stats["avg_len"] = (
        sum(len(v) for v in present) / len(present) if present else None)
    stats["top"] = _top(values, DICTIONARY_TOP_K)
  return stats


def planning_stats(
    fields: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    keys: frozenset[str] = frozenset()
) -> dict[str, Any]:
  """`context.plan.parse_planning`'s shape for `rows` (`{"rows": n,
  "columns": {name: {stat: value}}}`), computed exactly in Python
  (module docstring). A `keys` column stops after its distinct count,
  as the planning SELECT does; a row that lacks a column holds NULL."""
  return {
      "rows": len(rows),
      "columns": {
          str(f["name"]):
              _column_stats(f, [r.get(f["name"]) for r in rows],
                            str(f["name"]) in keys) for f in fields
      },
  }


def panel_of(rows: Sequence[Mapping[str, Any]], n: int) -> Panel:
  """R = the first `n` of `rows`, H the next `n`, E and H_E their first
  1,024 (D3), verified against R's own digest (module docstring)."""
  r_rows = [dict(row) for row in rows[:n]]
  h_rows = [dict(row) for row in rows[n:2 * n]]
  digest = reference.reference_digest(r_rows)
  return Panel(
      r_rows=r_rows,
      h_rows=h_rows,
      e_n=min(len(r_rows), EXPOSURE_ROWS),
      he_n=min(len(h_rows), EXPOSURE_ROWS),
      digest=digest,
      verified=True,
      expected_digest=digest)


def table_plan(name: str,
               fields: Sequence[Mapping[str, Any]],
               source_rows: Sequence[Mapping[str, Any]],
               synthetic_rows: Sequence[Mapping[str, Any]],
               *,
               landing_table: str,
               source_table: str,
               budget: Budget,
               pk: Sequence[str] = (),
               identity: Sequence[str] = (),
               edges: Sequence[Edge] = (),
               role: str = "isolated",
               panel: Panel | None = None,
               pair_max_columns: int = 20,
               run_id: str | None = None,
               model: str | None = None,
               reference_digest: str | None = None,
               scope_reason: str | None = None) -> TablePlan:
  """The `TablePlan` of one launch table whose two sides are
  `source_rows` and `synthetic_rows` (module docstring): planned from
  both sides' exact statistics, read whole and unpinned, its scope the
  landing rows as they are, expecting `len(synthetic_rows)` of them.
  `panel` is the R/H panel to carry (`panel_of`), if any."""
  keys = frozenset(pk) | {c for e in edges for c in e.cols}
  columns = apply_planning(
      kinds_from_schema(fields, keys=keys, identity=identity),
      planning_stats(fields, source_rows, keys),
      planning_stats(fields, synthetic_rows, keys),
      budget=budget)
  pairs = select_pairs(columns, pair_max_columns)
  return TablePlan(
      name=name,
      landing_table=landing_table,
      source_table=source_table,
      run_id=run_id,
      role=role,
      pk=tuple(pk),
      identity=tuple(identity),
      edges=tuple(edges),
      columns=tuple(columns),
      scope=ScopePlan(
          landing_table=landing_table,
          mode="table",
          status="ok",
          reason=scope_reason,
          read_table=landing_table,
          prepare_sql=(),
          window=(None, None),
          expected_rows=len(synthetic_rows),
          read_expr=f"`{landing_table}`"),
      source_read_table=source_table,
      source_pinned=False,
      panel=panel,
      pairs=pairs,
      encoding_plan_digest=encoding_plan_digest(columns, pairs, edges),
      model=model,
      synthetic_read_table=landing_table,
      rows_source=len(source_rows),
      rows_synthetic=len(synthetic_rows),
      sample_rate_source=1.0,
      sample_rate_synthetic=1.0,
      reference_digest=reference_digest)
