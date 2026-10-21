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
"""Tests for `sdfb_evaluation.stats.diversity` (Task 8).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from scipy.stats import entropy as scipy_entropy

from sdfb_evaluation.stats import diversity as d
from sdfb_evaluation.stats import distances

# ---------------------------------------------------------------------------
# entropy_bits
# ---------------------------------------------------------------------------


def test_entropy_bits_zero_for_a_single_category():
  # All mass on one value: no uncertainty, entropy EXACTLY 0 (k <= 1 short-
  # circuits the formula entirely — Ruling R20 — so this is an exact
  # equality, not a pytest.approx tolerance).
  for n in (3, 5, 10, 12, 37):
    clc = n * math.log(n)
    assert d.entropy_bits(n, clc, k=1) == 0.0


def test_entropy_bits_zero_for_zero_distinct_values():
  # k = 0 (no add() ever ran) is degenerate the same way k = 1 is.
  assert d.entropy_bits(10, 0.0, k=0) == 0.0


def test_entropy_bits_log2k_for_a_uniform_distribution():
  k, per_value = 16, 5
  n = k * per_value
  clc = k * per_value * math.log(per_value)
  assert d.entropy_bits(n, clc, k=k) == pytest.approx(math.log2(k))


def test_entropy_bits_matches_scipy_on_counts():
  rng = np.random.default_rng(3)
  counts = rng.integers(1, 50, size=30)
  n = int(counts.sum())
  clc = float(sum(c * math.log(c) for c in counts))
  expected = scipy_entropy(counts, base=2)
  assert d.entropy_bits(n, clc, k=len(counts)) == pytest.approx(expected)


def test_entropy_bits_none_for_nonpositive_n():
  assert d.entropy_bits(0, 0.0, k=1) is None
  assert d.entropy_bits(-1, 0.0, k=5) is None


@settings(max_examples=60)
@given(
    st.integers(1, 5000),
    st.lists(st.integers(1, 5000), min_size=1, max_size=50),
)
def test_entropy_bits_never_negative(n, raw_counts):
  # Build a valid (n, clc, k) triple from a random count vector scaled to
  # sum to n, and confirm entropy_bits never rounds below 0 (Ruling R20).
  total = sum(raw_counts)
  counts = [max(1, c * n // total) for c in raw_counts]
  actual_n = sum(counts)
  clc = sum(c * math.log(c) for c in counts)
  result = d.entropy_bits(actual_n, clc, k=len(counts))
  assert result is not None
  assert result >= 0.0


# ---------------------------------------------------------------------------
# miller_madow_bits
# ---------------------------------------------------------------------------


def test_miller_madow_bits_formula():
  h, k, n = 3.0, 10, 500
  expected = h + (k - 1) / (2.0 * n * math.log(2.0))
  assert d.miller_madow_bits(h, k, n) == pytest.approx(expected)


def test_miller_madow_bits_correction_shrinks_to_zero_as_n_grows():
  # The correction term is always positive (k >= 1) and vanishes as n -> inf.
  small_n_gap = d.miller_madow_bits(2.0, 5, 100) - 2.0
  large_n_gap = d.miller_madow_bits(2.0, 5, 1_000_000) - 2.0
  assert small_n_gap > large_n_gap > 0.0


def test_miller_madow_bits_clamped_to_nonnegative():
  # A raw h near 0 with k = 1 (no correction term at all, since k - 1 = 0)
  # must never come back negative (Ruling R20).
  assert d.miller_madow_bits(-1e-16, 1, 100) == 0.0
  assert d.miller_madow_bits(0.0, 1, 100) == 0.0


# ---------------------------------------------------------------------------
# chao_shen_bits / chao_shen_term (Ruling R3)
# ---------------------------------------------------------------------------


def _chao_shen_bits_reference(counts: list[int]) -> float:
  """An independent, fully-vectorized re-implementation of Chao & Shen
  (2003), used only to cross-check `chao_shen_bits`/`chao_shen_term`
  against a second derivation of the same formula.
  """
  arr = np.array([c for c in counts if c > 0], dtype=float)
  n = float(arr.sum())
  f1 = float(np.sum(arr == 1.0))
  coverage = 1.0 - (f1 - 1.0) / n if f1 >= n else 1.0 - f1 / n
  p_tilde = coverage * arr / n
  terms = -p_tilde * np.log(p_tilde) / (1.0 - (1.0 - p_tilde)**n)
  return float(terms.sum() / math.log(2.0))


@pytest.mark.parametrize(
    "counts",
    [
        [1, 1, 2, 3, 5, 8, 13, 21],  # known frequency-of-frequencies: f1 = 2
        [5] * 20,  # no singletons: f1 = 0
        [1] * 10,  # every value a singleton: triggers the f1 == n guard
        [100, 1, 1, 1],
    ],
)
def test_chao_shen_bits_matches_reference_formula(counts):
  assert d.chao_shen_bits(counts) == pytest.approx(
      _chao_shen_bits_reference(counts))


def test_chao_shen_term_sums_to_chao_shen_bits():
  counts = [1, 1, 2, 3, 5, 8, 13, 21]
  n = sum(counts)
  f1 = sum(1 for c in counts if c == 1)
  coverage = 1.0 - f1 / n  # f1 != n here, no guard branch needed
  total_nats = sum(d.chao_shen_term(c, n, coverage) for c in counts)
  assert total_nats / math.log(2.0) == pytest.approx(d.chao_shen_bits(counts))


def test_chao_shen_bits_is_the_value_python_3_11_computed():
  """CPython 3.12 made the builtin `sum()` of floats compensated, which
  moved the last bit of this total. The terms are added left to right, as
  3.11 added them and as the pipeline's combiners add them."""
  assert d.chao_shen_bits([7, 1, 5, 8, 7, 5]).hex() == "0x1.3e3d03ab4b0d8p+1"


