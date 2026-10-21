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
"""Dependence from dense accumulators: one mergeable `BivariateAccumulator`
per (table, side) over a plan-fixed list of column pairs, feeding
`pair.pearson_delta`, `pair.spearman_delta`, `pair.cramers_v_delta`,
`pair.nmi_delta`, `pair.contingency_tvd`, `table.corr_rms_delta` and
`table.corr_max_delta` (`src/sdfb_evaluation/catalogue/metrics.yaml`).

The Beam accumulator (a later task) feeds `BivariateAccumulator.add_batch`
one `(b, d)` numpy batch per bundle — `d` is the accumulator's own column
list (the union of columns its `pairs` index into, not the whole table) —
then merges accumulators pairwise across bundles/workers via `merge`. Two
parallel co-moment tables are kept per pair: `com_std` over values
standardized by the PLAN's mean/std (feeds `pair.pearson_delta`), and
`com_pit` over each side's own MID-CDF probability-integral transform
(feeds `pair.spearman_delta`) — `F_bar(x) = F(x) - p(x) / 2`, i.e. `(mid
rank - 0.5) / n` with ties given their AVERAGE rank (`scipy.stats.rankdata
(..., method="average")`), never a plain `rank / n`: the mid-CDF is what
keeps a tied column's PIT unbiased, and it is what makes
`pearson_from_comoments(com_pit)` agree with `scipy.stats.spearmanr` to
near machine precision even under heavy ties (Ruling R24; see
`test_pit_spearman_with_ties_matches_scipy_tightly`).

`com_std`/`com_pit` hold CENTERED co-moments per pair, `(n, mean_x, mean_y,
M2_x, M2_y, C_xy)` — `M2 = sum((x - mean_x) ** 2)`, `C_xy = sum((x -
mean_x) * (y - mean_y))`, both over the pair's PAIRWISE-COMPLETE rows (a
row missing either side, `NaN` in `z_std`/`z_pit`, contributes to neither).
This is `stats.moments.Moments`' representation generalized to two
variables (Ruling R23), not the sum-form `(n, Sx, Sy, Sxx, Syy, Sxy)` a
naive implementation might reach for: `add_batch` computes ONE batch's
co-moments via the "shifted data" trick — every column is first shifted by
its own batch mean before any product is summed, so the products stay near
the data's own scale regardless of how large or badly-centered the
caller's `z_std`/`z_pit` values are — and then folds that batch into the
running total with the Chan, Golub & LeVeque (1983) / Pébay (2008) pairwise
merge, the same delta-based update `stats.moments.Moments.merge` uses for
a single variable, generalized to a mean pair and a covariance term. The
result is accurate even for a value shifted far from zero with a tiny
spread relative to that shift (the same pathology raw epoch-microsecond
timestamps show — see `test_offset_plan_z_is_stable_across_many_batches`),
because nothing here ever sums a large raw value directly; only the
per-batch shift needs to be reasonably close to that batch's own mean, and
using the batch's own mean makes it exact by construction.

`counts2d` is the joint bin/dictionary table each pair needs for
`pair.cramers_v_delta`, `pair.nmi_delta` and `pair.contingency_tvd`: `codes`
carries one integer slot per column per row, `0..bins-1` for a bin or
dictionary entry and `bins` itself for a null — so every row lands somewhere
in the `(bins + 1, bins + 1)` table, the null bin included on both axes.
Every row contributes regardless of nulls (unlike the co-moments), and the
whole batch is folded in through a one-hot Gram matrix over all `d`
columns at once, not a per-pair gather.

Design: docs/designs/2026-07-07-evaluation-framework-design.md

References:
  Pearson, K. (1896), "Mathematical Contributions to the Theory of
    Evolution. III. Regression, Heredity, and Panmixia" (the product-moment
    correlation `pearson_from_comoments` computes).
  Spearman, C. (1904), "The Proof and Measurement of Association between
    Two Things" (rank correlation; here `pearson_from_comoments` applied to
    each side's own mid-CDF PIT, per Ruling R24).
  Chan, T., Golub, G., LeVeque, R. (1983), "Algorithms for Computing the
    Sample Variance: Analysis and Recommendations" (the shifted-data and
    pairwise-update algorithms `add_batch`/`merge` implement, generalized
    here to a bivariate mean/covariance state).
  Pébay, P. (2008), "Formulas for Robust, One-Pass Parallel Computation of
    Covariances and Arbitrary-Order Statistical Moments", Sandia Report
    SAND2008-6212.
  Bergsma, W. (2013), "A Bias-Correction for Cramér's V and Tschuprow's T".
  Cover, T., Thomas, J. (2006), "Elements of Information Theory", 2nd ed.
    (mutual information and entropy, both in nats here).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# com_std/com_pit column layout: the centered co-moments `(n, mean_x,
# mean_y, M2_x, M2_y, C_xy)` a Pearson correlation needs (Ruling R23) — not
# additive by themselves; combined via `_merge_bivariate_comoments`, which
# is what keeps `add_batch`/`merge` exact up to floating-point rounding.
_N_COMOMENTS = 6

# The default bin/dictionary-slot count `B` (Task 21's plan uses 10):
# `counts2d` then has `B + 1` slots per axis, the extra slot being the null
# bin every pair's joint table carries on both axes.
_DEFAULT_BINS = 10

# A correlation needs at least this many pairwise-complete points to be
# more than a perfect fit through noise; below it `pearson_from_comoments`
# returns NaN rather than a number with no statistical content.
_MIN_PEARSON_N = 3

# `pearson_from_comoments`'s zero-variance guard is RELATIVE
# (`M2 <= tol * n * max(1, mean ** 2)`), not a literal `M2 <= 0`: a column
# whose values sit far from zero (a large plan-mean offset, or an
# imperfectly-centered "standardized" value) can carry a tiny but nonzero
# `M2` purely from float64's ~1e-16 relative rounding at that offset's
# scale, and that residue must still read as "constant" rather than as a
# minuscule real correlation. `1e-14` sits ~100x above the rounding floor
# for offsets at least 100x larger than the true spread (the scale this
# module's stability tests exercise) while staying ~100x below a spread
# that is genuinely a millionth of its own mean.
_PEARSON_ZERO_VARIANCE_TOL = 1e-14

# Every array this module validates the shape of is (b, d): one batch axis,
# one column axis.
_NDIM_2D = 2

# A pair is (col_i, col_j) — exactly two column indices.
_PAIR_SIZE = 2

# `cramers_v_bias_corrected`/`nmi_min` are undefined with fewer than 2
# non-empty rows or columns (there is then no association left to measure).
_MIN_NONEMPTY_ROWS_COLS = 2


def _validate_pair_indices(pairs: tuple[tuple[int, int], ...]) -> None:
  """Raise `ValueError` unless every pair is a `(non-negative, non-negative)`
  pair of column indices — a caller bug (a negative or malformed plan index)
  should fail at construction, not silently misplace batches later.
  """
  for pair in pairs:
    if len(pair) != _PAIR_SIZE or pair[0] < 0 or pair[1] < 0:
      raise ValueError(
          f"pairs must be (col_i, col_j) with non-negative indices: got {pair!r}"
      )


@dataclass(eq=False)
class BivariateAccumulator:
  """One (table, side)'s mergeable dense dependence accumulator, over a
  plan-fixed list of column `pairs` (indices into the `d` columns of every
  `add_batch` call, not into the whole table).

  `counts2d` is `int64 (P, bins + 1, bins + 1)`: pair `p`'s joint table over
  `codes`, null bin included on both axes. `com_std`/`com_pit` are `float64
  (P, 6)`: the CENTERED co-moments `(n, mean_x, mean_y, M2_x, M2_y, C_xy)`,
  over standardized values and each side's own mid-CDF PIT respectively
  (pairwise complete — see the module docstring). All three start at zero
  counts/sums (`BivariateAccumulator(pairs=...)` is always a valid starting
  point for `add_batch`/`merge`, mirroring `stats.moments.Moments`).

  `eq=False`: the default dataclass `__eq__` would compare `counts2d`/
  `com_std`/`com_pit` with plain `==`, which raises on a multi-element numpy
  array rather than returning a bool — identity equality (the `object`
  default) is what a mutable, array-holding accumulator wants instead; use
  `numpy.testing.assert_array_equal`/`assert_allclose` on the fields to
  compare two by value.
  """
  pairs: tuple[tuple[int, int], ...]
  bins: int = _DEFAULT_BINS
  counts2d: np.ndarray = field(init=False, repr=False)
  com_std: np.ndarray = field(init=False, repr=False)
  com_pit: np.ndarray = field(init=False, repr=False)

  def __post_init__(self) -> None:
    _validate_pair_indices(self.pairs)
    p = len(self.pairs)
    b1 = self.bins + 1
    self.counts2d = np.zeros((p, b1, b1), dtype=np.int64)
    self.com_std = np.zeros((p, _N_COMOMENTS), dtype=np.float64)
    self.com_pit = np.zeros((p, _N_COMOMENTS), dtype=np.float64)
    # Column indices for every pair's two sides, precomputed once so
    # `add_batch` never re-derives them from `pairs` on the hot path.
    self._i_idx = np.array([pair[0] for pair in self.pairs], dtype=np.int64)
    self._j_idx = np.array([pair[1] for pair in self.pairs], dtype=np.int64)

  def add_batch(
      self,
      z_std: np.ndarray,
      z_pit: np.ndarray,
      codes: np.ndarray,
  ) -> None:
    """Fold one `(b, d)` batch into this accumulator, in place.

    `z_std` is standardized by the plan's mean/std; `z_pit` is each
    column's own MID-CDF probability integral transform, `(mid rank -
    0.5) / n` with ties at their average rank — NOT a plain `rank / n`
    (Ruling R24; see the module docstring). Both are float arrays (`NaN` =
    null, `+-inf` is invalid — see below). `codes` is an int array of
    bin/dictionary slots, `0..bins - 1` for a real slot and `bins` for a
    null — every row of `codes` lands in `counts2d` regardless of nulls,
    while `com_std`/`com_pit` only fold in rows that are non-null on BOTH
    sides of a pair (pairwise complete).

    Vectorized over every pair at once, and over every column pair the
    batch's `d` columns admit — not just this accumulator's own `pairs` —
    via two dense `(d, d)`/`(d * (bins + 1), d * (bins + 1))` products
    (`numpy.ndarray.T @ numpy.ndarray`, BLAS-backed): a "shifted data"
    Gram matrix for the co-moments, and a one-hot Gram matrix for
    `counts2d`. No per-pair Python loop, and both are what make this
    practical at the plan's ~2000-pair, ~8k-row-batch scale (see the
    module's perf test).

    Raises `ValueError` if `z_std`, `z_pit` and `codes` are not all the
    same `(b, d)` shape, if `codes` does not have an integer dtype or
    holds a value outside `[0, bins]`, if `z_std`/`z_pit` contain `+-inf`,
    or if a pair references a column index `>= d`.
    """
    z_std_arr = np.asarray(z_std, dtype=np.float64)
    z_pit_arr = np.asarray(z_pit, dtype=np.float64)
    codes_arr = np.asarray(codes)
    if z_std_arr.shape != z_pit_arr.shape or z_std_arr.shape != codes_arr.shape:
      raise ValueError(
          "z_std, z_pit and codes must share one (b, d) shape: got "
          f"{z_std_arr.shape}, {z_pit_arr.shape}, {codes_arr.shape}")
    if z_std_arr.ndim != _NDIM_2D:
      raise ValueError(
          f"z_std/z_pit/codes must be 2-D (b, d); got ndim={z_std_arr.ndim}")
    if not np.issubdtype(codes_arr.dtype, np.integer):
      raise ValueError(
          f"codes must have an integer dtype; got {codes_arr.dtype}")
    if np.isinf(z_std_arr).any() or np.isinf(z_pit_arr).any():
      raise ValueError(
          "z_std and z_pit must not contain +-inf (only NaN marks a null)")
    b, d = z_std_arr.shape
    if b == 0:
      return
    max_col = int(max(self._i_idx.max(),
                      self._j_idx.max())) if self._i_idx.size else -1
    if max_col >= d:
      raise ValueError(
          f"pairs reference column index {max_col}, but the batch has only "
          f"{d} columns")
    if codes_arr.size and (codes_arr.min() < 0 or codes_arr.max() > self.bins):
      raise ValueError(
          f"codes must be in [0, {self.bins}] (bin/dictionary slots "
          f"0..{self.bins - 1} plus null={self.bins}); got "
          f"[{codes_arr.min()}, {codes_arr.max()}]")

    batch_std = _pairwise_centered_comoments(z_std_arr, self._i_idx,
                                             self._j_idx)
    batch_pit = _pairwise_centered_comoments(z_pit_arr, self._i_idx,
                                             self._j_idx)
    self.com_std = _merge_bivariate_comoments(self.com_std, batch_std)
    self.com_pit = _merge_bivariate_comoments(self.com_pit, batch_pit)
    self.counts2d += _pairwise_counts(codes_arr, self._i_idx, self._j_idx,
                                      self.bins)

  def merge(self, other: BivariateAccumulator) -> BivariateAccumulator:
    """The pairwise combination of `self` and `other`, as a new accumulator.

    Pure: neither operand is modified. `counts2d` is a plain element-wise
    sum — EXACT (integer counts never round) and so associative and
    commutative regardless of how a Beam runner trees the merges.
    `com_std`/`com_pit` combine via the same Chan-Golub-LeVeque/Pébay
    pairwise update `add_batch` uses to fold in a batch (Ruling R23):
    mathematically commutative and associative, but only up to
    floating-point rounding — two merge trees over the identical data can
    differ in the last few ulps, the same as `stats.moments.Moments.merge`
    (Ruling R25 #5; see `test_merge_commutes`/`test_merge_equals_single_
    pass`, which compare with `numpy.testing.assert_allclose`, not exact
    equality).

    Raises `ValueError` if `other` was built over a different `pairs` list
    or a different `bins` — the two accumulators must describe the
    identical joint tables to be combined at all.
    """
    if self.pairs != other.pairs:
      raise ValueError(
          f"cannot merge accumulators with different pairs: {self.pairs!r} "
          f"vs {other.pairs!r}")
    if self.bins != other.bins:
      raise ValueError(
          f"cannot merge accumulators with different bins: {self.bins!r} "
          f"vs {other.bins!r}")
    result = BivariateAccumulator(pairs=self.pairs, bins=self.bins)
    result.counts2d = self.counts2d + other.counts2d
    result.com_std = _merge_bivariate_comoments(self.com_std, other.com_std)
    result.com_pit = _merge_bivariate_comoments(self.com_pit, other.com_pit)
    return result


def _pairwise_centered_comoments(
    z: np.ndarray,
    i_idx: np.ndarray,
    j_idx: np.ndarray,
) -> np.ndarray:
  """`(P, 6)` batch centered co-moments `(n, mean_x, mean_y, M2_x, M2_y,
  C_xy)`, one row per pair, over the pairwise-complete rows of each pair's
  two columns of `z` — the "shifted data" algorithm (Chan, Golub & LeVeque,
  1983) computed over dense `(d, d)` matrix products (BLAS), not a
  per-pair loop.

  Every column is first shifted by its OWN mean over its own non-null rows
  (`shift`, `(d,)`) — a per-column, whole-batch mean, not restricted to any
  one pair's pairwise-complete subset — so the products summed below stay
  near the data's own scale regardless of `z`'s raw magnitude or offset.
  `mask` (`M` in the module docstring) is `~isnan(z)`; `z0` is the shifted
  `z` with nulls zeroed. Four `(d, d)` Gram matrices then give every
  column-PAIR's pairwise-complete count and shifted sums in one product
  each: `n_dd = M^T M`, `sx_dd = z0^T M` (pair `(c1, c2)`'s sum of
  shifted `c1`, restricted to rows where `c1` AND `c2` are both present —
  `M`'s second factor supplies that restriction), `sxx_dd = (z0^2)^T M`,
  `sxy_dd = z0^T z0` (symmetric; nulls already zeroed on both sides, so no
  extra masking is needed). Only the `P` pairs this accumulator tracks are
  then read off by `i_idx`/`j_idx` — `sx_dd`/`sxx_dd` are NOT symmetric
  (`sx_dd[c1, c2]` sums `c1`'s shifted values, `sx_dd[c2, c1]` sums `c2`'s,
  over the SAME row subset), so a pair's `y`-side sum reads the transpose:
  `sy = sx_dd[j_idx, i_idx]`, `syy = sxx_dd[j_idx, i_idx]`.

  The centered co-moments themselves recover exactly from these shifted
  sums: `mean = shift + S / n`, `M2 = Sxx - Sx**2 / n`, `C = Sxy - Sx*Sy /
  n` — exact regardless of how close `shift` is to the TRUE pairwise mean,
  since `shift` only needs to keep the summed products near-zero-scaled for
  numerical stability, not to equal any particular mean.

  A pair with zero pairwise-complete rows in this batch (`n == 0`) gets
  `mean = M2 = C = 0` exactly (not `shift + 0/0`): `_merge_bivariate_
  comoments`'s `n_b == 0` branch never reads those values when folding this
  batch in, but zeroing them keeps this function's own output well-defined
  on its own.
  """
  mask = ~np.isnan(z)
  mask_f = mask.astype(np.float64)
  col_count = mask_f.sum(axis=0)
  safe_col_count = np.where(col_count > 0, col_count, 1.0)
  zeroed_for_shift = np.where(mask, z, 0.0)
  shift = np.where(col_count > 0,
                   zeroed_for_shift.sum(axis=0) / safe_col_count, 0.0)
  z_shifted = z - shift[np.newaxis, :]
  z0 = np.where(mask, z_shifted, 0.0)

  n_dd = mask_f.T @ mask_f
  sx_dd = z0.T @ mask_f
  sxx_dd = (z0 * z0).T @ mask_f
  sxy_dd = z0.T @ z0

  n = n_dd[i_idx, j_idx]
  sx = sx_dd[i_idx, j_idx]
  sy = sx_dd[j_idx, i_idx]
  sxx = sxx_dd[i_idx, j_idx]
  syy = sxx_dd[j_idx, i_idx]
  sxy = sxy_dd[i_idx, j_idx]

  zero_pair = n <= 0.0
  safe_n = np.where(zero_pair, 1.0, n)
  mean_x = np.where(zero_pair, 0.0, shift[i_idx] + sx / safe_n)
  mean_y = np.where(zero_pair, 0.0, shift[j_idx] + sy / safe_n)
  m2_x = np.where(zero_pair, 0.0, sxx - sx * sx / safe_n)
  m2_y = np.where(zero_pair, 0.0, syy - sy * sy / safe_n)
  c_xy = np.where(zero_pair, 0.0, sxy - sx * sy / safe_n)
  return np.stack([n, mean_x, mean_y, m2_x, m2_y, c_xy], axis=1)


def _merge_bivariate_comoments(a: np.ndarray, b: np.ndarray) -> np.ndarray:
  """The Chan-Golub-LeVeque (1983) / Pébay (2008) pairwise combination of
  two `(P, 6)` centered co-moment states `[n, mean_x, mean_y, M2_x, M2_y,
  C_xy]`, vectorized over all `P` pairs at once (Ruling R23).

  `delta_x`/`delta_y` are the two states' mean differences; `frac_b = n_b /
  n` and `cross = n_a * n_b / n` (both `0` when `n == 0`, via a `safe_n`
  floor of `1` — dividing `0` by `1` is still `0`, so no branch is needed
  for the all-empty case). The formulas hold uniformly for `n_a == 0`
  (`frac_b == 1`: the merged mean/M2/C become exactly `b`'s) and `n_b == 0`
  (`frac_b == cross == 0`: they stay exactly `a`'s), because an empty
  state's `mean`/`M2`/`C` are always `0` (`BivariateAccumulator`'s initial
  state, and `_pairwise_centered_comoments`'s `zero_pair` rows) and `0`
  multiplied by a `0` fraction/cross term drops out regardless of its own
  value.
  """
  n_a, mx_a, my_a, m2x_a, m2y_a, cxy_a = (a[:, k] for k in range(6))
  n_b, mx_b, my_b, m2x_b, m2y_b, cxy_b = (b[:, k] for k in range(6))
  n = n_a + n_b
  safe_n = np.where(n > 0, n, 1.0)
  delta_x = mx_b - mx_a
  delta_y = my_b - my_a
  frac_b = n_b / safe_n
  cross = n_a * n_b / safe_n
  mean_x = mx_a + delta_x * frac_b
  mean_y = my_a + delta_y * frac_b
  m2_x = m2x_a + m2x_b + delta_x**2 * cross
  m2_y = m2y_a + m2y_b + delta_y**2 * cross
  c_xy = cxy_a + cxy_b + delta_x * delta_y * cross
  return np.stack([n, mean_x, mean_y, m2_x, m2_y, c_xy], axis=1)


def _pairwise_counts(
    codes: np.ndarray,
    i_idx: np.ndarray,
    j_idx: np.ndarray,
    bins: int,
) -> np.ndarray:
  """`(P, bins + 1, bins + 1)` batch joint-table counts, one table per pair,
  over EVERY row (nulls included via the `bins` code).

  A one-hot Gram matrix over all `d` columns at once, not a per-pair
  gather-and-`bincount`: `one_hot[row, c, :]` one-hot-encodes `codes[row,
  c]` into `bins + 1` slots, and `(b, d * (bins + 1))`-reshaped `one_hot`'s
  Gram matrix (`X^T X`, BLAS-backed) has one `(bins + 1, bins + 1)` block
  per `(c1, c2)` column pair, holding exactly that pair's joint table over
  every row — `P` of those `d * d` blocks are then read off by `i_idx`/
  `j_idx`. Measured ~10x faster than gathering a `(b, P)` slice per pair
  and calling `numpy.bincount` once, at the ~2000-pair/64-column, densely-
  paired scale this module targets (`P` close to `d choose 2`; see the
  module's perf test) — the trade-off is `O(b * d**2)` work regardless of
  `P`, so a plan with very few pairs spread over many columns (`P << d
  choose 2`) would favor the gather-and-`bincount` approach instead; this
  module does not need that regime.
  """
  b, d = codes.shape
  b1 = bins + 1
  one_hot = np.zeros((b, d, b1), dtype=np.float64)
  rows = np.arange(b)[:, np.newaxis]
  cols = np.arange(d)[np.newaxis, :]
  one_hot[rows, cols, codes] = 1.0
  flat = one_hot.reshape((b, d * b1))
  gram = (flat.T @ flat).reshape((d, b1, d, b1))
  return gram[i_idx, :, j_idx, :].astype(np.int64)


def pearson_from_comoments(c: np.ndarray) -> np.ndarray:
  """Pearson product-moment correlation (Pearson, 1896) per pair, `(P,)`,
  from centered co-moment rows `[n, mean_x, mean_y, M2_x, M2_y, C_xy]`
  (`BivariateAccumulator.com_std`/`.com_pit`, shape `(P, 6)`; Ruling R23):
  `rho = C_xy / sqrt(M2_x * M2_y)`.

  `NaN` — this module's internal null sentinel; callers convert it to
  `None` — where `n < 3` (fewer points than needed for a correlation to
  mean anything beyond a perfect fit), or either side is constant relative
  to its own scale: `M2 <= tol * n * max(1, mean ** 2)`, `tol =
  _PEARSON_ZERO_VARIANCE_TOL` (see that constant's comment for why the
  guard is relative rather than a literal `M2 <= 0`).
  """
  c_arr = np.asarray(c, dtype=np.float64)
  n = c_arr[..., 0]
  mean_x = c_arr[..., 1]
  mean_y = c_arr[..., 2]
  m2_x = c_arr[..., 3]
  m2_y = c_arr[..., 4]
  c_xy = c_arr[..., 5]
  with np.errstate(divide="ignore", invalid="ignore"):
    rho = c_xy / np.sqrt(m2_x * m2_y)
  floor_x = _PEARSON_ZERO_VARIANCE_TOL * n * np.maximum(1.0, mean_x**2)
  floor_y = _PEARSON_ZERO_VARIANCE_TOL * n * np.maximum(1.0, mean_y**2)
  invalid = (n < _MIN_PEARSON_N) | (m2_x <= floor_x) | (m2_y <= floor_y)
  return np.where(invalid, np.nan, rho)


def cramers_v_bias_corrected(table: np.ndarray) -> float | None:
  """Bergsma's (2013) bias-corrected Cramér's V from a 2-D contingency
  `table` of counts.

  All-zero rows and columns are dropped first (they carry no association
  information and would otherwise make an `expected` cell zero). From the
  remaining `r x k` table: `chi2` (expected cells from the margins, skipping
  any that are still zero), `phi2 = chi2 / n`, then the bias correction
  `phi2_tilde = max(0, phi2 - (k-1)(r-1)/(n-1))`, `k_tilde = k -
  (k-1)**2/(n-1)`, `r_tilde = r - (r-1)**2/(n-1)`, and finally `V_tilde =
  sqrt(phi2_tilde / min(k_tilde - 1, r_tilde - 1))`, clipped to `[0, 1]`
  (Ruling R25 #9: the formula is bounded there mathematically, but a
  finite-`n` ratio can round a hair past `1.0`).

  `None` when `n <= 1`, fewer than 2 non-empty rows or columns remain after
  dropping, or the final denominator is `<= 0` — every one of these makes
  the bias correction undefined rather than merely noisy. Raises
  `ValueError` if `table` is not 2-D.
  """
  t = np.asarray(table, dtype=np.float64)
  if t.ndim != _NDIM_2D:
    raise ValueError(f"table must be 2-D; got ndim={t.ndim}")
  t = t[t.sum(axis=1) > 0][:, t.sum(axis=0) > 0]
  r, k = t.shape
  n = float(t.sum())
  if n <= 1.0 or r < _MIN_NONEMPTY_ROWS_COLS or k < _MIN_NONEMPTY_ROWS_COLS:
    return None
  row_sums = t.sum(axis=1, keepdims=True)
  col_sums = t.sum(axis=0, keepdims=True)
  expected = row_sums * col_sums / n
  valid = expected > 0.0
  safe_expected = np.where(valid, expected, 1.0)
  chi2 = float(np.sum(np.where(valid, (t - expected)**2 / safe_expected, 0.0)))
  phi2 = chi2 / n
  phi2_tilde = max(0.0, phi2 - (k - 1) * (r - 1) / (n - 1))
  k_tilde = k - (k - 1)**2 / (n - 1)
  r_tilde = r - (r - 1)**2 / (n - 1)
  denom = min(k_tilde - 1.0, r_tilde - 1.0)
  if denom <= 0.0:
    return None
  return float(np.clip(math.sqrt(phi2_tilde / denom), 0.0, 1.0))


def nmi_min(table: np.ndarray) -> float | None:
  """Mutual information normalized by the smaller marginal entropy,
  `I(X; Y) / min(H_X, H_Y)`, in natural logs (units cancel in the ratio),
  from a 2-D contingency `table` of counts (Cover & Thomas, 2006), clipped
  to `[0, 1]` (Ruling R25 #9: bounded there mathematically, but a
  finite-sample ratio can round a hair past `1.0`).

  Not the same normalization as `sklearn.metrics.normalized_mutual_info_
  score`'s default (arithmetic mean of `H_X`, `H_Y`); this deliberately
  uses the min instead — a caller comparing to sklearn needs a hand-computed
  value, not that default (see `test_nmi_min_hand_computed_value`).

  `None` when `table` has zero total mass, or fewer than 2 of its rows or
  of its columns hold any mass at all (Ruling R25 #1: decided on COUNTS,
  `numpy.count_nonzero` of the row/column sums — a marginal that is only
  ARITHMETICALLY near-constant, e.g. `1 - 1e-16` from float rounding, still
  has a genuinely nonzero entropy and must not be misread as `min_h <= 0`;
  a floating check on `H_X`/`H_Y` alone can silently miss that and divide
  by a near-zero entropy, producing a wildly inflated ratio — see
  `test_nmi_min_none_for_near_constant_marginal_regression`). Raises
  `ValueError` if `table` is not 2-D.
  """
  t = np.asarray(table, dtype=np.float64)
  if t.ndim != _NDIM_2D:
    raise ValueError(f"table must be 2-D; got ndim={t.ndim}")
  n = float(t.sum())
  if n <= 0.0:
    return None
  row_sums = t.sum(axis=1)
  col_sums = t.sum(axis=0)
  if (np.count_nonzero(row_sums) < _MIN_NONEMPTY_ROWS_COLS or
      np.count_nonzero(col_sums) < _MIN_NONEMPTY_ROWS_COLS):
    return None
  p = t / n
  p_row = row_sums / n
  p_col = col_sums / n
  h_x = _entropy_nats(p_row)
  h_y = _entropy_nats(p_col)
  min_h = min(h_x, h_y)
  if min_h <= 0.0:
    return None
  outer = np.outer(p_row, p_col)
  mask = p > 0.0
  with np.errstate(divide="ignore", invalid="ignore"):
    terms = p * np.log(p / outer)
  mi = float(np.sum(np.where(mask, terms, 0.0)))
  return float(np.clip(mi / min_h, 0.0, 1.0))


def _entropy_nats(p: np.ndarray) -> float:
  """Shannon entropy in nats, `-sum(p * ln(p))` over `p > 0` only (`x *
  ln(x) -> 0` as `x -> 0`, so a zero-mass category contributes nothing
  rather than raising on `ln(0)`).
  """
  mask = p > 0.0
  with np.errstate(divide="ignore", invalid="ignore"):
    terms = p * np.log(p)
  return float(-np.sum(np.where(mask, terms, 0.0)))


def contingency_tvd(t_src: np.ndarray, t_syn: np.ndarray) -> float | None:
  """Total variation distance `0.5 * sum(|p_ab - q_ab|)` between the
  normalized `t_src` and `t_syn` joint tables (same shape; `p`/`q` are
  `t_src`/`t_syn` each divided by their own total).

  `None` when either table has zero total mass. Raises `ValueError` if
  `t_src` and `t_syn` are not the same shape — they must be counted on the
  identical joint dictionary (source values plus the null bin) to be
  comparable at all.
  """
  src = np.asarray(t_src, dtype=np.float64)
  syn = np.asarray(t_syn, dtype=np.float64)
  if src.shape != syn.shape:
    raise ValueError(
        f"t_src and t_syn must be the same shape: got {src.shape} and "
        f"{syn.shape}")
  total_src = float(src.sum())
  total_syn = float(syn.sum())
  if total_src <= 0.0 or total_syn <= 0.0:
    return None
  p = src / total_src
  q = syn / total_syn
  return float(0.5 * np.sum(np.abs(p - q)))


def corr_rms_max(delta: np.ndarray) -> tuple[float | None, float | None]:
  """`(sqrt(mean(Delta**2)), max(abs(Delta)))` over the finite entries of a
  1-D array `delta` of per-pair correlation differences (`table.corr_rms_
  delta`, `table.corr_max_delta`); a non-finite entry (`NaN` — a pair
  `pearson_from_comoments` could not score — or `+-inf`) is skipped
  (Ruling R25 #8).

  `(None, None)` when no entry is finite — nothing to summarize. Raises
  `ValueError` if `delta` is not 1-D.
  """
  d = np.asarray(delta, dtype=np.float64)
  if d.ndim != 1:
    raise ValueError(f"delta must be 1-D; got ndim={d.ndim}")
  valid = d[np.isfinite(d)]
  if valid.size == 0:
    return None, None
  rms = float(np.sqrt(np.mean(valid**2)))
  max_abs = float(np.max(np.abs(valid)))
  return rms, max_abs
