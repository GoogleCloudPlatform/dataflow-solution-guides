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
"""Distances on count vectors: the pure maths behind `column.tvd`,
`column.jsd`, `column.psi` and `column.cohens_w`.

Every function here takes either raw counts or already-normalized
probabilities over the same fixed category/bin ordering — `normalize`
handles the conversion internally, so a caller never has to pre-divide by a
total — except `psi`, whose credit-risk pseudo-count smoothing is defined
directly on counts and would be meaningless applied twice. `align` builds
that shared ordering from two `Mapping[category, count]` sources over the
union of their keys, for callers (`packages/sdfb_evaluation/catalogue`'s
`column.tvd`/`column.jsd`/`column.cohens_w`) that start from two dicts
rather than two same-length arrays.

Design: docs/designs/2026-07-07-evaluation-framework-design.md

References:
  Lin, J. (1991), "Divergence Measures Based on the Shannon Entropy".
  Hellinger, E. (1909), "Neue Begruendung der Theorie quadratischer Formen
    von unendlichvielen Veraenderlichen". https://doi.org/10.1515/crll.1909.136.210
  Yurdakul, B., Naranjo, J. (2020), "Statistical Properties of Population
    Stability Index".
  Cohen, J. (1988), "Statistical Power Analysis for the Behavioral
    Sciences", 2nd ed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

import numpy as np


def _validate_same_length(p: np.ndarray, q: np.ndarray) -> None:
  """Raise `ValueError` unless `p` and `q` have the same length.

  Both must be counts or probabilities over the identical category/bin
  ordering to be comparable at all — a length mismatch is a caller bug
  (mirrors `stats.binned._validate_equal_counts`'s fail-loudly contract).
  """
  if p.shape != q.shape:
    raise ValueError(
        "p and q must be the same length (same category/bin ordering): "
        f"got shapes {p.shape} and {q.shape}")


def normalize(x: Sequence[float] | np.ndarray) -> np.ndarray | None:
  """`x` scaled to sum to 1, or `None` when `x` is empty or its sum is 0.

  Accepts either counts or already-normalized probabilities — dividing a
  probability vector by 1 is a no-op — so every distance below can take
  either without the caller pre-normalizing.
  """
  arr = np.asarray(x, dtype=float)
  if arr.size == 0:
    return None
  total = float(arr.sum())
  if total == 0.0:
    return None
  return arr / total


def align(
    src: Mapping[Any, float],
    syn: Mapping[Any, float],
) -> tuple[list[Any], np.ndarray, np.ndarray]:
  """`(keys, p, q)` over the union of `src`'s and `syn`'s keys, in a stable
  deterministic order.

  Keys are sorted by `(type(k).__name__, str(k))` rather than by `k` itself
  so a union of mixed key types (e.g. an `int` sentinel alongside `str`
  categories) never raises `TypeError` from an unorderable comparison, while
  staying reproducible run to run. Missing keys on either side fill in as
  0 — `p`/`q` are raw counts (or whatever scale `src`/`syn` used), not
  normalized; pass them to `normalize` or a distance function below, which
  normalizes internally.
  """
  keys = sorted(set(src) | set(syn), key=lambda k: (type(k).__name__, str(k)))
  p = np.array([float(src.get(k, 0)) for k in keys], dtype=float)
  q = np.array([float(syn.get(k, 0)) for k in keys], dtype=float)
  return keys, p, q


def tvd(p: Sequence[float], q: Sequence[float]) -> float | None:
  """Total variation distance `1 - sum_k min(p_k, q_k)` — the share of
  probability mass that would need to move to make `q` match `p`.

  `None` if either side is empty or has zero total mass; raises
  `ValueError` if `p` and `q` have different lengths.
  """
  p_arr = np.asarray(p, dtype=float)
  q_arr = np.asarray(q, dtype=float)
  _validate_same_length(p_arr, q_arr)
  p_n, q_n = normalize(p_arr), normalize(q_arr)
  if p_n is None or q_n is None:
    return None
  return float(1.0 - np.sum(np.minimum(p_n, q_n)))


def jsd_bits(p: Sequence[float], q: Sequence[float]) -> float | None:
  """Jensen-Shannon divergence in bits: `0.5 * sum(p * log2(p / m)) + 0.5 *
  sum(q * log2(q / m))`, `m = (p + q) / 2`, with no smoothing.

  Equal to `scipy.spatial.distance.jensenshannon(p, q, base=2) ** 2`. A
  category where `p_k` (or `q_k`) is 0 contributes 0 to its own sum by the
  standard `x * log(x) -> 0` convention (`m_k` is then at least half of the
  other side's mass, so no division by zero can occur); this is what lets a
  fully one-sided category still contribute exactly half its mass in bits,
  rather than being treated as undefined. `None` if either side is empty or
  has zero total mass; raises `ValueError` on a length mismatch.
  """
  p_arr = np.asarray(p, dtype=float)
  q_arr = np.asarray(q, dtype=float)
  _validate_same_length(p_arr, q_arr)
  p_n, q_n = normalize(p_arr), normalize(q_arr)
  if p_n is None or q_n is None:
    return None
  m = (p_n + q_n) / 2.0
  term_p = np.zeros_like(p_n)
  mask_p = p_n > 0
  term_p[mask_p] = p_n[mask_p] * np.log2(p_n[mask_p] / m[mask_p])
  term_q = np.zeros_like(q_n)
  mask_q = q_n > 0
  term_q[mask_q] = q_n[mask_q] * np.log2(q_n[mask_q] / m[mask_q])
  return float(0.5 * (term_p.sum() + term_q.sum()))


def hellinger(p: Sequence[float], q: Sequence[float]) -> float | None:
  """Hellinger distance `sqrt(0.5 * sum((sqrt(p) - sqrt(q)) ** 2))`, in
  `[0, 1]` (0 = identical, 1 = disjoint supports).

  `None` if either side is empty or has zero total mass; raises
  `ValueError` on a length mismatch.
  """
  p_arr = np.asarray(p, dtype=float)
  q_arr = np.asarray(q, dtype=float)
  _validate_same_length(p_arr, q_arr)
  p_n, q_n = normalize(p_arr), normalize(q_arr)
  if p_n is None or q_n is None:
    return None
  return float(np.sqrt(0.5 * np.sum((np.sqrt(p_n) - np.sqrt(q_n))**2)))


def psi(
    c_src: Sequence[float],
    c_syn: Sequence[float],
    pseudo: float = 0.5,
) -> float | None:
  """Population stability index over `B = len(c_src)` COUNT bins:
  `sum_b (q_b - p_b) * ln(q_b / p_b)`, with `+pseudo`-count smoothing on
  both sides: `p_b = (c_src[b] + pseudo) / (n_src + pseudo * B)` (`q_b`
  likewise from `c_syn`/`n_syn`).

  Unlike `tvd`/`jsd_bits`/`hellinger`, this takes COUNTS, not
  pre-normalized probabilities: the pseudo-count only means "half a row"
  relative to `n_src`/`n_syn`, so pre-normalizing first (making `n` add up
  to 1) would change what the smoothing means. Every bin gets a nonzero
  denominator even if `c_src[b]` or `c_syn[b]` is exactly 0 — including an
  open-ended tail bin that is empty on one side and holds an entire shifted
  mass on the other, which is exactly the drift PSI exists to catch (an
  out-of-range-mass regression can otherwise read as PSI near 0). `None` if
  either side is empty or has zero total count; raises `ValueError` on a
  length mismatch.
  """
  src_arr = np.asarray(c_src, dtype=float)
  syn_arr = np.asarray(c_syn, dtype=float)
  _validate_same_length(src_arr, syn_arr)
  if src_arr.size == 0:
    return None
  n_src, n_syn = float(src_arr.sum()), float(syn_arr.sum())
  if n_src <= 0.0 or n_syn <= 0.0:
    return None
  bins = src_arr.size
  p = (src_arr + pseudo) / (n_src + pseudo * bins)
  q = (syn_arr + pseudo) / (n_syn + pseudo * bins)
  return float(np.sum((q - p) * np.log(q / p)))


class CohensW(NamedTuple):
  """`cohens_w`'s result: the effect size `w`, plus the synthetic mass
  `cohens_w` had to exclude from it.
  """
  w: float
  q_mass_on_p0: float


def cohens_w(p: Sequence[float], q: Sequence[float]) -> CohensW | None:
  """Cohen's w effect size `sqrt(sum_{p_k > 0} (q_k - p_k) ** 2 / p_k)`,
  plus `q_mass_on_p0`: the synthetic mass sitting on categories where `p`
  has none (excluded from `w` itself, since `(q_k - 0) ** 2 / 0` is
  undefined — that mass is reported separately rather than dropped
  silently).

  `None` if either side is empty or has zero total mass; raises
  `ValueError` on a length mismatch.
  """
  p_arr = np.asarray(p, dtype=float)
  q_arr = np.asarray(q, dtype=float)
  _validate_same_length(p_arr, q_arr)
  p_n, q_n = normalize(p_arr), normalize(q_arr)
  if p_n is None or q_n is None:
    return None
  mask = p_n > 0
  w = float(np.sqrt(np.sum((q_n[mask] - p_n[mask])**2 / p_n[mask])))
  q_mass_on_p0 = float(np.sum(q_n[~mask]))
  return CohensW(w=w, q_mass_on_p0=q_mass_on_p0)