def test_chao_shen_bits_empty_is_none():
  assert d.chao_shen_bits([]) is None
  assert d.chao_shen_bits([0, 0]) is None


def test_chao_shen_term_degenerate_inputs_are_zero():
  assert d.chao_shen_term(0, 10, 0.9) == 0.0
  assert d.chao_shen_term(5, 0, 0.9) == 0.0
  assert d.chao_shen_term(5, 10, 0.0) == 0.0


def test_chao_shen_term_p_tilde_one_does_not_raise():
  # coverage == 1 and c == n (a single value holding the whole sample):
  # log1p(-1.0) would raise, so this exercises the p_tilde >= 1.0 shortcut.
  assert d.chao_shen_term(50, 50, 1.0) == 0.0


def test_chao_shen_bits_stable_at_large_n():
  # A large n stresses the (1 - p) ** n term the denominator is built
  # from; chao_shen_term's expm1/log1p path (Ruling R21 #8) must still
  # agree with the naive vectorized reference formula at this scale.
  counts = [1, 1, 2, 10, 100, 5000, 50000, 4_999_846]
  assert d.chao_shen_bits(counts) == pytest.approx(
      _chao_shen_bits_reference(counts), rel=1e-6)
  assert math.isfinite(d.chao_shen_bits(counts))


# ---------------------------------------------------------------------------
# CensusAccumulator.add / merge — the rev-1 example
# ---------------------------------------------------------------------------


def _rev1_accumulator() -> d.CensusAccumulator:
  # source a x20, b x2; synthetic a x5, b x3, z x2 (all substantive).
  acc = d.CensusAccumulator()
  acc.add(1, 20, 5, 20, 5, substantive=True)  # a
  acc.add(2, 2, 3, 2, 3, substantive=True)  # b
  acc.add(3, 0, 2, 0, 2, substantive=True)  # z (synthetic-only)
  return acc


