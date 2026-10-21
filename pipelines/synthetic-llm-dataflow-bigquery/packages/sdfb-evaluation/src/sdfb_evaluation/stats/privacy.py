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
"""Row-level privacy maths: exact Gower nearest neighbours, the holdout DCR
test, NNDR, and k-NN density/coverage, feeding `row.dcr_train_holdout_share`,
`row.dcr_p5_ratio`, `row.nndr_p5_ratio`, `row.density` and `row.coverage`
(`src/sdfb_evaluation/catalogue/metrics.yaml`).

The feature space (`GowerSpace`) comes from the evaluation plan, never from
the rows being compared, so R, H and every synthetic batch are encoded
identically on every worker:

  - numeric and temporal columns map through the SOURCE quantile grid's
    mid-CDF probability integral transform to `u` in `[0, 1]` (the Parzen
    mid-distribution convention of Rulings R16/R24: a value on a run of
    identical grid points maps to the middle of that run's probability
    range), so a numeric feature's Gower range normalisation is the source's
    own rank scale — robust to outliers, and tie-aware in the spirit of
    Podani's (1999) ordinal extension of Gower's coefficient;
  - categorical, boolean, text and identifier columns map to `hash64` codes;
    two cells match only on the identical canonical value, so distinct rare
    values stay distance 1 apart (no top-K / "other" collapse);
  - key columns (and nested ones) are left out of the space by the plan.

The Gower (1971) distance between two rows is the mean of per-feature
dissimilarities over all `d` features: `|u_q - u_r|` for a numeric feature,
`1[code_q != code_r]` for a categorical one, and for either kind a NULL
against a NULL is 0 and a NULL against a value is 1 (so every feature always
counts; the denominator is `d` for every pair).

`gower_knn` is exact brute force: it streams query chunks against the whole
reference set, accumulating per-feature dissimilarities into one float32
`(chunk, |R|)` plane, then selects the `k` smallest per row with
`np.partition`, ties broken by the lower reference index. No tree index is
used; Gower's mixed metric does not suit one and the reference sets are
small (10k rows). Query rows are independent, so the result for any row is
bit-identical whatever the chunk size or the other rows in its chunk — Beam
can split the synthetic sample across workers and concatenate.

The holdout DCR test follows Platzer & Reutterer (2021): R (the rows the
generator saw) and an equal-size, exchangeable holdout H drawn from the same
source; a synthetic row is "closer to R" when its nearest-R distance is
below its nearest-H distance, ties counting one half. A generator that does
not memorise sits at a share of 0.5 whatever the data's density. NNDR
(nearest over second-nearest distance to R) follows the singling-out
reading of Giomi et al. (2023). Density and coverage are Naeem et al.
(2020), k-NN balls around the real rows, at equal sample sizes.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, cast

import numpy as np

from sdfb_evaluation.canonical import hash_matrix, numeric_value
from sdfb_evaluation.stats.binned import bin_counts
from sdfb_evaluation.stats.noise import wilson_interval

# Bumped whenever the encoding changes meaning (grid convention, null codes,
# distance), so a cached encoding keyed by `GowerSpace.digest` cannot be
# reused across incompatible versions. /2: temporal cells read as UNIX
# microseconds, the plan grids' scale (Ruling R54), no longer epoch seconds.
_DIGEST_VERSION = b"sdfb-gower/2 mid-cdf-pit hash64 unix-micros"

# Upper bound for one chunk's working set, in bytes (64 MB).
_MAX_WORKING_SET_BYTES = 64_000_000
# The chunk's real working set per (query, reference) pair: the float32
# accumulator, a float32 scratch plane, a byte mismatch counter, a boolean
# plane, the float32 partition copy and a boolean selection mask (15 bytes;
# up to 26 on a chunk whose every row is tied at its k-th value). The budget
# charges `4 * max(d, 8)` bytes per pair: the documented `chunk * |R| * d * 4`
# bound for d >= 8, and still above the real working set for smaller d.
_MIN_FEATURES_CHARGED = 8

_MIN_GRID_POINTS = 2
# Encoded blocks are (rows, features) matrices.
_BLOCK_NDIM = 2
_MIN_SET_ROWS = 2
# Nearest neighbours needed for NNDR: the first and the second.
_NNDR_K = 2

_P5 = 0.05
_P50 = 0.5
# Two-sided 95 % normal quantile, as `noise.wilson_interval` uses.
_Z = 1.959964
# Fixed profile bins over [0, 1] for DCR and NNDR histograms: 49 interior
# edges 0.02, ..., 0.98, counted like `binned.bin_counts` (the GUI's
# `distanceHistPayloadSchema`: `counts` has `len(edges) + 1` entries).
_HIST_BINS = 50
_HIST_EDGES = tuple(i / _HIST_BINS for i in range(1, _HIST_BINS))


def _validated_grid(name: str, grid: Any) -> np.ndarray:
  """`grid` as a read-only 1-D float64 array, or `ValueError` if unusable."""
  arr = np.array(grid, dtype=np.float64)
  if arr.ndim != 1 or arr.size < _MIN_GRID_POINTS:
    raise ValueError(
        f"the quantile grid of numeric column {name!r} needs at least 2 "
        f"points (a 1-D APPROX_QUANTILES grid), got shape {arr.shape}")
  if not np.isfinite(arr).all():
    raise ValueError(
        f"the quantile grid of numeric column {name!r} must be finite")
  if np.any(np.diff(arr) < 0):
    raise ValueError(
        f"the quantile grid of numeric column {name!r} must be non-decreasing")
  arr.setflags(write=False)
  return arr


def _parse_numeric_text(text: str) -> float | None:
  """A numeric string (`"10.5"`), or ISO-8601 date/time text on
  `numeric_value`'s temporal scale (UNIX microseconds; a time of day as
  microseconds since midnight — Ruling R54)."""
  try:
    return float(text)
  except ValueError:
    pass
  try:
    return numeric_value(datetime.fromisoformat(text))
  except ValueError:
    pass
  try:
    return numeric_value(time.fromisoformat(text))
  except ValueError:
    return None


def _numeric_reading(column: str, value: Any) -> float:
  """One numeric/temporal cell as a float; `NaN` for NULL or non-finite.

  Beam reads give `Decimal`/`int`/`float`/`datetime`/`date`/`time` values,
  which `numeric_value` maps (temporal to UNIX microseconds, a naive
  `datetime` read as UTC, a `time` as microseconds since midnight); a
  canonicalised row carries the same values as strings (`"10.5"`, an ISO
  timestamp), which are parsed back. A value with no numeric reading at
  all is a plan/data mismatch and raises rather than being silently treated
  as NULL; the message names the column and the type, never the value.
  """
  if value is None:
    return math.nan
  reading = numeric_value(value)
  if reading is None and isinstance(value, str):
    reading = _parse_numeric_text(value)
  if reading is None:
    raise ValueError(f"numeric column {column!r} holds a "
                     f"{type(value).__name__} value with no numeric reading")
  return reading if math.isfinite(reading) else math.nan


def _mid_cdf_pit(x: np.ndarray, grid: np.ndarray) -> np.ndarray:
  """The source mid-CDF of every `x` through a quantile grid; `NaN` stays `NaN`.

  `grid[i]` is the quantile at probability `i / (len(grid) - 1)`. A value on
  a run of identical grid points `grid[a..b]` (an atom) maps to the middle of
  that run's probability range, `(p_a + p_b) / 2` — the Parzen mid-CDF
  `F - p/2` (Rulings R16/R24), so a constant source maps its value to 1/2.
  Between grid points the CDF is linear; below the grid it is 0, above 1.
  """
  probs = np.linspace(0.0, 1.0, grid.size)
  out = np.full(x.shape, np.nan)
  present = ~np.isnan(x)
  values = x[present]
  lo = np.searchsorted(grid, values, side="left")
  hi = np.searchsorted(grid, values, side="right")
  u = np.empty(values.shape)
  on_grid = hi > lo
  u[on_grid] = 0.5 * (probs[lo[on_grid]] + probs[hi[on_grid] - 1])
  u[~on_grid & (lo == 0)] = 0.0
  u[~on_grid & (lo == grid.size)] = 1.0
  inner = ~on_grid & (lo > 0) & (lo < grid.size)
  right = lo[inner]
  g0, g1 = grid[right - 1], grid[right]
  p0, p1 = probs[right - 1], probs[right]
  u[inner] = p0 + (values[inner] - g0) / (g1 - g0) * (p1 - p0)
  out[present] = np.clip(u, 0.0, 1.0)
  return out


def pit_mid_cdf(values: Sequence[Any],
                grid: Any,
                *,
                column: str = "values") -> np.ndarray:
  """Public entry to this module's numeric encoding, for `stats.detection`.

  `values` are raw numeric/temporal cells, read exactly as `GowerSpace.encode`
  reads them (`None`/non-finite -> `NaN`, temporal -> UNIX microseconds,
  numeric strings parsed back; no numeric reading raises), and mapped through
  `grid`'s mid-CDF PIT (`_mid_cdf_pit`), after `grid` passes the same
  validation as a `GowerSpace` grid. Returns float64 (`encode` stores the
  same values as float32). `column` only names the column in error messages.
  """
  checked = _validated_grid(column, grid)
  x = np.fromiter((_numeric_reading(column, v) for v in values),
                  dtype=np.float64,
                  count=len(values))
  return _mid_cdf_pit(x, checked)


@dataclass(frozen=True, eq=False)
class GowerSpace:
  """The plan's Gower feature space: numeric grids and categorical columns.

  `num_grids[j]` is the source's quantile grid for `num_names[j]` (a
  1,001-point `APPROX_QUANTILES(x, 1000)` grid, temporal columns in UNIX
  microseconds like the plan's, Ruling R54), evenly spaced in probability;
  `cat_names` are the categorical, boolean, text and identifier columns.
  Keys are never features. Equality
  is identity (`eq=False`: comparing arrays elementwise is not a truth
  value); compare spaces by `digest`.
  """
  num_grids: tuple[np.ndarray, ...]
  num_names: tuple[str, ...]
  cat_names: tuple[str, ...]
  _digest: str = field(init=False, repr=False, default="")

  def __post_init__(self) -> None:
    num_names = tuple(self.num_names)
    cat_names = tuple(self.cat_names)
    if len(self.num_grids) != len(num_names):
      raise ValueError(
          f"num_grids has {len(self.num_grids)} grids for {len(num_names)} "
          "numeric columns")
    names = num_names + cat_names
    if len(set(names)) != len(names):
      raise ValueError(f"duplicate column names in the Gower space: {names}")
    if not names:
      raise ValueError("the Gower space has no features")
    grids = tuple(
        _validated_grid(name, grid)
        for name, grid in zip(num_names, self.num_grids, strict=True))
    object.__setattr__(self, "num_grids", grids)
    object.__setattr__(self, "num_names", num_names)
    object.__setattr__(self, "cat_names", cat_names)
    object.__setattr__(self, "_digest", self._compute_digest())

  def _compute_digest(self) -> str:
    h = hashlib.blake2b(_DIGEST_VERSION, digest_size=16)

    def _put(data: bytes) -> None:
      h.update(len(data).to_bytes(8, "big"))
      h.update(data)

    for name, grid in zip(self.num_names, self.num_grids, strict=True):
      _put(b"numeric")
      _put(name.encode("utf-8"))
      _put(grid.astype("<f8").tobytes())
    for name in self.cat_names:
      _put(b"categorical")
      _put(name.encode("utf-8"))
    return h.hexdigest()

  @property
  def n_features(self) -> int:
    """`d`, the Gower denominator: numeric plus categorical features."""
    return len(self.num_names) + len(self.cat_names)

  @property
  def digest(self) -> str:
    """The `feature_set_digest`: blake2b-128 hex of names, kinds and grids."""
    return self._digest

  def encode(
      self, rows: Sequence[Mapping[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
    """`(num, cat)`: float32 `(n, dn)` PIT values (`NaN` = NULL) and uint64
    `(n, dc)` `hash64` codes (`NULL_CODE` = NULL).

    A column absent from a row reads as NULL. Raises `ValueError` for a
    numeric/temporal value with no numeric reading (see `_numeric_reading`).
    """
    n = len(rows)
    num = np.empty((n, len(self.num_names)), dtype=np.float32)
    for j, (name, grid) in enumerate(
        zip(self.num_names, self.num_grids, strict=True)):
      x = np.fromiter((_numeric_reading(name, row.get(name)) for row in rows),
                      dtype=np.float64,
                      count=n)
      num[:, j] = _mid_cdf_pit(x, grid)
    return num, hash_matrix(rows, self.cat_names)


def effective_chunk(n_ref: int, n_features: int, chunk: int = 64) -> int:
  """The query rows per chunk: at most `chunk`, and within the 64 MB budget.

  The working set is charged `4 * max(d, 8)` bytes per (query, reference)
  pair, so `effective_chunk * n_ref * d * 4 <= 64 MB` whenever one query row
  fits (at `n_ref = 10_000`, `d = 50`: 32 rows, 64 MB).
  """
  per_row = max(1, n_ref) * 4 * max(n_features, _MIN_FEATURES_CHARGED)
  return max(1, min(chunk, _MAX_WORKING_SET_BYTES // per_row))


def _as_blocks(num: Any, cat: Any, label: str) -> tuple[np.ndarray, np.ndarray]:
  """Validated float32 `(n, dn)` and uint64 `(n, dc)` blocks for one side."""
  num_arr = np.asarray(num, dtype=np.float32)
  cat_arr = np.asarray(cat)
  if num_arr.ndim != _BLOCK_NDIM or cat_arr.ndim != _BLOCK_NDIM:
    raise ValueError(f"{label} blocks must be 2-D, got {num_arr.shape} and "
                     f"{cat_arr.shape}")
  if cat_arr.size and cat_arr.dtype.kind not in "ui":
    raise ValueError(f"{label} categorical codes must be integers, got "
                     f"{cat_arr.dtype}")
  cat_arr = cat_arr.astype(np.uint64, copy=False)
  if num_arr.shape[0] != cat_arr.shape[0]:
    raise ValueError(f"{label} numeric and categorical blocks disagree on "
                     f"rows: {num_arr.shape[0]} vs {cat_arr.shape[0]}")
  finite = num_arr[~np.isnan(num_arr)]
  if finite.size and (finite.min() < 0.0 or finite.max() > 1.0):
    raise ValueError(f"{label} numeric features must lie in [0, 1] (PIT "
                     "values) or be NaN for NULL")
  return num_arr, cat_arr


class _Reference:
  """The reference side, laid out per feature for the chunk loop."""

  def __init__(self, num: np.ndarray, cat: np.ndarray) -> None:
    self.num = num
    self.cat = cat
    self.n = num.shape[0]
    self.num_cols = [
        np.ascontiguousarray(num[:, j]) for j in range(num.shape[1])
    ]
    self.num_null_idx = [np.flatnonzero(np.isnan(col)) for col in self.num_cols]
    # Per feature: what a NULL query cell scores against each reference cell
    # (1 against a value, 0 against a NULL).
    self.num_present = [
        (~np.isnan(col)).astype(np.float32) for col in self.num_cols
    ]
    self.cat_cols = [
        np.ascontiguousarray(cat[:, j]) for j in range(cat.shape[1])
    ]
    self.n_features = num.shape[1] + cat.shape[1]


def _pair_sums(q_num: np.ndarray, q_cat: np.ndarray, ref: _Reference,
               acc: np.ndarray, scratch: np.ndarray,
               mismatch: np.ndarray | None,
               neq: np.ndarray | None) -> np.ndarray:
  """The `(c, |R|)` float32 Gower SUMS (not yet divided by `d`) into `acc`.

  Numeric features are accumulated first, in feature order, then the
  integer mismatch count of the categorical features is added once, so a
  pair's sum depends only on its own two rows: identical whatever the chunk.
  A NULL cell is patched after the subtraction (its `NaN` differences are
  overwritten by exactly 0 or 1), so non-NULL pairs see the plain `|u - v|`.
  `mismatch`/`neq` are `None` exactly when there are no categorical features.
  The first feature of each kind is written straight into its accumulator
  (`0 + x == x`, so the sums are unchanged), saving two full passes each.
  """
  for j, rcol in enumerate(ref.num_cols):
    qcol = q_num[:, j]
    term = acc if j == 0 else scratch
    np.subtract(qcol[:, np.newaxis], rcol[np.newaxis, :], out=term)
    np.abs(term, out=term)
    q_null = np.isnan(qcol)
    if q_null.any():
      term[q_null, :] = ref.num_present[j]
    r_null = ref.num_null_idx[j]
    if r_null.size:
      term[:, r_null] = (~q_null).astype(np.float32)[:, np.newaxis]
    if j:
      np.add(acc, scratch, out=acc)
  if mismatch is not None and neq is not None:
    first = mismatch.view(bool) if mismatch.dtype == np.uint8 else None
    if first is None:
      mismatch.fill(0)
    for j, rcol in enumerate(ref.cat_cols):
      if j == 0 and first is not None:
        np.not_equal(q_cat[:, 0, np.newaxis], rcol[np.newaxis, :], out=first)
        continue
      np.not_equal(q_cat[:, j, np.newaxis], rcol[np.newaxis, :], out=neq)
      np.add(mismatch, neq, out=mismatch)
    if ref.num_cols:
      np.add(acc, mismatch, out=acc)
    else:
      np.copyto(acc, mismatch)
  return acc


def _iter_pair_sums(q_num: np.ndarray, q_cat: np.ndarray, ref: _Reference,
                    chunk: int) -> Iterator[tuple[int, np.ndarray]]:
  """`(start, sums)` per query chunk; `sums` is reused by the next chunk."""
  step = max(1,
             min(effective_chunk(ref.n, ref.n_features, chunk), q_num.shape[0]))
  acc = np.empty((step, ref.n), dtype=np.float32)
  scratch = np.empty((step, ref.n), dtype=np.float32)
  mismatch: np.ndarray | None = None
  neq: np.ndarray | None = None
  if ref.cat_cols:
    n_cat = len(ref.cat_cols)
    count_dtype = np.uint8 if n_cat <= np.iinfo(np.uint8).max else np.uint32
    mismatch = np.empty((step, ref.n), dtype=count_dtype)
    neq = np.empty((step, ref.n), dtype=bool)
  for start in range(0, q_num.shape[0], step):
    stop = min(start + step, q_num.shape[0])
    c = stop - start
    yield start, _pair_sums(q_num[start:stop], q_cat[start:stop], ref, acc[:c],
                            scratch[:c],
                            None if mismatch is None else mismatch[:c],
                            None if neq is None else neq[:c])


def _k_smallest_argmin(
    sums: np.ndarray, k: int,
    count_le: bool) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
  """`_k_smallest` for small `k`: `k` rounds of `argmin`, CONSUMING `sums`.

  `np.argmin` returns the first (lowest-index) minimum; each selected entry
  is then overwritten with `inf`, so round `m` picks the next pair in
  (value, index) order — the same selection and order as the partition
  path, at a fraction of its cost for the k = 1, 2 and 6 that the privacy
  metrics use.
  """
  rows = np.arange(sums.shape[0])
  vals = np.empty((sums.shape[0], k), dtype=sums.dtype)
  idx = np.empty((sums.shape[0], k), dtype=np.int64)
  for m in range(k):
    col = np.argmin(sums, axis=1)
    idx[:, m] = col
    vals[:, m] = sums[rows, col]
    sums[rows, col] = np.inf
  n_le = None
  if count_le:
    # The k selected entries are all <= t (and now inf); count the rest.
    n_le = k + np.count_nonzero(sums <= vals[:, k - 1:k], axis=1)
  return vals, idx, n_le


def _k_smallest_partition(
    sums: np.ndarray, k: int,
    count_le: bool) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
  """`_k_smallest` via `np.partition`, for larger `k`; `sums` is untouched.

  `np.partition` finds each row's k-th smallest value `t`; the rows with
  exactly `k` values `<= t` are then unambiguous. Rows with ties AT `t`
  keep every value below `t` plus the lowest-index values equal to it.
  """
  c = sums.shape[0]
  # `.copy()` so the partitioned (c, |R|) buffer is released right away.
  kth = np.partition(sums, k - 1, axis=1)[:, k - 1].copy()
  keep = sums <= kth[:, np.newaxis]
  n_le = np.count_nonzero(keep, axis=1)
  tied = np.flatnonzero(n_le > k)
  if tied.size:
    sub = sums[tied]
    t = kth[tied, np.newaxis]
    below = sub < t
    at = sub == t
    need = k - np.count_nonzero(below, axis=1)
    rank_at = np.cumsum(at, axis=1, dtype=np.int32)
    keep[tied] = below | (at & (rank_at <= need[:, np.newaxis]))
  idx = np.nonzero(keep)[1].reshape(c, k)
  vals = np.take_along_axis(sums, idx, axis=1)
  order = np.lexsort((idx, vals), axis=1)
  return (np.take_along_axis(vals, order, axis=1),
          np.take_along_axis(idx, order, axis=1), n_le if count_le else None)


# Up to this k, k rounds of argmin beat one partition + selection mask.
_ARGMIN_MAX_K = 8


def _k_smallest(
    sums: np.ndarray,
    k: int,
    count_le: bool = False) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
  """The `k` smallest per row, ascending, ties broken by the lower index.

  Returns `(values, indices, n_le)`; `n_le` (only when `count_le`) is each
  row's count of entries `<=` its k-th smallest value, ties included — what
  the tie-corrected density needs. MAY overwrite `sums` (small `k`).
  """
  if k <= _ARGMIN_MAX_K:
    return _k_smallest_argmin(sums, k, count_le)
  return _k_smallest_partition(sums, k, count_le)


def _knn_sums(
    q_num: np.ndarray,
    q_cat: np.ndarray,
    ref: _Reference,
    k: int,
    chunk: int,
    count_le: bool = False) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
  """The k-NN float32 Gower SUMS and int64 reference indices per query row,
  plus (with `count_le`) each row's count of reference rows `<=` its k-th."""
  if not 1 <= k <= ref.n:
    raise ValueError(f"k must be in [1, {ref.n}] (the reference rows), got {k}")
  n_q = q_num.shape[0]
  sums = np.empty((n_q, k), dtype=np.float32)
  idx = np.empty((n_q, k), dtype=np.int64)
  n_le = np.empty(n_q, dtype=np.int64) if count_le else None
  for start, block in _iter_pair_sums(q_num, q_cat, ref, chunk):
    stop = start + block.shape[0]
    vals, sel, le = _k_smallest(block, k, count_le)
    sums[start:stop], idx[start:stop] = vals, sel
    if n_le is not None and le is not None:
      n_le[start:stop] = le
  return sums, idx, n_le


