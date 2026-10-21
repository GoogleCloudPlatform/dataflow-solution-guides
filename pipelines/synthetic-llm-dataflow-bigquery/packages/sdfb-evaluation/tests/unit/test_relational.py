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
"""Tests for `sdfb_evaluation.stats.relational` (Task 13).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from sdfb_evaluation.stats import relational
from sdfb_evaluation.stats.relational import _folded_abs_interval

# ---------------------------------------------------------------------------
# fanout_histogram
# ---------------------------------------------------------------------------


def test_fanout_histogram_includes_zero_child_parents():
  hist = relational.fanout_histogram([0, 0, 1, 2], cap=5)
  assert hist.tolist() == [2, 1, 1, 0, 0, 0]


def test_fanout_histogram_cap_bin_collapses_overflow():
  hist = relational.fanout_histogram([5, 6, 100, 4], cap=5)
  # 4 stays in its own bin; 5, 6, 100 all collapse into the ">= 5" bin.
  assert hist.tolist() == [0, 0, 0, 0, 1, 3]


def test_fanout_histogram_empty_input_is_all_zero():
  hist = relational.fanout_histogram([], cap=5)
  assert hist.tolist() == [0, 0, 0, 0, 0, 0]


def test_fanout_histogram_rejects_cap_below_one():
  with pytest.raises(ValueError):
    relational.fanout_histogram([0, 1], cap=0)


def test_fanout_histogram_rejects_negative_counts():
  with pytest.raises(ValueError):
    relational.fanout_histogram([0, 1, -1], cap=5)


# ---------------------------------------------------------------------------
# fanout_metrics — steps 1-5 from the brief
# ---------------------------------------------------------------------------


def test_identical_histograms_give_tvd_zero_and_ratio_one():
  h = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 0.0])
  result = relational.fanout_metrics(
      h, h, mean_src=1.5, mean_syn=1.5, min_src=0, max_src=4, cap=5)
  assert result is not None
  assert result["tvd"] == pytest.approx(0.0)
  assert result["mean_ratio"] == pytest.approx(1.0)


def test_doubled_fanout_gives_mean_ratio_two():
  h_src = np.array([0.0, 10.0, 0.0, 0.0, 0.0, 0.0])
  h_syn = np.array([0.0, 0.0, 10.0, 0.0, 0.0, 0.0])
  result = relational.fanout_metrics(
      h_src, h_syn, mean_src=1.0, mean_syn=2.0, min_src=1, max_src=1, cap=5)
  assert result is not None
  assert result["mean_ratio"] == pytest.approx(2.0)


def test_fanout_w1_matches_hand_computed_point_masses():
  # src: every parent has exactly 1 child. syn: every parent has exactly 2.
  # W1 between two point masses 1 apart is exactly 1.
  h_src = np.array([0.0, 10.0, 0.0, 0.0, 0.0, 0.0])
  h_syn = np.array([0.0, 0.0, 10.0, 0.0, 0.0, 0.0])
  result = relational.fanout_metrics(
      h_src, h_syn, mean_src=1.0, mean_syn=2.0, min_src=1, max_src=1, cap=5)
  assert result is not None
  assert result["w1"] == pytest.approx(1.0)


def test_fanout_w1_uses_overflow_mean_when_available():
  cap = 5
  h_src = np.array([0.0, 0.0, 10.0, 0.0, 0.0, 0.0])  # point mass at 2
  h_syn = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 10.0])  # all in the >= cap bin

  without_override = relational.fanout_metrics(
      h_src, h_syn, mean_src=2.0, mean_syn=100.0, min_src=2, max_src=2, cap=cap)
  with_override = relational.fanout_metrics(
      h_src,
      h_syn,
      mean_src=2.0,
      mean_syn=100.0,
      min_src=2,
      max_src=2,
      cap=cap,
      mean_overflow_syn=100.0)

  assert without_override is not None and with_override is not None
  # Default: syn atom placed at cap=5 -> |2 - 5| * 1.0 = 3.
  assert without_override["w1"] == pytest.approx(3.0)
  # Overridden: syn atom placed at its true mean 100 -> |2 - 100| * 1.0 = 98.
  assert with_override["w1"] == pytest.approx(98.0)


def test_zero_child_share_delta_is_exact_with_newcombe_ci():
  h_src = np.array([3.0, 7.0, 0.0, 0.0, 0.0, 0.0])  # z_src = 0.3
  h_syn = np.array([1.0, 9.0, 0.0, 0.0, 0.0, 0.0])  # z_syn = 0.1
  result = relational.fanout_metrics(
      h_src, h_syn, mean_src=0.7, mean_syn=0.9, min_src=0, max_src=1, cap=5)
  assert result is not None
  assert result["zero_child_share_source"] == pytest.approx(0.3)
  assert result["zero_child_share_synthetic"] == pytest.approx(0.1)
  assert result["zero_child_share_delta"] == pytest.approx(0.2)
  lo, hi = result["zero_child_share_delta_ci_low"], result[
      "zero_child_share_delta_ci_high"]
  assert 0.0 <= lo <= result["zero_child_share_delta"] <= hi


def test_fanout_metrics_none_on_empty_input():
  zeros = np.zeros(6)
  nonzero = np.array([0.0, 1.0, 0.0, 0.0, 0.0, 0.0])
  assert relational.fanout_metrics(
      zeros, nonzero, mean_src=0.0, mean_syn=1.0, min_src=0, max_src=0,
      cap=5) is None
  assert relational.fanout_metrics(
      nonzero, zeros, mean_src=1.0, mean_syn=0.0, min_src=0, max_src=0,
      cap=5) is None
  assert relational.fanout_metrics(
      zeros, zeros, mean_src=0.0, mean_syn=0.0, min_src=0, max_src=0,
      cap=5) is None


def test_fanout_metrics_rejects_wrong_length_histograms():
  h_ok = np.zeros(6)
  h_bad = np.zeros(5)
  with pytest.raises(ValueError):
    relational.fanout_metrics(
        h_bad, h_ok, mean_src=0.0, mean_syn=0.0, min_src=0, max_src=0, cap=5)


def test_fanout_metrics_rejects_cap_below_one():
  h = np.zeros(2)
  with pytest.raises(ValueError):
    relational.fanout_metrics(
        h, h, mean_src=0.0, mean_syn=0.0, min_src=0, max_src=0, cap=0)


def test_fanout_metrics_rejects_negative_histogram_counts():
  h_bad = np.array([-1.0, 5.0, 0.0, 0.0, 0.0, 0.0])
  h_ok = np.array([1.0, 5.0, 0.0, 0.0, 0.0, 0.0])
  with pytest.raises(ValueError):
    relational.fanout_metrics(
        h_bad, h_ok, mean_src=0.0, mean_syn=0.0, min_src=0, max_src=1, cap=5)


def test_fanout_metrics_rejects_non_finite_histogram_counts():
  h_bad = np.array([float("nan"), 5.0, 0.0, 0.0, 0.0, 0.0])
  h_ok = np.array([1.0, 5.0, 0.0, 0.0, 0.0, 0.0])
  with pytest.raises(ValueError):
    relational.fanout_metrics(
        h_bad, h_ok, mean_src=0.0, mean_syn=0.0, min_src=0, max_src=1, cap=5)


def test_fanout_metrics_rejects_non_integral_histogram_counts():
  h_bad = np.array([0.5, 5.0, 0.0, 0.0, 0.0, 0.0])
  h_ok = np.array([1.0, 5.0, 0.0, 0.0, 0.0, 0.0])
  with pytest.raises(ValueError):
    relational.fanout_metrics(
        h_bad, h_ok, mean_src=0.0, mean_syn=0.0, min_src=0, max_src=1, cap=5)


def test_fanout_metrics_rejects_min_src_inconsistent_with_histogram():
  h_src = np.array([0.0, 5.0, 0.0, 0.0, 0.0, 0.0])  # lowest non-empty bin: 1
  h_syn = np.array([1.0, 1.0, 0.0, 0.0, 0.0, 0.0])
  with pytest.raises(ValueError):
    relational.fanout_metrics(
        h_src, h_syn, mean_src=1.0, mean_syn=0.5, min_src=0, max_src=1, cap=5)


def test_fanout_metrics_rejects_max_src_inconsistent_with_histogram():
  h_src = np.array([0.0, 5.0, 0.0, 0.0, 0.0, 0.0])  # highest non-empty bin: 1
  h_syn = np.array([1.0, 1.0, 0.0, 0.0, 0.0, 0.0])
  with pytest.raises(ValueError):
    relational.fanout_metrics(
        h_src, h_syn, mean_src=1.0, mean_syn=0.5, min_src=1, max_src=3, cap=5)


def test_fanout_metrics_accepts_extremes_in_the_overflow_bin():
  # All source mass is in the >= cap bin, so only min_src/max_src >= cap
  # can be checked (not equality to a specific count).
  h_src = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 9.0])
  h_syn = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 9.0])
  result = relational.fanout_metrics(
      h_src, h_syn, mean_src=80.0, mean_syn=80.0, min_src=70, max_src=90, cap=5)
  assert result is not None


def test_fanout_metrics_rejects_min_src_below_cap_when_all_mass_overflows():
  h_src = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 9.0])
  h_syn = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 9.0])
  with pytest.raises(ValueError):
    relational.fanout_metrics(
        h_src,
        h_syn,
        mean_src=80.0,
        mean_syn=80.0,
        min_src=4,
        max_src=90,
        cap=5)


def test_fanout_metrics_rejects_non_finite_overflow_mean_when_bin_nonempty():
  h_src = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 9.0])
  h_syn = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 9.0])
  with pytest.raises(ValueError):
    relational.fanout_metrics(
        h_src,
        h_syn,
        mean_src=80.0,
        mean_syn=80.0,
        min_src=70,
        max_src=90,
        cap=5,
        mean_overflow_src=float("nan"))


def test_fanout_metrics_rejects_overflow_mean_below_cap_when_bin_nonempty():
  h_src = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 9.0])
  h_syn = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 9.0])
  with pytest.raises(ValueError):
    relational.fanout_metrics(
        h_src,
        h_syn,
        mean_src=80.0,
        mean_syn=80.0,
        min_src=70,
        max_src=90,
        cap=5,
        mean_overflow_src=4.0)


def test_fanout_metrics_ignores_overflow_mean_when_bin_is_empty_no_nan():
  # h_src's >= cap bin is EMPTY, so a bogus/NaN mean_overflow_src must be
  # ignored entirely, not validated and not folded into w1 as nan.
  h_src = np.array([0.0, 9.0, 0.0, 0.0, 0.0, 0.0])
  h_syn = np.array([0.0, 9.0, 0.0, 0.0, 0.0, 0.0])
  result = relational.fanout_metrics(
      h_src,
      h_syn,
      mean_src=1.0,
      mean_syn=1.0,
      min_src=1,
      max_src=1,
      cap=5,
      mean_overflow_src=float("nan"))
  assert result is not None
  assert result["w1"] == pytest.approx(0.0)
  assert math.isfinite(result["w1"])


def test_tvd_with_different_parent_totals():
  h_src = np.array([3.0, 4.0, 0.0, 0.0, 0.0, 0.0])  # 7 parents, p = 3/7, 4/7
  h_syn = np.array([5.0, 15.0, 0.0, 0.0, 0.0, 0.0])  # 20 parents, q = .25, .75
  result = relational.fanout_metrics(
      h_src, h_syn, mean_src=4 / 7, mean_syn=0.75, min_src=0, max_src=1, cap=5)
  assert result is not None
  expected = 0.5 * (abs(3 / 7 - 0.25) + abs(4 / 7 - 0.75))
  assert result["tvd"] == pytest.approx(expected)


def test_fanout_w1_with_overflow_means_on_both_sides():
  cap = 5
  h_src = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 10.0])  # all overflow, mean 80
  h_syn = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 10.0])  # all overflow, mean 120
  result = relational.fanout_metrics(
      h_src,
      h_syn,
      mean_src=80.0,
      mean_syn=120.0,
      min_src=80,
      max_src=80,
      cap=cap,
      mean_overflow_src=80.0,
      mean_overflow_syn=120.0)
  assert result is not None
  assert result["w1"] == pytest.approx(40.0)


def test_cardinality_adherence_returns_count_and_wilson_ci():
  h_src = np.array([0.0, 16.0, 0.0, 0.0, 0.0, 0.0])
  h_syn = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 1.0])
  result = relational.fanout_metrics(
      h_src, h_syn, mean_src=1.0, mean_syn=2.5, min_src=1, max_src=1, cap=5)
  assert result is not None
  assert result["cardinality_adherence_count"] == 2  # bin[1] == 2
  lo = result["cardinality_adherence_ci_low"]
  hi = result["cardinality_adherence_ci_high"]
  assert 0.0 <= lo <= result["cardinality_adherence"] <= hi <= 1.0


# ---------------------------------------------------------------------------
# _folded_abs_interval
# ---------------------------------------------------------------------------


def test_folded_abs_interval_straddling_zero():
  assert _folded_abs_interval(-0.1, 0.3) == (0.0, 0.3)


def test_folded_abs_interval_both_positive():
  assert _folded_abs_interval(0.2, 0.5) == (0.2, 0.5)


def test_folded_abs_interval_both_negative():
  assert _folded_abs_interval(-0.5, -0.2) == (0.2, 0.5)


def test_mean_ratio_is_none_when_source_mean_is_zero():
  h_src = np.array([10.0, 0.0, 0.0, 0.0, 0.0, 0.0])
  h_syn = np.array([5.0, 5.0, 0.0, 0.0, 0.0, 0.0])
  result = relational.fanout_metrics(
      h_src, h_syn, mean_src=0.0, mean_syn=0.5, min_src=0, max_src=0, cap=5)
  assert result is not None
  assert result["mean_ratio"] is None


# ---------------------------------------------------------------------------
# cardinality_adherence: exact vs. overflow-approximation cases
# ---------------------------------------------------------------------------


def test_cardinality_adherence_exact_when_max_below_cap():
  cap = 10
  # index:            0  1  2  3  4  5  6  7  8  9 >=10
  h_syn = np.array([1., 2., 3., 4., 5., 1., 0., 0., 0., 0., 0.])
  # h_src's own nonzero bins must be consistent with min_src=2/max_src=4.
  h_src = np.array([0., 0., 8., 0., 8., 0., 0., 0., 0., 0., 0.])
  result = relational.fanout_metrics(
      h_src,
      h_syn,
      mean_src=1.0,
      mean_syn=2.8125,
      min_src=2,
      max_src=4,
      cap=cap)
  assert result is not None
  # adherent = bins[2..4] = 3 + 4 + 5 = 12; n_syn = 16.
  assert result["cardinality_adherence"] == pytest.approx(12 / 16)
  assert result["cardinality_adherence_exact"] is True


def test_cardinality_adherence_overflow_bin_counted_when_max_at_or_above_cap():
  cap = 10
  # index:            0  1  2  3  4  5  6  7  8  9  >=10
  h_syn = np.array([1., 2., 3., 4., 5., 1., 0., 0., 0., 0., 3.])
  # h_src's lowest nonzero bin is 2 (== min_src) and its highest is the
  # overflow bin itself, consistent with max_src=15 >= cap.
  h_src = np.array([0., 0., 18., 0., 0., 0., 0., 0., 0., 0., 1.])
  result = relational.fanout_metrics(
      h_src, h_syn, mean_src=1.0, mean_syn=3.0, min_src=2, max_src=15, cap=cap)
  assert result is not None
  # adherent = bins[2..9] (3+4+5+1) + overflow bin (3) = 16; n_syn = 19.
  assert result["cardinality_adherence"] == pytest.approx(16 / 19)
  assert result["cardinality_adherence_exact"] is False


# ---------------------------------------------------------------------------
# parent_coverage
# ---------------------------------------------------------------------------


def test_parent_coverage_ratio_of_shares_with_at_least_one_child():
  h_src = np.array([2.0, 8.0, 0.0, 0.0, 0.0,
                    0.0])  # 80% of parents have >=1 child
  h_syn = np.array([1.0, 9.0, 0.0, 0.0, 0.0,
                    0.0])  # 90% of parents have >=1 child
  result = relational.fanout_metrics(
      h_src, h_syn, mean_src=0.8, mean_syn=0.9, min_src=0, max_src=1, cap=5)
  assert result is not None
  assert result["parent_coverage"] == pytest.approx(0.9 / 0.8)


def test_parent_coverage_none_when_source_has_no_covered_parents():
  h_src = np.array([10.0, 0.0, 0.0, 0.0, 0.0,
                    0.0])  # every source parent childless
  h_syn = np.array([5.0, 5.0, 0.0, 0.0, 0.0, 0.0])
  result = relational.fanout_metrics(
      h_src, h_syn, mean_src=0.0, mean_syn=0.5, min_src=0, max_src=0, cap=5)
  assert result is not None
  assert result["parent_coverage"] is None


# ---------------------------------------------------------------------------
# orphan_summary — MATCH SIMPLE semantics
# ---------------------------------------------------------------------------


def test_orphan_summary_rate_excludes_null_keys():
  summary = relational.orphan_summary(
      total_nonnull=100, orphans=7, null_keys=13)
  assert summary["rate"] == pytest.approx(0.07)
  assert summary["total_nonnull"] == 100
  assert summary["orphans"] == 7
  assert summary["null_keys"] == 13


def test_orphan_summary_null_keys_never_change_the_rate():
  with_nulls = relational.orphan_summary(
      total_nonnull=100, orphans=7, null_keys=500)
  without_nulls = relational.orphan_summary(
      total_nonnull=100, orphans=7, null_keys=0)
  assert with_nulls["rate"] == without_nulls["rate"]


def test_orphan_summary_rate_none_when_no_nonnull_tuples():
  summary = relational.orphan_summary(total_nonnull=0, orphans=0, null_keys=5)
  assert summary["rate"] is None


def test_orphan_summary_rejects_negative_arguments():
  with pytest.raises(ValueError):
    relational.orphan_summary(total_nonnull=-1, orphans=0, null_keys=0)


def test_orphan_summary_rejects_orphans_exceeding_total():
  with pytest.raises(ValueError):
    relational.orphan_summary(total_nonnull=5, orphans=6, null_keys=0)