def test_rev1_example_coverage_novelty_substantive_copy_rate():
  res = d.summarize(_rev1_accumulator())
  assert res["coverage_mass"] == pytest.approx(1.0)
  assert res["novelty_mass"] == pytest.approx(0.2)
  assert res["category_adherence"] == pytest.approx(0.8)  # 1 - novelty_mass
  assert res["substantive_copy_rate"] == pytest.approx(0.3)


def test_rev1_example_raw_accumulator_fields():
  acc = _rev1_accumulator()
  assert acc.n_src == 22
  assert acc.n_syn == 10
  assert acc.coverage_mass_num == 22  # both a (20) and b (2) are shared
  assert acc.novelty_syn == 2  # z is synthetic-only
  assert acc.copies_substantive == 3  # b: c_src=2 < 10, 3 synthetic rows
  assert acc.substantive_syn == 10


# ---------------------------------------------------------------------------
# entropy_ratio on constant (k <= 1) columns — Ruling R20 regression
#
# The bug: `log2(n) - clc / (n * ln(2))` leaves float residue on a
# constant column (e.g. -8.9e-16 at n=10), and summarize's old
# `entropy_src_m != 0.0` guard let that residue through, producing
# entropy_ratio values like -9.9e14, -0.0 and -9.1e-16. entropy_ratio now
# gates on k_*_m instead of comparing floats to 0.0.
# ---------------------------------------------------------------------------


def _two_value_acc(n_src_m: int, syn_split: tuple[int,
                                                  int]) -> d.CensusAccumulator:
  # One value holds ALL of the source's matched rows (source constant);
  # the matched synthetic rows split across two distinct values.
  acc = d.CensusAccumulator()
  x, y = syn_split
  acc.add(1, n_src_m, x, n_src_m, x, substantive=True)
  acc.add(2, 0, y, 0, y, substantive=True)
  return acc


@pytest.mark.parametrize("n_src_m", [3, 10, 12])
def test_entropy_ratio_source_constant_synthetic_diverse_is_none_with_reason(
    n_src_m):
  acc = _two_value_acc(n_src_m, (5, 5))
  res = d.summarize(acc)
  assert res["entropy_src_m_bits"] == 0.0
  assert res["entropy_syn_m_bits"] > 0.0
  assert res["entropy_ratio"] is None
  assert res["entropy_ratio_reason"] == "source constant"


@pytest.mark.parametrize("n_src_m,n_syn_m", [(3, 5), (10, 12), (5, 5)])
def test_entropy_ratio_both_constant_is_exactly_one(n_src_m, n_syn_m):
  # A single add() call: the only value on BOTH sides, at different counts
  # -> both sides are constant (k_src_m == k_syn_m == 1) despite n_src_m !=
  # n_syn_m. Regression case: this used to produce -0.0.
  acc = d.CensusAccumulator()
  acc.add(1, n_src_m, n_syn_m, n_src_m, n_syn_m, substantive=True)
  res = d.summarize(acc)
  assert res["entropy_src_m_bits"] == 0.0
  assert res["entropy_syn_m_bits"] == 0.0
  # Regression: this used to be -0.0, which fails `== 1.0` on its own, but
  # spelling it out documents exactly what the old bug produced here.
  assert res["entropy_ratio"] == 1.0
  assert res["entropy_ratio_reason"] is None


def test_entropy_ratio_source_diverse_synthetic_constant_is_zero():
  acc = d.CensusAccumulator()
  acc.add(1, 5, 8, 5, 8, substantive=True)  # both sides have this value
  acc.add(2, 5, 0, 5, 0, substantive=True)  # source-only: source diverse
  res = d.summarize(acc)
  assert res["entropy_src_m_bits"] > 0.0
  assert res["entropy_syn_m_bits"] == 0.0
  assert res["entropy_ratio"] == 0.0
  assert res["entropy_ratio_reason"] is None