def _prepare(q_num: Any, q_cat: Any, r_num: Any,
             r_cat: Any) -> tuple[np.ndarray, np.ndarray, _Reference]:
  """Validated query blocks and the prepared reference, shapes checked."""
  qn, qc = _as_blocks(q_num, q_cat, "query")
  rn, rc = _as_blocks(r_num, r_cat, "reference")
  if qn.shape[1] != rn.shape[1] or qc.shape[1] != rc.shape[1]:
    raise ValueError(
        "query and reference disagree on the feature layout: numeric "
        f"{qn.shape[1]} vs {rn.shape[1]}, categorical {qc.shape[1]} vs "
        f"{rc.shape[1]}")
  if qn.shape[1] + qc.shape[1] == 0:
    raise ValueError("no features to measure a Gower distance on")
  return qn, qc, _Reference(rn, rc)


def gower_knn(q_num: Any,
              q_cat: Any,
              r_num: Any,
              r_cat: Any,
              *,
              k: int,
              chunk: int = 64) -> tuple[np.ndarray, np.ndarray]:
  """Exact k nearest reference rows per query row under the Gower distance.

  `q_num`/`r_num` are float32 PIT blocks (`NaN` = NULL), `q_cat`/`r_cat`
  uint64 code blocks (`NULL_CODE` = NULL), as `GowerSpace.encode` returns.
  Returns `(distances, indices)`, both `(n_q, k)`: float64 Gower distances
  ascending, and int64 reference row indices, ties broken by the lower
  index. Queries are processed `effective_chunk(|R|, d, chunk)` rows at a
  time: the working set is charged `4 * max(d, 8)` bytes per (query,
  reference) pair against a 64 MB budget, so it stays within 64 MB for any
  `d` (and within `chunk * |R| * d * 4` bytes once `d >= 8`).
  Deterministic, and bit-identical for a query row whatever `chunk`.
  """
  qn, qc, ref = _prepare(q_num, q_cat, r_num, r_cat)
  sums, idx, _ = _knn_sums(qn, qc, ref, k, chunk)
  return sums.astype(np.float64) / ref.n_features, idx


