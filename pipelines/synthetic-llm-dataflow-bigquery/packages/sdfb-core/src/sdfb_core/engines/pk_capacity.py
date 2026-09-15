"""PK tuple capacity under random draws, and the FK key-sample cap it
implies (ADR 0035).

A PK member that is an enforced FK column draws from the parent key
pool; a categorical member draws from its observed domain. Neither draw
rejects collisions, so a PK tuple built from such members is a
balls-into-bins process: ``N`` draws into ``K`` tuples land
``K (1 - e^{-N/K})`` distinct ones and the rest divert as
``pk.duplicate`` at the uniqueness barrier. ``K >= N`` is therefore NOT
enough (``K == N`` still loses 36.8%); the child needs ``K`` a healthy
multiple of ``N``.

For a child whose PK contains an FK, ``K`` is (parent key tuples the
child can see) x (the other members' capacity). The side-input cap on
parent keys — a flat 100k since ADR 0030 — is the lever: this module
sizes it from ``num_rows`` and the sibling members, between a floor
(the historic cap) and a ceiling (side-input memory).

Pure Python; no Beam, no GCP.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

__all__ = [
    "FK_KEY_SAMPLE_CEILING",
    "FK_KEY_SAMPLE_FLOOR",
    "FK_KEY_SAMPLE_MARGIN",
    "effective_cells",
    "expected_duplicate_share",
    "expected_duplicate_share_cells",
    "fk_key_sample_cap",
    "max_rows_under_share",
]

# The ADR 0030 side-input cap: a child samples at most this many parent
# key tuples unless its PK needs more. Mirrored by `io/fk_pools._DEFAULT_LIMIT`.
FK_KEY_SAMPLE_FLOOR = 100_000
# Largest side input this sizing will ask for. 1M single-column string
# keys is ~40 MB pickled / ~130 MB live per SDK process; beyond it the
# scaling answer is the co-partitioned join (ADR 0031 escape hatch).
FK_KEY_SAMPLE_CEILING = 1_000_000
# Capacity / num_rows target: at 10x, expected duplicates are ~4.8%.
FK_KEY_SAMPLE_MARGIN = 10


def expected_duplicate_share(num_rows: int, capacity: int | None) -> float:
  """Share of ``num_rows`` random draws into ``capacity`` slots that
    repeat an earlier draw: ``1 - K/N (1 - e^{-N/K})``. Unbounded
    capacity (``None``) or no rows means no duplicates."""
  if capacity is None or num_rows <= 0:
    return 0.0
  if capacity <= 0:
    return 1.0
  ratio = num_rows / capacity
  # -expm1(-x), never 1 - exp(-x): for x ~ 1e-20 (a pattern sampler's
  # 1e27 strings) the subtraction cancels to 0.0 and the share to 100 %
  # (2026-09-11 launch …-3742133137251240056, a false P4 stop).
  distinct = capacity * -math.expm1(-ratio)
  return max(0.0, 1.0 - distinct / num_rows)


def _normalised(cell_weights: Sequence[float]) -> list[float]:
  total = float(sum(cell_weights))
  if total <= 0:
    raise ValueError("cell weights must sum to a positive number")
  return [w / total for w in cell_weights]


def expected_duplicate_share_cells(num_rows: int, uniform_capacity: int | None,
                                   cell_weights: Sequence[float]) -> float:
  """Duplicate share when the PK tuple is (a uniform draw over
    ``uniform_capacity`` slots) x (a categorical cell drawn with
    ``cell_weights``). Collisions happen inside a cell, so the heavy
    cells saturate first: ``E[distinct] = sum_c U (1 - e^{-N p_c / U})``.
    Uniform weights reduce to `expected_duplicate_share` over
    ``U x cells``; skew always loses more (the 2026-09-09_16_44 run:
    56.5% measured against 32% from the uniform model)."""
  if uniform_capacity is None or num_rows <= 0:
    return 0.0
  if uniform_capacity <= 0:
    return 1.0
  distinct = sum(uniform_capacity *
                 -math.expm1(-num_rows * p / uniform_capacity)
                 for p in _normalised(cell_weights))
  return max(0.0, 1.0 - distinct / num_rows)


def effective_cells(cell_weights: Sequence[float]) -> float:
  """Inverse Simpson index ``1 / sum p_c^2`` — the number of EQUALLY
    likely cells that would collide as often as these skewed ones; the
    factor the FK key-sample sizing must use instead of the distinct
    count."""
  return 1.0 / sum(p * p for p in _normalised(cell_weights))


def fk_key_sample_cap(num_rows: int, other_capacity: float | None) -> int:
  """Parent key tuples a child must see so that
    ``keys x other_capacity >= MARGIN x num_rows``, clamped to
    ``[FLOOR, CEILING]``. ``other_capacity`` is the product of the PK's
    non-FK members' capacities; ``None`` (an unbounded member covers the
    tuple) keeps the floor."""
  if other_capacity is None or num_rows <= 0:
    return FK_KEY_SAMPLE_FLOOR
  needed = math.ceil(FK_KEY_SAMPLE_MARGIN * num_rows / max(1, other_capacity))
  return max(FK_KEY_SAMPLE_FLOOR, min(FK_KEY_SAMPLE_CEILING, needed))


def max_rows_under_share(
    capacity: int | None,
    share: float,
    cell_weights: Sequence[float] | None = None,
) -> int | None:
  """Largest ``num_rows`` whose expected duplicate share stays at or
    under ``share``; ``capacity`` is the whole tuple space, or the
    uniform part when ``cell_weights`` is given. ``None`` when nothing
    limits (unbounded capacity, or a share of 1.0)."""
  if capacity is None or share >= 1.0:
    return None
  if share <= 0.0:
    return 0

  def _share(n: int) -> float:
    if cell_weights is None:
      return expected_duplicate_share(n, capacity)
    return expected_duplicate_share_cells(n, capacity, cell_weights)

  cells = len(cell_weights) if cell_weights else 1
  lo, hi = 0, max(1, capacity * cells * FK_KEY_SAMPLE_MARGIN * 10)
  while _share(hi) <= share:
    hi *= 2
  while lo < hi:
    mid = (lo + hi + 1) // 2
    if _share(mid) <= share:
      lo = mid
    else:
      hi = mid - 1
  return lo
