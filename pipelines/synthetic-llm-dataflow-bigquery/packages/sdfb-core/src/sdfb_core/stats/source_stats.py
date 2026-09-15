"""Per-column source-table stats from the reference sample.

One computation, four consumers (2026-08-05 spec, WS-B): a compact
``source_table_stats`` milestone, a GCS JSON artifact, BQ rows via
``stats_rows``, and human reports. Engines do NOT read these — they profile
per worker from the same rows; this module is the driver/human view, so it
must never disagree with the profilers on definitions:

  - ``null_fraction``  — fraction of rows whose value is None;
  - ``empty_fraction`` — fraction of rows whose value is a string that is
    empty after ``.strip()`` (the crosscheck's trimmed-empty definition);
  - ``distinct``       — distinct NON-EMPTY, non-null values.

Performance contract (ADR 0022): one pass builds the per-column ``Counter``
(distinct, entropy, skew all read it), numerics sort once (min/max/deciles
share the sort), temporal values parse ONCE (v1 parsed twice via
``_day_granularity``), and the null-pattern mix is a single extra pass over
the rows. Everything is stdlib — this module stays importable without
numpy/Beam/GCP.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from math import log2
from typing import TYPE_CHECKING

from sdfb_core.engines.text_shapes import build_shape_mix

if TYPE_CHECKING:  # pragma: no cover - typing only
  from sdfb_core.contracts.relationships import TableRelations
  from sdfb_core.contracts.schema import TableSchema

_NUMERIC_BQ_TYPES = frozenset(
    {"INTEGER", "INT64", "FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"})
_TEMPORAL_BQ_TYPES = frozenset({"DATE", "DATETIME", "TIMESTAMP"})
_SHAPE_MIX_TOP_K = 8
_LEN_PCTS = (0.05, 0.50, 0.95)

# Part of the stats-table append-skip key: the reference digest hashes ROWS,
# not this module, so a profiler upgrade must bump this or already-profiled
# tables keep stale stats forever (ADR 0022).
PROFILER_VERSION = "2"

# Literal source values land in stats ONLY for enum-routed columns (the T4
# exemplar-leak lesson): above this distinct count, a column gets shapes /
# entropy / lengths — never values. Mirrors the engines'
# _FREE_TEXT_MAX_CATEGORIES so "enum-routed" means the same thing everywhere.
_TOP_VALUES_MAX_DISTINCT = 50
_TOP_VALUES_TOP_K = 8

# Row-level null co-occurrence: per-row is-null bitstrings, top-K patterns.
# Skipped above the column cap — the bitstring alphabet grows with width and
# the pass is O(rows x cols).
_NULL_PATTERN_MAX_COLS = 64
_NULL_PATTERN_TOP_K = 8

# The `__table__` pseudo-column carrying table-level stats (null-pattern
# mix). Not a source column: `in_source_schema` is False and consumers that
# iterate columns must treat the key as reserved.
TABLE_PSEUDO_COLUMN = "__table__"


def _is_empty_str(v: object) -> bool:
  return isinstance(v, str) and not v.strip()


def _mask(v: str) -> str:
  return "".join("9" if ch.isdigit() else "A" if ch.isupper() else "a" if ch
                 .islower() else ch for ch in v)


def _to_float(v: object) -> float | None:
  try:
    return float(v)  # type: ignore[arg-type]
  except (TypeError, ValueError):
    return None


def _parse_dt(s: str) -> datetime | None:
  """ISO-ish string → UTC-aware datetime (naive values pin to UTC so
    min/max/future comparisons never mix aware and naive)."""
  try:
    dt = datetime.fromisoformat(s.strip().replace("T", " "))
  except ValueError:
    return None
  return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _parse_temporals(strings: list[str], *, sniff: bool) -> list[datetime]:
  """Every parseable value, parsed exactly once.

    ``sniff`` (non-temporal BQ types): bail after 64 values when none parse,
    so arbitrary STRING columns never pay a full-parse pass.
    """
  if sniff and not any(_parse_dt(s) is not None for s in strings[:64]):
    return []
  return [d for d in (_parse_dt(s) for s in strings) if d is not None]


def profile_source_table(
    table_schema: TableSchema,
    reference_rows: list[dict],
    relations: TableRelations | None = None,
    generation_plan: dict[str, str] | None = None,
) -> dict[str, dict]:
  """name → stats dict for every top-level column of the schema, plus a
    ``__table__`` pseudo-column with table-level stats (null-pattern mix)."""
  pk = set(relations.pk) if relations else set()
  identity = set(relations.identity) if relations else set()
  fk_cols: set[str] = set()
  if relations:
    for fk in relations.fk:
      fk_cols.update(fk.cols)
  plan = generation_plan or {}
  n = len(reference_rows)

  stats: dict[str, dict] = {}
  for col in table_schema.columns:
    values = [r.get(col.name) for r in reference_rows]
    non_null = [v for v in values if v is not None]
    empties = sum(1 for v in non_null if _is_empty_str(v))
    substantive = [v for v in non_null if not _is_empty_str(v)]
    strings = [str(v) for v in substantive]
    counter = Counter(strings)
    distinct = len(counter)

    entry: dict = {
        "type": str(col.bq_type),
        "in_source_schema": True,
        "sample_rows": n,
        "null_fraction": round((n - len(non_null)) / n, 6) if n else 0.0,
        "empty_fraction": round(empties / n, 6) if n else 0.0,
        "zero_fraction": 0.0,
        "distinct": distinct,
        "distinct_ratio": round(distinct / n, 6) if n else 0.0,
        "is_constant": distinct <= 1 and not empties and len(non_null) == n,
        "is_pk": col.name in pk,
        "is_fk": col.name in fk_cols,
        "identity_col": col.name in identity,
        "min": None,
        "max": None,
        "mean": None,
        "stddev": None,
        "deciles": [],
        "entropy": None,
        "entropy_norm": None,
        "top1_share": None,
        "top_values": [],
        "len_p05": None,
        "len_p50": None,
        "len_p95": None,
        "mean_len": None,
        "shape_mix": [],
        "temporal_day_granularity": False,
        "temporal_min": None,
        "temporal_max": None,
        "dow_mix": [],
        "hour_mix": [],
        "month_mix": [],
        "future_fraction": None,
        "generation_plan": plan.get(col.name, ""),
        "stats_tier": "sample",
        "profiler_version": PROFILER_VERSION,
    }

    _add_value_mix(entry, counter, len(strings))

    if col.bq_type in _NUMERIC_BQ_TYPES:
      _add_numeric(entry, substantive)
    elif strings:
      lengths = sorted(len(s) for s in strings)
      last = len(lengths) - 1
      for p, key in zip(
          _LEN_PCTS, ("len_p05", "len_p50", "len_p95"), strict=True):
        entry[key] = lengths[int(p * last)]
      entry["mean_len"] = round(sum(lengths) / len(lengths), 2)
      parsed = _parse_temporals(
          strings, sniff=col.bq_type not in _TEMPORAL_BQ_TYPES)
      if parsed:
        _add_temporal(entry, parsed)
      shapes = build_shape_mix(strings, top_k=_SHAPE_MIX_TOP_K)
      if shapes:
        total = sum(w for w, _ in shapes)
        entry["shape_mix"] = [[_mask_of_template(shape),
                               round(w / total, 4)] for w, shape in shapes]

    stats[col.name] = entry

  if n and 0 < len(table_schema.columns) <= _NULL_PATTERN_MAX_COLS:
    stats[TABLE_PSEUDO_COLUMN] = _null_pattern_entry(
        [c.name for c in table_schema.columns], reference_rows)
  return stats


def _add_value_mix(entry: dict, counter: Counter, total: int) -> None:
  """Skew + information content from the one Counter every column builds.

    ``entropy`` is Shannon entropy in bits; ``entropy_norm`` divides by
    log2(distinct) → 1.0 = uniform over the observed support, →0 = one value
    dominates. Together with ``top1_share`` this is what "skewed copy
    pattern" means for WS-A frequency-weighted FK sampling, and the
    mode-collapse detector for validation (a synthetic column whose entropy
    is far below source collapsed even if its distinct count looks healthy).
    """
  if not total:
    return
  entropy = -sum((c / total) * log2(c / total) for c in counter.values())
  entry["entropy"] = round(entropy, 4)
  entry["entropy_norm"] = (
      round(entropy / log2(len(counter)), 4) if len(counter) > 1 else 0.0)
  entry["top1_share"] = round(counter.most_common(1)[0][1] / total, 6)
  if len(counter) <= _TOP_VALUES_MAX_DISTINCT:
    entry["top_values"] = [[v, round(c / total, 6)]
                           for v, c in counter.most_common(_TOP_VALUES_TOP_K)]


def _add_numeric(entry: dict, substantive: list[object]) -> None:
  """min/max/deciles from ONE sort; mean/stddev in one accumulation pass.

    Deciles are the 11-point empirical quantile vector (p0..p100) that the
    B.2 inverse-CDF sampler mirrors worker-side — persisting them here lets
    validation compare source vs landing marginals without re-scanning.
    """
  numbers = [f for v in substantive if (f := _to_float(v)) is not None]
  if not numbers:
    return
  ordered = sorted(numbers)
  count = len(ordered)
  entry["min"] = ordered[0]
  entry["max"] = ordered[-1]
  entry["zero_fraction"] = round(sum(1 for f in ordered if f == 0.0) / count, 6)
  mean = sum(ordered) / count
  entry["mean"] = round(mean, 6)
  variance = sum((f - mean)**2 for f in ordered) / count
  entry["stddev"] = round(variance**0.5, 6)
  entry["deciles"] = [
      round(ordered[round(i * (count - 1) / 10)], 6) for i in range(11)
  ]


def _add_temporal(entry: dict, parsed: list[datetime]) -> None:
  """Temporal shape from the single-parse list: observed range, calendar
    mixes, and how much of the column lies in the future.

    ``hour_mix`` stays empty at day granularity (all-midnight is noise, not
    shape). Engines re-derive their jitter ranges worker-side; these fields
    are the drift/report view plus the M2 seed for bucket-aware sampling.
    """
  total = len(parsed)
  day_granularity = all(
      (d.hour, d.minute, d.second, d.microsecond) == (0, 0, 0, 0)
      for d in parsed)
  entry["temporal_day_granularity"] = day_granularity
  lo, hi = min(parsed), max(parsed)
  entry["temporal_min"] = lo.date().isoformat(
  ) if day_granularity else lo.isoformat()
  entry["temporal_max"] = hi.date().isoformat(
  ) if day_granularity else hi.isoformat()
  dow: list[int] = [0] * 7
  months: list[int] = [0] * 12
  hours: list[int] = [0] * 24
  for d in parsed:
    dow[d.weekday()] += 1
    months[d.month - 1] += 1
    hours[d.hour] += 1
  entry["dow_mix"] = [round(c / total, 4) for c in dow]
  entry["month_mix"] = [round(c / total, 4) for c in months]
  entry["hour_mix"] = ([] if day_granularity else
                       [round(c / total, 4) for c in hours])
  now = datetime.now(UTC)
  entry["future_fraction"] = round(sum(1 for d in parsed if d > now) / total, 6)


def _null_pattern_entry(names: list[str], reference_rows: list[dict]) -> dict:
  """Row-level null co-occurrence: per-row is-null bitstring, top-K mix.

    Per-column ``null_fraction`` cannot say which columns are null TOGETHER
    (optional blocks null as a unit in real data); sampling row patterns
    instead of independent per-column coin flips is the one joint-fidelity
    stat cheap enough for M1 (ADR 0022 — correlations/copulas stay M2).
    """
  n = len(reference_rows)
  patterns = Counter("".join("1" if r.get(name) is None else "0"
                             for name in names)
                     for r in reference_rows)
  return {
      "type": "",
      "in_source_schema": False,
      "sample_rows": n,
      "null_fraction": 0.0,
      "empty_fraction": 0.0,
      "distinct": len(patterns),
      "distinct_ratio": round(len(patterns) / n, 6),
      "is_pk": False,
      "is_fk": False,
      "identity_col": False,
      "generation_plan": "",
      "null_pattern_columns": names,
      "null_pattern_mix": [[bits, round(
          c / n, 6)] for bits, c in patterns.most_common(_NULL_PATTERN_TOP_K)],
      "stats_tier": "sample",
      "profiler_version": PROFILER_VERSION,
  }


def _mask_of_template(shape: tuple[str, ...]) -> str:
  """Render a per-position template back into a 9/A/a mask string.

    A class position maps to the mask of its dominant character family;
    a literal position masks like a plain character.
    """
  out = []
  for entry in shape:
    if len(entry) == 1:
      out.append(_mask(entry))
    elif all(c.isdigit() for c in entry):
      out.append("9")
    elif entry.islower():
      out.append("a")
    else:
      out.append("A")
  return "".join(out)


def stats_rows(table_fqn: str, reference_digest: str, run_id: str,
               stats: dict[str, dict]) -> list[dict]:
  """Flatten per-column stats into BQ-loadable rows.

    Headline numerics are real columns; the full entry rides in ``stats``
    as JSON so the table schema never chases this module's field list.
    """
  computed_at = datetime.now(tz=UTC).isoformat()
  rows = []
  for column, entry in sorted(stats.items()):
    rows.append({
        "table_fqn": table_fqn,
        "reference_digest": reference_digest,
        "run_id": run_id,
        "column": column,
        "generation_plan": entry.get("generation_plan", ""),
        "null_fraction": entry["null_fraction"],
        "empty_fraction": entry["empty_fraction"],
        "distinct": entry["distinct"],
        "distinct_ratio": entry["distinct_ratio"],
        "is_pk": entry["is_pk"],
        "is_fk": entry["is_fk"],
        "stats": json.dumps(entry, default=str, sort_keys=True),
        "sample_rows": entry.get("sample_rows"),
        "stats_tier": entry.get("stats_tier", "sample"),
        "profiler_version": entry.get("profiler_version", PROFILER_VERSION),
        "computed_at": computed_at,
    })
  return rows


__all__ = [
    "PROFILER_VERSION",
    "TABLE_PSEUDO_COLUMN",
    "profile_source_table",
    "stats_rows",
]
