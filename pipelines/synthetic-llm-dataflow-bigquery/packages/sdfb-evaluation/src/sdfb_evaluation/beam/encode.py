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
"""The numpy batch encoding every Beam accumulator consumes.

    rows of one (table, side), as the BigQuery client types them
      │  BatchElements(min_batch_size=512, max_batch_size=8192)
      ▼
    EncodeBatchFn ──► one EncodedBatch per batch

Every column is placed by its `ColumnPlan.kind` — the plan decided the
kinds from the planning scan; nothing here looks at values to re-infer
one:

    plan kind            num  float64 (n, dn)    cat  uint64 (n, dc)   text
    ───────────────────  ──────────────────────  ──────────────────  ──────
    numeric              the value               ·                   ·
    temporal             UNIX micros (R54)       ·                   ·
    boolean              0 / 1                   hash64 code         ·
    categorical          ·                       hash64 code         string
    text, identifier     ·                       ·                   string
    nested (RECORD,      ·                       hash64 of its       ·
    REPEATED, JSON, …)                           canonical JSON

`num` is NaN for NULL and for a non-finite value (the planning SQL reads
FLOAT64 NaN/±Inf as NULL too); a FLOAT64/INT64 column holding only
int/float/None converts in one numpy call, every other cell goes through
`canonical.numeric_value` (same values; text still raises). `cat` is
`NULL_CODE` for NULL; `text` is column-major (`text[j][i]` is row i of
`layout.text_columns[j]`) and holds `str` cells as they are, any other
cell as its canonical text (an INT64 id as its digits). BYTES text is
base64, so its length is ≈ 4/3 of the byte length the planner's
`AVG(LENGTH(x))` (`avg_len`) measures: compare BYTES lengths only with
other encoded lengths, never with `avg_len`. A cell is NULL when it is
`None` or, for a REPEATED column, empty — BigQuery stores a NULL array as
an empty one, and the planning scan counts `ARRAY_LENGTH(x) = 0` as its
NULL.

The row-level uint64 hashes, all over `hash64` cells (so they agree with
`canonical.hash_matrix`, the plan's dictionaries and Task 16's
canonical-value conventions — a cell reads the same whatever client
produced it):

    row_hash       Σ_j a_j · hash64(col_j, v_j) mod 2^64 over ALL plan
                   columns, a = multipliers(columns, salt) (Ruling R2)
    h_nonkey       hash64 per non-key column (R2: numeric included)
    nonkey_hash    the same sum over the non-key columns only — not a PK,
                   identity or FK column — so a copied record under fresh
                   keys still matches (NULL_CODE when every column is a
                   key: there is nothing to match on)
    pk_hash        key_hash(PK tuple, in the model's column order)
    identity_hash  key_hash(identity tuple)
    fk_hash[:, e]  key_hash(edge e's child columns): equal to the parent's
                   key_hash over `ref_cols`, whatever the column names
                   (a NULL part → NULL_CODE, MATCH SIMPLE; no key → all
                   NULL_CODE)
    null_bits      bit j set ⇔ plan column j is NULL, for the first 64
                   columns; a wider table is flagged (`layout.
                   null_bits_complete` False, a warning) and its
                   null-pattern metric is not evaluated — the catalogue's
                   documented cap for `row.null_pattern_tvd`
    subsample_m    hash64(salt, row_hash) < p · 2^64: a Bernoulli(p)
                   draw with p = m / n, m = min(n_source, n_synthetic)
                   (`matched_rate`) — the matched-n subsample of the
                   n-dependent diversity metrics (D5)

A row's hashes and its subsample draw depend on its values and the salt
alone, never on the batch, bundle, worker or run. The draw is keyed on
the whole row, so exact duplicate rows are kept or dropped together: at
p < 1 the subsample thins distinct rows, not copies (a cluster sample of
duplicates). Ruling R59 bounds where that matters: `subsample_m` feeds
ONLY the n-dependent diversity metrics (entropy, distinct counts at
matched n); the internal-duplicate metrics (`row.internal_duplicate_rate`
/ `_excess`) are computed on the full data, never on the subsample, so a
duplicate rate never depends on the sampler. Whole-row duplicates (keys
included) arise only in key-less tables, where keeping them together
widens the diversity noise slightly — a documented, conservative bias.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import apache_beam as beam
import numpy as np

from sdfb_evaluation.canonical import (
    NULL_CODE,
    canonical_value,
    hash64,
    linear_hash,
    multipliers,
    numeric_value,
)
from sdfb_evaluation.types import ColumnKind, Side

if TYPE_CHECKING:
  from sdfb_evaluation.context.plan import TablePlan

__all__ = [
    "KEY_TUPLE_LABEL",
    "MAX_BATCH_SIZE",
    "MIN_BATCH_SIZE",
    "NULL_BITS_MAX_COLUMNS",
    "BatchEncoder",
    "BatchLayout",
    "EncodeBatchFn",
    "EncodeSide",
    "EncodedBatch",
    "cell_text",
    "key_hash",
    "key_hashes",
    "matched_rate",
    "subsample_flags",
]

MIN_BATCH_SIZE = 512
MAX_BATCH_SIZE = 8192
NULL_BITS_MAX_COLUMNS = 64  # one uint64 word per row
# The `column` a key tuple is hashed under: a constant, so an FK tuple and
# the parent tuple it references hash alike whatever their column names.
KEY_TUPLE_LABEL = "sdfb:key-tuple"

_NUM_KINDS = frozenset(
    {ColumnKind.NUMERIC, ColumnKind.TEMPORAL, ColumnKind.BOOLEAN})
_CAT_KINDS = frozenset(
    {ColumnKind.CATEGORICAL, ColumnKind.BOOLEAN, ColumnKind.NESTED})
_TEXT_KINDS = frozenset(
    {ColumnKind.CATEGORICAL, ColumnKind.TEXT, ColumnKind.IDENTIFIER})
_MATCHED_SIDES = frozenset({Side.SOURCE, Side.SYNTHETIC})
# Columns whose cells are int/float/None from both read paths: numpy
# converts them in one call (`_fast_numeric`).
_FAST_NUMERIC_TYPES = frozenset({"FLOAT64", "FLOAT", "INT64", "INTEGER"})
_FAST_CELL_TYPES = frozenset({int, float, type(None)})
_TWO_64 = 2**64
_LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# key tuples and the subsample
# --------------------------------------------------------------------------
def key_hash(values: Sequence[Any]) -> int:
  """The uint64 hash of one key tuple, in the model's column order.

  `hash64(KEY_TUPLE_LABEL, [v1, v2, …])`: canonical values, no column
  names, so an FK tuple hashes exactly like the parent tuple it
  references. Any NULL part — or no key at all — is `NULL_CODE` (MATCH
  SIMPLE: such a tuple references nothing).
  """
  if not values or any(v is None for v in values):
    return NULL_CODE
  return hash64(KEY_TUPLE_LABEL, list(values))


def _memo_key(values: Sequence[Any]) -> tuple[tuple[type, Any], ...]:
  """A dict key that tells `True`, `1` and `1.0` apart (their canonical
  forms differ) while equal values of one type share an entry."""
  return tuple((type(v), v) for v in values)


def key_hashes(rows: Sequence[Mapping[str, Any]],
               columns: Sequence[str]) -> np.ndarray:
  """`key_hash` of every row's `columns` tuple, as uint64 `(len(rows),)`.

  For a parent table this is the hash an FK edge's child tuples match,
  over the edge's `ref_cols` (in `ref_cols` order) — equal to `pk_hash`
  when `ref_cols` is the PK in its own order.
  """
  if not columns:
    return np.full(len(rows), NULL_CODE, dtype=np.uint64)
  memo: dict[Any, int] = {}
  out = []
  for row in rows:
    values = [row[c] for c in columns]
    try:
      key = _memo_key(values)
      code = memo.get(key)
    except TypeError:  # an unhashable part (never a BigQuery key type)
      out.append(key_hash(values))
      continue
    if code is None:
      code = memo[key] = key_hash(values)
    out.append(code)
  return np.array(out, dtype=np.uint64)


def subsample_flags(row_hash: np.ndarray, salt: str, rate: float) -> np.ndarray:
  """The matched-n Bernoulli(`rate`) draw of each row: `hash64(salt,
  row_hash) < rate · 2^64`, as a bool array (all True at `rate >= 1`)."""
  n = len(row_hash)
  if rate >= 1.0:
    return np.ones(n, dtype=bool)
  if rate <= 0.0:
    return np.zeros(n, dtype=bool)
  limit = int(rate * _TWO_64)
  return np.array([hash64(salt, h) < limit for h in row_hash.tolist()],
                  dtype=bool)


def matched_rate(table: TablePlan, side: Side | str) -> float:
  """`p = m / n` of one side: `m = min(n_source, n_synthetic)` rows read
  (`TablePlan.rows_read`, sampling applied), so the larger side is thinned
  to the smaller.

      the R/H panel sides                  1.0  already n rows each
      a count unknown (None) on either     1.0  NOTE: no matched n can be
        side (an external parent, a             formed, so every row is
        skipped table)                          kept; the n-dependent
                                                metrics are then not at
                                                matched n
      this side empty (0 rows)             1.0  nothing to thin
      the other side empty (0 rows)        0.0  no matched n exists (m = 0)
      the smaller side                     1.0
      the larger side                      m / n
  """
  side = Side(side)
  if side not in _MATCHED_SIDES:
    return 1.0
  if table.rows_source is None or table.rows_synthetic is None:
    return 1.0
  n_source, n_synthetic = table.rows_read
  n = n_source if side is Side.SOURCE else n_synthetic
  if n <= 0:
    return 1.0
  m = min(n_source, n_synthetic)
  return 1.0 if m >= n else m / n


def _checked_rate(rate: float) -> float:
  rate = float(rate)
  if not 0.0 <= rate <= 1.0:  # NaN fails too
    raise ValueError(f"subsample_rate {rate!r}: expected a probability in "
                     "[0, 1] (m / n)")
  return rate


# --------------------------------------------------------------------------
# layout and batch
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class BatchLayout:  # pylint: disable=too-many-instance-attributes  # one field per block of the batch
  """Where each plan column lives in an `EncodedBatch` (names only, so it
  is cheap to ship with every batch). Built from the plan alone
  (`from_table`), so every consumer derives the same layout."""
  table: str
  columns: tuple[str, ...]
  kinds: tuple[ColumnKind, ...]
  bq_types: tuple[str, ...]
  repeated: tuple[bool, ...]
  num_columns: tuple[str, ...]
  cat_columns: tuple[str, ...]
  text_columns: tuple[str, ...]
  nonkey_columns: tuple[str, ...]
  pk: tuple[str, ...]
  identity: tuple[str, ...]
  fk: tuple[tuple[str, ...], ...]
  edge_labels: tuple[str, ...]

  @property
  def null_bits_columns(self) -> int:
    """How many plan columns (the first ones) `null_bits` covers."""
    return min(len(self.columns), NULL_BITS_MAX_COLUMNS)

  @property
  def null_bits_complete(self) -> bool:
    """`null_bits` covers every column (at most 64); when False the
    null-pattern metric is not evaluated."""
    return len(self.columns) <= NULL_BITS_MAX_COLUMNS

  @classmethod
  def from_table(cls, table: TablePlan) -> BatchLayout:
    """The layout of `table`'s plan columns, in plan order.

    Raises:
      ValueError: a PK, identity or FK column is not among the plan's
        columns (the key hashes could not be computed).
    """
    columns = tuple(c.name for c in table.columns)
    kinds = tuple(ColumnKind(c.kind) for c in table.columns)
    fk = tuple(tuple(e.cols) for e in table.edges)
    keys = {c.name for c in table.columns if c.is_key}
    keys |= set(table.pk) | set(table.identity)
    keys |= {c for cols in fk for c in cols}
    missing = sorted(keys - set(columns))
    if missing:
      raise ValueError(
          f"{table.landing_table}: key column(s) {missing} are not among the "
          "plan's columns (only columns both sides hold are planned), so "
          "their key hashes cannot be computed")

    def placed(kinds_in: frozenset[ColumnKind]) -> tuple[str, ...]:
      return tuple(
          n for n, k in zip(columns, kinds, strict=True) if k in kinds_in)

    return cls(
        table=table.name,
        columns=columns,
        kinds=kinds,
        bq_types=tuple(c.bq_type for c in table.columns),
        repeated=tuple(c.mode == "REPEATED" for c in table.columns),
        num_columns=placed(_NUM_KINDS),
        cat_columns=placed(_CAT_KINDS),
        text_columns=placed(_TEXT_KINDS),
        nonkey_columns=tuple(n for n in columns if n not in keys),
        pk=tuple(table.pk),
        identity=tuple(table.identity),
        fk=fk,
        edge_labels=tuple(e.label(table.name) for e in table.edges))


@dataclass(frozen=True, eq=False)
class EncodedBatch:  # pylint: disable=too-many-instance-attributes  # the interface's blocks, one field each
  """One batch of one (table, side), encoded (module docstring).

  Arrays are row-aligned (`n` rows); `layout` names their columns.
  Equality is identity (`eq=False`: arrays have no truth value).
  """
  table: str
  side: Side
  n: int
  num: np.ndarray  # float64 (n, dn), NaN = NULL or non-finite
  cat: np.ndarray  # uint64 (n, dc), NULL_CODE = NULL
  text: list[list[str | None]]  # column-major: text[j][i]
  row_hash: np.ndarray  # uint64 (n,)
  nonkey_hash: np.ndarray  # uint64 (n,)
  pk_hash: np.ndarray  # uint64 (n,)
  identity_hash: np.ndarray  # uint64 (n,)
  fk_hash: np.ndarray  # uint64 (n, E)
  null_bits: np.ndarray  # uint64 (n,), first 64 columns
  subsample_m: np.ndarray  # bool (n,)
  h_nonkey: np.ndarray  # uint64 (n, d_nonkey), Ruling R2
  layout: BatchLayout


# --------------------------------------------------------------------------
# the encoder
# --------------------------------------------------------------------------
def _is_null(value: Any, repeated: bool) -> bool:
  return value is None or (repeated and isinstance(value,
                                                   (list, tuple)) and not value)


def cell_text(value: Any) -> str | None:
  """A cell as the text block holds it: `str` as it is, NULL as None, any
  other value as its canonical text (an INT64 id as its digits, BYTES as
  base64). Shared with the census so both read a cell identically."""
  if value is None or isinstance(value, str):
    return value
  canonical = canonical_value(value)
  return None if canonical is None else str(canonical)


class BatchEncoder:
  """Encodes batches of one (table, side) — pure numpy, no Beam, so it is
  testable on its own and `EncodeBatchFn` only wraps it.

  Raises (from `encode`):
    ValueError: a row lacks a plan column (a NULL is `None`, never an
      absent key), or a numeric/temporal/boolean cell has no numeric
      reading (the message names the column and the type, never the
      value).
  """

  def __init__(self,
               layout: BatchLayout,
               side: Side | str,
               *,
               salt: str,
               subsample_rate: float = 1.0):
    self.layout = layout
    self.side = Side(side)
    self.salt = salt
    self.subsample_rate = _checked_rate(subsample_rate)
    position = {name: j for j, name in enumerate(layout.columns)}
    self._num_idx = [position[c] for c in layout.num_columns]
    self._cat_idx = [position[c] for c in layout.cat_columns]
    self._text_idx = [position[c] for c in layout.text_columns]
    self._nonkey_idx = [position[c] for c in layout.nonkey_columns]
    self._wanted = frozenset(layout.columns)
    self._a_all = multipliers(layout.columns, salt)
    self._a_nonkey = multipliers(layout.nonkey_columns, salt)
    if not layout.null_bits_complete:
      _LOGGER.warning(
          "%s: %d columns; null_bits covers the first %d only, so "
          "row.null_pattern_tvd is not evaluated for this table", layout.table,
          len(layout.columns), NULL_BITS_MAX_COLUMNS)

  @classmethod
  def from_table(cls,
                 table: TablePlan,
                 side: Side | str,
                 *,
                 salt: str,
                 subsample_rate: float | None = None) -> BatchEncoder:
    """The encoder of `table`'s `side`; `subsample_rate` defaults to the
    plan's matched-n rate (`matched_rate`)."""
    rate = matched_rate(table,
                        side) if subsample_rate is None else subsample_rate
    return cls(
        BatchLayout.from_table(table), side, salt=salt, subsample_rate=rate)

  def encode(self, rows: Sequence[Mapping[str, Any]]) -> EncodedBatch:
    """One `EncodedBatch` of `rows` (module docstring)."""
    layout = self.layout
    self._check(rows)
    n = len(rows)
    values = [[row[name] for row in rows] for name in layout.columns]
    nulls = [[_is_null(v, rep)
              for v in column]
             for column, rep in zip(values, layout.repeated, strict=True)]
    h = np.empty((n, len(layout.columns)), dtype=np.uint64)
    for j, name in enumerate(layout.columns):
      h[:, j] = self._hash_column(name, values[j], nulls[j])
    row_hash = linear_hash(h, self._a_all)
    h_nonkey = h[:, self._nonkey_idx]
    fk = np.empty((n, len(layout.fk)), dtype=np.uint64)
    for e, cols in enumerate(layout.fk):
      fk[:, e] = key_hashes(rows, cols)
    return EncodedBatch(
        table=layout.table,
        side=self.side,
        n=n,
        num=self._numeric(values, n),
        cat=h[:, self._cat_idx],
        text=[[cell_text(v) for v in values[j]] for j in self._text_idx],
        row_hash=row_hash,
        nonkey_hash=self._nonkey_hash(h_nonkey),
        pk_hash=key_hashes(rows, layout.pk),
        identity_hash=key_hashes(rows, layout.identity),
        fk_hash=fk,
        null_bits=self._null_bits(nulls, n),
        subsample_m=subsample_flags(row_hash, self.salt, self.subsample_rate),
        h_nonkey=h_nonkey,
        layout=layout)

  def _check(self, rows: Sequence[Mapping[str, Any]]) -> None:
    for row in rows:
      if not row.keys() >= self._wanted:
        missing = sorted(self._wanted - row.keys())
        raise ValueError(
            f"{self.layout.table} ({self.side}): a row has no {missing} — "
            "every row must carry every plan column (a NULL is None, never "
            "an absent key)")

  @staticmethod
  def _hash_column(name: str, values: Sequence[Any],
                   nulls: Sequence[bool]) -> np.ndarray:
    """`hash64(name, v)` per cell, NULL → `NULL_CODE`, memoised per
    distinct value within the batch (a column repeats values a lot)."""
    memo: dict[Any, int] = {}
    out = []
    for value, null in zip(values, nulls, strict=True):
      if null:
        out.append(NULL_CODE)
        continue
      try:
        key = (type(value), value)
        code = memo.get(key)
      except TypeError:  # unhashable nested value: no memo
        out.append(hash64(name, value))
        continue
      if code is None:
        code = memo[key] = hash64(name, value)
      out.append(code)
    return np.array(out, dtype=np.uint64)

  def _numeric(self, values: Sequence[Sequence[Any]], n: int) -> np.ndarray:
    num = np.full((n, len(self._num_idx)), np.nan, dtype=np.float64)
    for k, j in enumerate(self._num_idx):
      fast = self._fast_numeric(j, values[j])
      if fast is not None:
        num[:, k] = fast
        continue
      name, kind = self.layout.columns[j], self.layout.kinds[j]
      column = num[:, k]
      for i, value in enumerate(values[j]):
        if value is None:
          continue
        reading = numeric_value(value)
        if reading is None:
          raise ValueError(
              f"{self.layout.table} ({self.side}): {kind} column {name!r} "
              f"holds a {type(value).__name__} value with no numeric reading")
        if math.isfinite(reading):
          column[i] = reading
    return num

  def _fast_numeric(self, j: int, values: Sequence[Any]) -> np.ndarray | None:
    """Column j in one numpy call, or None for the per-cell path: only a
    FLOAT64/INT64 column whose cells are all int/float/None (numpy would
    otherwise parse text and read a bool as a number). `float()` of each
    cell, None → NaN, non-finite → NaN: exactly the per-cell values."""
    if self.layout.bq_types[j] not in _FAST_NUMERIC_TYPES:
      return None
    if not set(map(type, values)) <= _FAST_CELL_TYPES:
      return None
    column = np.array(values, dtype=np.float64)
    column[~np.isfinite(column)] = np.nan
    return column

  def _nonkey_hash(self, h_nonkey: np.ndarray) -> np.ndarray:
    if not self.layout.nonkey_columns:  # an all-key table: no content hash
      return np.full(len(h_nonkey), NULL_CODE, dtype=np.uint64)
    return linear_hash(h_nonkey, self._a_nonkey)

  def _null_bits(self, nulls: Sequence[Sequence[bool]], n: int) -> np.ndarray:
    bits = np.zeros(n, dtype=np.uint64)
    for j in range(self.layout.null_bits_columns):
      mask = np.array(nulls[j], dtype=np.uint64)
      bits |= mask << np.uint64(j)
    return bits


