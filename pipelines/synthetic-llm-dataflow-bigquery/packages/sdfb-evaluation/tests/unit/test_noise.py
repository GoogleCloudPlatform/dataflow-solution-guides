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
"""Tests for `sdfb_evaluation.stats.noise` (Task 5).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import stats

from sdfb_evaluation.stats import noise, relational


def test_dkw_10k_is_1_36_percent():
  assert math.isclose(noise.dkw_epsilon(10_000), 0.013581, rel_tol=1e-3)


def test_tvd_null_expectation_is_the_value_python_3_11_computed():
  """CPython 3.12 made the builtin `sum()` of floats compensated, which
  moved the last bit of this floor. The categories are added left to
  right, as 3.11 added them."""
  p = [10 / 27, 4 / 27, 13 / 27]
  assert noise.tvd_null_expectation(p, 1000,
                                    500).hex() == "0x1.def189f6eb274p-6"


def test_ks_critical_matches_asymptotic_table():
  # c(0.05)=1.358 -> n=m=1000: 1.358*sqrt(2/1000)=0.06073
  assert math.isclose(noise.ks_critical(1000, 1000), 0.06073, rel_tol=2e-3)


def test_ks_critical_empirical_type1_rate():
  rng = np.random.default_rng(7)
  hits = 0
  for _ in range(400):
    a, b = rng.normal(size=500), rng.normal(size=500)
    hits += stats.ks_2samp(a, b).statistic > noise.ks_critical(500, 500)
  assert 0.02 < hits / 400 < 0.09


def test_wilson_contains_p_and_is_bounded():
  lo, hi = noise.wilson_interval(0, 100)
  assert lo == 0.0 and 0.0 < hi < 0.05
  lo, hi = noise.wilson_interval(50, 100)
  assert lo < 0.5 < hi


def test_newcombe_zero_difference_straddles_zero():
  lo, hi = noise.newcombe_diff_interval(10, 100, 10, 100)
  assert lo < 0.0 < hi


@pytest.mark.slow
def test_newcombe_coverage_at_equal_proportions():
  # Monte-Carlo coverage sanity check: at the true difference 0 (p1=p2=0.1,
  # n=200), the interval should contain 0 at (at least close to) its nominal
  # 95% rate.
  rng = np.random.default_rng(13)
  n, p = 200, 0.1
  hits = 0
  for _ in range(500):
    k1, k2 = rng.binomial(n, p), rng.binomial(n, p)
    lo, hi = noise.newcombe_diff_interval(k1, n, k2, n)
    hits += lo <= 0.0 <= hi
  assert hits / 500 >= 0.93


def test_tvd_null_expectation_matches_simulation():
  rng = np.random.default_rng(3)
  p = np.array([0.5, 0.3, 0.2])
  sims = []
  for _ in range(2000):
    a = rng.multinomial(1000, p) / 1000
    b = rng.multinomial(1000, p) / 1000
    sims.append(0.5 * np.abs(a - b).sum())
  assert math.isclose(
      noise.tvd_null_expectation(p, 1000, 1000), np.mean(sims), rel_tol=0.1)


def test_jsd_null_expectation_matches_simulation():
  # k=5, equiprobable, n=m=2000: the same style of chance-alone-floor check
  # as the TVD test above, for the other null-gated shape metric.
  rng = np.random.default_rng(5)
  k = 5
  p = np.full(k, 1.0 / k)
  sims = []
  for _ in range(500):
    a = rng.multinomial(2000, p) / 2000
    b = rng.multinomial(2000, p) / 2000
    mix = 0.5 * (a + b)
    mask_a, mask_b = a > 0, b > 0
    kl_a = np.sum(a[mask_a] * np.log2(a[mask_a] / mix[mask_a]))
    kl_b = np.sum(b[mask_b] * np.log2(b[mask_b] / mix[mask_b]))
    sims.append(0.5 * kl_a + 0.5 * kl_b)
  assert math.isclose(
      noise.jsd_null_expectation_bits(k, 2000, 2000),
      np.mean(sims),
      rel_tol=0.2)


def test_mi_bias_nats_matches_formula():
  assert noise.mi_bias_nats(5, 5, 10_000) == 16 / 20_000


def test_rate_ratio_ci_straddles_one_at_equal_rates():
  ratio, lo, hi = noise.rate_ratio(20, 1000, 20, 1000)
  assert ratio == 1.0
  assert lo < 1.0 < hi


def test_rate_ratio_point_estimate_and_lower_bound():
  ratio, lo, hi = noise.rate_ratio(30, 1e4, 3, 1e4)
  assert math.isclose(ratio, 10.0, rel_tol=1e-9)
  assert lo > 2.5
  assert hi > ratio


def test_rate_ratio_degenerate_total_is_the_widest_interval():
  assert noise.rate_ratio(0, 10, 0, 10) == (None, 0.0, math.inf)


@pytest.mark.slow
def test_rate_ratio_monte_carlo_coverage_at_true_ratio_one():
  rng = np.random.default_rng(11)
  lam = 8.0
  hits = 0
  for _ in range(500):
    m1, m2 = rng.poisson(lam), rng.poisson(lam)
    _, lo, hi = noise.rate_ratio(m1, 1.0, m2, 1.0)
    hits += lo <= 1.0 <= hi
  assert hits / 500 >= 0.93


def test_degenerate_sizes_return_none():
  assert noise.noise_floor("ks_two_sample", n=0, m=10) is None
  assert noise.noise_floor(None) is None


def test_noise_floor_none_string_returns_none():
  assert noise.noise_floor("none", n=10, m=10) is None


@pytest.mark.parametrize("method",
                         ["wilson", "newcombe", "rate_ratio", "delong"])
def test_noise_floor_returns_none_for_interval_and_unimplemented_methods(
    method):
  assert noise.noise_floor(method, n=10, m=10, k=3, r=2, c=2) is None


def test_noise_floor_dispatches_ks_two_sample():
  assert noise.noise_floor(
      "ks_two_sample", n=1000, m=1000) == noise.ks_critical(1000, 1000)


def test_noise_floor_dispatches_tvd_null():
  p = [0.5, 0.3, 0.2]
  assert noise.noise_floor(
      "tvd_null", p=p, n=1000,
      m=1000) == noise.tvd_null_expectation(p, 1000, 1000)


def test_noise_floor_dispatches_jsd_null():
  assert noise.noise_floor(
      "jsd_null", k=5, n=2000,
      m=2000) == noise.jsd_null_expectation_bits(5, 2000, 2000)


def test_noise_floor_dispatches_fisher_z():
  assert noise.noise_floor(
      "fisher_z", n=100, m=100) == noise.fisher_z_delta_floor(100, 100)


def test_noise_floor_dispatches_mi_bias():
  assert noise.noise_floor(
      "mi_bias", r=5, c=5, n=10_000) == noise.mi_bias_nats(5, 5, 10_000)


def test_noise_floor_missing_kwarg_returns_none():
  assert noise.noise_floor("tvd_null", n=1000, m=1000) is None
  assert noise.noise_floor("jsd_null", n=1000, m=1000) is None
  assert noise.noise_floor("mi_bias", r=5, n=1000) is None


def test_noise_floor_fisher_z_needs_more_than_three_samples():
  assert noise.noise_floor("fisher_z", n=3, m=100) is None


def test_noise_floor_unknown_method_raises():
  with pytest.raises(ValueError, match="unknown noise_floor method"):
    noise.noise_floor("bootstrap", n=10, m=10)


@pytest.mark.parametrize(("lo", "hi", "folded"), [
    (-0.1, 0.3, (0.0, 0.3)),
    (0.2, 0.5, (0.2, 0.5)),
    (-0.5, -0.2, (0.2, 0.5)),
    (-0.4, 0.1, (0.0, 0.4)),
])
def test_folded_abs_interval(lo, hi, folded):
  assert noise.folded_abs_interval(lo, hi) == folded


def test_folded_abs_interval_is_still_importable_from_relational():
  assert relational._folded_abs_interval is noise.folded_abs_interval  # pylint: disable=protected-access  # the compatibility alias (R63)


def _share(head_y, head_x, ys, xs):
  ys, xs = np.asarray(ys, dtype=float), np.asarray(xs, dtype=float)
  return noise.StratifiedShare(head_y, head_x, ys.sum(), xs.sum(),
                               (ys * ys).sum(), (ys * xs).sum(),
                               (xs * xs).sum(), float((xs > 0).sum()))


def test_stratified_ratio_interval_degenerate_cases():
  assert noise.stratified_ratio_interval(_share(0, 0, [], []), 0.5) is None
  # R75: nothing observed (no head row, no sampled tail row) is no
  # estimate, even when the tail is known to hold rows — never a made-up 0
  assert noise.stratified_ratio_interval(
      _share(0, 0, [], []), 0.02, tail_total=5000.0) is None
  # an empty tail: the head is the whole column, known exactly
  head_only = _share(3.0, 10.0, [], [])
  assert noise.stratified_ratio_interval(
      head_only, 0.5, tail_total=0.0) == (0.3, 0.3, 0.3)
  # a tail with rows but none sampled: every tail rate is possible, the
  # point takes the head's rate
  ratio, lo, hi = noise.stratified_ratio_interval(
      head_only, 0.5, tail_total=10.0)
  assert (ratio, lo, hi) == pytest.approx((0.3, 3 / 20, 13 / 20))


def test_stratified_ratio_interval_is_tail_only_korn_graubard():
  """R70: the interval is built for the tail ratio q̂ alone (linearised
  variance with the (1 - rate) factor, n* capped at the Kish cluster
  count, K&G's t degrees-of-freedom factor, Clopper-Pearson), then mapped
  through (Y_head + X_t q) / (X_head + X_t), the head a known constant."""
  rate, head_y, head_x, tail_total = 0.2, 40.0, 400.0, 1000.0
  ys = [40.0, 0.0, 0.0, 10.0, 0.0, 0.0]
  xs = [40.0, 40.0, 20.0, 10.0, 60.0, 30.0]
  ratio, lo, hi = noise.stratified_ratio_interval(
      _share(head_y, head_x, ys, xs), rate, tail_total=tail_total)
  y, x = np.array(ys), np.array(xs)
  q = y.sum() / x.sum()
  s = len(xs)
  variance = (1 - rate) * ((y - q * x)**2).sum() / x.sum()**2 * s / (s - 1)
  kish = x.sum()**2 / (x * x).sum()
  n_star = min(kish, q * (1 - q) / variance)
  n_star *= (stats.t.ppf(0.975, x.sum() - 1) / stats.t.ppf(0.975, s - 1))**2
  q_lo = stats.beta.ppf(0.025, q * n_star, n_star - q * n_star + 1)
  q_hi = stats.beta.ppf(0.975, q * n_star + 1, n_star - q * n_star)

  def mapped(value):
    return (head_y + tail_total * value) / (head_x + tail_total)

  assert ratio == pytest.approx(mapped(q))
  assert lo == pytest.approx(mapped(q_lo))
  assert hi == pytest.approx(mapped(q_hi))
  # no tail_total: the tail's Horvitz-Thompson total x_t / rate
  ht = noise.stratified_ratio_interval(_share(head_y, head_x, ys, xs), rate)
  assert ht[0] == pytest.approx(
      (head_y + x.sum() / rate * q) / (head_x + x.sum() / rate))


def test_stratified_ratio_interval_bounds_a_rate_with_no_sampled_copy():
  """No copy among the sampled tail clusters: n* is the Kish cluster
  count (times the df factor), so the upper bound still bounds the rate;
  the lower bound is the head's own copies over the column."""
  xs = [40.0] * 5
  ratio, lo, hi = noise.stratified_ratio_interval(
      _share(80.0, 1000.0, [0.0] * 5, xs), 0.02, tail_total=10_000.0)
  n_star = 5 * (stats.t.ppf(0.975, 199) / stats.t.ppf(0.975, 4))**2
  q_hi = 1 - 0.025**(1 / n_star)
  assert ratio == pytest.approx(lo) == pytest.approx(80 / 11_000)
  assert hi == pytest.approx((80 + 10_000 * q_hi) / 11_000)


def test_stratified_ratio_interval_widens_with_cluster_size():
  """The same share from the same number of rows: heavy clusters (40 rows
  a value) carry far more sampling variance than singletons."""
  rate = 0.2

  def interval(size):
    values = 400 // size
    copied = values // 5
    ys = [size] * copied + [0] * (values - copied)
    xs = [size] * values
    return noise.stratified_ratio_interval(
        _share(0.0, 0.0, ys, xs), rate, tail_total=400 / rate)

  singletons, clusters = interval(1), interval(40)
  assert singletons[0] == clusters[0] == pytest.approx(0.2)
  assert clusters[2] - clusters[1] > 3 * (singletons[2] - singletons[1])
