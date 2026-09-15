"""Local fidelity primitives for the B.1 RAG engine.

These implement the spine's "fidelity by construction" guarantees (ADR
0013): constants are copied, numerics are clipped to the observed range,
categoricals are sampled at their empirical frequency, and free-text is
drawn from a bounded pool. Sampling is seeded → reproducible.

Kept LOCAL to `b1_rag/` per the spec (§2): "Kept local per engine during
parallel development; consolidate to a shared `engines/_fidelity.py`
post-merge if duplication warrants" — avoids a shared-file merge conflict
with the B.2 worktree.

**Sampling backend seam.** `NumPy` is the default when installed (vectorized,
the path the reproducibility tests pin). It is *deferred-imported*; when
absent the engine falls back to a pure-Python `random.Random` sampler that
produces the same *kind* of draws (seeded, in-range). The two backends are
not bit-identical to each other, but each is internally deterministic for a
fixed seed — which is what `test_seed_reproducibility` requires.

REF: spec §2 sampling-backend seam; cuDF/CuPy is the M1-optional GPU backend
(not implemented here — NumPy is the baseline).
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, TypeGuard, cast

from sdfb_core.engines.b1_rag.profile import (
    ColumnKind,
    ColumnProfile,
    temporal_from_float,
    temporal_string_from_float,
    temporal_string_to_float,
    temporal_to_float,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
  from collections.abc import Sequence


def numpy_available() -> bool:
  """True if NumPy can be imported (selects the vectorized backend)."""
  try:
    import numpy  # noqa: F401  # pylint: disable=unused-import

    return True
  except ImportError:
    return False


class ColumnSampler:
  """Samples one column's values for a batch, honoring its profile.

    Marginal fidelity is not a dial (2026-08-11 R1 pair — 22 numeric
    columns at decile-KS 0.40-0.90, ~25 categoricals with entropy gaps up
    to -0.99, all at the default `similarity=0.5`):
      - NUMERIC: inverse transform sampling through the full sorted
        observed sample (Devroye 1986 ch. II; the B.2/ADR 0022 primitive at
        sample resolution). `similarity` does not shape numeric draws.
      - CATEGORICAL: empirical frequencies exactly (ADR 0013's original
        contract); sparsity categories keep their exact mass. `similarity`
        does not flatten categoricals.
      - TEMPORAL: anchored/uniform blend within the (clamped) observed
        range — owned by the interim temporal-age policy, deliberately NOT
        distribution-following yet.
      - FREE_TEXT: handled by the engine via the LLM pool; this sampler only
        provides the null mask and a fallback draw from observed examples.
    `similarity` remains the retrieval-tightness / LLM-temperature dial.
    """

  def __init__(self, profile: ColumnProfile) -> None:
    self.profile = profile
    # Derived views of the (frozen) profile. Built on first use and kept
    # for the sampler's lifetime. The 2026-07-26 1M E2E rebuilt these on
    # EVERY generate_batch() call: 42 INT64 columns x ~10k observed
    # values x 62,500 elements ~= 3,900 CPU-seconds. `_temporal_floats`
    # was already memoized, but the np.asarray() around it was not.
    self._temporal_floats: list[float] | None = None
    self._numeric_array = None  # np.ndarray | None (SORTED ascending)
    self._numeric_sorted: list[float] | None = None
    self._temporal_array = None  # np.ndarray | None

  def _temporal_obs_floats(self) -> list[float]:
    """Observed TEMPORAL values on the float axis (computed once).

        Filtered to the profile's [numeric_min, numeric_max]: sentinel
        values (excluded from the range by the profiler) and pre-clamp
        history must not survive as blend anchors — a year-1 anchor is how
        the 2026-07-23 E2E landed "72-08-01" dates.
        """
    if self._temporal_floats is None:
      fmt = self.profile.temporal_format
      if fmt is not None:  # date-shaped STRING column
        floats = [
            temporal_string_to_float(str(v), fmt)
            for v in self.profile.observed_values
        ]
      else:
        floats = [
            f for f in (temporal_to_float(v)
                        for v in self.profile.observed_values) if f is not None
        ]
      lo, hi = self.profile.numeric_min, self.profile.numeric_max
      if lo is not None and hi is not None:
        floats = [f for f in floats if lo <= f <= hi]
      self._temporal_floats = floats
    return self._temporal_floats

  def _numeric_obs_array(self, np):
    """Observed NUMERIC values as a SORTED float64 array (built once) —
        the empirical quantile vector the inverse-CDF draw interpolates.

        The per-call rebuild of this list comprehension was the dominant
        cost of the generation stage on the 2026-07-26 1M run.
        """
    if self._numeric_array is None:
      self._numeric_array = np.sort(
          np.asarray(
              [float(x) for x in self.profile.observed_values if _is_number(x)],
              dtype="float64",
          ))
    return self._numeric_array

  def _numeric_obs_sorted(self) -> list[float]:
    """Pure-Python mirror of `_numeric_obs_array` (built once)."""
    if self._numeric_sorted is None:
      self._numeric_sorted = sorted(
          float(x) for x in self.profile.observed_values if _is_number(x))
    return self._numeric_sorted

  def _temporal_obs_array(self, np):
    """Observed TEMPORAL values as a float64 array (built once)."""
    if self._temporal_array is None:
      self._temporal_array = np.asarray(
          self._temporal_obs_floats(), dtype="float64")
    return self._temporal_array

  def _render_temporal(self, base: list[float]) -> list:
    p = self.profile
    if p.temporal_format is not None:
      return [temporal_string_from_float(v, p.temporal_format) for v in base]
    return [temporal_from_float(v, p.bq_type) for v in base]

  def _inject_temporal_sentinels(self, values: list, rand_many) -> list:
    """Overwrite jittered values with the profile's sentinel values at
        their observed fractions. ``rand_many(k)`` returns k uniforms in
        [0, 1) — only called when sentinels exist, so sentinel-free columns
        consume no extra RNG stream (seeded reproducibility unchanged)."""
    sentinels = self.profile.temporal_sentinels
    if not sentinels:
      return values
    draws = rand_many(len(values))
    out = list(values)
    for i, r in enumerate(draws):
      acc = 0.0
      for sentinel_value, fraction in sentinels:
        acc += fraction
        if r < acc:
          out[i] = sentinel_value
          break
    return out

  # -- NumPy (vectorized) backend ----------------------------------------

  def sample_numpy(self, rng, n: int, similarity: float) -> list:
    import numpy as np

    p = self.profile
    if p.kind is ColumnKind.CONSTANT:
      return [p.constant_value] * n

    values = self._draw_numpy(np, rng, n, similarity)
    return self._apply_nulls_numpy(np, rng, values, n)

  def _draw_numpy(self, np, rng, n: int, similarity: float) -> list:
    p = self.profile
    if p.kind is ColumnKind.NUMERIC:
      return self._numeric_numpy(np, rng, n, similarity)
    if p.kind is ColumnKind.TEMPORAL:
      return self._temporal_numpy(np, rng, n, similarity)
    if p.kind is ColumnKind.CATEGORICAL:
      return self._categorical_numpy(np, rng, n, similarity)
    # FREE_TEXT fallback (engine normally patches these via the LLM pool).
    return self._from_pool_numpy(np, rng, p.text_examples or p.observed_values,
                                 n)

  def _blend_floats_numpy(self, rng, obs, lo: float, hi: float, n: int,
                          similarity: float) -> list:
    """Anchored/uniform blend of n floats within [lo, hi].

        similarity→1: sample observed values + small jitter (tight).
        similarity→0: uniform over [lo, hi] (wide). Out-of-range blends are
        REDRAWN uniformly inside the range, not clipped — clipping stacked
        ~5 % of draws exactly on the observed min/max (2026-07-15 E2E:
        single-value spikes at the bounds on COL_007/009/016/047/057).
        """
    if hi <= lo:
      return [lo] * n
    if not obs.size:
      return cast("list", rng.uniform(lo, hi, size=n).tolist())
    idx = rng.integers(0, obs.size, size=n)
    anchored = obs[idx]
    spread = hi - lo
    jitter = rng.uniform(-0.5, 0.5, size=n) * spread * (1.0 - similarity)
    uniform = rng.uniform(lo, hi, size=n)
    blended = similarity * (anchored + jitter) + (1.0 - similarity) * uniform
    oob = (blended < lo) | (blended > hi)
    n_oob = int(oob.sum())
    if n_oob:
      blended[oob] = rng.uniform(lo, hi, size=n_oob)
    return cast("list", blended.tolist())

  def _numeric_numpy(self, np, rng, n: int, similarity: float) -> list:  # pylint: disable=unused-argument
    """Inverse transform sampling through the sorted observed sample.

        The anchored+uniform VALUE-AVERAGE this replaces was a convolution
        that reproduced neither shape (2026-08-11 R1: 22 numeric columns at
        decile-KS 0.40-0.90) and let one in-range outlier hand uniform mass
        to the whole span (COL_009's 40xx-prefix band broke mid-range).
        Interpolating between consecutive order statistics keeps every draw
        in-range and novel-by-interpolation; `similarity` is unused here.
        """
    p = self.profile
    obs = self._numeric_obs_array(np)
    if not obs.size:
      lo = float(p.numeric_min if p.numeric_min is not None else 0.0)
      hi = float(p.numeric_max if p.numeric_max is not None else 0.0)
      base = ([lo] * n if hi <= lo else rng.uniform(lo, hi, size=n).tolist())
      return [self._coerce_numeric(v) for v in base]
    if obs.size == 1:
      base = [float(obs[0])] * n
    else:
      grid = np.linspace(0.0, 1.0, obs.size)
      base = np.interp(rng.random(n), grid, obs).tolist()
    return [self._coerce_numeric(v) for v in base]

  def _temporal_numpy(self, np, rng, n: int, similarity: float) -> list:
    p = self.profile
    lo = float(p.numeric_min if p.numeric_min is not None else 0.0)
    hi = float(p.numeric_max if p.numeric_max is not None else 0.0)
    obs = self._temporal_obs_array(np)
    base = self._blend_floats_numpy(rng, obs, lo, hi, n, similarity)
    return self._inject_temporal_sentinels(
        self._render_temporal(base), rng.random)

  @staticmethod
  def _categorical_masses(categories: dict) -> tuple[list, list[float]]:
    """(categories, probabilities) at exact empirical frequencies.

        Sparsity categories were already pinned (2026-08-09 B_TABLE R1,
        COL_033-class empty-parity Δ0.28); the 2026-08-11 R1 pair then
        measured the *substantive* similarity blend flattening every
        skewed enum toward uniform at the default 0.5 (~25 categorical
        columns, entropy gaps -0.2..-0.99 — A_TABLE COL_004-class: source
        ~all EUR, synthetic near-uniform over 37 currencies). Categorical
        marginals now follow the frequency table exactly, ADR 0013's
        original contract; `similarity` stays an LLM/retrieval dial.
        """
    cats = list(categories.keys())
    counts = [float(categories[c]) for c in cats]
    total = sum(counts)
    if not total:
      return cats, [0.0 for _ in cats]
    return cats, [cnt / total for cnt in counts]

  def _categorical_numpy(self, np, rng, n: int, similarity: float) -> list:  # pylint: disable=unused-argument
    p = self.profile
    if not p.categories:
      return [None] * n
    cats, probs = self._categorical_masses(p.categories)
    idx = rng.choice(len(cats), size=n, p=np.asarray(probs))
    return [cats[int(i)] for i in idx]

  def _from_pool_numpy(self, np, rng, pool: Sequence, n: int) -> list:  # pylint: disable=unused-argument
    pool = list(pool)
    if not pool:
      return [None] * n
    idx = rng.integers(0, len(pool), size=n)
    return [pool[int(i)] for i in idx]

  def _apply_nulls_numpy(self, np, rng, values: list, n: int) -> list:  # pylint: disable=unused-argument
    p = self.profile
    if not p.nullable or p.null_fraction <= 0.0:
      return values
    mask = rng.random(n) < p.null_fraction
    return [None if mask[i] else values[i] for i in range(n)]

  # -- pure-Python backend (no NumPy) ------------------------------------

  def sample_python(self, rng, n: int, similarity: float) -> list:
    p = self.profile
    if p.kind is ColumnKind.CONSTANT:
      return [p.constant_value] * n
    values = self._draw_python(rng, n, similarity)
    return self._apply_nulls_python(rng, values, n)

  def _draw_python(self, rng, n: int, similarity: float) -> list:
    p = self.profile
    if p.kind is ColumnKind.NUMERIC:
      return self._numeric_python(rng, n, similarity)
    if p.kind is ColumnKind.TEMPORAL:
      return self._temporal_python(rng, n, similarity)
    if p.kind is ColumnKind.CATEGORICAL:
      return self._categorical_python(rng, n, similarity)
    return self._from_pool_python(rng, p.text_examples or p.observed_values, n)

  def _blend_floats_python(self, rng, obs: list[float], lo: float, hi: float,
                           n: int, similarity: float) -> list[float]:
    """Pure-Python mirror of `_blend_floats_numpy` (same redraw-not-clip
        semantics; not bit-identical to the NumPy backend, but seeded)."""
    out: list[float] = []
    spread = hi - lo
    for _ in range(n):
      if hi <= lo:
        v = lo
      elif obs:
        anchored = rng.choice(obs)
        jitter = (rng.random() - 0.5) * spread * (1.0 - similarity)
        uniform = rng.uniform(lo, hi)
        v = similarity * (anchored + jitter) + (1.0 - similarity) * uniform
        if v < lo or v > hi:
          v = rng.uniform(lo, hi)
      else:
        v = rng.uniform(lo, hi)
      out.append(v)
    return out

  def _numeric_python(self, rng, n: int, similarity: float) -> list:  # pylint: disable=unused-argument
    """Pure-Python mirror of `_numeric_numpy` (same inverse-CDF
        semantics; seeded, not bit-identical across backends)."""
    p = self.profile
    obs = self._numeric_obs_sorted()
    if not obs:
      lo = float(p.numeric_min if p.numeric_min is not None else 0.0)
      hi = float(p.numeric_max if p.numeric_max is not None else 0.0)
      base = [lo if hi <= lo else rng.uniform(lo, hi) for _ in range(n)]
      return [self._coerce_numeric(v) for v in base]
    m = len(obs)
    base = []
    for _ in range(n):
      if m == 1:
        base.append(obs[0])
        continue
      idx = rng.random() * (m - 1)
      i = int(idx)
      frac = idx - i
      base.append(obs[i] + (obs[i + 1] - obs[i]) * frac)
    return [self._coerce_numeric(v) for v in base]

  def _temporal_python(self, rng, n: int, similarity: float) -> list:
    p = self.profile
    lo = float(p.numeric_min if p.numeric_min is not None else 0.0)
    hi = float(p.numeric_max if p.numeric_max is not None else 0.0)
    base = self._blend_floats_python(rng, self._temporal_obs_floats(), lo, hi,
                                     n, similarity)
    return self._inject_temporal_sentinels(
        self._render_temporal(base),
        lambda k: [rng.random() for _ in range(k)],
    )

  def _categorical_python(self, rng, n: int, similarity: float) -> list:  # pylint: disable=unused-argument
    p = self.profile
    if not p.categories:
      return [None] * n
    cats, probs = self._categorical_masses(p.categories)
    return cast("list", rng.choices(cats, weights=probs, k=n))

  def _from_pool_python(self, rng, pool: Sequence, n: int) -> list:
    pool = list(pool)
    if not pool:
      return [None] * n
    return [pool[rng.randrange(len(pool))] for _ in range(n)]

  def _apply_nulls_python(self, rng, values: list, n: int) -> list:
    p = self.profile
    if not p.nullable or p.null_fraction <= 0.0:
      return values
    return [
        None if rng.random() < p.null_fraction else values[i] for i in range(n)
    ]

  # -- shared --------------------------------------------------------------

  def _coerce_numeric(self, v: float):
    p = self.profile
    if p.is_integral:
      return round(v)
    if p.is_decimal:
      if _isnan(v):
        return None
      # Quantize to the column's DDL scale so the value satisfies the
      # derived model's `decimal_places` constraint exactly.
      scale = p.decimal_scale if p.decimal_scale is not None else 2
      quantum = Decimal(1).scaleb(-scale)  # e.g. scale=2 -> Decimal("0.01")
      return Decimal(str(v)).quantize(quantum)
    return float(v)


def _is_number(x: object) -> TypeGuard[int | float | Decimal | str]:
  if isinstance(x, bool):
    return False
  if isinstance(x, (int, float, Decimal)):
    return True
  if isinstance(x, str):
    try:
      Decimal(x)
      return True
    except Exception:  # pylint: disable=broad-exception-caught
      return False
  return False


def _isnan(v: float) -> bool:
  return v != v  # noqa: PLR0124 — NaN check (NaN != NaN); robust for any value type


__all__ = ["ColumnSampler", "numpy_available"]
