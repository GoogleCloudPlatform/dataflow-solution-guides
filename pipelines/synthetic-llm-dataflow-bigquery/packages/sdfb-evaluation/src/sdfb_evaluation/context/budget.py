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
"""What an evaluation may spend: BigQuery bytes and Beam shuffle.

Two budgets, two different enforcement points:

    budget            enforced by                       when exceeded
    ────────────────  ────────────────────────────────  ─────────────────────────
    max_bytes_billed  the planner, from dry runs         refused (raises) before
                      (planning SQL, panel, prepare;     anything is billed;
                      sampled: every side whose table    re-checked once the
                      holds > sample_rows rows counts    planning counts pick
                      as a full sample read)             the sides to sample
    max_shuffle_gb    the census method per column       high-cardinality columns
                      (exact | value_sampled)            switch to value sampling

The shuffle prediction (GB = 10^9 bytes), per evaluated table and side,
on the rows the pipeline actually reads:

    census       Σ census columns  (head + (keys - head)  keys = min(distinct,
                                   * rate) * 24 B         rows), both sides;
                                                          head = the census
                                                          head (R67), never
                                                          sampled
    masks        Σ text/identifier keys * (24 B + 1.5 *   the shape-mask pass:
                   census columns   avg_len)              never value-sampled
                                                          (R67), at most one
                                                          key per value, the
                                                          mask's UTF-8 text in
                                                          each key (R70); a
                                                          column averaging over
                                                          256 chars pools into
                                                          one `<long>` key a
                                                          side (R75)
    relational   Σ edges           child rows * 16 B     (hash, count) per row
    membership   codes * (MEMBERSHIP_CODE_BYTES +        the membership pass's
                   UTF-8 bytes of the table name)        exact keyed counts:
                                                         the non-key hash on
                                                         both sides (content),
                                                         the PK and identity
                                                         hashes on the
                                                         synthetic side, and
                                                         the row hash on both
                                                         sides when a keyed
                                                         table's source sets
                                                         exceed the side-input
                                                         cap (keyed-count mode)
    null bits    min(rows, 4096) * 16 B                  bounded pattern dict

`MEMBERSHIP_CODE_BYTES` is measured, not assumed: a membership code
crosses the shuffle as 8 B of uint64 plus its count packed to the
smallest unsigned width, inside bundle-merged elements whose per-element
overhead is spread over the bundle's codes of a bucket (FastPrimitivesCoder:
9.0-9.5 B a code with 64 or more rows a bucket in a bundle; 12.4 B for a
lone 8192-row batch at the 10-bit bucket maximum). Every element also
repeats its key `(table, kind, bucket)`, whose table name costs its
UTF-8 bytes L, so a code is budgeted at `MEMBERSHIP_CODE_BYTES + L`
(Ruling R76, R79): an AMORTISED ESTIMATE, generous for the usual bundle
(hundreds of codes an element), not an upper bound — an element that
carries a single code (a tiny bundle spread over many buckets) costs
about 30 + L bytes.

The census gets what the fixed parts leave, shared max-min fairly
(`water_fill`): first across tables, then across one table's census
columns, so a column needing little is never sampled to feed one needing
much, and a column expecting at most 1 000 keys is exact whatever is
left. A column granted less than its demand is value-sampled at `K /
10 000` (`VALUE_SAMPLE_MODULUS`): its census head enters with certainty
and, of the other values, only hashes with `hash mod M < K`, which keeps
every retained value's count exact. The mask pass cannot be sampled, so
the planner counts it with the fixed shuffle (`mask_bytes`).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  from sdfb_evaluation.context.plan import ColumnPlan, TablePlan
  from sdfb_evaluation.context.relationships import Edge

__all__ = [
    "CENSUS_KEY_BYTES",
    "GB",
    "MASK_KEY_OVERHEAD",
    "MASK_UTF8_FACTOR",
    "MEMBERSHIP_CODE_BYTES",
    "SOURCE_SET_MAX_BYTES",
    "VALUE_SAMPLE_MODULUS",
    "Budget",
    "BudgetExceededError",
    "census_bytes",
    "census_demand",
    "fixed_shuffle_bytes",
    "mask_bytes",
    "mask_key_bytes",
    "membership_bytes",
    "membership_code_bytes",
    "predict_shuffle_gb",
    "source_set_arrays",
    "source_sets_fit",
    "table_key_shape",
    "value_census_bytes",
    "value_sample_rate",
    "water_fill",
]

GB = 1e9
CENSUS_KEY_BYTES = 24  # (table, column, value hash) key + four counts
# A mask key: (table, column, mask hash) + two counts, then the mask's text,
# which is as long as the value (up to 256 characters; longer ones pool into
# one `<long>` key). Its UTF-8 size per character is an estimate: class
# placeholders and ASCII punctuation are 1 byte, the space mark `␣` 3.
MASK_KEY_OVERHEAD = 24
MASK_UTF8_FACTOR = 1.5
MASK_MAX_CHARS = 256
MASK_DEFAULT_CHARS = 16  # no planned length (an INT64 identity's digits)
_MASKED_KINDS = frozenset({"text", "identifier"})
ROW_KEY_BYTES = 16  # (uint64 hash, count)
# One membership keyed-count code as shuffled (module docstring: measured
# 9.0-9.5 B with FastPrimitivesCoder over bundle-merged, packed elements)
MEMBERSHIP_CODE_BYTES = 12
# The full-source side input of the membership pass, all its sorted uint64
# arrays together (8 B a source row and array): above it, keyed counts
SOURCE_SET_MAX_BYTES = 160_000_000
NULL_PATTERN_LIMIT = 4096  # the dense null-pattern dict's key cap
VALUE_SAMPLE_MODULUS = 10_000


@dataclass(frozen=True)
class Budget:
  """The evaluation's spending caps (`--max_shuffle_gb`,
  `--max_bytes_billed`)."""
  max_shuffle_gb: float
  max_bytes_billed: int

  def __post_init__(self) -> None:
    if (isinstance(self.max_shuffle_gb, bool) or
        not isinstance(self.max_shuffle_gb, (int, float)) or
        math.isnan(self.max_shuffle_gb) or self.max_shuffle_gb < 0):
      raise ValueError(f"max_shuffle_gb: expected a number >= 0, got "
                       f"{self.max_shuffle_gb!r}")
    if (isinstance(self.max_bytes_billed, bool) or
        not isinstance(self.max_bytes_billed, int) or
        self.max_bytes_billed <= 0):
      raise ValueError(f"max_bytes_billed: expected a positive int, got "
                       f"{self.max_bytes_billed!r}")

  def check_bytes(self, parts: Mapping[str, int]) -> int:
    """The total of `parts` (bytes per kind of query), refused above
    `max_bytes_billed`.

    Raises:
      BudgetExceededError: the message carries every part, the total and
        the cap.
    """
    total = sum(parts.values())
    if total > self.max_bytes_billed:
      detail = ", ".join(f"{k} {v}" for k, v in parts.items())
      raise BudgetExceededError(
          f"planning would process {total} bytes ({total / 2**30:.2f} GiB: "
          f"{detail}), over max_bytes_billed = {self.max_bytes_billed} "
          f"bytes. Raise --max_bytes_billed, or narrow the evaluation (fewer "
          f"tables, --mode sampled).")
    return total


class BudgetExceededError(ValueError):
  """The dry-run bytes exceed `Budget.max_bytes_billed`."""


def water_fill(demands: Sequence[float], capacity: float) -> list[float]:
  """Max-min fair shares of `capacity`: every demand below the running
  fair share is met in full, the rest split what remains equally. Ties
  keep input order, so the result is deterministic."""
  shares = [0.0] * len(demands)
  remaining = max(float(capacity), 0.0)
  left = len(demands)
  for index in sorted(range(len(demands)), key=lambda i: demands[i]):
    give = min(float(demands[index]), remaining / left)
    shares[index] = give
    remaining -= give
    left -= 1
  return shares


def census_demand(column: ColumnPlan, rows_source: float,
                  rows_synthetic: float) -> float:
  """Keys an exact census of `column` shuffles: its distinct values on
  each side, never more than that side's rows (a missing count is taken
  as all rows distinct)."""

  def side(distinct: int | None, rows: float) -> float:
    return float(rows if distinct is None else min(distinct, rows))

  return (side(column.source_distinct, rows_source) +
          side(column.synthetic_distinct, rows_synthetic))


def value_sample_rate(granted: float, demand: float) -> float | None:
  """None when `granted` covers `demand` (exact census); otherwise the
  largest `K / VALUE_SAMPLE_MODULUS` rate within the grant, at least
  `1 / VALUE_SAMPLE_MODULUS`."""
  if demand <= granted:
    return None
  keep = math.floor(VALUE_SAMPLE_MODULUS * granted / demand)
  return max(keep, 1) / VALUE_SAMPLE_MODULUS


def table_key_shape(columns: Iterable[ColumnPlan], pk: Iterable[str],
                    identity: Iterable[str],
                    edges: Iterable[Edge]) -> tuple[bool, bool]:
  """(the table has a non-key column, the table has a key column), with
  the key set `beam.encode.BatchLayout` uses: `is_key` columns, the PK,
  the identity columns and every FK column."""
  names = [column.name for column in columns]
  keys = {column.name for column in columns if column.is_key}
  keys |= set(pk) | set(identity)
  keys |= {c for edge in edges for c in edge.cols}
  return any(name not in keys for name in names), bool(keys)


def source_set_arrays(*, nonkey: bool, keyed: bool) -> int:
  """How many sorted arrays the full-source side input holds: the record
  hashes (the non-key hash, or the row hash of an all-key table), plus the
  row hashes when a keyed table also has content."""
  return 1 + int(nonkey and keyed)


def source_sets_fit(rows_source: float | None,
                    *,
                    nonkey: bool,
                    keyed: bool,
                    max_bytes: int = SOURCE_SET_MAX_BYTES) -> bool:
  """Whether the membership pass reads the full source as a side input
  (8 B a source row and array, within `max_bytes`) rather than through
  the keyed count; an unknown source count never fits."""
  if rows_source is None:
    return False
  arrays = source_set_arrays(nonkey=nonkey, keyed=keyed)
  return rows_source * 8 * arrays <= max_bytes


def membership_code_bytes(table: str) -> int:
  """The budgeted shuffle bytes of one keyed-count code of `table`: the
  measured code plus the UTF-8 bytes of the key's table name, repeated
  per element — an amortised estimate (module docstring)."""
  return MEMBERSHIP_CODE_BYTES + len(table.encode("utf-8"))


def membership_bytes(*, rows_source: float, rows_synthetic: float, nonkey: bool,
                     keyed: bool, keyed_counts: int, side_input: bool,
                     table: str) -> float:
  """The membership pass's keyed-count shuffle (module docstring)."""
  both = rows_source + rows_synthetic
  codes = both if nonkey else 0.0
  codes += keyed_counts * rows_synthetic
  if keyed and not side_input:
    codes += both
  return codes * membership_code_bytes(table)


