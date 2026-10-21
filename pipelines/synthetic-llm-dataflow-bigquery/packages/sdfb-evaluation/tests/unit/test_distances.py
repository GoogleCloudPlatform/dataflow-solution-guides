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
"""Tests for `sdfb_evaluation.stats.distances` (Task 7).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.spatial.distance import jensenshannon

from sdfb_evaluation.stats import distances as d

# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


def test_normalize_counts_to_probabilities():
  np.testing.assert_allclose(d.normalize([2, 2, 4]), [0.25, 0.25, 0.5])


def test_normalize_empty_is_none():
  assert d.normalize([]) is None


def test_normalize_zero_mass_is_none():
  assert d.normalize([0, 0, 0]) is None


# ---------------------------------------------------------------------------
# align
# ---------------------------------------------------------------------------


def test_align_union_keys():
  keys, p, q = d.align({"a": 2, "b": 1}, {"b": 3, "c": 1})
  assert keys == ["a", "b", "c"]
  assert p.tolist() == [2, 1, 0]
  assert q.tolist() == [0, 3, 1]


def test_align_is_type_stable_sort():
  # Mixed int/str keys must not raise (sorted by (type name, str(k))).
  keys, p, q = d.align({1: 1, "a": 1}, {1: 1, "a": 1})
  assert set(keys) == {1, "a"}
  assert p.tolist() == [1, 1]
  assert q.tolist() == [1, 1]


# ---------------------------------------------------------------------------
# tvd
# ---------------------------------------------------------------------------


def test_tvd_bounds():
  assert d.tvd([1, 0], [1, 0]) == 0.0
  assert d.tvd([1, 0], [0, 1]) == 1.0


def test_tvd_accepts_counts_or_probabilities():
  # Same shape, different scale -> same TVD once normalized.
  assert d.tvd([2, 2], [1, 3]) == pytest.approx(d.tvd([0.5, 0.5], [0.25, 0.75]))


def test_tvd_mismatched_length_raises():
  with pytest.raises(ValueError, match="same length"):
    d.tvd([1, 0], [1, 0, 0])


# ---------------------------------------------------------------------------
# jsd_bits
# ---------------------------------------------------------------------------


def test_jsd_bits_matches_scipy():
  p, q = np.array([0.2, 0.5, 0.3]), np.array([0.1, 0.6, 0.3])
  assert abs(d.jsd_bits(p, q) - jensenshannon(p, q, base=2)**2) < 1e-12


def test_jsd_bits_one_sided_zero_entries_matches_scipy():
  p, q = np.array([1.0, 0.0, 0.0]), np.array([0.0, 0.5, 0.5])
  assert abs(d.jsd_bits(p, q) - jensenshannon(p, q, base=2)**2) < 1e-12


def test_jsd_bits_disjoint_is_one_bit():
  assert d.jsd_bits([1, 0], [0, 1]) == pytest.approx(1.0)


def test_jsd_bits_identical_is_zero():
  assert d.jsd_bits([0.2, 0.8], [0.2, 0.8]) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# hellinger
# ---------------------------------------------------------------------------


def test_hellinger_identical_is_zero():
  assert d.hellinger([0.3, 0.7], [0.3, 0.7]) == pytest.approx(0.0)


def test_hellinger_disjoint_is_one():
  assert d.hellinger([1, 0], [0, 1]) == pytest.approx(1.0)


def test_hellinger_matches_direct_formula():
  p, q = np.array([0.2, 0.5, 0.3]), np.array([0.1, 0.6, 0.3])
  expected = np.sqrt(0.5 * np.sum((np.sqrt(p) - np.sqrt(q))**2))
  assert d.hellinger(p, q) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# psi
# ---------------------------------------------------------------------------


def test_psi_catches_out_of_range_mass():
  # ws3 defect: out-of-range mass dropped -> PSI ~ 0.
  src = [100, 100, 100, 100, 0]  # last bin = open upper tail, empty on source
  syn = [0, 0, 0, 0, 400]
  assert d.psi(src, syn) > 5.0


def test_psi_identical_counts_is_near_zero():
  assert d.psi([25, 25, 25, 25], [25, 25, 25, 25]) == pytest.approx(
      0.0, abs=1e-9)


def test_psi_empty_is_none():
  assert d.psi([], []) is None


def test_psi_mismatched_length_raises():
  with pytest.raises(ValueError, match="same length"):
    d.psi([1, 2], [1, 2, 3])


# ---------------------------------------------------------------------------
# cohens_w
# ---------------------------------------------------------------------------


def test_cohens_w_identical_is_zero():
  result = d.cohens_w([0.3, 0.7], [0.3, 0.7])
  assert result.w == pytest.approx(0.0)
  assert result.q_mass_on_p0 == pytest.approx(0.0)


def test_cohens_w_excludes_synthetic_only_categories_from_w():
  # p has zero mass on the 3rd category; q's mass there is reported
  # separately as q_mass_on_p0, not folded into w.
  result = d.cohens_w([0.5, 0.5, 0.0], [0.4, 0.4, 0.2])
  expected_w = np.sqrt((0.4 - 0.5)**2 / 0.5 + (0.4 - 0.5)**2 / 0.5)
  assert result.w == pytest.approx(expected_w)
  assert result.q_mass_on_p0 == pytest.approx(0.2)


def test_cohens_w_empty_is_none():
  assert d.cohens_w([], []) is None


# ---------------------------------------------------------------------------
# degenerate / empty inputs
# ---------------------------------------------------------------------------


def test_degenerate_empty_returns_none():
  assert d.tvd([], []) is None and d.jsd_bits([0, 0], [1, 0]) is None


def test_degenerate_all_none_family():
  assert d.hellinger([], []) is None
  assert d.cohens_w([0, 0], [1, 0]) is None