def test_entropy_ratio_never_divides_by_a_zero_source_entropy():
  """An estimated view (the census's value-sampled Horvitz-Thompson
  counts, Ruling R67) can hold k >= 2 with Σ c ln c >= n ln n, so the
  clamped source entropy is 0 although the source is not constant."""
  n = 100
  acc = d.CensusAccumulator(
      n_src_m=n,
      k_src_m=5,
      clc_src_m=n * math.log(n) * 1.5,
      n_syn_m=n,
      k_syn_m=4,
      clc_syn_m=10.0)
  res = d.summarize(acc)
  assert res["entropy_src_m_bits"] == 0.0
  assert res["entropy_ratio"] is None
  assert res["entropy_ratio_reason"] == "source entropy is 0"


# ---------------------------------------------------------------------------
# CensusAccumulator.merge — purity, associativity, commutativity
# ---------------------------------------------------------------------------


def test_merge_is_pure():
  a, b = d.CensusAccumulator(), d.CensusAccumulator()
  a.add(1, 5, 5, 5, 5, substantive=True)
  b.add(2, 3, 3, 3, 3, substantive=True)
  a_before = (a.n_src, a.n_syn, tuple(a.top_src))
  b_before = (b.n_src, b.n_syn, tuple(b.top_src))
  merged = a.merge(b)
  assert (a.n_src, a.n_syn, tuple(a.top_src)) == a_before
  assert (b.n_src, b.n_syn, tuple(b.top_src)) == b_before
  assert merged.n_src == 8


def test_merge_with_empty_other_is_identity():
  a = d.CensusAccumulator()
  a.add(1, 5, 5, 5, 5, substantive=True)
  merged = a.merge(d.CensusAccumulator())
  assert merged.n_src == a.n_src
  assert merged.top_src == a.top_src


def _assert_accumulators_equal(a: d.CensusAccumulator, b: d.CensusAccumulator):
  assert a.n_src == b.n_src
  assert a.n_syn == b.n_syn
  assert a.n_src_m == b.n_src_m
  assert a.n_syn_m == b.n_syn_m
  assert a.k_src == b.k_src
  assert a.k_syn == b.k_syn
  assert a.k_src_m == b.k_src_m
  assert a.k_syn_m == b.k_syn_m
  assert a.k_both == b.k_both
  assert a.clc_src_m == pytest.approx(b.clc_src_m, rel=1e-9, abs=1e-9)
  assert a.clc_syn_m == pytest.approx(b.clc_syn_m, rel=1e-9, abs=1e-9)
  assert a.f1_src == b.f1_src
  assert a.f1_src_m == b.f1_src_m
  assert a.f1_syn_m == b.f1_syn_m
  assert a.coverage_mass_num == b.coverage_mass_num
  assert a.novelty_syn == b.novelty_syn
  assert a.copies_substantive == b.copies_substantive
  assert a.substantive_syn == b.substantive_syn
  assert a.top_src == b.top_src
  assert a.top_syn == b.top_syn


_entry_strategy = st.tuples(
    st.integers(0, 20),  # c_src
    st.integers(0, 20),  # c_syn
    st.integers(0, 20),  # c_src_m
    st.integers(0, 20),  # c_syn_m
    st.booleans(),  # substantive
)


@settings(max_examples=40)
@given(st.lists(_entry_strategy, min_size=1, max_size=25))
def test_merge_is_associative_and_commutative(entries):
  # Each entry gets a unique hash (its index), honouring the module
  # precondition that a value key is added exactly once.
  keyed = list(enumerate(entries))

  def build(pairs) -> d.CensusAccumulator:
    acc = d.CensusAccumulator()
    for h, (c_src, c_syn, c_src_m, c_syn_m, substantive) in pairs:
      acc.add(h, c_src, c_syn, c_src_m, c_syn_m, substantive=substantive)
    return acc

  single_pass = build(keyed)

  mid = len(keyed) // 2
  left, right = build(keyed[:mid]), build(keyed[mid:])
  split_then_merged = left.merge(right)

  reversed_pass = build(list(reversed(keyed)))

  _assert_accumulators_equal(single_pass, split_then_merged)
  _assert_accumulators_equal(single_pass, reversed_pass)
  # Commutativity of merge itself.
  _assert_accumulators_equal(left.merge(right), right.merge(left))


