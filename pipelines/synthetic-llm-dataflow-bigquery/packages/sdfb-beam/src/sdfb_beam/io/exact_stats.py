"""One-scan exact source stats (``--source_stats=exact``, ADR 0022).

Tier 1 profiles the eager reference sample, which caps ``distinct`` at the
sample size and mis-weights tail mass — the five-run verdict's pool
starvation (sample distinct 95 vs source 4k) is exactly this. This module
runs ONE aggregate SELECT over the live table (the
``scripts/e2e/freetext_crosscheck._aggregate`` pattern) using BigQuery's
approximate aggregates, so cost stays a single table scan regardless of
column count:

  - ``APPROX_COUNT_DISTINCT`` — HyperLogLog++ cardinality;
  - ``APPROX_QUANTILES`` — exact-scan approximate deciles for numerics;
  - ``APPROX_TOP_COUNT`` — top literals, ONLY for enum-routed columns
    (Tier-1 distinct ≤ 50): higher-cardinality columns never leak literal
    source values into the stats table (the T4 exemplar-leak lesson).

Driver-side only; workers never import this — exact results reach engines
via ``GenerationContext.source_distinct``. Non-scalar / exotic columns
(STRUCT, REPEATED, JSON, GEOGRAPHY, BYTES) keep their Tier-1 entry.
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
  from sdfb_core.contracts.schema import TableSchema

_NUMERIC_BQ_TYPES = frozenset(
    {"INTEGER", "INT64", "FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"})
# Types SAFE_CAST(col AS STRING) accepts — the generic distinct/empty
# expressions rely on it. JSON/GEOGRAPHY/BYTES need per-type rendering and
# stay Tier 1 (documented deferral, ADR 0022).
_CASTABLE_BQ_TYPES = _NUMERIC_BQ_TYPES | frozenset(
    {"STRING", "BOOL", "BOOLEAN", "DATE", "DATETIME", "TIME", "TIMESTAMP"})
# Mirrors sdfb_core.stats.source_stats._TOP_VALUES_MAX_DISTINCT.
_TOP_VALUES_MAX_DISTINCT = 50


def _exact_columns(table_schema: TableSchema) -> list:
  return [
      c for c in table_schema.columns if c.bq_type in _CASTABLE_BQ_TYPES and
      not c.is_struct and not c.is_repeated
  ]


def build_exact_stats_sql(
    table_fqn: str,
    table_schema: TableSchema,
    tier1_stats: dict[str, dict],
    top_k: int = 8,
) -> str:
  """The single aggregate SELECT. Aliases are positional (``c0_null``…)
    so arbitrary column names never have to survive as SQL identifiers."""
  selects = ["COUNT(*) AS total_rows"]
  for i, col in enumerate(_exact_columns(table_schema)):
    c = f"`{col.name}`"
    s = f"SAFE_CAST({c} AS STRING)"
    selects.append(f"COUNTIF({c} IS NULL) AS c{i}_null")
    selects.append(f"COUNTIF(TRIM({s}) = '') AS c{i}_empty")
    selects.append(f"APPROX_COUNT_DISTINCT({s}) AS c{i}_distinct")
    if col.bq_type in _NUMERIC_BQ_TYPES:
      f64 = f"SAFE_CAST({c} AS FLOAT64)"
      selects.append(f"APPROX_QUANTILES({f64}, 10) AS c{i}_q")
      selects.append(f"AVG({f64}) AS c{i}_avg")
      selects.append(f"STDDEV_POP({f64}) AS c{i}_sd")
    tier1 = tier1_stats.get(col.name, {})
    if 0 < tier1.get("distinct", 0) <= _TOP_VALUES_MAX_DISTINCT:
      selects.append(f"TO_JSON_STRING(APPROX_TOP_COUNT({s}, {int(top_k)})) "
                     f"AS c{i}_top")
  return "SELECT " + ", ".join(selects) + f" FROM `{table_fqn}`"


def compute_exact_stats(
    table_fqn: str,
    table_schema: TableSchema,
    tier1_stats: dict[str, dict],
    *,
    client: Any = None,
    top_k: int = 8,
) -> dict[str, dict]:
  """Tier-1 stats merged with the exact aggregate pass.

    Returns a new dict: covered columns get ``stats_tier="exact"`` with
    exact ``distinct``/fractions/``source_rows`` (and exact deciles /
    moments for numerics); skipped columns and the ``__table__``
    pseudo-column keep their sample-tier entry untouched — per-entry tiers
    stay honest about what was measured how.
    """
  if client is None:  # pragma: no cover - GCP-only path
    from google.cloud import bigquery

    client = bigquery.Client()
  sql = build_exact_stats_sql(table_fqn, table_schema, tier1_stats, top_k)
  rows = list(client.query(sql).result())
  if not rows:
    return dict(tier1_stats)
  row = rows[0]
  total = int(row["total_rows"])
  merged = {name: dict(entry) for name, entry in tier1_stats.items()}
  if not total:
    return merged
  for i, col in enumerate(_exact_columns(table_schema)):
    entry = merged.get(col.name)
    if entry is None:
      continue
    nulls = int(row[f"c{i}_null"])
    distinct = int(row[f"c{i}_distinct"])
    non_null = total - nulls
    entry["source_rows"] = total
    entry["sample_rows"] = total  # fraction denominators are now exact
    entry["null_fraction"] = round(nulls / total, 6)
    entry["empty_fraction"] = round(int(row[f"c{i}_empty"]) / total, 6)
    entry["distinct"] = distinct
    entry["distinct_ratio"] = round(distinct / total, 6)
    if col.bq_type in _NUMERIC_BQ_TYPES:
      quantiles = row[f"c{i}_q"]
      if quantiles:
        entry["deciles"] = [round(float(q), 6) for q in quantiles]
        entry["min"] = entry["deciles"][0]
        entry["max"] = entry["deciles"][-1]
      avg, sd = row[f"c{i}_avg"], row[f"c{i}_sd"]
      entry["mean"] = round(float(avg), 6) if avg is not None else None
      entry["stddev"] = round(float(sd), 6) if sd is not None else None
    top_json = row.get(f"c{i}_top") if hasattr(row, "get") else None
    if top_json and non_null:
      entry["top_values"] = [[
          item["value"], round(item["count"] / non_null, 6)
      ] for item in json.loads(top_json) if item.get("value") is not None]
    entry["stats_tier"] = "exact"
  return merged


__all__ = ["build_exact_stats_sql", "compute_exact_stats"]