def density_coverage(real_num: Any,
                     real_cat: Any,
                     fake_num: Any,
                     fake_cat: Any,
                     *,
                     k: int = 5,
                     chunk: int = 64) -> tuple[float, float]:
  """Tie-corrected Naeem et al. (2020) density, and coverage, of `fake` rows
  around `real` rows (Ruling R28).

  Each real row `X_i` gets the closed ball `B(X_i, r_i)` whose radius is its
  k-th nearest-neighbour distance among the OTHER real rows (the `(k+1)`-th
  smallest distance with itself included, since its own distance is 0), and
  `K_i >= k` is the number of other real rows inside that ball:

    density  = (1 / (k M)) sum_j sum_i 1[d(Y_j, X_i) <= r_i] * k / K_i
    coverage = (1 / N) sum_i 1[exists j: d(Y_j, X_i) <= r_i]

  Without distance ties `K_i = k` and density is exactly Naeem's. On a
  lattice (categorical-only spaces: many zero radii, balls holding far more
  than k real rows) the plain closed-ball count inflates density several
  fold for a perfect generator; weighting each ball by `k / K_i` divides out
  the real rows the ball holds beyond k. Coverage keeps the plain closed
  balls. The catalogue metrics (`row.density`, `row.coverage`) need
  `N == M`; `nn_privacy` guarantees it. Raises `ValueError` unless `N > k`
  and `M >= 1`.
  """
  fn, fc, ref = _prepare(fake_num, fake_cat, real_num, real_cat)
  if k < 1 or ref.n <= k:
    raise ValueError(
        f"density/coverage need k >= 1 and more than k = {k} real rows, got "
        f"{ref.n}")
  if fn.shape[0] < 1:
    raise ValueError("density/coverage need at least 1 fake row")
  radii_sums, _, n_le = _knn_sums(ref.num, ref.cat, ref, k + 1, chunk, True)
  radii = radii_sums[:, k]
  # n_le counts the ball's real rows with X_i itself; K_i excludes it.
  others = cast(np.ndarray, n_le) - 1
  ball_hits = np.zeros(ref.n, dtype=np.int64)
  for _, block in _iter_pair_sums(fn, fc, ref, chunk):
    ball_hits += np.count_nonzero(block <= radii[np.newaxis, :], axis=0)
  # k / K_i is exactly 1.0 without ties, so the sum is Naeem's integer count.
  density = float(np.sum(ball_hits * (k / others))) / (k * fn.shape[0])
  return density, float(np.count_nonzero(ball_hits) / ref.n)