def test_merge_top_truncation_above_k_matches_single_pass():
  # > _TOP_K (1000) distinct values on each side: a single linear pass and
  # a merge tree (chunks small enough that no individual chunk truncates,
  # only the merges across them do) must land on the identical top list.
  rng = np.random.default_rng(42)
  n_values = 1500
  counts = rng.integers(1, 10_000, size=n_values)

  def build(hashes) -> d.CensusAccumulator:
    acc = d.CensusAccumulator()
    for h in hashes:
      c = int(counts[h])
      acc.add(h, c, c, c, c, substantive=True)
    return acc

  single_pass = build(range(n_values))

  chunk_size = 400  # < _TOP_K: no chunk truncates on its own
  chunks = [
      build(range(i, min(i + chunk_size, n_values)))
      for i in range(0, n_values, chunk_size)
  ]
  merged = chunks[0]
  for chunk in chunks[1:]:
    merged = merged.merge(chunk)

  assert len(single_pass.top_src) == 1000
  assert single_pass.top_src == merged.top_src
  assert single_pass.top_syn == merged.top_syn


# ---------------------------------------------------------------------------
# top_src / top_syn — bound, tie-break, order-independence
# ---------------------------------------------------------------------------


def test_top_list_bounded_and_ties_broken_by_hash_ascending():
  acc = d.CensusAccumulator()
  # Fill to _TOP_K - 1 = 999 with strictly larger, unique counts.
  for i in range(999):
    acc.add(1000 + i, 2000 - i, 0, 0, 0, substantive=False)
  assert len(acc.top_src) == 999

  # Three ties at the boundary count (1000): only one slot is free, and the
  # tie-break must keep the SMALLEST hash among them, regardless of the
  # order they arrive in (a larger hash arriving first is evicted later by
  # a smaller one; a larger hash arriving after is rejected outright).
  acc.add(500, 1000, 0, 0, 0, substantive=False)  # hash 500, fills the slot
  acc.add(700, 1000, 0, 0, 0, substantive=False)  # hash 700 > 500, rejected
  acc.add(100, 1000, 0, 0, 0, substantive=False)  # hash 100 < 500, replaces it

  assert len(acc.top_src) == 1000
  assert acc.top_src[-1] == (1000, 100)
  assert (1000, 500) not in acc.top_src
  assert (1000, 700) not in acc.top_src
  assert acc.top_src[0] == (2000, 1000)  # the largest count leads


@settings(max_examples=25)
@given(
    st.lists(
        st.tuples(st.integers(1, 500), st.integers(0, 10_000)),
        min_size=2,
        max_size=30,
        unique_by=lambda item: item[1],  # unique hashes
    ).flatmap(lambda items: st.permutations(items).map(lambda perm:
                                                       (items, perm))))
def test_top_list_is_deterministic_regardless_of_add_order(pair):
  items, permuted = pair

  def build(order) -> list[tuple[int, int]]:
    acc = d.CensusAccumulator()
    for count, h in order:
      acc.add(h, count, 0, 0, 0, substantive=False)
    return acc.top_src

  assert build(items) == build(permuted)


# ---------------------------------------------------------------------------
# Good-Turing unseen mass (f1_src / n_src) — known frequency-of-frequencies
# ---------------------------------------------------------------------------


def test_good_turing_unseen_mass_from_known_frequency_of_frequencies():
  # Source: three singletons (A, B, C), one value at count 4 (D), one at
  # count 10 (E) -> n_src = 17, f1_src = 3.
  acc = d.CensusAccumulator()
  for h, c in [(1, 1), (2, 1), (3, 1), (4, 4), (5, 10)]:
    acc.add(h, c, c, c, c, substantive=True)
  res = d.summarize(acc)
  assert acc.f1_src == 3
  assert acc.n_src == 17
  assert res["good_turing_unseen_src"] == pytest.approx(3 / 17)