def fixed_shuffle_bytes(*,
                        rows_source: float,
                        rows_synthetic: float,
                        edges: int,
                        keyed_counts: int,
                        nonkey: bool = True,
                        keyed: bool = True,
                        side_input: bool = True,
                        table: str) -> float:
  """The non-census shuffle of one table: relational child rows per edge,
  the membership pass's keyed counts and the null-pattern dicts (see the
  module docstring). `nonkey`/`keyed` are `table_key_shape`, `side_input`
  is `source_sets_fit` (the keyed-count mode adds the row counts), `table`
  the table's name (required: its UTF-8 bytes are in every keyed-count
  key)."""
  both = rows_source + rows_synthetic
  relational = edges * both * ROW_KEY_BYTES
  row_keys = membership_bytes(
      rows_source=rows_source,
      rows_synthetic=rows_synthetic,
      nonkey=nonkey,
      keyed=keyed,
      keyed_counts=keyed_counts,
      side_input=side_input,
      table=table)
  null_bits = (min(rows_source, NULL_PATTERN_LIMIT) +
               min(rows_synthetic, NULL_PATTERN_LIMIT)) * ROW_KEY_BYTES
  return relational + row_keys + null_bits


def value_census_bytes(columns: Iterable[ColumnPlan], rows_source: float,
                       rows_synthetic: float) -> float:
  """The value census's shuffle, value sampling applied: a column's head
  (R67) always enters, the rest at its rate."""
  total = 0.0
  for column in columns:
    if column.census == "none":
      continue
    rate = column.value_sample_rate or 1.0  # set only when value-sampled
    keys = census_demand(column, rows_source, rows_synthetic)
    head = min(float(len(column.census_head or ())), keys)
    total += (head + (keys - head) * rate) * CENSUS_KEY_BYTES
  return total


