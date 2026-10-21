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
"""Tests for `sdfb_evaluation.stats.binned` (Task 6).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from scipy import stats

from sdfb_evaluation.stats.binned import (
    bin_counts,
    decile_edges,
    decile_ks_legacy,
    ks_bracket,
    pit_w1,
    profile_edges,
    quantiles_from_bins,
    tail_masses,
    union_edges,
    w1_from_bins,
)

_GRID = np.linspace(0.0, 1.0, 1001)

# ---------------------------------------------------------------------------
# union_edges / profile_edges / decile_edges
# ---------------------------------------------------------------------------


def test_union_edges_sorts_dedups_and_drops_infinities():
  edges = union_edges([1.0, 2.0, 3.0, np.inf], [2.0, 3.0, 4.0, -np.inf])
  np.testing.assert_array_equal(edges, [1.0, 2.0, 3.0, 4.0])


def test_union_edges_includes_atoms():
  edges = union_edges([1.0, 3.0], [1.0, 3.0], atoms=[2.5])
  np.testing.assert_array_equal(edges, [1.0, 2.5, 3.0])


def test_decile_edges_of_a_linear_grid():
  q_src = np.linspace(0.0, 100.0, 1001)  # value at prob k/1000 is k * 0.1
  np.testing.assert_allclose(
      decile_edges(q_src), [10, 20, 30, 40, 50, 60, 70, 80, 90])


def test_profile_edges_bins_100_lands_on_percentiles():
  q_src = np.linspace(0.0, 1000.0, 1001)  # value at prob k/1000 is k
  edges = profile_edges(q_src, bins=100)
  assert edges.size == 99
  np.testing.assert_allclose(edges, np.arange(10, 1000, 10))


def test_profile_edges_collapses_duplicates():
  q_src = np.concatenate([np.zeros(500), np.ones(501)])
  edges = profile_edges(q_src, bins=10)
  assert edges.size <= 9
  assert set(np.unique(edges)) <= {0.0, 1.0}


def test_profile_edges_empty_source_is_an_empty_array_not_a_crash():
  # An all-NULL source column has no quantile grid at all (Ruling R17).
  edges = profile_edges(np.array([]), bins=100)
  assert isinstance(edges, np.ndarray)
  assert edges.size == 0


def test_decile_edges_empty_source_is_an_empty_array():
  assert decile_edges(np.array([])).size == 0


# ---------------------------------------------------------------------------
# bin_counts
# ---------------------------------------------------------------------------


def test_bin_counts_matches_hand_worked_edges():
  # edges=[1, 2, 3] -> bins (-inf,1], (1,2], (2,3], (3,inf).
  # 0.5, 1.0, -10.0 -> bin 0; 2.0 -> bin 1; nothing -> bin 2; 3.5 -> bin 3.
  counts = bin_counts(
      np.array([0.5, 1.0, 2.0, 3.5, -10.0]), np.array([1.0, 2.0, 3.0]))
  np.testing.assert_array_equal(counts, [3, 1, 0, 1])


def test_bin_counts_excludes_nan():
  counts = bin_counts(np.array([1.0, float("nan"), 2.0]), np.array([1.5]))
  assert counts.sum() == 2


def test_bin_counts_no_edges_is_one_bin():
  counts = bin_counts(np.array([1.0, 2.0, 3.0]), np.array([]))
  np.testing.assert_array_equal(counts, [3])


# ---------------------------------------------------------------------------
# ks_bracket
# ---------------------------------------------------------------------------


def test_ks_bracket_none_when_a_side_is_empty():
  edges = np.array([1.0, 2.0])
  assert ks_bracket(np.array([0, 0, 0]), np.array([1, 2, 3])) is None
  assert ks_bracket(bin_counts(np.array([1.0]), edges), np.zeros(3)) is None


def test_ks_bracket_raises_on_length_mismatch():
  with pytest.raises(ValueError, match="same length"):
    ks_bracket(np.array([1.0, 2.0]), np.array([1.0, 2.0, 3.0]))


def test_ks_bracket_zero_for_identical_samples():
  rng = np.random.default_rng(0)
  x = rng.normal(size=2000)
  q = np.quantile(x, _GRID)
  edges = union_edges(q, q)
  c = bin_counts(x, edges)
  d_lo, d_hi = ks_bracket(c, c)
  assert d_lo == 0.0
  assert d_hi >= 0.0


def test_ks_bracket_brackets_scipy_ks_2samp():
  rng = np.random.default_rng(42)
  a = rng.normal(loc=0.0, scale=1.0, size=50_000)
  b = rng.normal(loc=0.05, scale=1.1, size=50_000)
  q_a = np.quantile(a, _GRID)
  q_b = np.quantile(b, _GRID)
  edges = union_edges(q_a, q_b)
  c_a = bin_counts(a, edges)
  c_b = bin_counts(b, edges)
  d_lo, d_hi = ks_bracket(c_a, c_b)
  d_true = stats.ks_2samp(a, b).statistic
  assert d_lo <= d_true + 1e-12
  assert d_true <= d_hi + 1e-12
  assert d_hi - d_lo < 0.01


def test_shift_500_is_resolved():
  # The ws3 regression: a huge, fully-resolving shift must saturate both
  # the KS lower bound and the PIT-W1 distance near their maxima once the
  # comparison uses the full 1,001-point grid (rather than a coarse one).
  rng = np.random.default_rng(7)
  src = rng.normal(loc=0.0, scale=1.0, size=5000)
  syn = rng.normal(loc=500.0, scale=1.0, size=5000)
  q_src = np.quantile(src, _GRID)
  q_syn = np.quantile(syn, _GRID)
  edges = union_edges(q_src, q_syn)
  c_src = bin_counts(src, edges)
  c_syn = bin_counts(syn, edges)
  d_lo, d_hi = ks_bracket(c_src, c_syn)
  assert d_lo > 0.99
  assert d_hi >= d_lo

  # pit_w1 on the SAME union grid (Ruling R16/R17: union_edges is the
  # recommended grid for pit_w1 too, not just a source-only one).
  assert pit_w1(c_src, c_syn) > 0.49


def test_collapse_within_one_source_bin_is_missed_by_deciles_alone():
  # 10,000 points spread exactly evenly over [0, 100): source is a clean
  # uniform grid, so its own deciles are well-defined. Synthetic matches
  # every decile bin's TOTAL count exactly (collapse_mask is defined by
  # the SAME decile edges, so no point crosses a bin boundary), but the
  # points inside one decile bin are all collapsed onto a single value
  # near that bin's low edge instead of spread through the bin.
  source = np.linspace(0.0, 100.0, 10_000, endpoint=False)
  q_src = np.quantile(source, _GRID)
  deciles = decile_edges(q_src)
  lo, hi = deciles[4], deciles[5]
  collapse_mask = (source > lo) & (source <= hi)
  collapse_value = lo + (hi - lo) * 0.01
  synthetic = np.where(collapse_mask, collapse_value, source)

  q_syn = np.quantile(synthetic, _GRID)

  c_src_dec = bin_counts(source, deciles)
  c_syn_dec = bin_counts(synthetic, deciles)
  np.testing.assert_array_equal(c_src_dec, c_syn_dec)  # bin totals match
  d_lo_deciles, _ = ks_bracket(c_src_dec, c_syn_dec)
  assert d_lo_deciles < 0.01

  edges = union_edges(q_src, q_syn)
  c_src_union = bin_counts(source, edges)
  c_syn_union = bin_counts(synthetic, edges)
  d_lo_union, _ = ks_bracket(c_src_union, c_syn_union)
  assert d_lo_union > 0.09


# ---------------------------------------------------------------------------
# w1_from_bins / tail_masses
# ---------------------------------------------------------------------------


def test_w1_from_bins_none_when_degenerate():
  assert w1_from_bins(np.array([1.0]), np.array([1, 2]), np.array([1,
                                                                   2])) is None
  assert w1_from_bins(np.array([1.0, 2.0]), np.zeros(3), np.array([1, 2,
                                                                   3])) is None


def test_w1_from_bins_raises_on_count_length_mismatch():
  edges = np.array([1.0, 2.0])
  with pytest.raises(ValueError, match="same length"):
    w1_from_bins(edges, np.array([1.0, 2.0, 3.0]), np.array([1.0, 2.0]))


def test_w1_from_bins_raises_on_edges_counts_mismatch():
  # edges.size + 1 counts are required; here edges has 2 entries but the
  # counts only have 2 (should be 3).
  edges = np.array([1.0, 2.0])
  with pytest.raises(ValueError, match=r"edges\.size"):
    w1_from_bins(edges, np.array([1.0, 2.0]), np.array([1.0, 2.0]))


def test_w1_from_bins_matches_scipy_wasserstein_distance():
  rng = np.random.default_rng(3)
  a = rng.normal(loc=0.0, scale=1.0, size=5000)
  b = rng.normal(loc=0.3, scale=1.2, size=5000)
  q_a = np.quantile(a, _GRID)
  q_b = np.quantile(b, _GRID)
  edges = union_edges(q_a, q_b)
  c_a = bin_counts(a, edges)
  c_b = bin_counts(b, edges)
  w1 = w1_from_bins(edges, c_a, c_b)
  expected = stats.wasserstein_distance(a, b)
  assert math.isclose(w1, expected, rel_tol=0.02)


def test_tail_masses_reports_fractions_outside_outermost_edges():
  masses = tail_masses(np.array([2, 3, 4, 1]), np.array([0, 5, 5, 0]))
  assert masses["below_e0_source"] == 0.2
  assert masses["above_e_last_source"] == 0.1
  assert masses["below_e0_synthetic"] == 0.0
  assert masses["above_e_last_synthetic"] == 0.0


# ---------------------------------------------------------------------------
# pit_w1
# ---------------------------------------------------------------------------


def test_pit_w1_zero_for_identical_samples():
  rng = np.random.default_rng(5)
  x = rng.normal(size=3000)
  q = np.quantile(x, _GRID)
  edges = decile_edges(q)
  c = bin_counts(x, edges)
  assert math.isclose(pit_w1(c, c), 0.0, abs_tol=1e-12)


def test_pit_w1_none_when_degenerate():
  assert pit_w1(np.zeros(3), np.array([1, 2, 3])) is None


def test_pit_w1_in_range():
  rng = np.random.default_rng(11)
  a = rng.normal(size=4000)
  b = rng.normal(loc=1.0, scale=2.0, size=4000)
  q_a = np.quantile(a, _GRID)
  edges = profile_edges(q_a, bins=200)
  c_a = bin_counts(a, edges)
  c_b = bin_counts(b, edges)
  value = pit_w1(c_a, c_b)
  assert 0.0 <= value <= 0.5


def test_pit_w1_raises_on_length_mismatch():
  with pytest.raises(ValueError, match="same length"):
    pit_w1(np.array([1.0, 2.0]), np.array([1.0, 2.0, 3.0]))


def test_pit_w1_constant_source_synthetic_entirely_above_is_exactly_half():
  # A single-point source (a constant column) puts all of p_src on bin 0;
  # synthetic entirely above it (bin 1) is the maximally-disjoint case the
  # mid-CDF formula must saturate at exactly 0.5, not overshoot past it
  # (the CRITICAL regression: the old right-edge-CDF version gave 1.0).
  c_src = np.array([100.0, 0.0])
  c_syn = np.array([0.0, 100.0])
  assert pit_w1(c_src, c_syn) == 0.5


def test_pit_w1_symmetric_under_integer_shift():
  # An exactly-uniform discrete column (10,000 of each digit 0-9) shifted
  # by +1 and by -1 must give the SAME pit_w1 magnitude: the old
  # right-edge-CDF version was asymmetric (it always credited a bin's mass
  # to its upper edge), inflating one direction over the other.
  source = np.repeat(np.arange(10, dtype=float), 10_000)
  edges = np.arange(0.5, 9.5, 1.0)
  c_src = bin_counts(source, edges)
  c_plus = bin_counts(source + 1.0, edges)
  c_minus = bin_counts(source - 1.0, edges)
  value_plus = pit_w1(c_src, c_plus)
  value_minus = pit_w1(c_src, c_minus)
  assert math.isclose(value_plus, value_minus, rel_tol=1e-9)


def test_pit_w1_continuous_disjoint_on_union_edges_is_near_half():
  rng = np.random.default_rng(13)
  a = rng.normal(loc=0.0, scale=1.0, size=20_000)
  b = rng.normal(loc=500.0, scale=1.0, size=20_000)
  edges = union_edges(np.quantile(a, _GRID), np.quantile(b, _GRID))
  c_a = bin_counts(a, edges)
  c_b = bin_counts(b, edges)
  assert math.isclose(pit_w1(c_a, c_b), 0.5, abs_tol=1e-3)


@settings(max_examples=200)
@given(
    st.lists(
        st.integers(min_value=0, max_value=10_000), min_size=1, max_size=40),
    st.lists(
        st.integers(min_value=0, max_value=10_000), min_size=1, max_size=40))
def test_pit_w1_stays_within_bounds_for_random_counts(counts_a, counts_b):
  n = min(len(counts_a), len(counts_b))
  c_src = np.array(counts_a[:n], dtype=float)
  c_syn = np.array(counts_b[:n], dtype=float)
  value = pit_w1(c_src, c_syn)
  if value is None:
    return
  assert 0.0 <= value <= 0.5 + 1e-9


# ---------------------------------------------------------------------------
# decile_ks_legacy
# ---------------------------------------------------------------------------


def test_decile_ks_legacy_empty_inputs_return_zero():
  assert decile_ks_legacy([], [1, 2, 3]) == 0.0
  assert decile_ks_legacy([1, 2, 3], []) == 0.0


def test_decile_ks_legacy_zero_for_identical():
  a = list(range(11))
  assert decile_ks_legacy(a, a) == 0.0


def test_decile_ks_legacy_positive_for_shifted():
  a = list(range(11))
  b = [x + 5 for x in a]
  assert decile_ks_legacy(a, b) > 0.3


def test_decile_ks_legacy_constant_vector_no_div_by_zero():
  assert decile_ks_legacy([3.0, 3.0, 3.0], [3.0, 3.0, 3.0]) == 0.0


# ---------------------------------------------------------------------------
# quantiles_from_bins
# ---------------------------------------------------------------------------


def test_quantiles_from_bins_inverts_the_edge_exact_cdf():
  edges = np.array([10.0, 20.0, 30.0])
  counts = np.array([10, 10, 10, 10])
  values = quantiles_from_bins(edges, counts, [0.25, 0.5, 0.75])
  np.testing.assert_allclose(values, [10.0, 20.0, 30.0])


def test_quantiles_from_bins_clamps_outside_the_observed_range():
  edges = np.array([10.0, 20.0, 30.0])
  counts = np.array([10, 10, 10, 10])
  values = quantiles_from_bins(edges, counts, [0.0, 1.0])
  np.testing.assert_allclose(values, [10.0, 30.0])


def test_quantiles_from_bins_degenerate_returns_nan():
  assert math.isnan(quantiles_from_bins(np.array([]), np.array([5]), [0.5])[0])
  assert math.isnan(
      quantiles_from_bins(np.array([1.0, 2.0]), np.zeros(3), [0.5])[0])
