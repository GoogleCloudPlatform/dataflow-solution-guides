"""Joint FK key pools — referential integrity by construction (ADR 0031).

A child table's FK columns must land a tuple its parent actually holds.
v1 (ADR 0021/0030) shipped per-column pools and drew each FK column
INDEPENDENTLY: for a k-column edge that lands a real parent key with
probability ``|parent keys| / prod_c(distinct_c)`` — 2.7% on the
2026-08-23 run's measured cardinalities. The tuple is the unit of
referential integrity, so the tuple is the unit of the draw.

Drawing uniformly over the parent's distinct keys would fix integrity and
break the child's marginals (a parent key held by 30% of the child's rows
would become 1/|keys| of them). Nor is the naive product of the child's
per-column shares enough: restricting it to the parent's key set and
renormalizing DISTORTS those shares, because a value the parent holds in
many key tuples collects mass from all of them (the 2-column worked case
in the design doc: a 70% child share lands at 84%).

So the weights are fitted, not assumed. Iterative proportional fitting
(Deming & Stephan 1940) rakes a weight vector over the parent's key
tuples until every column's induced marginal matches the child's own
observed marginal — the I-projection of the child's distribution onto the
parent's key set. Marginals preserved, orphans impossible.

Per-column targets carry a Good-Turing floor (Good 1953, "The population
frequencies of species…") so parent values the child sample never saw
stay reachable instead of being silently dropped from the synthetic
domain.

NULL FK tuples are legitimate ("this child has no parent", SQL MATCH
SIMPLE) and are preserved at the child's observed rate rather than
forced to a parent — v1 collapses partial-NULL patterns to all-NULL,
which is the dominant real shape.
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import accumulate
from typing import Any

from sdfb_core.observability import log_milestone

__all__ = [
    "FkKeyPool",
    "bind_fk_key_pools",
    "build_fk_key_pools",
    "per_column_pools",
]


def _column_target(values: set, observed: Counter,
                   total: int) -> dict[Any, float]:
  """Child mass per parent value, with the Good-Turing unseen floor.

    ``p0`` (the singleton rate) is the Turing estimate of the mass the
    child sample would give to values it has not seen; it is spread over
    exactly the parent values that are missing from the sample. Floored
    above zero so a sample with no singletons still reaches them. The
    result is renormalized over the parent's value set — child values the
    parent does not hold cannot be generated, so their mass is not ours
    to keep.
    """
  singletons = sum(1 for c in observed.values() if c == 1)
  p0 = max(singletons, 1) / (total + 1)
  unseen = [v for v in values if v not in observed]
  per_unseen = p0 / len(unseen) if unseen else 0.0
  seen_scale = (1.0 - p0) / total if total else 0.0
  raw = {
      v: (observed[v] * seen_scale if v in observed else per_unseen)
      for v in values
  }
  mass = sum(raw.values())
  return {v: w / mass for v, w in raw.items()} if mass else raw


def _fit_weights(
    keys: tuple[tuple, ...],
    targets: list[dict[Any, float]],
    sweeps: int = 16,
    tol: float = 1e-3,
) -> list[float]:
  """Iterative proportional fitting: rake key weights until every
    column's induced marginal matches its target (Deming & Stephan 1940).

    Converges in a handful of sweeps; each sweep is O(|keys| x columns),
    and it runs ONCE per engine setup, never per row.
    """
  groups: list[dict[Any, list[int]]] = [{} for _ in targets]
  for j, key in enumerate(keys):
    for i, by_value in enumerate(groups):
      by_value.setdefault(key[i], []).append(j)
  weights = [1.0 / len(keys)] * len(keys)
  for _ in range(sweeps):
    drift = 0.0
    for i, by_value in enumerate(groups):
      for value, rows in by_value.items():
        current = sum(weights[j] for j in rows)
        if current <= 0.0:
          continue
        scale = targets[i].get(value, 0.0) / current
        drift = max(drift, abs(scale - 1.0))
        for j in rows:
          weights[j] *= scale
    if drift < tol:
      break
  return weights


@dataclass(frozen=True)
class FkKeyPool:
  """One enforced FK edge's parent keys, ready to draw from.

    ``cum`` is the normalized cumulative weight over ``keys`` — a draw is
    one uniform variate plus a binary search, so per-row cost is
    O(log |keys|) and independent of the child's row count.
    """

  cols: tuple[str, ...]
  keys: tuple[tuple, ...]
  cum: tuple[float, ...]
  null_fraction: float = 0.0
  weighting: str = "child_marginal"
  # Lazily-built numpy view of `cum`. Rebuilding it per batch cost
  # 17 ms on a 100k-key pool — 17 s over a 1M-row run, for a value
  # that never changes.
  _cum_np: Any = field(default=None, compare=False, repr=False)

  @classmethod
  def from_reference(
      cls,
      cols: Sequence[str],
      keys: Sequence[Sequence],
      reference_rows: Sequence[dict] = (),
  ) -> FkKeyPool:
    cols = tuple(cols)
    # A side input's element order is not stable across runs; sorting
    # by repr is total (mixed types never raise) and makes a seeded
    # draw reproducible from the key SET alone.
    ordered = tuple(sorted((tuple(k) for k in keys), key=repr))
    if not ordered:
      raise ValueError(
          f"empty parent key pool for FK columns {list(cols)} — the "
          f"parent landed no rows; refusing to generate the child "
          f"from marginals (ADR 0031).")
    rows = list(reference_rows or ())
    complete = [
        tuple(r.get(c) for c in cols) for r in rows if all(
            r.get(c) is not None for c in cols)
    ]
    null_fraction = (len(rows) - len(complete)) / len(rows) if rows else 0.0

    weights = [1.0] * len(ordered)
    weighting = "uniform"
    targets: list[dict[Any, float]] = []
    overlap = False
    for i in range(len(cols)):
      values = {k[i] for k in ordered}
      observed = Counter(t[i] for t in complete)
      overlap = overlap or any(v in values for v in observed)
      targets.append(_column_target(values, observed, len(complete)))
    if complete and overlap:
      weights = _fit_weights(ordered, targets)
      weighting = "child_marginal"
    total = sum(weights)
    cum = tuple(w / total for w in accumulate(weights))
    return cls(
        cols=cols,
        keys=ordered,
        cum=cum,
        null_fraction=null_fraction,
        weighting=weighting,
    )

  def draw(self, n: int, rng, use_numpy: bool = False) -> list[tuple]:
    """``n`` parent key tuples; a NULL row is a tuple of ``None``."""
    if n <= 0:
      return []
    null_tuple = (None,) * len(self.cols)
    if use_numpy:
      import numpy as np

      cum = self._cum_np
      if cum is None:
        cum = np.asarray(self.cum)
        object.__setattr__(self, "_cum_np", cum)
      idx = np.searchsorted(cum, rng.random(n))
      idx = np.clip(idx, 0, len(self.keys) - 1)
      drawn = [self.keys[i] for i in idx.tolist()]
      if self.null_fraction > 0.0:
        nulls = rng.random(n) < self.null_fraction
        drawn = [
            null_tuple if is_null else t
            for t, is_null in zip(drawn, nulls.tolist(), strict=True)
        ]
      return drawn
    last = len(self.keys) - 1
    return [
        null_tuple if self.null_fraction > 0.0 and
        rng.random() < self.null_fraction else self.keys[min(
            bisect_right(self.cum, rng.random()), last)] for _ in range(n)
    ]


def build_fk_key_pools(
    payloads: Sequence[dict], reference_rows: Sequence[dict] = ()
) -> list[FkKeyPool]:
  """``[{"cols": [...], "keys": [[...], ...]}, …]`` → drawable pools.

    The payload shape is what crosses the Beam seam (side input for an
    in-job parent, driver-side BQ read for an already-landed one).
    """
  return [
      FkKeyPool.from_reference(
          cols=p["cols"],
          keys=p.get("keys") or (),
          reference_rows=reference_rows) for p in payloads if p.get("cols")
  ]


def bind_fk_key_pools(ctx) -> list[FkKeyPool]:
  """Every enforced edge this run must honor, drawable, from a context.

    Both engines call this in ``setup()`` so referential integrity has
    ONE implementation. A context carrying only the per-column
    ``fk_pools`` (single-column edges from the driver-side loader) binds
    each column as its own 1-tuple edge — same machinery, same weighting.
    """
  payloads = list(getattr(ctx, "fk_key_pools", []) or [])
  covered = {c for p in payloads for c in (p.get("cols") or ())}
  for col, values in (getattr(ctx, "fk_pools", {}) or {}).items():
    if col not in covered and values:
      payloads.append({"cols": [col], "keys": [(v,) for v in values]})
  pools = build_fk_key_pools(payloads, getattr(ctx, "reference_rows", ()))
  for pool in pools:
    log_milestone(
        "fk_key_pool_bound",
        columns=",".join(pool.cols),
        key_tuples=len(pool.keys),
        weighting=pool.weighting,
        null_fraction=round(pool.null_fraction, 4),
    )
  return pools


def per_column_pools(pools: Sequence[FkKeyPool]) -> dict[str, tuple]:
  """Per-column projections of the joint keys — display/metadata only
    (`generation_plan`, `relational_e2e`). Sampling truth is the tuple."""
  out: dict[str, tuple] = {}
  for pool in pools:
    for i, col in enumerate(pool.cols):
      out[col] = tuple(dict.fromkeys(k[i] for k in pool.keys))
  return out
