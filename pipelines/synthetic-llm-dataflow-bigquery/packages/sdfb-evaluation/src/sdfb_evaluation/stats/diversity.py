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
"""Diversity census at matched n: one mergeable accumulator per column that
feeds `column.entropy_ratio`, `column.distinct_ratio`, `column.coverage_mass`,
`column.novelty_mass`, `column.top1_share_delta`, `field.category_adherence`
and `field.substantive_copy_rate` (`src/sdfb_evaluation/catalogue/metrics.yaml`).

The Beam census (a later task) streams one `(value_hash, c_src, c_syn,
c_src_m, c_syn_m)` row per DISTINCT value of a column into `CensusAccumulator
.add`, then merges accumulators pairwise across bundles/workers via `merge`,
and finally calls `summarize`. `c_src`/`c_syn` are counts over ALL rows;
`c_src_m`/`c_syn_m` restrict to rows a row-hash Bernoulli flag selected for
the matched-n subsample, `m = min(n_src, n_syn)` — entropy and distinct
counts are n-dependent (a plug-in estimate on more rows looks more diverse
for no real reason), so both sides are compared at the same `m`, while
coverage/novelty/top1 are not (their catalogue entries are `n_dependent:
false`) and are computed on the full counts instead.

PRECONDITION: every value key is added via `add` exactly once, after the
upstream `CombinePerKey` has already grouped rows by (column, value) — two
accumulators being `merge`d must never have both seen the same value key.
`k_src`/`k_syn`/`f1_src`/etc. and the pairwise-summed `merge` below are only
exact under that precondition; nothing here can detect a violation of it.

Chao & Shen (2003) entropy needs the GLOBAL sample-coverage estimate before
any per-value term can be computed, so `summarize` does not attempt it from
the accumulator alone (Ruling R3): it reports the coverage estimate only
(`chao_shen_coverage_src_m`/`_syn_m`, from `f1_src_m`/`f1_syn_m`), and the
Beam census (Task 22) sums `chao_shen_term` itself in a second pass, once it
also has every value's raw count in hand. `chao_shen_bits` is the same maths
in one pass, for a caller that already has the whole count vector (detail
reporting, tests).

Design: docs/designs/2026-07-07-evaluation-framework-design.md

References:
  Shannon, C. (1948), "A Mathematical Theory of Communication".
  Good, I.J. (1953), "The Population Frequencies of Species and the
    Estimation of Population Parameters".
  Miller, G.A. (1955), "Note on the Bias of Information Estimates", in
    H. Quastler (ed.), Information Theory in Psychology: Problems and
    Methods, pp. 95-100 (the primary source for `miller_madow_bits`'s
    correction term).
  Paninski, L. (2003), "Estimation of Entropy and Mutual Information" (a
    secondary source restating Miller's bias term in modern notation).
  Chao, A., Shen, T-J. (2003), "Nonparametric Estimation of Shannon's Index
    of Diversity when there are Unseen Species in Sample".
  Hurlbert, S. (1971), "The Nonconcept of Species Diversity: A Critique and
    Alternative Parameters".
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

# The top-K bound on `CensusAccumulator.top_src`/`top_syn` (the value
# census's "top offenders" list): more than enough to read off top1 shares
# and a detail table, while staying bounded regardless of a column's true
# cardinality.
_TOP_K = 1000

# The repo's k-anonymity floor (`e2e_gcp_probe.py`'s `_MEM_KANON_MIN_COUNT`):
# a source value held by fewer rows than this is "rare", so a substantive
# synthetic copy of it is scored as a memorization risk, not ordinary
# category mass (`field.substantive_copy_rate`).
_K_ANON_FLOOR = 10

# A distribution with 0 or 1 distinct value carries no uncertainty at all
# (entropy is EXACTLY 0, not "close to 0"); below this `k`, `entropy_bits`
# skips the floating-point formula entirely rather than let cancellation
# between its two nearly-equal terms (`log2(n)` and `clc / (n ln 2)`) leave
# residue like `-8.9e-16` (Ruling R20).
_CONSTANT_K = 1

# `summarize`'s float outputs are rounded to this many significant digits
# (Ruling R21 #9): floating-point summation over a `merge` tree is not
# strictly associative, so two runs over the identical rows in a different
# bundle order can differ in the last few ulps; float64 carries ~15-17
# significant digits, so rounding to 12 is well past real precision and
# only erases that reordering noise, never a genuine difference.
_SUMMARIZE_SIG_DIGITS = 12


def _round_sig(x: float, sig: int = _SUMMARIZE_SIG_DIGITS) -> float:
  """`x` rounded to `sig` significant digits (not decimal places).

  `0.0` and non-finite values (`nan`/`inf`, which should not occur in
  practice but must never raise here) pass through unchanged — `log10` is
  undefined for the former and meaningless for the latter.
  """
  if x == 0.0 or not math.isfinite(x):
    return x
  shift = sig - math.floor(math.log10(abs(x))) - 1
  return round(x, shift)


def _top_sort_key(item: tuple[int, int]) -> tuple[int, int]:
  """Sort key for a `(count, hash)` entry: highest count first, hash
  ascending on ties — the tie-break that makes `top_src`/`top_syn`
  deterministic regardless of `add`/`merge` order.
  """
  count, value_hash = item
  return (-count, value_hash)


def _add_top(
    top: list[tuple[int, int]],
    count: int,
    value_hash: int,
) -> list[tuple[int, int]]:
  """`top` with `(count, value_hash)` folded in, kept to the best `_TOP_K`.

  `count <= 0` is a no-op (a value absent from this side never belongs in
  its top list), and so is a `count` that cannot beat the current worst
  kept entry once `top` is already full — both skip the list copy and
  sort entirely, which is what keeps a long tail of low-count values from
  paying `_TOP_K log _TOP_K` on every single one of them.
  """
  if count <= 0:
    return top
  if len(top) >= _TOP_K and _top_sort_key(
      (count, value_hash)) >= _top_sort_key(top[-1]):
    return top
  candidates = [*top, (count, value_hash)]
  candidates.sort(key=_top_sort_key)
  return candidates[:_TOP_K]


def _merge_top(
    a: list[tuple[int, int]],
    b: list[tuple[int, int]],
) -> list[tuple[int, int]]:
  """The best `_TOP_K` entries of `a` and `b` combined.

  `a` and `b` hold disjoint value hashes under the module precondition
  (each value key is added exactly once, on exactly one side of a merge
  tree), so this is a plain union-then-trim — associative and commutative,
  which is what lets `CensusAccumulator.merge` be either.
  """
  merged = a + b
  merged.sort(key=_top_sort_key)
  return merged[:_TOP_K]


def _top1_share(top: list[tuple[int, int]], total: int) -> float | None:
  """The leading entry's share of `total`, or `None` if there is no entry
  or no mass to share it against.
  """
  if not top or total <= 0:
    return None
  return top[0][0] / total


@dataclass
class CensusAccumulator:
  """One column's mergeable diversity census.

  Value keys are uint64 hashes (`value_hash`); literal labels are resolved
  later from the hash, under the literal-vs-hashed-label policy (D6). All
  counters default to the empty column (`CensusAccumulator()` is always a
  valid starting point for `add`/`merge`, mirroring `stats.moments.Moments`).

  `n_*`/`k_*` are full-n; `*_m` suffixes are the matched-n (`m = min(n_src,
  n_syn)`) subsample entropy/distinct-count needs (D5: n-dependent metrics
  are computed at matched n). `clc_src_m`/`clc_syn_m` are `sum(c * ln(c))`
  over the matched-n counts — the sufficient statistic `entropy_bits` needs,
  additive over disjoint value keys so merging stays exact without
  re-reading any per-value count. `f1_src` is the FULL-n source singleton
  count (Good-Turing's `f1/N` unseen-mass estimate, `column.novelty_mass`'s
  target); `f1_src_m`/`f1_syn_m` are the matched-n singleton counts Chao-Shen
  coverage needs (Ruling R3).
  """
  n_src: int = 0
  n_syn: int = 0
  n_src_m: int = 0
  n_syn_m: int = 0
  k_src: int = 0
  k_syn: int = 0
  k_src_m: int = 0
  k_syn_m: int = 0
  k_both: int = 0
  clc_src_m: float = 0.0
  clc_syn_m: float = 0.0
  f1_src: int = 0
  f1_src_m: int = 0
  f1_syn_m: int = 0
  coverage_mass_num: int = 0
  novelty_syn: int = 0
  copies_substantive: int = 0
  substantive_syn: int = 0
  top_src: list[tuple[int, int]] = field(default_factory=list)
  top_syn: list[tuple[int, int]] = field(default_factory=list)

  def add(
      self,
      h: int,
      c_src: int,
      c_syn: int,
      c_src_m: int,
      c_syn_m: int,
      *,
      substantive: bool,
  ) -> None:
    """Fold one distinct value's counts into this column's census, in place.

    `h` is the value's hash; `c_src`/`c_syn` are its FULL row counts on
    each side, `c_src_m`/`c_syn_m` its matched-n row counts (see the class
    docstring). `substantive` is the caller's own call (non-empty after
    trim, not a date sentinel) — `copies_substantive` counts this value's
    synthetic rows only when it is substantive AND rare in the source
    (`0 < c_src < 10`, the k-anonymity floor); `substantive_syn` counts
    every substantive value's synthetic rows, the denominator of
    `field.substantive_copy_rate`.
    """
    self.n_src += c_src
    self.n_syn += c_syn
    self.n_src_m += c_src_m
    self.n_syn_m += c_syn_m

    if c_src > 0:
      self.k_src += 1
      if c_src == 1:
        self.f1_src += 1
    if c_syn > 0:
      self.k_syn += 1
    if c_src > 0 and c_syn > 0:
      self.k_both += 1
      self.coverage_mass_num += c_src
    elif c_syn > 0:
      self.novelty_syn += c_syn

    if c_src_m > 0:
      self.k_src_m += 1
      self.clc_src_m += c_src_m * math.log(c_src_m)
      if c_src_m == 1:
        self.f1_src_m += 1
    if c_syn_m > 0:
      self.k_syn_m += 1
      self.clc_syn_m += c_syn_m * math.log(c_syn_m)
      if c_syn_m == 1:
        self.f1_syn_m += 1

    if substantive:
      self.substantive_syn += c_syn
      if 0 < c_src < _K_ANON_FLOOR:
        self.copies_substantive += c_syn

    self.top_src = _add_top(self.top_src, c_src, h)
    self.top_syn = _add_top(self.top_syn, c_syn, h)

  def merge(self, other: CensusAccumulator) -> CensusAccumulator:
    """The pairwise combination of `self` and `other`, as a new accumulator.

    Pure (neither operand is modified), and exact: every counter is a plain
    sum over disjoint value keys (the module precondition), and `top_src`/
    `top_syn` merge via `_merge_top`, itself associative and commutative —
    so the combination is associative and commutative regardless of how a
    Beam runner trees the merges.
    """
    return CensusAccumulator(
        n_src=self.n_src + other.n_src,
        n_syn=self.n_syn + other.n_syn,
        n_src_m=self.n_src_m + other.n_src_m,
        n_syn_m=self.n_syn_m + other.n_syn_m,
        k_src=self.k_src + other.k_src,
        k_syn=self.k_syn + other.k_syn,
        k_src_m=self.k_src_m + other.k_src_m,
        k_syn_m=self.k_syn_m + other.k_syn_m,
        k_both=self.k_both + other.k_both,
        clc_src_m=self.clc_src_m + other.clc_src_m,
        clc_syn_m=self.clc_syn_m + other.clc_syn_m,
        f1_src=self.f1_src + other.f1_src,
        f1_src_m=self.f1_src_m + other.f1_src_m,
        f1_syn_m=self.f1_syn_m + other.f1_syn_m,
        coverage_mass_num=self.coverage_mass_num + other.coverage_mass_num,
        novelty_syn=self.novelty_syn + other.novelty_syn,
        copies_substantive=self.copies_substantive + other.copies_substantive,
        substantive_syn=self.substantive_syn + other.substantive_syn,
        top_src=_merge_top(self.top_src, other.top_src),
        top_syn=_merge_top(self.top_syn, other.top_syn),
    )


def entropy_bits(n: int, clc: float, k: int) -> float | None:
  """Plug-in Shannon entropy in bits, from the total count `n`, `clc =
  sum(c * ln(c))` over the same values, and `k` the number of distinct
  values `clc` was summed over (Shannon, 1948).

  `None` when `n <= 0` — entropy is undefined for an empty sample, not
  zero. `k <= 1` (a constant column: 0 or 1 distinct value) returns
  EXACTLY `0.0` rather than evaluating the general formula: mathematically
  `log2(n) - clc / (n * ln(2))` is 0 there too (`clc = n * ln(n)` when a
  single value holds every row), but subtracting two nearly-equal
  floating-point terms leaves residue on the order of `1e-16` — enough for
  a caller dividing by it (`entropy_ratio`) to blow up (Ruling R20). For
  `k >= 2` the general closed form of `-sum((c/n) * log2(c/n))` is used,
  clamped to `max(0.0, ...)` for the same reason (entropy can never be
  negative; a near-zero true value can still round below 0).
  """
  if n <= 0:
    return None
  if k <= _CONSTANT_K:
    return 0.0
  return max(0.0, math.log2(n) - clc / (n * math.log(2.0)))


def miller_madow_bits(h: float, k: int, n: int) -> float:
  """The Miller-Madow bias-corrected entropy estimate, in bits:
  `h + (k - 1) / (2 * n * ln(2))` (Miller, 1955; restated in Paninski,
  2003).

  A finite-sample plug-in entropy `h` is biased low by roughly this much;
  `k` is the number of distinct values `h` was computed over and `n` the
  total count. Only meaningful where `h` itself is defined (`n > 0`); the
  caller only reaches this once `entropy_bits` has returned a value, not
  `None`. Clamped to `max(0.0, ...)`: entropy is never negative (Ruling
  R20), and the correction term itself is never negative for `k >= 1`, so
  this only guards a caller passing a raw, unclamped `h`.
  """
  return max(0.0, h + (k - 1) / (2.0 * n * math.log(2.0)))


def _chao_shen_coverage(f1: int, n: int) -> float | None:
  """The Good-Turing sample-coverage estimate `C_hat = 1 - f1/n`
  (Chao & Shen, 2003), guarded when every observed value is a singleton
  (`f1 == n`, where the plain formula would give 0): `C_hat = 1 - (f1 - 1)
  / n`. `None` when `n <= 0` (no sample, no coverage to estimate).
  """
  if n <= 0:
    return None
  if f1 >= n:
    return max(0.0, 1.0 - (f1 - 1) / n)
  return 1.0 - f1 / n


def chao_shen_term(c: int, n: int, coverage: float) -> float:
  """One value's nats contribution to the Chao-Shen entropy estimate:
  `-p_tilde * ln(p_tilde) / (1 - (1 - p_tilde) ** n)`, `p_tilde = coverage *
  c / n` (Chao & Shen, 2003).

  The building block both `chao_shen_bits` (summed over a full count
  vector) and Task 22's Beam second pass (summed while streaming, once
  `n` and the global `coverage` estimate are known) use, kept in one place
  so the two can never drift apart. `0.0` for a degenerate `c <= 0`, `n <=
  0` or `coverage <= 0` (no mass, no contribution) rather than raising or
  dividing by zero.

  The denominator is `1 - (1 - p_tilde) ** n`, computed as
  `-expm1(n * log1p(-p_tilde))` rather than literally (Ruling R21 #8):
  for a small `p_tilde` and a large `n` (a long-tailed column with many
  values and a big `n`), `(1 - p_tilde) ** n` rounds to something very
  close to 1, and `1 - (that)` then loses most of its significant digits
  to catastrophic cancellation; `expm1`/`log1p` are built for exactly this
  "answer near zero" regime and keep full precision there.
  """
  if c <= 0 or n <= 0 or coverage <= 0.0:
    return 0.0
  p_tilde = coverage * c / n
  if p_tilde <= 0.0:
    return 0.0
  # p_tilde == 1.0 (a single value holding the whole sample, coverage == 1)
  # would make log1p(-1.0) raise (log(0) is undefined); (1 - 1) ** n == 0
  # directly, so the denominator is exactly 1 without needing the
  # expm1/log1p path at all.
  denom = 1.0 if p_tilde >= 1.0 else -math.expm1(n * math.log1p(-p_tilde))
  if denom <= 0.0:
    return 0.0
  return -p_tilde * math.log(p_tilde) / denom


def chao_shen_bits(counts: Sequence[int]) -> float | None:
  """Chao & Shen's (2003) bias-corrected entropy estimate, in bits, from a
  full vector of per-value `counts`.

  One pass: builds the sample-coverage estimate (`_chao_shen_coverage`)
  from `counts`' own frequency-of-frequencies, then sums `chao_shen_term`
  over every count and converts the nats total to bits. `None` when
  `counts` is empty or every count is non-positive (nothing to estimate
  a distribution from).
  """
  positive = [int(c) for c in counts if c > 0]
  n = sum(positive)
  if n <= 0:
    return None
  f1 = sum(1 for c in positive if c == 1)
  coverage = _chao_shen_coverage(f1, n)
  if coverage is None:
    return None
  # Added left to right, not with the builtin sum(): CPython 3.12 made the
  # sum() of floats compensated, which moves this total's last bit. The
  # pipeline's combiners add the same terms one by one.
  total_nats = 0.0
  for c in positive:
    total_nats += chao_shen_term(c, n, coverage)
  return total_nats / math.log(2.0)


def tvd_jsd_from_census(
    pairs: Iterable[tuple[int, int]],
    n_src: int,
    n_syn: int,
) -> tuple[float, float]:
  """Total variation distance and Jensen-Shannon divergence (bits) between
  the source and synthetic distributions, from a second pass over per-value
  `(c_src, c_syn)` count pairs and the already-known totals `n_src`, `n_syn`.

  Each pair's mass is `c_src / n_src`, `c_syn / n_syn` — the TRUE column
  totals, not `sum(c_src for c_src, c_syn in pairs)` — so `pairs` never has
  to be padded with an explicit zero-count entry for every value the other
  side lacks the way `stats.distances.align`'s aligned vectors do; a value
  entirely absent from `pairs` simply contributes 0 to both sums, which is
  what a missing entry means either way. On the same aligned vectors this
  agrees with `stats.distances.tvd`/`jsd_bits` (see `test_diversity.py`).

  Unlike `distances.tvd`/`jsd_bits` (which return `None` for a degenerate
  side), this raises `ValueError` when `n_src <= 0` or `n_syn <= 0`
  (Ruling R21 #4): Task 22 only runs this second pass once both totals are
  already known positive from the first pass, so a non-positive total
  here is a caller bug to surface loudly, not a valid degenerate case to
  swallow into a silent `(0.0, 0.0)`.
  """
  if n_src <= 0 or n_syn <= 0:
    raise ValueError(
        f"n_src and n_syn must be positive: got n_src={n_src}, n_syn={n_syn}")
  tvd_sum = 0.0
  jsd_p_terms = 0.0
  jsd_q_terms = 0.0
  for c_src, c_syn in pairs:
    p = c_src / n_src
    q = c_syn / n_syn
    tvd_sum += abs(p - q)
    m = (p + q) / 2.0
    if p > 0.0:
      jsd_p_terms += p * math.log2(p / m)
    if q > 0.0:
      jsd_q_terms += q * math.log2(q / m)
  return (0.5 * tvd_sum, 0.5 * (jsd_p_terms + jsd_q_terms))


def summarize(
    acc: CensusAccumulator) -> dict[str, float | int | str | list[Any] | None]:
  """The catalogue-ready diversity metrics for one column's census.

  Entropy/distinct metrics read the matched-n fields (D5: n-dependent);
  coverage/novelty/top1 read the full-n fields (their catalogue entries are
  `n_dependent: false`). Chao-Shen is reported only as its global coverage
  estimate (`chao_shen_coverage_src_m`/`_syn_m`) — the full estimate needs a
  second pass over every value's count (Ruling R3; see `chao_shen_term`).
  Every derived field is `None` where its inputs are degenerate (an empty
  column, a zero-entropy constant column, ...) rather than raising or
  dividing by zero. Every float value is rounded to `_SUMMARIZE_SIG_DIGITS`
  significant digits (Ruling R21 #9): merge-tree floating-point summation
  is not strictly associative, so this is what makes re-running a census
  over the same rows (a different bundle order, a retried job) compare
  equal rather than differ in the last few ulps.

  `entropy_ratio` gates on `k_src_m`/`k_syn_m` (`_CONSTANT_K`), never on
  comparing a computed entropy to `0.0` (Ruling R20: `entropy_bits`'s
  near-zero floating-point residue on a near-constant column made a plain
  `!= 0.0` check unreliable — see `entropy_bits`'s docstring):
    - both sides constant (`k_src_m <= 1` and `k_syn_m <= 1`): `1.0` —
      neither side has any diversity to compare, so they match exactly.
    - source constant, synthetic not: `None`, with `entropy_ratio_reason`
      set to `"source constant"` — the ratio's denominator is genuinely
      zero, so no value describes this case.
    - source not constant, synthetic constant: `0.0` — the synthetic side
      collapsed to no diversity at all.
    - neither constant: the plain ratio `entropy_syn_m / entropy_src_m`,
      unless the source entropy is not positive (only an estimated view
      can get there, Ruling R67): `None` with reason "source entropy is 0".
  """
  entropy_src_m = entropy_bits(acc.n_src_m, acc.clc_src_m, acc.k_src_m)
  entropy_syn_m = entropy_bits(acc.n_syn_m, acc.clc_syn_m, acc.k_syn_m)
  src_constant = acc.k_src_m <= _CONSTANT_K
  syn_constant = acc.k_syn_m <= _CONSTANT_K

  entropy_ratio: float | None = None
  entropy_ratio_reason: str | None = None
  if entropy_src_m is None or entropy_syn_m is None:
    pass  # no matched-n data at all on one side; ratio stays None.
  elif src_constant and syn_constant:
    entropy_ratio = 1.0
  elif src_constant:
    entropy_ratio_reason = "source constant"
  elif syn_constant:
    entropy_ratio = 0.0
  elif entropy_src_m <= 0.0:
    # k >= 2 with a zero entropy only arises from a view whose counts are
    # estimates (the census's value-sampled Horvitz-Thompson view, Ruling
    # R67): never divide by it.
    entropy_ratio_reason = "source entropy is 0"
  else:
    entropy_ratio = entropy_syn_m / entropy_src_m

  miller_madow_src_m = (
      miller_madow_bits(entropy_src_m, acc.k_src_m, acc.n_src_m)
      if entropy_src_m is not None else None)
  miller_madow_syn_m = (
      miller_madow_bits(entropy_syn_m, acc.k_syn_m, acc.n_syn_m)
      if entropy_syn_m is not None else None)

  top1_share_src = _top1_share(acc.top_src, acc.n_src)
  top1_share_syn = _top1_share(acc.top_syn, acc.n_syn)
  top1_share_delta = (
      abs(top1_share_syn - top1_share_src)
      if top1_share_src is not None and top1_share_syn is not None else None)

  novelty_mass = (acc.novelty_syn / acc.n_syn) if acc.n_syn > 0 else None
  # field.category_adherence: the mass-weighted share of synthetic ROWS
  # (not distinct values) whose value is in the source vocabulary — exactly
  # the complement of novelty_mass, which is the same mass split the other
  # way (Ruling R21 #7).
  category_adherence = (1.0 -
                        novelty_mass) if novelty_mass is not None else None

  result: dict[str, float | int | str | list[Any] | None] = {
      "n_src":
          acc.n_src,
      "n_syn":
          acc.n_syn,
      "n_src_m":
          acc.n_src_m,
      "n_syn_m":
          acc.n_syn_m,
      "distinct_src_m":
          acc.k_src_m,
      "distinct_syn_m":
          acc.k_syn_m,
      "distinct_both":
          acc.k_both,
      "distinct_ratio":
          (acc.k_syn_m / acc.k_src_m) if acc.k_src_m > 0 else None,
      "entropy_src_m_bits":
          entropy_src_m,
      "entropy_syn_m_bits":
          entropy_syn_m,
      "entropy_ratio":
          entropy_ratio,
      "entropy_ratio_reason":
          entropy_ratio_reason,
      "miller_madow_src_m_bits":
          miller_madow_src_m,
      "miller_madow_syn_m_bits":
          miller_madow_syn_m,
      "chao_shen_coverage_src_m":
          _chao_shen_coverage(acc.f1_src_m, acc.n_src_m),
      "chao_shen_coverage_syn_m":
          _chao_shen_coverage(acc.f1_syn_m, acc.n_syn_m),
      "coverage_mass":
          (acc.coverage_mass_num / acc.n_src) if acc.n_src > 0 else None,
      "novelty_mass":
          novelty_mass,
      "category_adherence":
          category_adherence,
      "good_turing_unseen_src":
          (acc.f1_src / acc.n_src) if acc.n_src > 0 else None,
      "top1_share_src":
          top1_share_src,
      "top1_share_syn":
          top1_share_syn,
      "top1_share_delta":
          top1_share_delta,
      "substantive_copy_rate":
          (acc.copies_substantive /
           acc.substantive_syn) if acc.substantive_syn > 0 else None,
      "top_src":
          list(acc.top_src),
      "top_syn":
          list(acc.top_syn),
  }
  return {
      key: (_round_sig(value) if isinstance(value, float) else value)
      for key, value in result.items()
  }