def holdout_mass(d_r: np.ndarray, i_r: np.ndarray, d_h: np.ndarray,
                 i_h: np.ndarray, *, n_r: int,
                 n_h: int) -> tuple[float, np.ndarray]:
  """The closer-to-R count and the pooled nearest-neighbour mass of a batch.

  Inputs are, per synthetic row, the nearest distance and index in R
  (`d_r`, `i_r`) and in H (`d_h`, `i_h`), as `gower_knn` returns them (the
  lowest index among equally near rows of one side). Returns:

    - `closer = #[d_R < d_H] + 0.5 * #[d_R == d_H]`, the share's numerator;
    - `nn_mass`, float64 of length `n_r + n_h` (R rows first, then H): the
      synthetic mass whose POOLED nearest neighbour over R u H is each row.
      A decisive synthetic row adds 1 to its winning neighbour (its nearest
      R row when `d_R < d_H`, its nearest H row when `d_H < d_R`); a tie adds
      1/2 to its nearest R row and 1/2 to its nearest H row. So `nn_mass[:n_r]
      .sum() == closer` and `nn_mass.sum()` is the batch size.

  Both are sums over synthetic rows (half-integers, exact in float64), so
  the batches of a chunked run merge by plain addition. Raises `ValueError`
  on misaligned inputs or an index outside its side.
  """
  if not d_r.shape == i_r.shape == d_h.shape == i_h.shape or d_r.ndim != 1:
    raise ValueError("holdout_mass needs four aligned 1-D arrays, got shapes "
                     f"{d_r.shape}, {i_r.shape}, {d_h.shape}, {i_h.shape}")
  in_range = (i_r.min() >= 0 and i_r.max() < n_r and i_h.min() >= 0 and
              i_h.max() < n_h) if i_r.size else True
  if not in_range:
    raise ValueError(f"holdout_mass indices must lie in [0, {n_r}) for R and "
                     f"[0, {n_h}) for H")
  closer_r = d_r < d_h
  tie = d_r == d_h
  closer_h = d_h < d_r
  w_r = np.where(closer_r, 1.0, 0.0) + np.where(tie, 0.5, 0.0)
  w_h = np.where(closer_h, 1.0, 0.0) + np.where(tie, 0.5, 0.0)
  nn_mass = np.concatenate([
      np.bincount(i_r, weights=w_r, minlength=n_r),
      np.bincount(i_h, weights=w_h, minlength=n_h),
  ])
  closer = np.count_nonzero(closer_r) + 0.5 * np.count_nonzero(tie)
  return float(closer), nn_mass