# --------------------------------------------------------------------------
# Beam
# --------------------------------------------------------------------------
class EncodeBatchFn(beam.DoFn):
  """`process(rows: list[dict]) -> EncodedBatch`, after `BatchElements`.

  Only the slim layout, side, salt and rate are pickled with the DoFn
  (never the plan's grids or its R/H panel rows); the encoder is built in
  `setup()`, once per DoFn instance on each worker. An empty batch yields
  nothing (there is no row to drop).
  """

  def __init__(self,
               table: TablePlan,
               side: Side | str,
               *,
               salt: str,
               subsample_rate: float | None = None):
    super().__init__()
    self._layout = BatchLayout.from_table(table)
    self._side = Side(side)
    self._salt = salt
    rate = matched_rate(table,
                        side) if subsample_rate is None else subsample_rate
    self._rate = _checked_rate(rate)
    self._encoder: BatchEncoder | None = None

  def setup(self) -> None:
    self._encoder = BatchEncoder(
        self._layout, self._side, salt=self._salt, subsample_rate=self._rate)

  # pylint: disable-next=arguments-renamed  # Beam passes the element positionally
  def process(self, rows: Sequence[Mapping[str,
                                           Any]]) -> Iterator[EncodedBatch]:
    if not rows:
      return
    if self._encoder is None:
      raise RuntimeError("EncodeBatchFn.process before setup(): Beam calls "
                         "setup() first; call it yourself outside Beam")
    yield self._encoder.encode(rows)


class EncodeSide(beam.PTransform):
  """`PCollection[dict] → PCollection[EncodedBatch]` for one (table, side):
  `BatchElements(min_batch_size=512, max_batch_size=8192)` then
  `ParDo(EncodeBatchFn)`, labelled by landing table and side so every
  table and side can be encoded in one pipeline."""

  def __init__(self,
               table: TablePlan,
               side: Side | str,
               *,
               salt: str,
               subsample_rate: float | None = None):
    side = Side(side)
    super().__init__(f"Encode[{table.landing_table}/{side}]")
    self._fn = EncodeBatchFn(
        table, side, salt=salt, subsample_rate=subsample_rate)

  def expand(self, input_or_inputs: beam.PCollection) -> beam.PCollection:
    batches: beam.PCollection = (
        input_or_inputs
        | "Batch" >> beam.BatchElements(
            min_batch_size=MIN_BATCH_SIZE, max_batch_size=MAX_BATCH_SIZE)
        | "Encode" >> beam.ParDo(self._fn))
    return batches