_LONG_MASK_KEY_CHARS = len("<long>")
_SIDES = 2


def _pooled_long(column: ColumnPlan) -> bool:
  """Values averaging over `MASK_MAX_CHARS` characters: their masks pool
  into the one `<long>` key per column (the census's count-time pooling),
  so the column costs a key a side, not a key per distinct value."""
  return column.avg_len is not None and column.avg_len > MASK_MAX_CHARS


def mask_key_bytes(column: ColumnPlan) -> float:
  """One mask key's bytes for `column`: the overhead plus its mask text,
  from the planner's AVG(LENGTH(x)) (R70); the short `<long>` label for a
  pooled column (R75)."""
  if _pooled_long(column):
    return MASK_KEY_OVERHEAD + MASK_UTF8_FACTOR * _LONG_MASK_KEY_CHARS
  length = column.avg_len if column.avg_len is not None else MASK_DEFAULT_CHARS
  return MASK_KEY_OVERHEAD + MASK_UTF8_FACTOR * length


def mask_bytes(columns: Iterable[ColumnPlan], rows_source: float,
               rows_synthetic: float) -> float:
  """The shape-mask pass's shuffle: every text/identifier census column,
  never value-sampled, at most one mask key per distinct value (a
  near-unique column — short prose, UUID-like ids — costs about its value
  census again; row-sampling the synthetic mask side is future work). A
  column whose values average over 256 characters is charged one pooled
  `<long>` key a side (R75). Only avg_len is planned, so this is an
  estimate either way: a column averaging below 256 is charged a key per
  distinct value (over, when some of its values are long), one averaging
  above as pooled (under, when some of its values are short)."""
  total = 0.0
  for column in columns:
    if column.census == "none" or str(column.kind) not in _MASKED_KINDS:
      continue
    keys = (
        _SIDES if _pooled_long(column) else census_demand(
            column, rows_source, rows_synthetic))
    total += keys * mask_key_bytes(column)
  return total