def nndr(dist: np.ndarray) -> np.ndarray:
  """`d1 / d2` per row of a `(n, 2)` nearest-two distance block (as
  `gower_knn(..., k=2)` returns it); 1.0 where `d2 == 0` (two exact
  copies: nobody stands out)."""
  ratio = np.ones(dist.shape[0])
  np.divide(dist[:, 0], dist[:, 1], out=ratio, where=dist[:, 1] > 0)
  return ratio


@dataclass(frozen=True, eq=False)
class NNPrivacyResult:
  """Per-row nearest-neighbour distances and the aggregates built on them.

  `dcr_syn_r`/`dcr_syn_h`/`dcr_h_r` are each row's nearest Gower distance
  (synthetic to R, synthetic to H, holdout to R); `nndr_syn`/`nndr_h` the
  nearest over second-nearest distance to R. `nn_mass` (length |R| + |H|,
  R first) is the synthetic mass per pooled nearest neighbour
  (`holdout_mass`); it merges across synthetic chunks by elementwise sum.
  `closer_to_r` is the holdout share (ties one half) over `n_syn` synthetic
  rows. `density`/`coverage` are `None` when fewer than `k + 1` rows are
  available at equal n.
  """
  dcr_syn_r: np.ndarray
  dcr_syn_h: np.ndarray
  dcr_h_r: np.ndarray
  nndr_syn: np.ndarray
  nndr_h: np.ndarray
  nn_mass: np.ndarray
  closer_to_r: float
  n_syn: int
  density: float | None
  coverage: float | None


