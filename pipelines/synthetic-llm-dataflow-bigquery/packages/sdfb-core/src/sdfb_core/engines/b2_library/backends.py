"""Sampling-backend seam for the B.2 library engine (design §2).

Two backends sit behind one interface:

* :class:`EmpiricalBackend` — pure NumPy. Fits per-column empirical
  distributions and samples vectorized on CPU. Deterministic given a seed.
  This is the laptop / DirectRunner default and the offline fallback, and
  it is what the 5 ``GenerationEngine`` ABC contract tests pin (the spec
  fixes the NumPy backend for reproducibility — CuPy/CTGAN RNG is not
  bit-for-bit reproducible across machines).

* :class:`SdgxBackend` — wraps ``sdgx`` (hitsz-ids, Apache-2.0, CTGAN
  family). The ``sdgx`` import is **deferred** to ``fit()`` because it is
  heavy (pulls torch); importing this module must succeed with only
  ``sdfb-core``'s base deps. Used in production on the M4 / GPU workers.
  Falls back to :class:`EmpiricalBackend` if ``sdgx`` is not importable so
  the engine never hard-fails on the laptop.

Both fitted backends pickle across the Beam worker boundary: the engine is
built once in ``DoFn.setup()`` and must survive serialization. NumPy state
is plain data; ``sdgx``'s ``Synthesizer`` exposes ``save``/``load`` and the
underlying torch model is picklable.

Free-text columns are **excluded** from both backends — they are produced
by the ``ModelClient`` free-text hook (``freetext.py``), not sampled here.
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, cast

import numpy as np

from sdfb_core.engines.b2_library.fidelity import ColumnKind, ColumnProfile
from sdfb_core.engines.b2_library.temporal import sample_temporal
from sdfb_core.observability import log_milestone

if TYPE_CHECKING:
  import pandas as pd

# Below this temperature, categorical sampling collapses to the modal value
# (maximal mimicry) instead of softmax-reweighting — avoids divide-by-tiny.
_TEMP_EPSILON = 1e-9
# An inverse-CDF needs at least two quantile points to interpolate between.
_MIN_QUANTILE_POINTS = 2
# Smoothing added to weights before the log in temperature reweighting.
_LOG_SMOOTHING = 1e-12


class SamplingBackend(Protocol):
  """What the engine needs from a fitted distribution model.

    ``sample_columns(n, rng)`` returns one Python ``list`` per non-free-text
    column name → length-``n`` list of sampled values. Free-text columns are
    omitted (the LLM hook fills them). Seeded via the passed ``np.random``
    generator for reproducibility.
    """

  def fit(self, reference_rows: list[dict],
          profiles: dict[str, ColumnProfile]) -> None:
    ...

  def sample_columns(
      self,
      n: int,
      rng: np.random.Generator,
      *,
      temperature: float = 1.0,
  ) -> dict[str, list]:
    """Sample ``n`` values for every non-free-text column."""


def _samplable_profiles(
    profiles: dict[str, ColumnProfile],) -> dict[str, ColumnProfile]:
  """Profiles the backend is responsible for (everything but free-text)."""
  return {
      name: p
      for name, p in profiles.items()
      if p.kind is not ColumnKind.FREE_TEXT
  }


class EmpiricalBackend:
  """Pure-NumPy empirical sampler. Deterministic. The contract-test backend.

    Per the FASTGEN spine: constants are copied, numerics sampled uniformly
    within the observed ``[min, max]`` (a deliberately conservative,
    fidelity-by-construction choice that never escapes the support),
    categoricals drawn from the empirical frequency table. ``temperature``
    (from ``cfg.similarity``) widens categorical sampling toward uniform as
    it rises; at ``temperature == 0`` categorical draws collapse to the
    modal value (maximal mimicry).
    """

  def __init__(self) -> None:
    self._profiles: dict[str, ColumnProfile] = {}

  def fit(
      self,
      reference_rows: list[dict],  # pylint: disable=unused-argument
      profiles: dict[str, ColumnProfile]) -> None:
    # Profiling already happened in the engine; the empirical backend is
    # stateless beyond the profiles it samples from.
    self._profiles = _samplable_profiles(profiles)

  def sample_columns(
      self,
      n: int,
      rng: np.random.Generator,
      *,
      temperature: float = 1.0,
  ) -> dict[str, list]:
    out: dict[str, list] = {}
    for name, p in self._profiles.items():
      out[name] = self._sample_one(p, n, rng, temperature)
    return out

  @staticmethod
  def _sample_one(
      p: ColumnProfile,
      n: int,
      rng: np.random.Generator,
      temperature: float,
  ) -> list:
    # Inject nulls first where the schema allows and the reference showed
    # them, so the marginal null-rate is preserved.
    null_mask = (
        rng.random(n) < p.null_fraction if
        (p.nullable and p.null_fraction > 0.0) else np.zeros(n, dtype=bool))

    if p.kind is ColumnKind.CONSTANT:
      values: list = [p.constant_value] * n

    elif p.kind is ColumnKind.NUMERIC:
      lo = p.minimum if p.minimum is not None else 0.0
      hi = p.maximum if p.maximum is not None else lo
      if len(p.quantiles) >= _MIN_QUANTILE_POINTS:
        # Inverse transform sampling over the empirical CDF: uniform
        # draws map through the observed decile vector, so a skewed
        # source marginal lands skewed. Plain uniform-in-range put
        # ~99% of a 90/10 heavy-tailed column above its true p90
        # (ADR 0022).
        grid = np.linspace(0.0, 1.0, len(p.quantiles))
        draws = np.interp(rng.random(n), grid, np.asarray(p.quantiles))
      elif hi > lo:
        draws = rng.uniform(lo, hi, size=n)
      else:
        draws = np.full(n, lo)
      values = [round(x) for x in draws
               ] if p.is_integer else [float(x) for x in draws]

    elif p.kind is ColumnKind.CATEGORICAL:
      values = _sample_categorical(p, n, rng, temperature)

    elif p.kind is ColumnKind.TEMPORAL:
      # TEMPORAL profiles always carry a value type (set beside kind).
      values = _inject_temporal_sentinels(
          p,
          sample_temporal(
              p.minimum,
              p.maximum,
              cast("str", p.temporal_value_type),
              p.temporal_format,
              n,
              rng,
              quantiles=p.quantiles,
          ),
          rng,
      )

    else:  # FREE_TEXT shouldn't reach here (filtered in fit()).
      values = [None] * n

    return [None if null_mask[i] else values[i] for i in range(n)]


def _inject_temporal_sentinels(
    p: ColumnProfile,
    values: list,
    rng: np.random.Generator,
) -> list:
  """Overwrite jittered temporal values with the profile's sentinel values
    at their observed fractions (0001-01-01 / 9999-12-31 style — excluded
    from the jitter [min, max] by the profiler, reproduced here instead)."""
  if not p.temporal_sentinels:
    return values
  draws = rng.random(len(values))
  out = list(values)
  for i, r in enumerate(draws):
    acc = 0.0
    for sentinel_value, fraction in p.temporal_sentinels:
      acc += fraction
      if r < acc:
        out[i] = sentinel_value
        break
  return out


def _sample_categorical(
    p: ColumnProfile,
    n: int,
    rng: np.random.Generator,
    temperature: float,
) -> list:
  """Empirical-frequency categorical draw with temperature reweighting.

    ``temperature`` interpolates between the empirical distribution
    (``1.0``) and either uniform (``>1``) or the mode (``→0``). We map
    ``cfg.similarity`` so similarity→1 ⇒ temperature→0 (mimic) and
    similarity→0 ⇒ temperature→~2 (diverge) in the engine.
    """
  cats = list(p.categories)
  if not cats:
    # A driven child's inherited-edge placeholder (ADR 0036): the real
    # values are not known at fit time — `generate_for_keys` overwrites
    # this column from the parent keys right after sampling — so there
    # is nothing to draw from. Mirrors `_fill_from_profile`'s empty-
    # categories fallback; without this guard `np.asarray([])` on the
    # empty `cats` list below defaults to float64 (not bool), and the
    # sparsity mask's `~sparse` raises TypeError.
    return [None] * n
  weights = np.asarray(p.weights, dtype=float)
  if weights.sum() <= 0:
    weights = np.ones(len(cats))
  weights = weights / weights.sum()

  # Sparsity categories (empty/whitespace strings, None) keep their exact
  # empirical mass — empty parity is a hard fidelity metric and the
  # similarity blend flattening it failed `freetext.empty_parity` on the
  # 2026-08-09 B_TABLE R1 (B.1 parity: ColumnSampler._categorical_masses).
  # Reweighting applies only within the substantive remainder.
  sparse = np.asarray(
      [c is None or (isinstance(c, str) and not c.strip()) for c in cats])
  sub_mass = float(weights[~sparse].sum())
  if sparse.any() and sub_mass > 0:
    sub_w = weights[~sparse] / sub_mass
    if temperature <= _TEMP_EPSILON:
      reweighted = np.zeros_like(sub_w)
      reweighted[int(np.argmax(sub_w))] = 1.0
    else:
      logits = np.log(sub_w + _LOG_SMOOTHING) / temperature
      logits -= logits.max()
      reweighted = np.exp(logits)
      reweighted /= reweighted.sum()
    probs = weights.copy()
    probs[~sparse] = reweighted * sub_mass
    probs /= probs.sum()
    picks = rng.choice(len(cats), size=n, p=probs)
    return [cats[int(i)] for i in picks]

  if temperature <= _TEMP_EPSILON:
    # Collapse to the mode: maximal mimicry.
    idx = int(np.argmax(weights))
    return [cats[idx]] * n

  # Temperature reweighting in log space, then renormalize.
  logits = np.log(weights + _LOG_SMOOTHING) / temperature
  logits -= logits.max()
  probs = np.exp(logits)
  probs /= probs.sum()
  picks = rng.choice(len(cats), size=n, p=probs)
  return [cats[int(i)] for i in picks]


class SdgxBackend:
  """Production backend wrapping ``sdgx`` CTGAN. Deferred heavy import.

    Fits ``sdgx``'s :class:`Synthesizer` with a CTGAN-family model on the
    reference rows in :meth:`fit`, then :meth:`sample_columns` calls
    ``Synthesizer.sample``. Non-numeric/categorical columns are still routed
    through the engine's fidelity clamps afterward, and free-text columns
    are dropped here (the LLM hook owns them).

    On any failure to import or fit ``sdgx`` (e.g. the laptop, where the
    ``[library]`` extra is not installed), this backend transparently
    delegates to :class:`EmpiricalBackend` so the engine still runs. The
    fact of the fallback is recorded on ``self.used_fallback``.
    """

  def __init__(self, *, epochs: int = 100) -> None:
    self.epochs = epochs
    self._synthesizer = None  # sdgx.synthesizer.Synthesizer | None
    self._fallback: EmpiricalBackend | None = None
    self._profiles: dict[str, ColumnProfile] = {}
    self._free_text_cols: set[str] = set()
    self.used_fallback: bool = False

  def fit(self, reference_rows: list[dict],
          profiles: dict[str, ColumnProfile]) -> None:
    self._profiles = _samplable_profiles(profiles)
    self._free_text_cols = {
        name for name, p in profiles.items() if p.kind is ColumnKind.FREE_TEXT
    }
    try:
      self._fit_sdgx(reference_rows)
    except Exception as e:  # pylint: disable=broad-exception-caught
      self._synthesizer = None
      self._fallback = EmpiricalBackend()
      self._fallback.fit(reference_rows, profiles)
      self.used_fallback = True
      # Loud, like every other fallback: the 2026-07-22 b2 E2E run's
      # worker logs could not tell which backend actually generated
      # (a 3 s "fit" is the fallback, but nothing said so).
      log_milestone(
          "b2_backend_fallback",
          level=logging.WARNING,
          backend="empirical",
          error=type(e).__name__,
          # The message names WHAT failed (e.g. the missing module) —
          # the 2026-07-22 re-run logged only the type, leaving the
          # actual sdgx import defect unknowable from worker logs.
          detail=str(e)[:160],
      )
    else:
      log_milestone("b2_backend_fitted", backend="sdgx")

  def _fit_sdgx(self, reference_rows: list[dict]) -> None:
    # Deferred heavy imports — only here, never at module load. ANY
    # failure (missing extra, version-skewed API) propagates to fit()'s
    # except clause, which falls back to the NumPy EmpiricalBackend.
    import pandas as pd
    from sdgx.data_connectors.dataframe_connector import DataFrameConnector
    from sdgx.data_loader import DataLoader
    from sdgx.models.ml.single_table.ctgan import CTGANSynthesizerModel
    from sdgx.synthesizer import Synthesizer

    frame = self._reference_frame(reference_rows, pd)
    connector = DataFrameConnector(df=frame)
    loader = DataLoader(connector)
    metadata = self._build_metadata(loader, frame)
    synthesizer = Synthesizer(
        model=CTGANSynthesizerModel(epochs=self.epochs),
        data_connector=connector,
        metadata=metadata,
    )
    synthesizer.fit()
    self._synthesizer = synthesizer

  @staticmethod
  def _build_metadata(loader, frame):
    """Build sdgx Metadata across known API shapes (version-tolerant).

        sdgx has moved metadata construction over releases; we try the
        documented ``Metadata.from_dataloader`` first, then
        ``from_dataframe``, then let the Synthesizer auto-infer (return
        ``None``). The outer ``fit()`` try/except catches any residual
        breakage and falls back to the empirical backend.
        """
    from sdgx.data_models.metadata import Metadata

    if hasattr(Metadata, "from_dataloader"):
      return Metadata.from_dataloader(loader)
    if hasattr(Metadata, "from_dataframe"):
      return Metadata.from_dataframe(frame)
    return None

  def _reference_frame(self, reference_rows: list[dict],
                       pd_module) -> pd.DataFrame:
    """Reference rows → DataFrame, dropping free-text and temporal columns.

        Temporal columns are jitter-sampled from their profile, never fed
        to CTGAN — fitting raw timestamps makes the model resample the
        observed table (the 2026-07-20 memorization defect).
        """
    keep = {
        name for name, p in self._profiles.items()
        if p.kind is not ColumnKind.TEMPORAL
    }
    rows = [{
        k: v for k, v in row.items() if k in keep
    } for row in reference_rows]
    return pd_module.DataFrame(rows)

  def sample_columns(
      self,
      n: int,
      rng: np.random.Generator,
      *,
      temperature: float = 1.0,
  ) -> dict[str, list]:
    if self._synthesizer is None:
      assert self._fallback is not None
      return self._fallback.sample_columns(n, rng, temperature=temperature)

    # sdgx is not bit-for-bit reproducible across machines; we seed torch
    # best-effort and rely on the engine's post-hoc enforcement + the
    # NumPy backend for the reproducibility contract test.
    self._seed_torch(int(rng.integers(0, 2**31 - 1)))
    sampled = self._synthesizer.sample(n)  # pandas DataFrame
    out: dict[str, list] = {}
    for name, p in self._profiles.items():
      if p.kind is ColumnKind.TEMPORAL:
        values = _inject_temporal_sentinels(
            p,
            sample_temporal(p.minimum, p.maximum, p.temporal_value_type,
                            p.temporal_format, n, rng),
            rng,
        )
        if p.nullable and p.null_fraction > 0.0:
          null_mask = rng.random(n) < p.null_fraction
          values = [None if null_mask[i] else values[i] for i in range(n)]
        out[name] = values
      elif name in sampled.columns:
        out[name] = list(sampled[name])
      else:
        # CTGAN dropped a column (e.g. constant) — fill from profile.
        out[name] = _fill_from_profile(p, n)
    return out

  @staticmethod
  def _seed_torch(seed: int) -> None:
    try:
      import torch

      torch.manual_seed(seed)
    except Exception:  # pylint: disable=broad-exception-caught
      pass


def _fill_from_profile(p: ColumnProfile, n: int) -> list:
  """Deterministic constant fill for a column sdgx omitted."""
  if p.kind is ColumnKind.CONSTANT:
    return [p.constant_value] * n
  if p.kind is ColumnKind.NUMERIC:
    lo = p.minimum if p.minimum is not None else 0.0
    return [round(lo) if p.is_integer else float(lo)] * n
  if p.kind is ColumnKind.CATEGORICAL and p.categories:
    return [p.categories[0]] * n
  return [None] * n