def census_bytes(columns: Iterable[ColumnPlan], rows_source: float,
                 rows_synthetic: float) -> float:
  """The census shuffle of one table's columns: the value census (value
  sampling applied) plus the mask pass."""
  columns = list(columns)
  return (value_census_bytes(columns, rows_source, rows_synthetic) +
          mask_bytes(columns, rows_source, rows_synthetic))


def predict_shuffle_gb(tables: Sequence[TablePlan]) -> float:
  """Predicted Beam shuffle of evaluating `tables`, in GB (10^9 bytes):
  the value census (head unsampled, the rest at its rate, 24 B a key) +
  the shape-mask pass (text/identifier columns, never sampled; a key's
  bytes grow with the planned value length, `mask_key_bytes`) +
  relational child rows * 16 B + row-hash counts + null patterns, over
  the rows each side actually reads. Read-only and skipped tables shuffle
  nothing of their own."""
  total = 0.0
  for table in tables:
    if not table.evaluated:
      continue
    rows_source, rows_synthetic = table.rows_read
    nonkey, keyed = table_key_shape(table.columns, table.pk, table.identity,
                                    table.edges)
    total += census_bytes(table.columns, rows_source, rows_synthetic)
    total += fixed_shuffle_bytes(
        rows_source=rows_source,
        rows_synthetic=rows_synthetic,
        edges=len(table.edges),
        keyed_counts=int(bool(table.pk)) + int(bool(table.identity)),
        nonkey=nonkey,
        keyed=keyed,
        side_input=source_sets_fit(
            rows_source if table.rows_source is not None else None,
            nonkey=nonkey,
            keyed=keyed),
        table=table.name)
  return total / GB