def _check_sets(n_r: int, n_h: int, n_syn: int, k: int) -> None:
  """Raise `ValueError` for set sizes the metrics cannot be computed on."""
  if n_r < _MIN_SET_ROWS:
    raise ValueError("nearest-neighbour privacy needs at least 2 reference "
                     f"rows (R), got {n_r}")
  if n_h < _MIN_SET_ROWS:
    raise ValueError("nearest-neighbour privacy needs at least 2 holdout rows "
                     f"(H), got {n_h}")
  if n_syn < 1:
    raise ValueError("nearest-neighbour privacy needs at least 1 synthetic row")
  if k < 1:
    raise ValueError(f"density/coverage k must be >= 1, got {k}")


def nn_privacy(space: GowerSpace,
               r_rows: Sequence[Mapping[str, Any]],
               h_rows: Sequence[Mapping[str, Any]],
               syn_rows: Sequence[Mapping[str, Any]],
               *,
               k: int = 5) -> NNPrivacyResult:
  """The holdout DCR test, NNDR and density/coverage for one table.

  Encodes the rows through `space` (R and H trimmed to their first
  `min(|R|, |H|)` rows first) and runs `nn_privacy_encoded`. Raises
  `ValueError` when R or H has fewer than 2 rows, the synthetic side is
  empty, or `k < 1` (the caller records the metrics as not evaluated).
  """
  _check_sets(len(r_rows), len(h_rows), len(syn_rows), k)
  n_set = min(len(r_rows), len(h_rows))
  return nn_privacy_encoded(
      space.encode(r_rows[:n_set]),
      space.encode(h_rows[:n_set]),
      space.encode(syn_rows),
      k=k)