# ---------------------------------------------------------------------------
# summarize on a hand example where c_*_m != c_* (matched is a real subset)
# ---------------------------------------------------------------------------


def test_summarize_hand_example_matched_differs_from_full():
  # v1, v2: shared between source and synthetic, matched-n subsamples them.
  # v3: source-only (full AND matched). v4: synthetic-only (full AND
  # matched). c_src_m/c_syn_m are each well below c_src/c_syn.
  acc = d.CensusAccumulator()
  acc.add(1, 50, 40, 10, 8, substantive=True)  # v1
  acc.add(2, 30, 20, 6, 4, substantive=True)  # v2
  acc.add(3, 20, 0, 4, 0, substantive=True)  # v3: source-only
  acc.add(4, 0, 15, 0, 3, substantive=True)  # v4: synthetic-only
  res = d.summarize(acc)

  assert acc.k_both == 2  # v1, v2
  assert res["distinct_both"] == 2
  assert res["distinct_src_m"] == 3  # v1, v2, v3
  assert res["distinct_syn_m"] == 3  # v1, v2, v4
  assert res["distinct_ratio"] == pytest.approx(1.0)

  matched_src_counts = [10, 6, 4]  # v1, v2, v3
  matched_syn_counts = [8, 4, 3]  # v1, v2, v4
  expected_h_src = scipy_entropy(matched_src_counts, base=2)
  expected_h_syn = scipy_entropy(matched_syn_counts, base=2)
  assert res["entropy_src_m_bits"] == pytest.approx(expected_h_src)
  assert res["entropy_syn_m_bits"] == pytest.approx(expected_h_syn)

  assert res["top1_share_src"] == pytest.approx(50 / 100)  # full c_src
  assert res["top1_share_syn"] == pytest.approx(40 / 75)  # full c_syn
  assert res["top1_share_delta"] == pytest.approx(abs(40 / 75 - 50 / 100))

  expected_mm_src = expected_h_src + (3 - 1) / (2 * 20 * math.log(2.0))
  expected_mm_syn = expected_h_syn + (3 - 1) / (2 * 15 * math.log(2.0))
  assert res["miller_madow_src_m_bits"] == pytest.approx(expected_mm_src)
  assert res["miller_madow_syn_m_bits"] == pytest.approx(expected_mm_syn)

  # No singleton (count == 1) among the matched counts on either side.
  assert res["chao_shen_coverage_src_m"] == pytest.approx(1.0)
  assert res["chao_shen_coverage_syn_m"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Degenerate: an all-NULL column (no values ever added)
# ---------------------------------------------------------------------------


def test_summarize_all_null_column_is_all_none():
  res = d.summarize(d.CensusAccumulator())
  assert res["entropy_src_m_bits"] is None
  assert res["entropy_syn_m_bits"] is None
  assert res["entropy_ratio"] is None
  assert res["entropy_ratio_reason"] is None
  assert res["distinct_ratio"] is None
  assert res["coverage_mass"] is None
  assert res["novelty_mass"] is None
  assert res["category_adherence"] is None
  assert res["good_turing_unseen_src"] is None
  assert res["top1_share_delta"] is None
  assert res["substantive_copy_rate"] is None
  assert res["chao_shen_coverage_src_m"] is None
  assert not res["top_src"]
  assert not res["top_syn"]


# ---------------------------------------------------------------------------
# Entropy ratio at matched n vs the naive full-n ratio (n-dependence bias)
# ---------------------------------------------------------------------------


def _clc(counts: np.ndarray) -> float:
  return float(sum(c * math.log(c) for c in counts if c > 0))


def _matched_and_naive_entropy_ratio(seed: int) -> tuple[float, float]:
  # Source (10k draws) and synthetic (900k draws) come from the IDENTICAL
  # 20,000-value Zipf(s=0.8) distribution (Ruling R19 — support and s
  # widened from an earlier 5,000-value/s=1.1 draw whose naive-ratio bound
  # only held for ~2.5% of seeds; a controller review scan of 100 seeds at
  # THESE parameters found naive ratio >= 1.087 and matched |ratio - 1| <=
  # 0.011 on every one of them, so the bounds below hold broadly, not just
  # for the seed picked here). At matched n the entropy ratio must be
  # close to 1; the naive full-n ratio (900k vs 10k, no matching) is
  # biased upward by the sample-size-dependent plug-in entropy bias — this
  # documents exactly the effect D5's "n-dependent metrics use matched n"
  # rule exists to cancel.
  vocab = 20_000
  ranks = np.arange(1, vocab + 1, dtype=float)
  p = ranks**(-0.8)
  p /= p.sum()

  n_src, n_syn = 10_000, 900_000
  rng = np.random.default_rng(seed)
  src_draws = rng.choice(vocab, size=n_src, p=p)
  syn_draws = rng.choice(vocab, size=n_syn, p=p)

  m = min(n_src, n_syn)
  thinned = rng.random(n_syn) < (m / n_syn)  # Bernoulli(m / n_syn) thinning

  c_src = np.bincount(src_draws, minlength=vocab)
  c_syn = np.bincount(syn_draws, minlength=vocab)
  c_syn_m = np.bincount(syn_draws[thinned], minlength=vocab)
  c_src_m = c_src  # m == n_src: every source row is already matched

  acc = d.CensusAccumulator()
  for v in range(vocab):
    acc.add(
        v,
        int(c_src[v]),
        int(c_syn[v]),
        int(c_src_m[v]),
        int(c_syn_m[v]),
        substantive=True)
  matched_ratio = d.summarize(acc)["entropy_ratio"]

  naive_src = d.entropy_bits(
      int(c_src.sum()), _clc(c_src), k=int(np.count_nonzero(c_src)))
  naive_syn = d.entropy_bits(
      int(c_syn.sum()), _clc(c_syn), k=int(np.count_nonzero(c_syn)))
  naive_ratio = naive_syn / naive_src
  return matched_ratio, naive_ratio


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_entropy_ratio_is_one_for_same_distribution_different_n(seed):
  matched_ratio, naive_ratio = _matched_and_naive_entropy_ratio(seed)
  assert matched_ratio == pytest.approx(1.0, abs=0.02)
  assert naive_ratio > 1.05


# ---------------------------------------------------------------------------
# tvd_jsd_from_census — second-pass agreement with stats.distances
# ---------------------------------------------------------------------------


def test_tvd_jsd_from_census_matches_distances_module():
  src = {"a": 20, "b": 5}
  syn = {"a": 10, "b": 5, "c": 15}
  _, p, q = distances.align(src, syn)
  n_src, n_syn = int(p.sum()), int(q.sum())
  pairs = list(zip(p.astype(int).tolist(), q.astype(int).tolist(), strict=True))

  tvd_value, jsd_value = d.tvd_jsd_from_census(pairs, n_src, n_syn)
  assert tvd_value == pytest.approx(distances.tvd(p, q))
  assert jsd_value == pytest.approx(distances.jsd_bits(p, q))


def test_tvd_jsd_from_census_nonpositive_totals_raises():
  # Ruling R21 #4: a non-positive total is a caller bug (Task 22 only
  # calls this once both totals are known positive), so it raises rather
  # than returning a silent (0.0, 0.0).
  with pytest.raises(ValueError, match="positive"):
    d.tvd_jsd_from_census([], 0, 0)
  with pytest.raises(ValueError, match="positive"):
    d.tvd_jsd_from_census([(1, 1)], 0, 5)
  with pytest.raises(ValueError, match="positive"):
    d.tvd_jsd_from_census([(1, 1)], 5, -1)
