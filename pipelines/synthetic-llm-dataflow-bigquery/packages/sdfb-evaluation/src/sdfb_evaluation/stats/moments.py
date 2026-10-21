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
"""Mergeable summary statistics: one accumulator per column, one merge per
worker-pair, exact either way.

`Moments` tracks `(n, mean, M2, M3, M4)` plus `min`/`max`/`zeros`, where `M2`,
`M3` and `M4` are the second-, third- and fourth-order sums of centered
powers (`Mk = sum((x - mean) ** k)`), not the usual per-sample moments — this
is the sufficient statistic Pebay's pairwise formulas combine exactly, with
no re-reading of the raw values. `add_array` computes a batch's exact
moments with numpy in one pass, then folds them into the running state via
the same pairwise `merge` a Beam combiner would use across bundles/workers;
`merge` itself is pure (it returns a new `Moments`, leaving both operands
untouched) so a combiner can merge accumulators without racing on shared
state.

Design: docs/designs/2026-07-07-evaluation-framework-design.md

Reference: Pebay, P. (2008), "Formulas for Robust, One-Pass Parallel
Computation of Covariances and Arbitrary-Order Statistical Moments",
Sandia Report SAND2008-6212.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

# Below this sample size (or at zero variance, a constant column) skewness
# and kurtosis are undefined rather than merely noisy, so both properties
# return `None` instead of dividing by a zero or near-zero scale.
_MIN_HIGHER_MOMENT_N = 2


@dataclass
class Moments:
  """A Pebay (2008) pairwise-mergeable moment accumulator for one column.

  All fields default to the empty accumulator (`n=0`, `min=+inf`,
  `max=-inf`) so `Moments()` is always a valid starting point for
  `add_array`/`merge`. `m2`/`m3`/`m4` are the centered-power SUMS (`Mk`),
  not per-sample moments; `variance`/`skewness`/`kurtosis_excess` divide by
  `n` on read, not on write, which is what keeps `merge` exact.
  """
  n: int = 0
  mean: float = 0.0
  m2: float = 0.0
  m3: float = 0.0
  m4: float = 0.0
  min: float = math.inf
  max: float = -math.inf
  zeros: int = 0
  nonfinite: int = 0

  def add_array(self, xs: np.ndarray) -> None:
    """Fold a batch of values into this accumulator, in place.

    NaN and +-inf are dropped first and counted in `nonfinite` — neither
    carries moment information, and letting either through would poison
    `mean`/`m2`/`m3`/`m4` with a NaN that then contaminates every future
    `merge` (and breaks `to_dict`'s JSON-safety promise). The batch's own
    exact moments are computed directly with numpy over what remains — one
    pass over `xs`, no incremental per-element update — and then combined
    with whatever this accumulator already held via `merge`, so a column
    processed as ten batches or as one gives the identical result up to
    floating-point rounding (see `test_merge_equals_single_pass`).
    """
    arr = np.asarray(xs, dtype=float)
    finite_mask = np.isfinite(arr)
    nonfinite_count = int(arr.size - int(np.sum(finite_mask)))
    arr = arr[finite_mask]
    if arr.size == 0:
      batch = Moments(nonfinite=nonfinite_count)
    else:
      mean_b = float(arr.mean())
      centered = arr - mean_b
      batch = Moments(
          n=int(arr.size),
          mean=mean_b,
          m2=float(np.sum(centered**2)),
          m3=float(np.sum(centered**3)),
          m4=float(np.sum(centered**4)),
          min=float(arr.min()),
          max=float(arr.max()),
          zeros=int(np.sum(arr == 0.0)),
          nonfinite=nonfinite_count)
    merged = self.merge(batch)
    self.n, self.mean = merged.n, merged.mean
    self.m2, self.m3, self.m4 = merged.m2, merged.m3, merged.m4
    self.min, self.max, self.zeros = merged.min, merged.max, merged.zeros
    self.nonfinite = merged.nonfinite

  def merge(self, other: Moments) -> Moments:
    """The pairwise combination of `self` and `other`, as a new `Moments`.

    Pure: neither operand is modified. `n == 0` on either side is handled
    up front (a copy of the non-empty side) rather than by the general
    formula, which would divide by `n_a + n_b == 0` when both are empty.
    `nonfinite` always adds directly in every branch: it counts values
    that never entered `n` in the first place, on either side.
    """
    nonfinite = self.nonfinite + other.nonfinite
    if self.n == 0:
      return Moments(
          n=other.n,
          mean=other.mean,
          m2=other.m2,
          m3=other.m3,
          m4=other.m4,
          min=other.min,
          max=other.max,
          zeros=other.zeros,
          nonfinite=nonfinite)
    if other.n == 0:
      return Moments(
          n=self.n,
          mean=self.mean,
          m2=self.m2,
          m3=self.m3,
          m4=self.m4,
          min=self.min,
          max=self.max,
          zeros=self.zeros,
          nonfinite=nonfinite)
    n_a, n_b = self.n, other.n
    n = n_a + n_b
    delta = other.mean - self.mean
    mean = self.mean + delta * n_b / n
    m2 = self.m2 + other.m2 + delta**2 * n_a * n_b / n
    m3 = (
        self.m3 + other.m3 + delta**3 * n_a * n_b * (n_a - n_b) / n**2 +
        3.0 * delta * (n_a * other.m2 - n_b * self.m2) / n)
    m4 = (
        self.m4 + other.m4 + delta**4 * n_a * n_b *
        (n_a**2 - n_a * n_b + n_b**2) / n**3 + 6.0 * delta**2 *
        (n_a**2 * other.m2 + n_b**2 * self.m2) / n**2 + 4.0 * delta *
        (n_a * other.m3 - n_b * self.m3) / n)
    return Moments(
        n=n,
        mean=mean,
        m2=m2,
        m3=m3,
        m4=m4,
        min=min(self.min, other.min),
        max=max(self.max, other.max),
        zeros=self.zeros + other.zeros,
        nonfinite=nonfinite)

  @property
  def variance(self) -> float | None:
    """The population variance `M2 / n`. `None` when `n == 0` (variance is
    undefined for an empty column, not zero); `0.0` for a constant
    non-empty column.
    """
    return self.m2 / self.n if self.n > 0 else None

  @property
  def std(self) -> float | None:
    """`sqrt(variance)`. `None` when `n == 0`; `0.0` for a constant column."""
    var = self.variance
    return None if var is None else math.sqrt(var)

  @property
  def skewness(self) -> float | None:
    """The population (biased) skewness `g1`, matching `scipy.stats.skew`'s
    default `bias=True`. `None` below `n=2` or at zero variance (a constant
    column), where skewness is undefined rather than merely noisy.
    """
    if self.n < _MIN_HIGHER_MOMENT_N:
      return None
    var = self.variance
    if var is None or var == 0.0:
      return None
    return (self.m3 / self.n) / (var * math.sqrt(var))

  @property
  def kurtosis_excess(self) -> float | None:
    """The population excess kurtosis `g2 - 3`, matching
    `scipy.stats.kurtosis`'s default `fisher=True, bias=True`. `None` below
    `n=2` or at zero variance, for the same reason as `skewness`.
    """
    if self.n < _MIN_HIGHER_MOMENT_N:
      return None
    var = self.variance
    if var is None or var == 0.0:
      return None
    return (self.m4 / self.n) / (var**2) - 3.0

  def to_dict(self) -> dict[str, Any]:
    """A JSON-safe dict: `min`/`max` become `None` for an empty accumulator
    (their `+inf`/`-inf` sentinels are not valid JSON numbers).
    """
    return {
        "n": self.n,
        "mean": self.mean,
        "m2": self.m2,
        "m3": self.m3,
        "m4": self.m4,
        "min": None if self.n == 0 else self.min,
        "max": None if self.n == 0 else self.max,
        "zeros": self.zeros,
        "nonfinite": self.nonfinite,
    }

  @classmethod
  def from_dict(cls, data: Mapping[str, Any]) -> Moments:
    """The inverse of `to_dict`: a `None` `min`/`max` round-trips back to
    the `+inf`/`-inf` empty-accumulator sentinel. `nonfinite` defaults to
    `0` for a dict written before that field existed.
    """
    return cls(
        n=int(data["n"]),
        mean=float(data["mean"]),
        m2=float(data["m2"]),
        m3=float(data["m3"]),
        m4=float(data["m4"]),
        min=math.inf if data.get("min") is None else float(data["min"]),
        max=-math.inf if data.get("max") is None else float(data["max"]),
        zeros=int(data.get("zeros", 0)),
        nonfinite=int(data.get("nonfinite", 0)))