def nn_privacy_encoded(r: tuple[np.ndarray, np.ndarray],
                       h: tuple[np.ndarray, np.ndarray],
                       syn: tuple[np.ndarray, np.ndarray],
                       *,
                       k: int = 5) -> NNPrivacyResult:
  """`nn_privacy` on `(num, cat)` blocks already encoded by one `GowerSpace`.

  R and H are trimmed to the same size (their first `min(|R|, |H|)` rows;
  D3 makes both exchangeable ranks of the same source ordering). Distances:
  synthetic to R (nearest two) and to H (nearest), and H to R (nearest two;
  H and R are disjoint, so nothing is excluded). `closer_to_r = (#[d_R <
  d_H] + 0.5 #[d_R == d_H]) / n_syn`, with `nn_mass` from `holdout_mass`.
  Density/coverage use `k` with real = R and fake = the first rows of the
  synthetic sample at equal n (`min(|R|, n_syn)`, R trimmed alike if the
  synthetic side is smaller); both are `None` when that n is `<= k`.
  """
  (r_num, r_cat), (h_num, h_cat), (s_num, s_cat) = r, h, syn
  _check_sets(len(r_num), len(h_num), len(s_num), k)
  n_set = min(len(r_num), len(h_num))
  r_num, r_cat, h_num, h_cat = (r_num[:n_set], r_cat[:n_set], h_num[:n_set],
                                h_cat[:n_set])
  syn_r, syn_r_idx = gower_knn(s_num, s_cat, r_num, r_cat, k=_NNDR_K)
  syn_h, syn_h_idx = gower_knn(s_num, s_cat, h_num, h_cat, k=1)
  h_r = gower_knn(h_num, h_cat, r_num, r_cat, k=_NNDR_K)[0]
  n_syn = len(s_num)
  closer, nn_mass = holdout_mass(
      syn_r[:, 0],
      syn_r_idx[:, 0],
      syn_h[:, 0],
      syn_h_idx[:, 0],
      n_r=n_set,
      n_h=n_set)

  n_eq = min(n_set, n_syn)
  density: float | None = None
  coverage: float | None = None
  if n_eq > k:
    density, coverage = density_coverage(
        r_num[:n_eq], r_cat[:n_eq], s_num[:n_eq], s_cat[:n_eq], k=k)
  return NNPrivacyResult(
      dcr_syn_r=syn_r[:, 0],
      dcr_syn_h=syn_h[:, 0],
      dcr_h_r=h_r[:, 0],
      nndr_syn=nndr(syn_r),
      nndr_h=nndr(h_r),
      nn_mass=nn_mass,
      closer_to_r=closer / n_syn,
      n_syn=n_syn,
      density=density,
      coverage=coverage,
  )


