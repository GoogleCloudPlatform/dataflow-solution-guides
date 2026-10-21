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
"""Tests for `sdfb_evaluation.stats.dependence` (Task 10).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from scipy import stats
from scipy.stats import contingency

from sdfb_evaluation.stats.dependence import BivariateAccumulator
from sdfb_evaluation.stats.dependence import contingency_tvd
from sdfb_evaluation.stats.dependence import corr_rms_max
from sdfb_evaluation.stats.dependence import cramers_v_bias_corrected
from sdfb_evaluation.stats.dependence import nmi_min
from sdfb_evaluation.stats.dependence import pearson_from_comoments

# ---------------------------------------------------------------------------
# BivariateAccumulator — construction
# ---------------------------------------------------------------------------


def test_empty_accumulator_is_a_valid_starting_point():
  acc = BivariateAccumulator(pairs=((0, 1), (1, 2)))
  assert acc.counts2d.shape == (2, 11, 11)
  assert acc.com_std.shape == (2, 6)
  assert acc.com_pit.shape == (2, 6)
  assert acc.counts2d.sum() == 0
  assert acc.com_std.sum() == 0
  assert acc.com_pit.sum() == 0


def test_accumulator_honours_custom_bins():
  acc = BivariateAccumulator(pairs=((0, 1),), bins=3)
  assert acc.counts2d.shape == (1, 4, 4)


def test_accumulator_with_no_pairs_is_empty_but_valid():
  acc = BivariateAccumulator(pairs=())
  assert acc.counts2d.shape == (0, 11, 11)
  assert acc.com_std.shape == (0, 6)


def test_negative_pair_index_raises():
  with pytest.raises(ValueError, match="non-negative"):
    BivariateAccumulator(pairs=((-1, 0),))


def test_add_batch_and_merge_on_a_no_pairs_accumulator_does_not_crash():
  acc = BivariateAccumulator(pairs=())
  z = np.zeros((5, 3))
  codes = np.zeros((5, 3), dtype=np.int64)
  acc.add_batch(z, z, codes)
  merged = acc.merge(BivariateAccumulator(pairs=()))
  assert merged.counts2d.shape == (0, 11, 11)
  assert merged.com_std.shape == (0, 6)


# ---------------------------------------------------------------------------
# add_batch — centered co-moments, pairwise-complete
# ---------------------------------------------------------------------------


def _manual_centered_comoments(x: np.ndarray, y: np.ndarray) -> np.ndarray:
  """`[n, mean_x, mean_y, M2_x, M2_y, C_xy]` computed directly (no shift
  trick), for cross-checking `add_batch` on small, well-scaled examples.
  """
  mean_x, mean_y = x.mean(), y.mean()
  return np.array([
      x.size,
      mean_x,
      mean_y,
      ((x - mean_x)**2).sum(),
      ((y - mean_y)**2).sum(),
      ((x - mean_x) * (y - mean_y)).sum(),
  ])


def test_add_batch_comoments_match_manual_pairwise_complete_sums():
  # column 0 and 1 form the pair; row 2 has a null on column 0, row 3 a
  # null on column 1 — neither row may contribute to the pair's co-moments.
  z = np.array([
      [1.0, 2.0],
      [2.0, 1.0],
      [np.nan, 5.0],
      [3.0, np.nan],
      [4.0, 0.0],
  ])
  codes = np.zeros_like(z, dtype=np.int64)
  acc = BivariateAccumulator(pairs=((0, 1),))
  acc.add_batch(z, z, codes)

  complete = z[~np.isnan(z).any(axis=1)]
  expected = _manual_centered_comoments(complete[:, 0], complete[:, 1])
  np.testing.assert_allclose(acc.com_std[0], expected, atol=1e-9)
  np.testing.assert_allclose(acc.com_pit[0], expected, atol=1e-9)


def test_add_batch_vectorizes_correctly_over_multiple_pairs():
  rng = np.random.default_rng(42)
  b, d = 200, 4
  z = rng.normal(size=(b, d))
  # Scatter some nulls independently per column.
  for col in range(d):
    null_rows = rng.choice(b, size=10, replace=False)
    z[null_rows, col] = np.nan
  codes = np.zeros((b, d), dtype=np.int64)

  pairs = ((0, 1), (0, 2), (2, 3))
  acc = BivariateAccumulator(pairs=pairs)
  acc.add_batch(z, z, codes)

  for p_idx, (i, j) in enumerate(pairs):
    mask = ~np.isnan(z[:, i]) & ~np.isnan(z[:, j])
    expected = _manual_centered_comoments(z[mask, i], z[mask, j])
    np.testing.assert_allclose(acc.com_std[p_idx], expected, atol=1e-9)


def test_add_batch_shape_mismatch_raises():
  acc = BivariateAccumulator(pairs=((0, 1),))
  z = np.zeros((3, 2))
  codes = np.zeros((3, 2), dtype=np.int64)
  with pytest.raises(ValueError, match="shape"):
    acc.add_batch(z, np.zeros((4, 2)), codes)


def test_add_batch_wrong_ndim_raises():
  acc = BivariateAccumulator(pairs=((0, 1),))
  z1d = np.zeros(4)
  with pytest.raises(ValueError, match="2-D"):
    acc.add_batch(z1d, z1d, np.zeros(4, dtype=np.int64))


def test_add_batch_pair_column_out_of_range_raises():
  acc = BivariateAccumulator(pairs=((0, 5),))
  z = np.zeros((3, 2))
  codes = np.zeros((3, 2), dtype=np.int64)
  with pytest.raises(ValueError, match="column index"):
    acc.add_batch(z, z, codes)


def test_add_batch_codes_out_of_range_raises():
  acc = BivariateAccumulator(pairs=((0, 1),), bins=2)
  z = np.zeros((2, 2))
  bad_codes = np.array([[0, 1], [3, 0]], dtype=np.int64)  # 3 > bins (=2)
  with pytest.raises(ValueError, match="codes must be"):
    acc.add_batch(z, z, bad_codes)


def test_add_batch_rejects_non_integer_codes_dtype():
  acc = BivariateAccumulator(pairs=((0, 1),))
  z = np.zeros((2, 2))
  float_codes = np.array([[0.0, 1.0], [1.0, 0.0]])
  with pytest.raises(ValueError, match="integer dtype"):
    acc.add_batch(z, z, float_codes)


def test_add_batch_rejects_bool_codes_dtype():
  # bool is not treated as an integer dtype here, even though numpy allows
  # arithmetic on it — a caller must send real bin/dictionary slot ints.
  acc = BivariateAccumulator(pairs=((0, 1),))
  z = np.zeros((2, 2))
  bool_codes = np.array([[False, True], [True, False]])
  with pytest.raises(ValueError, match="integer dtype"):
    acc.add_batch(z, z, bool_codes)


def test_add_batch_rejects_positive_infinity_in_z_std():
  acc = BivariateAccumulator(pairs=((0, 1),))
  z_std = np.array([[1.0, 2.0], [np.inf, 3.0]])
  z_pit = np.zeros((2, 2))
  codes = np.zeros((2, 2), dtype=np.int64)
  with pytest.raises(ValueError, match="inf"):
    acc.add_batch(z_std, z_pit, codes)


def test_add_batch_rejects_negative_infinity_in_z_pit():
  acc = BivariateAccumulator(pairs=((0, 1),))
  z_std = np.zeros((2, 2))
  z_pit = np.array([[1.0, 2.0], [3.0, -np.inf]])
  codes = np.zeros((2, 2), dtype=np.int64)
  with pytest.raises(ValueError, match="inf"):
    acc.add_batch(z_std, z_pit, codes)


def test_add_batch_empty_batch_is_a_no_op():
  acc = BivariateAccumulator(pairs=((0, 1),))
  acc.add_batch(
      np.zeros((0, 2)), np.zeros((0, 2)), np.zeros((0, 2), dtype=np.int64))
  assert acc.com_std.sum() == 0
  assert acc.counts2d.sum() == 0


# ---------------------------------------------------------------------------
# add_batch — counts2d, null bin included on both axes
# ---------------------------------------------------------------------------


def test_add_batch_counts2d_includes_the_null_bin_on_both_axes():
  bins = 2  # valid slots 0, 1; null = 2
  codes = np.array(
      [
          [0, 1],
          [2, 0],  # null on column 0
          [1, 2],  # null on column 1
          [2, 2],  # null on both
      ],
      dtype=np.int64)
  z = np.zeros_like(codes, dtype=np.float64)
  acc = BivariateAccumulator(pairs=((0, 1),), bins=bins)
  acc.add_batch(z, z, codes)

  table = acc.counts2d[0]
  assert table.shape == (3, 3)
  assert table[0, 1] == 1
  assert table[2, 0] == 1
  assert table[1, 2] == 1
  assert table[2, 2] == 1
  assert table.sum() == 4


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


def _random_accumulator(pairs, bins, seed, b=50, d=3):
  rng = np.random.default_rng(seed)
  z_std = rng.normal(size=(b, d))
  z_pit = rng.uniform(size=(b, d))
  codes = rng.integers(0, bins + 1, size=(b, d)).astype(np.int64)
  acc = BivariateAccumulator(pairs=pairs, bins=bins)
  acc.add_batch(z_std, z_pit, codes)
  return acc


def test_merge_is_pure():
  pairs = ((0, 1), (1, 2))
  a = _random_accumulator(pairs, bins=4, seed=1)
  b = _random_accumulator(pairs, bins=4, seed=2)
  a_counts_before = a.counts2d.copy()
  a_com_std_before = a.com_std.copy()
  b_counts_before = b.counts2d.copy()
  merged = a.merge(b)
  np.testing.assert_array_equal(a.counts2d, a_counts_before)
  np.testing.assert_array_equal(a.com_std, a_com_std_before)
  np.testing.assert_array_equal(b.counts2d, b_counts_before)
  np.testing.assert_array_equal(merged.counts2d, a.counts2d + b.counts2d)


def test_merge_commutes():
  pairs = ((0, 1), (1, 2))
  a = _random_accumulator(pairs, bins=4, seed=3)
  b = _random_accumulator(pairs, bins=4, seed=4)
  left = a.merge(b)
  right = b.merge(a)
  np.testing.assert_array_equal(left.counts2d, right.counts2d)
  np.testing.assert_allclose(left.com_std, right.com_std, atol=1e-9)
  np.testing.assert_allclose(left.com_pit, right.com_pit, atol=1e-9)


def test_merge_equals_single_pass():
  pairs = ((0, 2), (1, 2))
  rng = np.random.default_rng(5)
  b, d = 80, 3
  z_std = rng.normal(size=(b, d))
  z_pit = rng.uniform(size=(b, d))
  codes = rng.integers(0, 6, size=(b, d)).astype(np.int64)

  whole = BivariateAccumulator(pairs=pairs, bins=5)
  whole.add_batch(z_std, z_pit, codes)

  split_a = BivariateAccumulator(pairs=pairs, bins=5)
  split_a.add_batch(z_std[:30], z_pit[:30], codes[:30])
  split_b = BivariateAccumulator(pairs=pairs, bins=5)
  split_b.add_batch(z_std[30:], z_pit[30:], codes[30:])
  merged = split_a.merge(split_b)

  np.testing.assert_array_equal(whole.counts2d, merged.counts2d)
  np.testing.assert_allclose(whole.com_std, merged.com_std, atol=1e-8)
  np.testing.assert_allclose(whole.com_pit, merged.com_pit, atol=1e-8)


@settings(max_examples=25)
@given(
    st.integers(min_value=0, max_value=40),
    st.integers(min_value=0, max_value=40))
def test_merge_is_associative_and_commutative_property(n1, n2):
  pairs = ((0, 1),)
  rng = np.random.default_rng(n1 * 1000 + n2)
  a = BivariateAccumulator(pairs=pairs, bins=3)
  b = BivariateAccumulator(pairs=pairs, bins=3)
  c = BivariateAccumulator(pairs=pairs, bins=3)
  for acc, n in ((a, n1), (b, n2), (c, 7)):
    if n:
      z_std = rng.normal(size=(n, 2))
      z_pit = rng.uniform(size=(n, 2))
      codes = rng.integers(0, 4, size=(n, 2)).astype(np.int64)
      acc.add_batch(z_std, z_pit, codes)
  left = a.merge(b).merge(c)
  right = a.merge(b.merge(c))
  np.testing.assert_array_equal(left.counts2d, right.counts2d)
  np.testing.assert_array_equal(a.merge(b).counts2d, b.merge(a).counts2d)


def test_merge_raises_on_mismatched_pairs():
  a = BivariateAccumulator(pairs=((0, 1),))
  b = BivariateAccumulator(pairs=((0, 2),))
  with pytest.raises(ValueError, match="pairs"):
    a.merge(b)


def test_merge_raises_on_mismatched_bins():
  a = BivariateAccumulator(pairs=((0, 1),), bins=4)
  b = BivariateAccumulator(pairs=((0, 1),), bins=5)
  with pytest.raises(ValueError, match="bins"):
    a.merge(b)


# ---------------------------------------------------------------------------
# pearson_from_comoments
# ---------------------------------------------------------------------------


def test_pearson_matches_corrcoef_on_complete_rows():
  rng = np.random.default_rng(7)
  b = 300
  z = rng.normal(size=(b, 2))
  z[:, 1] = 0.6 * z[:, 0] + 0.4 * rng.normal(size=b)
  null_rows = rng.choice(b, size=15, replace=False)
  z[null_rows[:8], 0] = np.nan
  z[null_rows[8:], 1] = np.nan
  codes = np.zeros((b, 2), dtype=np.int64)

  acc = BivariateAccumulator(pairs=((0, 1),))
  acc.add_batch(z, z, codes)
  rho = pearson_from_comoments(acc.com_std)[0]

  complete = z[~np.isnan(z).any(axis=1)]
  expected = np.corrcoef(complete[:, 0], complete[:, 1])[0, 1]
  assert rho == pytest.approx(expected, abs=1e-9)


def test_pearson_none_below_min_n():
  # n=2 < 3 forces NaN regardless of the other (arbitrary, non-degenerate)
  # co-moment values.
  c = np.array([[2, 1.0, 1.0, 3.0, 4.0, 1.5]])
  result = pearson_from_comoments(c)
  assert math.isnan(result[0])


def test_pearson_none_for_zero_variance():
  c = np.array([[10.0, 5.0, 3.0, 0.0, 9.0, 2.0]])  # M2_x == 0
  result = pearson_from_comoments(c)
  assert math.isnan(result[0])


def test_pearson_none_for_relatively_tiny_variance_at_a_large_mean():
  # M2_x is absolutely tiny but also tiny RELATIVE to n * mean_x**2 -- the
  # relative guard must still catch this as "constant", the way a badly
  # offset plan-z column's float64 shift residue would look.
  c = np.array([[1000.0, 1.0e6, 0.0, 1e-5, 5.0, 0.1]])
  result = pearson_from_comoments(c)
  assert math.isnan(result[0])


def test_pearson_small_variance_near_zero_mean_is_not_flagged():
  # The same absolute M2_x as above, but with mean_x == 0: the `max(1,
  # mean**2)` floor keeps this from being wrongly treated as degenerate.
  c = np.array([[1000.0, 0.0, 0.0, 50.0, 50.0, 25.0]])
  result = pearson_from_comoments(c)
  assert not math.isnan(result[0])


def test_pearson_vectorizes_over_multiple_rows():
  # Two independent pairs in one accumulator: perfect positive and perfect
  # negative correlation.
  x = np.arange(5, dtype=np.float64)
  z = np.stack([x, x.copy(), x, -x], axis=1)  # columns: x, x, x, -x
  codes = np.zeros((5, 4), dtype=np.int64)
  acc = BivariateAccumulator(pairs=((0, 1), (2, 3)))
  acc.add_batch(z, z, codes)
  result = pearson_from_comoments(acc.com_std)
  np.testing.assert_allclose(result, [1.0, -1.0], atol=1e-9)


# ---------------------------------------------------------------------------
# numerical stability (Ruling R23)
# ---------------------------------------------------------------------------


def test_epoch_microsecond_raw_sum_form_loses_precision():
  """Documents the pathology `add_batch`'s shifted-data algorithm exists to
  avoid: the naive sum-form variance numerator `n * Sxx - Sx**2`, applied
  directly to unshifted epoch-microsecond-scale values (~1.7e15), differs
  two ~1e21-magnitude terms to recover a true value around ~1e11 — far
  below float64's ~16 significant digits — so the result is off by many
  orders of magnitude. This is a deterministic assertion (no tautological
  branch): `numpy.ndarray.var` computes the true variance via its own
  stable two-pass (shift-by-mean) algorithm, independent of the formula
  under test.
  """
  n = 2000
  x = 1.7e15 + np.arange(n, dtype=np.float64)
  true_var = x.var()
  sx = float(x.sum())
  sxx = float((x * x).sum())
  raw_numerator = n * sxx - sx * sx
  true_numerator = n**2 * true_var
  assert abs(raw_numerator - true_numerator) > 1e6 * abs(true_numerator)


def test_offset_plan_z_is_stable_across_many_batches():
  """A `z_std` column offset far from zero (a stale or imperfect plan
  mean/std, `mean/std` around 1e6) with its own tiny spread, split across
  many 8192-row batches, must still give `pearson_from_comoments` within
  1e-9 of `np.corrcoef` on the raw data — the shifted-data-per-batch plus
  Chan-Golub-LeVeque/Pébay pairwise merge never sums a large raw value
  directly, so this holds regardless of the offset or how many batches the
  input is split across (Ruling R23).
  """
  rng = np.random.default_rng(21)
  n_total = 8192 * 6
  raw_x = rng.normal(size=n_total)
  raw_y = 0.7 * raw_x + 0.3 * rng.normal(size=n_total)
  # A badly-offset "standardization": mean 1.0e6, std 1.0 -- mean/std of
  # 1e6, the same order of pathology as the raw epoch-microsecond case.
  offset = 1.0e6
  z_std = np.stack([raw_x + offset, raw_y + offset], axis=1)
  codes = np.zeros((n_total, 2), dtype=np.int64)

  acc = BivariateAccumulator(pairs=((0, 1),))
  batch_size = 8192
  for start in range(0, n_total, batch_size):
    end = min(start + batch_size, n_total)
    acc.add_batch(z_std[start:end], z_std[start:end], codes[start:end])

  rho = pearson_from_comoments(acc.com_std)[0]
  expected = np.corrcoef(raw_x, raw_y)[0, 1]
  assert abs(rho - expected) < 1e-9


def test_constant_column_is_nan_after_merging_many_batches():
  """A genuinely constant column, split across several `add_batch` calls
  and accumulator merges, must still read as zero-variance (NaN) — the
  relative guard must not be fooled by floating-point residue that
  accumulates differently across many folds.
  """
  rng = np.random.default_rng(23)
  pairs = ((0, 1),)
  acc_a = BivariateAccumulator(pairs=pairs)
  acc_b = BivariateAccumulator(pairs=pairs)
  for acc in (acc_a, acc_b):
    for _ in range(5):
      n = 400
      x = np.full(n, 42.0)  # constant
      y = rng.normal(size=n)
      z = np.stack([x, y], axis=1)
      codes = np.zeros((n, 2), dtype=np.int64)
      acc.add_batch(z, z, codes)
  merged = acc_a.merge(acc_b)
  rho = pearson_from_comoments(merged.com_std)[0]
  assert math.isnan(rho)


def test_add_batch_perf_smoke_dense_pairs():
  """A densely-paired plan (`P` close to `d choose 2`) at the scale this
  module targets — `d=64` columns, ~2000 pairs, `b=8192` rows — must
  complete `add_batch` well inside the 0.5s budget: measured ~0.1-0.2s on
  a laptop CPU (one-hot/Gram-matrix `counts2d` plus the shifted-data
  co-moment matrix products, both BLAS-backed; see the module docstring
  and `_pairwise_counts`'s docstring for the ~10x speedup over a per-pair
  gather-and-bincount at this pair count). Not marked `slow` (that marker
  means ">5s" repo-wide) — this test is fast by design.
  """
  rng = np.random.default_rng(99)
  d = 64
  pairs = tuple((i, j) for i in range(d) for j in range(i + 1, d))
  b = 8192
  z_std = rng.normal(size=(b, d))
  z_pit = rng.uniform(size=(b, d))
  codes = rng.integers(0, 11, size=(b, d)).astype(np.int64)

  acc = BivariateAccumulator(pairs=pairs)
  start = time.perf_counter()
  acc.add_batch(z_std, z_pit, codes)
  elapsed = time.perf_counter() - start
  assert elapsed < 0.5, f"add_batch took {elapsed:.3f}s for P={len(pairs)}, d={d}, b={b}"


# ---------------------------------------------------------------------------
# PIT Spearman — mid-CDF contract (Ruling R24)
# ---------------------------------------------------------------------------


def _mid_rank_pit(x: np.ndarray) -> np.ndarray:
  """`(mid rank - 0.5) / n`, ties at their average rank — the mid-CDF PIT
  `add_batch`/`com_pit` require (Ruling R24).
  """
  n = x.size
  return (stats.rankdata(x, method="average") - 0.5) / n


def test_pit_spearman_matches_scipy_within_tolerance():
  rng = np.random.default_rng(11)
  n = 500
  x = rng.normal(size=n)
  y = 2.0 * x + rng.normal(scale=0.5, size=n)
  z_pit = np.stack([_mid_rank_pit(x), _mid_rank_pit(y)], axis=1)
  codes = np.zeros((n, 2), dtype=np.int64)

  acc = BivariateAccumulator(pairs=((0, 1),))
  acc.add_batch(z_pit, z_pit, codes)
  rho_pit = pearson_from_comoments(acc.com_pit)[0]

  expected_rho, _ = stats.spearmanr(x, y)
  assert abs(rho_pit - expected_rho) < 0.01


@pytest.mark.parametrize("n_levels", [10, 50])
def test_pit_spearman_with_ties_matches_scipy_tightly(n_levels):
  """Mid-rank PIT (average rank for ties, per Ruling R24) makes `pearson_
  from_comoments(com_pit)` agree with `scipy.stats.spearmanr`'s own
  tie-corrected rank correlation to near machine precision, because
  `spearmanr` computes Pearson correlation of the SAME average ranks
  internally (an affine transform of the mid-CDF PIT, which Pearson
  correlation is invariant to per side).
  """
  rng = np.random.default_rng(29)
  n = 2000
  x = rng.integers(0, n_levels, size=n).astype(np.float64)
  y = rng.integers(0, n_levels, size=n).astype(np.float64)  # also heavily tied
  z_pit = np.stack([_mid_rank_pit(x), _mid_rank_pit(y)], axis=1)
  codes = np.zeros((n, 2), dtype=np.int64)

  acc = BivariateAccumulator(pairs=((0, 1),))
  acc.add_batch(z_pit, z_pit, codes)
  rho_pit = pearson_from_comoments(acc.com_pit)[0]

  expected_rho, _ = stats.spearmanr(x, y)
  assert abs(rho_pit - expected_rho) < 1e-9


# ---------------------------------------------------------------------------
# cramers_v_bias_corrected
# ---------------------------------------------------------------------------


def test_cramers_v_near_zero_for_independent_5x5_at_10k():
  rng = np.random.default_rng(13)
  n = 10_000
  rows = rng.integers(0, 5, size=n)
  cols = rng.integers(0, 5, size=n)
  table = np.zeros((5, 5), dtype=np.int64)
  np.add.at(table, (rows, cols), 1)
  v = cramers_v_bias_corrected(table)
  assert v is not None
  assert v < 0.03


def test_cramers_v_one_for_diagonal_table():
  table = np.eye(4, dtype=np.int64) * 100
  v = cramers_v_bias_corrected(table)
  assert v == pytest.approx(1.0, abs=1e-9)


def test_cramers_v_le_scipy_uncorrected_association():
  rng = np.random.default_rng(17)
  table = rng.integers(1, 50, size=(4, 6)).astype(np.int64)
  v = cramers_v_bias_corrected(table)
  v_scipy = contingency.association(table, method="cramer")
  assert v is not None
  assert v <= v_scipy + 1e-9


def test_cramers_v_never_exceeds_one():
  rng = np.random.default_rng(31)
  for _ in range(20):
    r, k = rng.integers(2, 6, size=2)
    table = rng.integers(0, 30, size=(r, k)).astype(np.int64)
    v = cramers_v_bias_corrected(table)
    if v is not None:
      assert v <= 1.0


def test_cramers_v_none_for_degenerate_table():
  # After dropping the all-zero row/column, only a 1x1 table remains.
  assert cramers_v_bias_corrected(np.array([[1, 0], [0, 0]])) is None


def test_cramers_v_drops_all_zero_rows_and_columns():
  # A zero row/column padded around an otherwise-independent table must
  # not change the result versus the table with them removed.
  rng = np.random.default_rng(19)
  n = 4000
  rows = rng.integers(0, 3, size=n)
  cols = rng.integers(0, 3, size=n)
  core = np.zeros((3, 3), dtype=np.int64)
  np.add.at(core, (rows, cols), 1)
  padded = np.zeros((5, 4), dtype=np.int64)
  padded[1:4, 1:4] = core
  assert cramers_v_bias_corrected(padded) == pytest.approx(
      cramers_v_bias_corrected(core))


def test_cramers_v_raises_for_non_2d_table():
  with pytest.raises(ValueError, match="2-D"):
    cramers_v_bias_corrected(np.array([1, 2, 3]))


# ---------------------------------------------------------------------------
# nmi_min
# ---------------------------------------------------------------------------


def test_nmi_min_hand_computed_value():
  table = np.array([[3.0, 1.0], [1.0, 3.0]])
  n = table.sum()
  p = table / n
  p_row = p.sum(axis=1)
  p_col = p.sum(axis=0)
  h_x = -sum(pi * math.log(pi) for pi in p_row if pi > 0)
  h_y = -sum(pi * math.log(pi) for pi in p_col if pi > 0)
  mi = 0.0
  for i in range(2):
    for j in range(2):
      if p[i, j] > 0:
        mi += p[i, j] * math.log(p[i, j] / (p_row[i] * p_col[j]))
  expected = mi / min(h_x, h_y)

  result = nmi_min(table)
  assert result == pytest.approx(expected, rel=1e-9)


def test_nmi_min_is_one_for_perfect_diagonal_association():
  table = np.array([[10, 0], [0, 10]], dtype=np.float64)
  assert nmi_min(table) == pytest.approx(1.0, abs=1e-9)


def test_nmi_min_never_exceeds_one():
  rng = np.random.default_rng(37)
  for _ in range(20):
    r, k = rng.integers(2, 6, size=2)
    table = rng.integers(0, 30, size=(r, k)).astype(np.float64)
    v = nmi_min(table)
    if v is not None:
      assert 0.0 <= v <= 1.0


def test_nmi_min_none_for_zero_mass():
  assert nmi_min(np.zeros((2, 2))) is None


def test_nmi_min_none_when_one_side_is_constant():
  # Every row lands in category 0 of X: only 1 non-empty row.
  table = np.array([[5, 5], [0, 0]], dtype=np.float64)
  assert nmi_min(table) is None


def test_nmi_min_none_for_near_constant_marginal_regression():
  """Regression for the CRITICAL review-round-1 bug: with float marginals
  that are `1 - epsilon` away from constant (rather than exactly constant),
  the OLD `min_h <= 0` floating check missed it (`H` rounds to ~1e-16, not
  exactly 0), producing a wildly inflated ratio like ~1.9999999999999998.
  The fix decides on integer-like COUNTS instead: fewer than 2 non-empty
  rows (or columns) is always `None`, regardless of any floating entropy
  computation.
  """
  # All mass in row 0; row 1 is entirely empty -- only 1 non-empty row.
  table = np.array([[1, 4, 1], [0, 0, 0]], dtype=np.float64)
  assert nmi_min(table) is None


def test_nmi_min_none_for_near_constant_marginal_11x11():
  table = np.zeros((11, 11), dtype=np.float64)
  table[0, :] = np.arange(1, 12)  # all mass in row 0
  assert nmi_min(table) is None


def test_nmi_min_raises_for_non_2d_table():
  with pytest.raises(ValueError, match="2-D"):
    nmi_min(np.array([1.0, 2.0]))


# ---------------------------------------------------------------------------
# contingency_tvd
# ---------------------------------------------------------------------------


def test_contingency_tvd_zero_for_identical_tables():
  table = np.array([[3.0, 1.0], [1.0, 3.0]])
  assert contingency_tvd(table, table) == pytest.approx(0.0, abs=1e-12)


def test_contingency_tvd_one_for_disjoint_tables():
  t_src = np.array([[1.0, 0.0], [0.0, 0.0]])
  t_syn = np.array([[0.0, 0.0], [0.0, 1.0]])
  assert contingency_tvd(t_src, t_syn) == pytest.approx(1.0, abs=1e-12)


def test_contingency_tvd_shape_mismatch_raises():
  with pytest.raises(ValueError, match="shape"):
    contingency_tvd(np.zeros((2, 2)), np.zeros((2, 3)))


def test_contingency_tvd_none_for_zero_mass():
  assert contingency_tvd(np.zeros((2, 2)), np.array([[1.0, 0.0],
                                                     [0.0, 1.0]])) is None


# ---------------------------------------------------------------------------
# corr_rms_max
# ---------------------------------------------------------------------------


def test_corr_rms_max_basic():
  delta = np.array([0.1, -0.2, 0.3])
  rms, max_abs = corr_rms_max(delta)
  assert rms == pytest.approx(math.sqrt(np.mean(delta**2)))
  assert max_abs == pytest.approx(0.3)


def test_corr_rms_max_skips_nan():
  delta = np.array([0.1, np.nan, -0.5])
  rms, max_abs = corr_rms_max(delta)
  assert rms == pytest.approx(math.sqrt((0.1**2 + 0.5**2) / 2))
  assert max_abs == pytest.approx(0.5)


def test_corr_rms_max_skips_infinity():
  delta = np.array([0.1, np.inf, -0.5, -np.inf])
  rms, max_abs = corr_rms_max(delta)
  assert rms == pytest.approx(math.sqrt((0.1**2 + 0.5**2) / 2))
  assert max_abs == pytest.approx(0.5)


def test_corr_rms_max_none_when_no_finite_value():
  assert corr_rms_max(np.array([np.nan, np.nan])) == (None, None)


def test_corr_rms_max_none_for_empty_array():
  assert corr_rms_max(np.array([])) == (None, None)


def test_corr_rms_max_raises_for_non_1d_array():
  with pytest.raises(ValueError, match="1-D"):
    corr_rms_max(np.zeros((2, 2)))
