"""Derived sampler arrays are built once per sampler, not once per call.

2026-07-26 1M E2E: `_numeric_numpy` re-materialized a Python listcomp over
every observed value on EVERY generate_batch() call — 42 INT64 columns x
~10k observed values x 62,500 elements at batch_size=16, roughly 3,900
CPU-seconds of pure waste. `_temporal_numpy` was a milder instance: the
float list was memoized, but the np.asarray() around it was not.

The profile is frozen for the sampler's lifetime, so its derived arrays
must be too.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=protected-access

from __future__ import annotations

import numpy as np
from sdfb_core.engines.b1_rag._fidelity import ColumnSampler
from sdfb_core.engines.b1_rag.profile import ColumnKind, ColumnProfile


def _numeric_profile() -> ColumnProfile:
  return ColumnProfile(
      name="amount",
      bq_type="INT64",
      kind=ColumnKind.NUMERIC,
      nullable=False,
      null_fraction=0.0,
      numeric_min=0.0,
      numeric_max=999.0,
      observed_values=tuple(range(1000)),
  )


def _temporal_profile() -> ColumnProfile:
  return ColumnProfile(
      name="booked_on",
      bq_type="STRING",
      kind=ColumnKind.TEMPORAL,
      nullable=False,
      null_fraction=0.0,
      temporal_format="%Y-%m-%d",
      numeric_min=1_609_459_200.0,  # 2021-01-01
      numeric_max=1_612_137_600.0,  # 2021-02-01
      observed_values=tuple(f"2021-01-{d:02d}" for d in range(1, 29)),
  )


def test_numeric_observed_array_built_once_across_calls():
  sampler = ColumnSampler(_numeric_profile())
  rng = np.random.default_rng(0)
  sampler.sample_numpy(rng, 4, 0.5)
  first = sampler._numeric_array
  assert first is not None
  sampler.sample_numpy(rng, 4, 0.5)
  assert sampler._numeric_array is first, "numeric array re-materialized"


def test_temporal_observed_array_built_once_across_calls():
  sampler = ColumnSampler(_temporal_profile())
  rng = np.random.default_rng(0)
  sampler.sample_numpy(rng, 4, 0.5)
  first = sampler._temporal_array
  assert first is not None
  sampler.sample_numpy(rng, 4, 0.5)
  assert sampler._temporal_array is first, "temporal array re-materialized"


def test_hoisting_preserves_numeric_draws_exactly():
  """Same seed, same draws — hoisting is a pure performance change."""
  prof = _numeric_profile()
  a = ColumnSampler(prof).sample_numpy(np.random.default_rng(1234), 32, 0.7)
  b = ColumnSampler(prof).sample_numpy(np.random.default_rng(1234), 32, 0.7)
  assert a == b
  assert all(0 <= v <= 999 for v in a)


def test_hoisting_preserves_temporal_draws_exactly():
  prof = _temporal_profile()
  a = ColumnSampler(prof).sample_numpy(np.random.default_rng(99), 16, 0.6)
  b = ColumnSampler(prof).sample_numpy(np.random.default_rng(99), 16, 0.6)
  assert a == b


def test_repeated_calls_stay_consistent_with_a_fresh_sampler():
  """The cached array must not drift the distribution: a sampler that has
    already drawn produces the same values as a fresh one given the same
    rng state."""
  prof = _numeric_profile()
  warm = ColumnSampler(prof)
  warm.sample_numpy(np.random.default_rng(7), 8, 0.5)  # prime the cache
  warm_draw = warm.sample_numpy(np.random.default_rng(42), 8, 0.5)
  cold_draw = ColumnSampler(prof).sample_numpy(
      np.random.default_rng(42), 8, 0.5)
  assert warm_draw == cold_draw