def permutation_se(nn_mass: np.ndarray, n_syn: int) -> float:
  """The standard error of the holdout share under random balanced R/H splits
  (Ruling R29).

  With the pooled rows `x` of R u H (`2n` of them, `n = |R| = |H|`) and
  `w_x` the synthetic mass whose pooled nearest neighbour is `x`
  (`holdout_mass`), the share is `sum_{x in R} w_x / n_syn`. Drawing which
  `n` pooled rows form R uniformly without replacement gives

    Var(share) = (2n / (2n - 1)) sum_x (w_x - n_syn / (2n))^2 / (4 n_syn^2),

  the finite-population variance of a half-sample total. It carries the
  R/H split noise a binomial interval over synthetic rows ignores: many
  synthetic rows sharing one nearest source row move together. `0.0` when
  there is nothing to measure.
  """
  pooled = nn_mass.size
  if n_syn <= 0 or pooled < _MIN_SET_ROWS:
    return 0.0
  centered = nn_mass - n_syn / pooled
  variance = (pooled / (pooled - 1)) * float(np.dot(
      centered, centered)) / (4.0 * n_syn * n_syn)
  return math.sqrt(variance)


def _quantile(values: np.ndarray, q: float) -> float | None:
  """`np.quantile` (linear interpolation) as a float; `None` when empty."""
  return float(np.quantile(values, q)) if values.size else None


def _ratio(num: float | None, den: float | None) -> float | None:
  """`num / den`, or `None` when either is missing or `den` is not positive."""
  if num is None or den is None or den <= 0.0:
    return None
  return num / den


def _hist_payload(values: np.ndarray) -> dict[str, Any]:
  """A `dcr_hist`/`nndr_hist` profile payload: 50 fixed bins over [0, 1]."""
  counts = bin_counts(values, np.asarray(_HIST_EDGES))
  return {
      "edges": list(_HIST_EDGES),
      "counts": [int(c) for c in counts],
      "p5": _quantile(values, _P5),
      "p50": _quantile(values, _P50),
      "n": int(values.size),
  }


def summarize_nn(res: NNPrivacyResult) -> dict[str, Any]:
  """Catalogue-ready values from one `NNPrivacyResult`.

  Keys named for their metric (`row.<key>`):
    - `dcr_train_holdout_share` with `..._se_wilson`, `..._se_perm` and the
      interval `..._ci_low`/`..._ci_high` = share -/+ z * max(se_wilson,
      se_perm), clipped to [0, 1] (Ruling R29; D5 gates on `ci_low`).
      `se_wilson` is the Wilson (1927) 95 % interval's half-width over z at
      `n_synthetic`: the synthetic-sampling noise alone. Ties add one half,
      so the count `share * n` may be a half-integer; `wilson_interval` reads
      it only through `p = k / n`, and a score in {0, 1/2, 1} has variance
      at most `p (1 - p)`. `se_perm` (`permutation_se`) adds the R/H split
      noise that Wilson ignores, which dominates once the synthetic sample
      outnumbers the source rows.
    - `dcr_p5_ratio = Q.05(dcr_syn_r) / Q.05(dcr_h_r)` and `nndr_p5_ratio =
      Q.05(nndr_syn) / Q.05(nndr_h)`, `None` when the holdout quantile is 0.
    - `density`, `coverage` (`None` when not computable).
  Plus `n_synthetic`, `n_reference` (= |R| = |H| after trimming), the p5/p50
  of every distance distribution (`<dist>_p5`, `<dist>_p50`; quantiles are
  linear-interpolated) and `profiles`: `dcr_hist`/`nndr_hist` payloads per
  side (`synthetic` = syn to R, `holdout` = H to R).
  """
  n_syn = res.n_syn
  share = res.closer_to_r
  # See the docstring: the half-integer count is exact through p = k / n.
  w_low, w_high = wilson_interval(cast(int, share * n_syn), n_syn, z=_Z)
  se_wilson = (w_high - w_low) / (2.0 * _Z)
  se_perm = permutation_se(res.nn_mass, n_syn)
  se = max(se_wilson, se_perm)
  summary: dict[str, Any] = {
      "n_synthetic": n_syn,
      "n_reference": int(res.dcr_h_r.size),
      "dcr_train_holdout_share": share,
      "dcr_train_holdout_share_se_wilson": se_wilson,
      "dcr_train_holdout_share_se_perm": se_perm,
      "dcr_train_holdout_share_ci_low": max(0.0, share - _Z * se),
      "dcr_train_holdout_share_ci_high": min(1.0, share + _Z * se),
  }
  dists = {
      "dcr_syn_r": res.dcr_syn_r,
      "dcr_syn_h": res.dcr_syn_h,
      "dcr_h_r": res.dcr_h_r,
      "nndr_syn": res.nndr_syn,
      "nndr_h": res.nndr_h,
  }
  for name, values in dists.items():
    summary[f"{name}_p5"] = _quantile(values, _P5)
    summary[f"{name}_p50"] = _quantile(values, _P50)
  summary["dcr_p5_ratio"] = _ratio(summary["dcr_syn_r_p5"],
                                   summary["dcr_h_r_p5"])
  summary["nndr_p5_ratio"] = _ratio(summary["nndr_syn_p5"],
                                    summary["nndr_h_p5"])
  summary["density"] = res.density
  summary["coverage"] = res.coverage
  summary["profiles"] = {
      "dcr_hist": {
          "synthetic": _hist_payload(res.dcr_syn_r),
          "holdout": _hist_payload(res.dcr_h_r),
      },
      "nndr_hist": {
          "synthetic": _hist_payload(res.nndr_syn),
          "holdout": _hist_payload(res.nndr_h),
      },
  }
  return summary
