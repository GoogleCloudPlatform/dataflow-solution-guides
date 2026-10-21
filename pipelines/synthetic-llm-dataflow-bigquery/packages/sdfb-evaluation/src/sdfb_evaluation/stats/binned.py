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
"""Exact binned-CDF maths: edge construction, bin counting, and the
distance metrics `column.ks`, `column.pit_w1` and `column.wasserstein` build
on.

Every metric here is computed from bin COUNTS on a fixed set of edges, never
from the raw rows: the planning query's `APPROX_QUANTILES(x, 1000)` gives a
1,001-point grid per side, and Task 21's Beam accumulator turns every row
into an exact count against that fixed grid (`bin_counts`). Nothing here
re-reads a raw value, which is what lets a table-sized column be measured
with two passes (plan, then count) instead of a sort.

CDFs are evaluated at the edges themselves, right-continuous: bin `i` is
`(e[i-1], e[i]]` (`e[-1] = -inf`), so `F(e[i])` is exact — it is exactly
`cumsum(counts)[i] / total`, not an interpolation. That convention is what
makes `w1_from_bins`'s integral exact for a step CDF rather than merely a
trapezoidal approximation of one (see its docstring).

Design: docs/designs/2026-07-07-evaluation-framework-design.md

References:
  Smirnov, N. (1948), "Table for Estimating the Goodness of Fit of
    Empirical Distributions".
  Ramdas, A., Garcia Trillos, N., Cuturi, M. (2017), "On Wasserstein
    Two-Sample Testing and Related Families of Nonparametric Tests".
  Czado, C., Gneiting, T., Held, L. (2009), "Predictive Model Assessment
    for Count Data" (the mid-distribution / non-randomized PIT that
    `pit_w1` uses to stay unbiased on discrete and binned columns).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

# w1_from_bins needs at least one finite interval [e0, e_last] to integrate
# over, which takes 2 edges.
_MIN_EDGES_FOR_INTERVAL = 2


def _validate_equal_counts(c_src: np.ndarray, c_syn: np.ndarray) -> None:
  """Raise `ValueError` unless `c_src` and `c_syn` are the same length.

  Both must be `bin_counts` against the identical edges to be comparable at
  all; a length mismatch is a caller bug (Task 21 pins this contract, so it
  fails loudly rather than broadcasting or truncating silently).
  """
  if c_src.shape != c_syn.shape:
    raise ValueError(
        "c_src and c_syn must be counted against the same edges (same "
        f"length): got shapes {c_src.shape} and {c_syn.shape}")


def _validate_edges_match_counts(edges: np.ndarray, counts: np.ndarray) -> None:
  """Raise `ValueError` unless `counts` has exactly `edges.size + 1` bins."""
  expected = edges.shape[0] + 1
  if counts.shape[0] != expected:
    raise ValueError(
        f"counts must have edges.size + 1 = {expected} entries for "
        f"{edges.shape[0]} edges, got {counts.shape[0]}")


def union_edges(
    q_src: Sequence[float],
    q_syn: Sequence[float],
    atoms: Sequence[float] = (),
) -> np.ndarray:
  """The sorted, unique, finite edges from both sides' quantile grids.

  `q_src`/`q_syn` are each a 1,001-point `APPROX_QUANTILES` grid (or any
  quantile grid); `atoms` are extra edges to pin in regardless (e.g. a
  known sentinel value). Used to resolve `ks_bracket`: a collapse on one
  side that a source-only grid would miss shows up in `q_syn`'s own grid,
  so the union always has an edge near it.
  """
  combined = np.concatenate([
      np.asarray(q_src, dtype=float),
      np.asarray(q_syn, dtype=float),
      np.asarray(atoms, dtype=float),
  ])
  return np.unique(combined[np.isfinite(combined)])


def profile_edges(q_src: Sequence[float], bins: int = 100) -> np.ndarray:
  """`bins - 1` equiprobable interior edges from the source's own grid.

  `q_src` is treated as a quantile grid evenly spaced in probability over
  `[0, 1]` (e.g. the 1,001-point `APPROX_QUANTILES(x, 1000)` grid); this
  picks the `bins - 1` interior points at probabilities `1/bins, ...,
  (bins - 1)/bins` by nearest-index lookup into it, so `bins` dividing the
  grid's own resolution evenly (e.g. `bins=100` against a 1,001-point grid)
  lands exactly on those quantiles. Duplicates collapse via `np.unique`
  (a source column with repeated values can make adjacent quantiles equal).
  An empty `q_src` (an all-NULL source column has no quantile grid at all)
  returns an empty array rather than raising.
  """
  q = np.asarray(q_src, dtype=float)
  if q.size == 0:
    return np.array([], dtype=float)
  probs = np.arange(1, bins) / bins
  idx = np.round(probs * (q.size - 1)).astype(int)
  edges = q[idx]
  return np.unique(edges[np.isfinite(edges)])


def decile_edges(q_src: Sequence[float]) -> np.ndarray:
  """The 9 interior source deciles (p10, p20, ..., p90) — `profile_edges`
  with `bins=10`.
  """
  return profile_edges(q_src, bins=10)


def bin_counts(x: np.ndarray, edges: np.ndarray) -> np.ndarray:
  """Exact counts of `x` against `edges`: `len(edges) + 1` bins,
  `(-inf, e0], (e0, e1], ..., (e_last, +inf)`. NaNs in `x` are excluded
  (they belong to neither bin).
  """
  values = np.asarray(x, dtype=float)
  values = values[~np.isnan(values)]
  edges = np.asarray(edges, dtype=float)
  idx = np.searchsorted(edges, values, side="left")
  return np.bincount(idx, minlength=edges.size + 1)[:edges.size + 1]


def _cdf_at_edges(counts: np.ndarray) -> tuple[np.ndarray, float]:
  """`(F(-inf)=0, F(e0), ..., F(e_last), F(+inf)=1)` and the total count."""
  total = float(counts.sum())
  if total <= 0.0:
    return np.zeros(counts.size + 1), 0.0
  return np.concatenate(([0.0], np.cumsum(counts) / total)), total


def ks_bracket(
    c_src: np.ndarray,
    c_syn: np.ndarray,
) -> tuple[float, float] | None:
  """A rigorous `(D_lo, D_hi)` bracket on the true two-sample KS statistic,
  from exact bin counts on a shared, fixed set of edges (Smirnov, 1948).

  `D_lo` is the largest CDF gap actually observed at an edge (a true lower
  bound: the real KS statistic can only be at least this large). `D_hi`
  additionally accounts for what could happen strictly *inside* each bin —
  since both CDFs are only known to be monotone between edges, the worst
  case in bin `i` is one side already at its right-edge value while the
  other is still at its left-edge value — so `D_hi` is a true upper bound.
  The two converge as the edge grid gets finer; `D_hi - D_lo` is the
  resolution penalty of the fixed grid.

  Raises `ValueError` if `c_src` and `c_syn` are not the same length (they
  must be counts against the same edges). Returns `None` if either side's
  total count is zero (nothing to compare).
  """
  c_src_arr = np.asarray(c_src, dtype=float)
  c_syn_arr = np.asarray(c_syn, dtype=float)
  _validate_equal_counts(c_src_arr, c_syn_arr)
  f_src, total_src = _cdf_at_edges(c_src_arr)
  f_syn, total_syn = _cdf_at_edges(c_syn_arr)
  if total_src <= 0.0 or total_syn <= 0.0:
    return None
  d_lo = float(np.max(np.abs(f_src - f_syn)))
  d_hi = float(
      np.max(np.maximum(f_src[1:] - f_syn[:-1], f_syn[1:] - f_src[:-1])))
  return d_lo, d_hi


def tail_masses(c_src: np.ndarray, c_syn: np.ndarray) -> dict[str, float]:
  """The fraction of each side's mass that falls outside `[e0, e_last]` —
  the outermost bin on either end, `(-inf, e0]` and `(e_last, +inf)`.

  Companion to `w1_from_bins`, which integrates only between the outermost
  edges: this reports what that integral leaves out, for a metric
  producer's `detail`, rather than folding an unlocated tail mass into the
  value itself.
  """
  c_src = np.asarray(c_src, dtype=float)
  c_syn = np.asarray(c_syn, dtype=float)
  total_src = float(c_src.sum())
  total_syn = float(c_syn.sum())
  return {
      "below_e0_source":
          float(c_src[0]) / total_src if total_src > 0 else 0.0,
      "above_e_last_source":
          float(c_src[-1]) / total_src if total_src > 0 else 0.0,
      "below_e0_synthetic":
          float(c_syn[0]) / total_syn if total_syn > 0 else 0.0,
      "above_e_last_synthetic":
          float(c_syn[-1]) / total_syn if total_syn > 0 else 0.0,
  }


def w1_from_bins(
    edges: np.ndarray,
    c_src: np.ndarray,
    c_syn: np.ndarray,
) -> float | None:
  """The Wasserstein-1 distance `int |F_syn(x) - F_src(x)| dx` restricted
  to `[e0, e_last]`, exact for a CDF that is piecewise-constant on each
  `(e[i], e[i+1])` at its left edge's value `F(e[i])` — the same
  right-continuous step convention `bin_counts` bins under (Ramdas et al.,
  2017, Prop. 1's L1-of-CDFs identity for W1).

  Mass outside `[e0, e_last]` has no known location, so it is excluded from
  the integral rather than guessed at; `tail_masses` reports it separately.
  Use `union_edges`, not a source-only grid: edges that are coarse or
  absent on the synthetic side push its mass into fewer, wider bins, which
  biases this integral (a source-only decile grid, in particular, is too
  coarse for this metric even though it is fine for `pit_w1`).

  Raises `ValueError` if `c_src`/`c_syn` are not the same length, or if
  either does not have exactly `edges.size + 1` entries. Returns `None` if
  either side's total count is zero, or fewer than 2 edges are given (no
  interval to integrate over).
  """
  edge_arr = np.asarray(edges, dtype=float)
  c_src_arr = np.asarray(c_src, dtype=float)
  c_syn_arr = np.asarray(c_syn, dtype=float)
  _validate_equal_counts(c_src_arr, c_syn_arr)
  _validate_edges_match_counts(edge_arr, c_src_arr)
  if edge_arr.size < _MIN_EDGES_FOR_INTERVAL:
    return None
  total_src = float(c_src_arr.sum())
  total_syn = float(c_syn_arr.sum())
  if total_src <= 0.0 or total_syn <= 0.0:
    return None
  f_src = (np.cumsum(c_src_arr) / total_src)[:-1]  # F(e0), ..., F(e_last)
  f_syn = (np.cumsum(c_syn_arr) / total_syn)[:-1]
  widths = np.diff(edge_arr)
  gap = np.abs(f_src[:-1] - f_syn[:-1])
  return float(np.sum(gap * widths))


def _mid_cdf(counts: np.ndarray, total: float) -> np.ndarray:
  """The Parzen mid-distribution value at every bin: `cumsum(p) - p / 2`.

  For bin `b`, this sits halfway between the CDF just before `b`'s mass
  arrives and the CDF just after — the discrete/binned analogue of "the
  point itself" a continuous CDF would use, and the reason `pit_w1` stays
  unbiased on a column with a large point mass (a spike of zeros, a
  constant column, an integer column) instead of always crediting that
  mass to one edge of its bin (Czado, Gneiting & Held, 2009).
  """
  p = counts / total
  return np.cumsum(p) - p / 2.0


def pit_w1(c_src: np.ndarray, c_syn: np.ndarray) -> float | None:
  """The PIT Wasserstein-1 distance `sum_b p_src(b) * |Fbar_syn(b) -
  Fbar_src(b)|`, where `Fbar` is the Parzen mid-distribution function
  (`_mid_cdf`) rather than the ordinary right-continuous CDF — the
  probability integral transform of both columns through the source's own
  distribution, so the result is scale-free and lands in `[0, 1/2]` (0.5
  for two fully disjoint distributions).

  Summed over ALL bins, including the open-ended last one: unlike
  `w1_from_bins`, `pit_w1` needs no finite outer edge, because the
  mid-distribution function is already well-defined for an unbounded bin
  (its own `p(b)` still has a well-defined midpoint, even though the bin
  itself has no right edge to evaluate an ordinary CDF at).

  The weighting by `p_src(b)` (not `p_syn(b)`) is what makes this a PIT
  distance rather than a plain binned W1, and it holds on ANY shared edge
  set — unlike the ordinary-CDF version this replaces, it does not need
  `p_src(b)` to be uniform. `union_edges` (the grid Task 21 uses for every
  binned metric) is the recommended grid; `profile_edges`/`decile_edges`
  also work and remain useful where a source-only grid is wanted.

  Raises `ValueError` if `c_src` and `c_syn` are not the same length.
  Returns `None` if either side's total count is zero.
  """
  c_src_arr = np.asarray(c_src, dtype=float)
  c_syn_arr = np.asarray(c_syn, dtype=float)
  _validate_equal_counts(c_src_arr, c_syn_arr)
  total_src = float(c_src_arr.sum())
  total_syn = float(c_syn_arr.sum())
  if total_src <= 0.0 or total_syn <= 0.0:
    return None
  p_src = c_src_arr / total_src
  f_mid_src = _mid_cdf(c_src_arr, total_src)
  f_mid_syn = _mid_cdf(c_syn_arr, total_syn)
  return float(np.sum(p_src * np.abs(f_mid_syn - f_mid_src)))


def decile_ks_legacy(a: Sequence[float], b: Sequence[float]) -> float:
  """Max absolute difference between two piecewise-linear empirical CDFs
  built from each side's decile vector, evaluated on the union grid via
  `numpy.interp`.

  Verbatim port of `decile_ks` in
  `scripts/e2e/source_synthetic_stats_diff.py`, kept independent (this
  package never imports from `scripts/`) so `column.decile_ks_legacy` stays
  numerically identical to that script's history. No division anywhere in
  this computation, so a degenerate (all-equal) vector — e.g. a collapsed
  synthetic column whose deciles are all the same value — can't raise a
  div-by-zero; `numpy.interp` extrapolates a fully-flat `xp` to
  `fp[0]`/`fp[-1]` on either side, which is exactly the step-function CDF a
  constant column implies.
  """
  if not a or not b:
    return 0.0
  arr_a = np.asarray(a, dtype=float)
  arr_b = np.asarray(b, dtype=float)
  fp_a = np.linspace(0.0, 1.0, num=len(arr_a))
  fp_b = np.linspace(0.0, 1.0, num=len(arr_b))
  grid = np.union1d(arr_a, arr_b)
  f_a = np.interp(grid, arr_a, fp_a)
  f_b = np.interp(grid, arr_b, fp_b)
  return float(np.max(np.abs(f_a - f_b)))


def quantiles_from_bins(
    edges: np.ndarray,
    counts: np.ndarray,
    probs: Sequence[float],
) -> list[float]:
  """Approximate quantile values at `probs`, inverted from bin `counts` on
  `edges` by linear interpolation of the edge-exact CDF — for a profile's
  reconstructed distribution or a GUI histogram, not for a gated metric.

  A probability outside `[F(e0), F(e_last)]` clamps to `e0`/`e_last`
  (`numpy.interp`'s default flat extrapolation): the outermost bins extend
  to `+-inf`, so there is no better single value to report for them.
  Returns `nan` for every probability when there are no edges or no mass.
  """
  edge_arr = np.asarray(edges, dtype=float)
  count_arr = np.asarray(counts, dtype=float)
  total = float(count_arr.sum())
  if edge_arr.size == 0 or total <= 0.0:
    return [float("nan")] * len(probs)
  cdf = np.cumsum(count_arr)[:edge_arr.size] / total
  values = np.interp(np.asarray(list(probs), dtype=float), cdf, edge_arr)
  return [float(v) for v in values]
