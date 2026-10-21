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
"""Tests for `sdfb_evaluation.stats.moments` (Task 6).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import math

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st
from scipy import stats

from sdfb_evaluation.stats.moments import Moments

# ---------------------------------------------------------------------------
# empty accumulator
# ---------------------------------------------------------------------------


def test_empty_moments_is_a_valid_empty_state():
  m = Moments()
  assert m.n == 0
  assert m.variance is None
  assert m.std is None
  assert m.skewness is None
  assert m.kurtosis_excess is None
  assert m.min == math.inf
  assert m.max == -math.inf
  assert m.zeros == 0
  assert m.nonfinite == 0


# ---------------------------------------------------------------------------
# add_array
# ---------------------------------------------------------------------------


def test_add_array_matches_direct_computation():
  x = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 0.0, 0.0])
  m = Moments()
  m.add_array(x)
  assert m.n == 7
  assert math.isclose(m.mean, x.mean())
  assert math.isclose(m.variance, x.var())
  assert m.min == 0.0
  assert m.max == 5.0
  assert m.zeros == 2


def test_add_array_drops_nan():
  m = Moments()
  m.add_array(np.array([1.0, float("nan"), 3.0]))
  assert m.n == 2
  assert math.isclose(m.mean, 2.0)
  assert m.nonfinite == 1


def test_add_array_drops_infinite_rows_and_counts_them():
  # A +inf row must never poison mean/variance with NaN, and must not
  # silently count as a value either — it is tracked in `nonfinite`.
  m = Moments()
  m.add_array(np.array([1.0, 2.0, 3.0, float("inf")]))
  assert m.n == 3
  assert math.isclose(m.mean, 2.0)
  assert m.nonfinite == 1
  assert math.isfinite(m.mean)
  assert math.isfinite(m.m2)
  d = m.to_dict()
  assert all(
      v is None or math.isfinite(v) for v in d.values() if isinstance(v, float))


def test_add_array_counts_negative_infinity_and_nan_together():
  m = Moments()
  m.add_array(np.array([1.0, float("-inf"), float("nan"), 2.0]))
  assert m.n == 2
  assert m.nonfinite == 2


def test_add_array_all_nonfinite_batch_still_counts_nonfinite():
  m = Moments()
  m.add_array(np.array([float("inf"), float("nan")]))
  assert m.n == 0
  assert m.nonfinite == 2


def test_add_array_empty_batch_is_a_no_op():
  m = Moments()
  m.add_array(np.array([]))
  assert m.n == 0
  m.add_array(np.array([1.0, 2.0]))
  m.add_array(np.array([]))
  assert m.n == 2


def test_add_array_twice_accumulates():
  m = Moments()
  m.add_array(np.array([1.0, 2.0]))
  m.add_array(np.array([3.0, 4.0]))
  full = np.array([1.0, 2.0, 3.0, 4.0])
  assert m.n == 4
  np.testing.assert_allclose([m.mean, m.variance], [full.mean(), full.var()])


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


def test_merge_is_pure():
  a, b = Moments(), Moments()
  a.add_array(np.array([1.0, 2.0, 3.0]))
  b.add_array(np.array([4.0, 5.0]))
  a_before, b_before = (a.n, a.mean, a.m2), (b.n, b.mean, b.m2)
  merged = a.merge(b)
  assert (a.n, a.mean, a.m2) == a_before
  assert (b.n, b.mean, b.m2) == b_before
  assert merged.n == 5


def test_merge_with_empty_other_is_identity():
  a = Moments()
  a.add_array(np.array([1.0, 2.0, 3.0]))
  merged = a.merge(Moments())
  assert merged.n == a.n
  assert merged.mean == a.mean
  assert merged.m2 == a.m2
  merged2 = Moments().merge(a)
  assert merged2.n == a.n
  assert merged2.mean == a.mean


def test_merge_of_two_empties_is_empty():
  merged = Moments().merge(Moments())
  assert merged.n == 0
  assert merged.min == math.inf
  assert merged.max == -math.inf
  assert merged.nonfinite == 0


def test_merge_sums_nonfinite_from_both_sides():
  a, b = Moments(), Moments()
  a.add_array(np.array([1.0, float("inf")]))
  b.add_array(np.array([2.0, float("nan"), float("nan")]))
  merged = a.merge(b)
  assert merged.n == 2
  assert merged.nonfinite == 3


@settings(max_examples=60)
@given(
    st.lists(st.floats(-1e6, 1e6), min_size=1, max_size=200),
    st.lists(st.floats(-1e6, 1e6), min_size=1, max_size=200))
def test_merge_equals_single_pass(a, b):
  ma, mb, mall = Moments(), Moments(), Moments()
  ma.add_array(np.array(a))
  mb.add_array(np.array(b))
  mall.add_array(np.array(a + b))
  merged = ma.merge(mb)
  assert merged.n == mall.n
  np.testing.assert_allclose([merged.mean, merged.variance],
                             [mall.mean, mall.variance],
                             rtol=1e-6,
                             atol=1e-6)


def test_moments_match_scipy():
  x = np.random.default_rng(1).gamma(2.0, size=5000)
  m = Moments()
  m.add_array(x)
  np.testing.assert_allclose(m.skewness, stats.skew(x), rtol=1e-6)
  np.testing.assert_allclose(m.kurtosis_excess, stats.kurtosis(x), rtol=1e-6)


# ---------------------------------------------------------------------------
# skewness / kurtosis edge cases
# ---------------------------------------------------------------------------


def test_skewness_kurtosis_none_below_n2():
  m = Moments()
  m.add_array(np.array([1.0]))
  assert m.n == 1
  assert m.skewness is None
  assert m.kurtosis_excess is None


def test_constant_column_has_zero_std_and_no_skewness():
  m = Moments()
  m.add_array(np.array([7.0, 7.0, 7.0, 7.0]))
  assert m.std == 0.0
  assert m.skewness is None
  assert m.kurtosis_excess is None


# ---------------------------------------------------------------------------
# to_dict / from_dict
# ---------------------------------------------------------------------------


def test_to_dict_from_dict_round_trip():
  m = Moments()
  m.add_array(np.array([1.0, 2.0, 3.0, 4.0, float("inf")]))
  d = m.to_dict()
  assert d["min"] == 1.0
  assert d["max"] == 4.0
  assert d["nonfinite"] == 1
  restored = Moments.from_dict(d)
  assert restored.n == m.n
  assert restored.mean == m.mean
  assert restored.m2 == m.m2
  assert restored.m3 == m.m3
  assert restored.m4 == m.m4
  assert restored.min == m.min
  assert restored.max == m.max
  assert restored.zeros == m.zeros
  assert restored.nonfinite == m.nonfinite


def test_empty_moments_to_dict_is_json_safe():
  d = Moments().to_dict()
  assert d["min"] is None
  assert d["max"] is None
  assert d["nonfinite"] == 0
  restored = Moments.from_dict(d)
  assert restored.min == math.inf
  assert restored.max == -math.inf
  assert restored.n == 0
  assert restored.nonfinite == 0


def test_from_dict_defaults_nonfinite_for_a_pre_existing_dict():
  # A dict written before `nonfinite` existed must still round-trip.
  d = {
      "n": 3,
      "mean": 2.0,
      "m2": 2.0,
      "m3": 0.0,
      "m4": 2.0,
      "min": 1.0,
      "max": 3.0
  }
  restored = Moments.from_dict(d)
  assert restored.nonfinite == 0
