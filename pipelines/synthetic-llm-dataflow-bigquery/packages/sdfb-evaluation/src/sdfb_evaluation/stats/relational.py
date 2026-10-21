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
"""Parent/child relational maths: the pure functions behind every
`relationship.*` entry in `catalogue/metrics.yaml` — fan-out shape (TVD,
W1, mean ratio), childless-parent share, cardinality adherence, parent
coverage, and the foreign-key orphan rate.

A parent-driven launch generates every child from its parent's landed keys
(ADR 0036/0037), so these metrics compare the resulting children-per-parent
distribution against the source's, not individual rows. `fanout_histogram`
turns one count per parent into a fixed-width histogram; `fanout_metrics`
turns a pair of those (plus the exact means/extremes a capped histogram
alone cannot recover) into every `relationship.fanout_*`/
`relationship.parent_coverage` value. `orphan_summary` is the SQL MATCH
SIMPLE-aware rate/denominator shared by `relationship.orphan_rate` (on the
synthetic table) and `relationship.orphan_rate_source` (the baseline).

Design: docs/designs/2026-07-07-evaluation-framework-design.md

References:
  Patki, N., Wedge, R., Veeramachaneni, K. (2016), "The Synthetic Data
    Vault" (parent-child cardinality as a generation target).
  Ramdas, A., Garcia Trillos, N., Cuturi, M. (2017), "On Wasserstein
    Two-Sample Testing and Related Families of Nonparametric Tests" (the
    CDF-integral identity for W1 that `_atoms_w1` applies to fan-out atoms).
  Newcombe, R. (1998), "Interval Estimation for the Difference Between
    Independent Proportions: Comparison of Eleven Methods" (the signed
    interval `noise.folded_abs_interval` folds into an unsigned one).

Design: docs/DESIGN.md §4 Relational generation
(ADR 0036, 0037).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np

from sdfb_evaluation.stats import distances
from sdfb_evaluation.stats.noise import (
    folded_abs_interval,
    newcombe_diff_interval,
    wilson_interval,
)


def _validate_cap(cap: int) -> None:
  """Raise `ValueError` unless `cap >= 1` (a histogram needs at least one
  exact-count bin, `0`, below its open-ended overflow bin).
  """
  if cap < 1:
    raise ValueError(f"cap must be >= 1, got {cap}")


def fanout_histogram(children_per_parent: Iterable[int],
                     cap: int = 50) -> np.ndarray:
  """The fan-out histogram over ALL parents, zero-child ones included:
  `cap + 1` bins, one per exact count `0 .. cap - 1` and one open-ended
  `>= cap` bin for everything at or beyond the cap.

  `children_per_parent` is one count per parent row — every parent in the
  table, not just the ones with at least one child — or the zero-child bin
  (which `fanout_metrics` reads as `z_src`/`z_syn`) would be empty by
  construction instead of measuring anything. An empty `children_per_parent`
  returns an all-zero histogram rather than raising.

  Raises `ValueError` if `cap < 1`, or if any count in `children_per_parent`
  is negative (a fan-out count, a number of children, can never be).
  """
  _validate_cap(cap)
  values = np.fromiter(children_per_parent, dtype=np.int64)
  if values.size == 0:
    return np.zeros(cap + 1, dtype=np.int64)
  if values.min() < 0:
    raise ValueError(
        f"children_per_parent must be non-negative, got {int(values.min())}")
  clipped = np.minimum(values, cap)
  return np.bincount(clipped, minlength=cap + 1)[:cap + 1]


def _fanout_atoms(
    counts: np.ndarray,
    total: float,
    cap: int,
    overflow_mean: float | None,
) -> dict[float, float]:
  """One side's fan-out histogram as point masses, `{location: probability}`.

  Bins `0 .. cap - 1` sit at their own count as an exact atom (a fan-out of
  exactly `c` children IS the value `c`). The open-ended `>= cap` bin has no
  single exactly-known value, so it is placed at `overflow_mean` — that
  side's own observed mean fan-out among its `>= cap` parents, when the
  caller has it — or, failing that, at `cap` itself (the catalogue's
  `fanout_w1` formula, Ruling R36).

  `overflow_mean` is used only when the `>= cap` bin actually holds mass
  (`counts[cap] > 0`); when it is empty, `overflow_mean` is ignored
  entirely, even if the caller passed one (e.g. a stale or undefined value
  from a side with no overflowing parents at all) — a mean of zero parents
  has no meaning, and placing a bogus or non-finite atom there would (with
  zero weight or not) corrupt `_atoms_w1`'s sorted locations and could turn
  its result into `nan`.
  """
  overflow_count = counts[cap]
  if overflow_count > 0.0 and overflow_mean is not None:
    centre = float(overflow_mean)
  else:
    centre = float(cap)
  atoms: dict[float, float] = {float(c): counts[c] / total for c in range(cap)}
  atoms[centre] = atoms.get(centre, 0.0) + overflow_count / total
  return atoms


def _atoms_w1(atoms_src: dict[float, float], atoms_syn: dict[float,
                                                             float]) -> float:
  """The exact Wasserstein-1 distance between two finite point-mass
  distributions at arbitrary, possibly non-matching, locations.

  `W1(P, Q) = int |F_P(x) - F_Q(x)| dx`. Both CDFs are step functions,
  constant between consecutive points of the sorted union of the two
  supports `x_1 < ... < x_m`, so the integral is exact and finite:
  `sum_i (x_{i+1} - x_i) * |F_P(x_i) - F_Q(x_i)|` (Ramdas, Garcia Trillos &
  Cuturi, 2017, the CDF-integral identity for W1 — applied here to atoms
  rather than a quantile-grid's edges, so nothing is truncated: every
  fan-out atom's location is exactly known, unlike
  `stats.binned.w1_from_bins`'s unbounded outer tail). Beyond `x_m` both
  CDFs equal 1 and contribute nothing, so no explicit tail term is needed.
  """
  locations = sorted(set(atoms_src) | set(atoms_syn))
  p = np.array([atoms_src.get(x, 0.0) for x in locations])
  q = np.array([atoms_syn.get(x, 0.0) for x in locations])
  f_p = np.cumsum(p)[:-1]
  f_q = np.cumsum(q)[:-1]
  widths = np.diff(np.array(locations, dtype=float))
  return float(np.sum(np.abs(f_p - f_q) * widths))


# The fold moved to `stats.noise` (Ruling R63); the old private name stays
# importable for compatibility.
_folded_abs_interval = folded_abs_interval


def _validate_histogram_counts(counts: np.ndarray, name: str) -> None:
  """Raise `ValueError` unless every bin of `counts` is a finite,
  non-negative, whole-number count.

  A float array with integral VALUES is accepted (`fanout_histogram`
  itself returns integer dtype, but a caller reconstructing a histogram
  from a BigQuery aggregation query typically has floats) — only a
  fractional, negative, `nan` or `inf` bin is a caller bug.
  """
  if not np.all(np.isfinite(counts)):
    raise ValueError(f"{name} must be finite, got {counts.tolist()}")
  if np.any(counts < 0.0):
    raise ValueError(f"{name} must be non-negative, got {counts.tolist()}")
  if not np.all(counts == np.round(counts)):
    raise ValueError(
        f"{name} must hold whole-number counts, got {counts.tolist()}")


def _validate_overflow_mean(value: float | None, overflow_count: float,
                            cap: int, name: str) -> None:
  """Raise `ValueError` unless a supplied overflow mean is a finite number
  `>= cap` — but only when that side's `>= cap` bin actually has parents in
  it.

  `value is None` (never supplied) and `overflow_count <= 0` (the bin is
  empty, so the mean is meaningless and `_fanout_atoms` ignores it anyway)
  both skip validation entirely: there is nothing to check.
  """
  if value is None or overflow_count <= 0.0:
    return
  if not math.isfinite(value) or value < cap:
    raise ValueError(
        f"{name} must be finite and >= cap ({cap}) when the >= cap bin is "
        f"non-empty, got {value}")


def _validate_extremes(h_src_arr: np.ndarray, cap: int, min_src: int,
                       max_src: int) -> None:
  """Raise `ValueError` unless `min_src`/`max_src` are consistent with
  `h_src_arr`'s own lowest/highest non-empty bin.

  Below `cap` a histogram bin IS the exact count, so if the lowest (or
  highest) non-empty bin sits there, `min_src` (`max_src`) must equal it
  exactly. If the lowest (or highest) non-empty bin is instead the
  `>= cap` overflow bin, the true extreme is unknown beyond "at least
  `cap`", so only `min_src >= cap` (`max_src >= cap`) can be checked. Does
  nothing when `h_src_arr` is entirely empty (`fanout_metrics` already
  returns `None` for that case before this ever runs).
  """
  nonzero = np.flatnonzero(h_src_arr)
  if nonzero.size == 0:
    return
  lowest, highest = int(nonzero[0]), int(nonzero[-1])
  if lowest < cap:
    if min_src != lowest:
      raise ValueError(
          f"min_src ({min_src}) must equal h_src's lowest non-empty bin "
          f"({lowest})")
  elif min_src < cap:
    raise ValueError(
        f"min_src ({min_src}) must be >= cap ({cap}): h_src's lowest "
        "non-empty bin is the >= cap overflow bin")
  if highest < cap:
    if max_src != highest:
      raise ValueError(
          f"max_src ({max_src}) must equal h_src's highest non-empty bin "
          f"({highest})")
  elif max_src < cap:
    raise ValueError(
        f"max_src ({max_src}) must be >= cap ({cap}): h_src's highest "
        "non-empty bin is the >= cap overflow bin")


def fanout_metrics(
    h_src: Sequence[float],
    h_syn: Sequence[float],
    mean_src: float,
    mean_syn: float,
    min_src: int,
    max_src: int,
    *,
    cap: int = 50,
    mean_overflow_src: float | None = None,
    mean_overflow_syn: float | None = None,
) -> dict[str, Any] | None:
  """Every `relationship.fanout_*`/`relationship.parent_coverage` value,
  from one pair of `fanout_histogram(..., cap=cap)` outputs.

  `h_src`/`h_syn` carry the shape; `mean_src`/`mean_syn` (the exact,
  uncapped means) and `min_src`/`max_src` (the exact, uncapped source
  extremes) are passed in separately because none of the three is
  recoverable from a capped histogram alone. `mean_overflow_src`/
  `mean_overflow_syn` are each side's exact mean fan-out among its own
  `>= cap` parents, when known; see `_fanout_atoms` for what happens when
  they are omitted.

  Returns `None` when either side has no parents at all (`sum(h_src) == 0`
  or `sum(h_syn) == 0`) — every metric below needs at least one parent on
  each side to be defined.

  Returns a dict:
    - `tvd`: `relationship.fanout_tvd` — total variation between the raw
      histograms (`stats.distances.tvd`).
    - `w1`: `relationship.fanout_w1` (informational) — Wasserstein-1 in
      children per parent, on bin centres (`_atoms_w1`).
    - `mean_ratio`: `relationship.fanout_mean_ratio`, `mean_syn / mean_src`;
      `None` when `mean_src` is 0 (an undefined ratio, not a 0 or inf one).
    - `zero_child_share_source`/`zero_child_share_synthetic`: `h_src[0] /
      sum(h_src)` and its synthetic twin.
    - `zero_child_share_delta`: `relationship.zero_child_share_delta`, plus
      `zero_child_share_delta_ci_low`/`_ci_high` (`folded_abs_interval`
      over `newcombe_diff_interval`).
    - `cardinality_adherence`: `relationship.cardinality_adherence` — the
      share of synthetic parents with a fan-out in `[min_src, max_src]`,
      plus `cardinality_adherence_count` (the raw adherent count Task 25
      needs) and `cardinality_adherence_ci_low`/`_ci_high`
      (`wilson_interval(count, n_syn)` — the catalogue declares
      `noise_floor: wilson` for this metric). Exact when `max_src < cap`
      (every count that matters is an exact histogram bin). When
      `max_src >= cap`, the whole `>= cap` bin is approximated as inside
      the range — `cardinality_adherence_exact` records which case
      applied. That approximation can be wrong in EITHER direction once
      `max_src >= cap`: a synthetic parent counted there could in truth
      exceed `max_src` (over-counted), and if `min_src > cap` too, an
      overflow parent could equally be genuinely below `min_src`
      (under-counted) — the histogram alone cannot distinguish any of
      these from one another.
    - `parent_coverage`: `relationship.parent_coverage`, `Pr_syn[c >= 1] /
      Pr_src[c >= 1]`; `None` when the source has no parent with a child
      (an undefined ratio).

  Raises `ValueError` if `cap < 1`; if `h_src`/`h_syn` are not both
  exactly `cap + 1` long (they must come from
  `fanout_histogram(..., cap=cap)`), or hold a negative, non-finite or
  non-integral count; if `min_src`/`max_src` are inconsistent with
  `h_src`'s own non-empty bins (`_validate_extremes`); or if a supplied
  `mean_overflow_src`/`mean_overflow_syn` is non-finite or `< cap` while
  that side's `>= cap` bin is non-empty (`_validate_overflow_mean` — see
  `_fanout_atoms` for why an empty bin's overflow mean is never
  validated, only ignored).
  """
  _validate_cap(cap)
  h_src_arr = np.asarray(h_src, dtype=np.float64)
  h_syn_arr = np.asarray(h_syn, dtype=np.float64)
  if h_src_arr.shape != (cap + 1,) or h_syn_arr.shape != (cap + 1,):
    raise ValueError(
        f"h_src and h_syn must each have cap + 1 = {cap + 1} bins, got "
        f"shapes {h_src_arr.shape} and {h_syn_arr.shape}")
  _validate_histogram_counts(h_src_arr, "h_src")
  _validate_histogram_counts(h_syn_arr, "h_syn")

  n_src = float(h_src_arr.sum())
  n_syn = float(h_syn_arr.sum())
  if n_src <= 0.0 or n_syn <= 0.0:
    return None

  _validate_extremes(h_src_arr, cap, min_src, max_src)
  _validate_overflow_mean(mean_overflow_src, float(h_src_arr[cap]), cap,
                          "mean_overflow_src")
  _validate_overflow_mean(mean_overflow_syn, float(h_syn_arr[cap]), cap,
                          "mean_overflow_syn")

  # distances.tvd takes Sequence[float]; .tolist() also satisfies mypy,
  # which does not treat ndarray as a Sequence[float].
  tvd_value = distances.tvd(h_src_arr.tolist(), h_syn_arr.tolist())
  # Type narrowing only, not a runtime safety check: n_src, n_syn > 0 above
  # already guarantee stats.distances.tvd finds nonzero mass on both sides,
  # so this can never actually fire.
  assert tvd_value is not None, (
      "unreachable: n_src, n_syn > 0 above guarantee stats.distances.tvd "
      "finds nonzero mass on both sides")

  atoms_src = _fanout_atoms(h_src_arr, n_src, cap, mean_overflow_src)
  atoms_syn = _fanout_atoms(h_syn_arr, n_syn, cap, mean_overflow_syn)
  w1_value = _atoms_w1(atoms_src, atoms_syn)

  mean_ratio = mean_syn / mean_src if mean_src > 0.0 else None

  z_src = float(h_src_arr[0] / n_src)
  z_syn = float(h_syn_arr[0] / n_syn)
  zero_delta = abs(z_syn - z_src)
  ci_lo, ci_hi = newcombe_diff_interval(
      int(h_syn_arr[0]), int(n_syn), int(h_src_arr[0]), int(n_src))
  ci_lo, ci_hi = folded_abs_interval(ci_lo, ci_hi)

  exact = max_src < cap
  lo_idx = max(0, min_src)
  if exact:
    hi_idx = min(max_src, cap - 1)
    adherent = float(h_syn_arr[lo_idx:hi_idx +
                               1].sum()) if hi_idx >= lo_idx else 0.0
  else:
    adherent = float(h_syn_arr[lo_idx:cap].sum()) + float(h_syn_arr[cap])
  adherent_count = round(adherent)
  adherence = adherent / n_syn
  adherence_ci_lo, adherence_ci_hi = wilson_interval(adherent_count, int(n_syn))

  src_share_with_child = 1.0 - z_src
  syn_share_with_child = 1.0 - z_syn
  parent_coverage = (
      syn_share_with_child /
      src_share_with_child if src_share_with_child > 0.0 else None)

  return {
      "tvd": tvd_value,
      "w1": w1_value,
      "mean_ratio": mean_ratio,
      "zero_child_share_source": z_src,
      "zero_child_share_synthetic": z_syn,
      "zero_child_share_delta": zero_delta,
      "zero_child_share_delta_ci_low": ci_lo,
      "zero_child_share_delta_ci_high": ci_hi,
      "cardinality_adherence": adherence,
      "cardinality_adherence_count": adherent_count,
      "cardinality_adherence_ci_low": adherence_ci_lo,
      "cardinality_adherence_ci_high": adherence_ci_hi,
      "cardinality_adherence_exact": exact,
      "parent_coverage": parent_coverage,
  }


def orphan_summary(total_nonnull: int, orphans: int,
                   null_keys: int) -> dict[str, Any]:
  """`relationship.orphan_rate`/`relationship.orphan_rate_source`'s rate and
  denominator, under SQL MATCH SIMPLE semantics: a child key tuple with any
  NULL part is neither an orphan nor part of `total_nonnull` — it is
  reported separately as `null_keys`, which never enters the rate.

  `total_nonnull` is the count of child key tuples with every part
  non-null; `orphans` is the subset of those with no matching parent row.
  `rate = orphans / total_nonnull`, or `None` when `total_nonnull` is 0 (no
  non-null tuples to measure — not the same as a rate of 0).

  Raises `ValueError` if any argument is negative, or if `orphans` exceeds
  `total_nonnull` (both are caller invariants, never data conditions).
  """
  if total_nonnull < 0 or orphans < 0 or null_keys < 0:
    raise ValueError(
        "total_nonnull, orphans and null_keys must all be non-negative: "
        f"got {total_nonnull}, {orphans}, {null_keys}")
  if orphans > total_nonnull:
    raise ValueError(
        f"orphans ({orphans}) cannot exceed total_nonnull ({total_nonnull})")
  rate = orphans / total_nonnull if total_nonnull > 0 else None
  return {
      "total_nonnull": total_nonnull,
      "orphans": orphans,
      "null_keys": null_keys,
      "rate": rate,
  }
