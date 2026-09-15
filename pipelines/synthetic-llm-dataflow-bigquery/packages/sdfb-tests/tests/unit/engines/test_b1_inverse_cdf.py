"""B.1 numeric sampling follows the empirical CDF (2026-08-11 R1 evidence).

The anchored+uniform VALUE-AVERAGE (`s*(anchored+jitter) + (1-s)*uniform`)
is a convolution: it reproduces neither the observed shape nor uniform, and
any single outlier in [lo, hi] lets uniform mass fill the whole span. Both
R1 cold baselines measured it — 22 numeric columns at decile-KS 0.40-0.90,
and COL_009-class banded integers (every source value in the 40xx-prefixed
band) landing mid-range values that break the band.

Fix: inverse transform sampling through the FULL sorted observed sample
(Devroye 1986, ch. II — same primitive as B.2's decile vector, ADR 0022,
at sample resolution instead of 11 points). `similarity` no longer shapes
numeric draws — fidelity to the reference marginal is not a dial.
"""

import random

import numpy as np
import pytest
from sdfb_core.engines.b1_rag._fidelity import ColumnSampler
from sdfb_core.engines.b1_rag.profile import ColumnKind, ColumnProfile


def _numeric_profile(values: list[float],
                     *,
                     integral: bool = False) -> ColumnProfile:
  return ColumnProfile(
      name="n",
      bq_type="INT64" if integral else "FLOAT64",
      kind=ColumnKind.NUMERIC,
      nullable=False,
      null_fraction=0.0,
      numeric_min=min(values),
      numeric_max=max(values),
      is_integral=integral,
      observed_values=tuple(values),
  )


def _draw(profile: ColumnProfile,
          n: int,
          *,
          use_numpy: bool,
          similarity: float = 0.5):
  sampler = ColumnSampler(profile)
  if use_numpy:
    return sampler.sample_numpy(np.random.default_rng(7), n, similarity)
  return sampler.sample_python(random.Random(7), n, similarity)


@pytest.mark.parametrize("use_numpy", [True, False])
def test_skewed_marginal_survives_sampling(use_numpy: bool) -> None:
  # 90% of mass in [0, 10], 10% at 1000 — the blend put ~50% of draws
  # mid-range where the source has zero mass.
  values = [float(i % 11) for i in range(900)] + [1000.0] * 100
  drawn = [
      v for v in _draw(_numeric_profile(values), 2000, use_numpy=use_numpy)
  ]
  low = sum(1 for v in drawn if v <= 10.0) / len(drawn)
  assert 0.82 <= low <= 0.98, f"low-mass fraction {low} lost the skew"
  assert min(drawn) >= 0.0 and max(drawn) <= 1000.0


@pytest.mark.parametrize("use_numpy", [True, False])
def test_prefix_band_survives_one_outlier(use_numpy: bool) -> None:
  # COL_009-class: every substantive value is a 10-digit integer starting
  # 40; one low outlier used to hand ~half the mass to uniform(outlier,
  # max) — synthetic opened with 3, never 40.
  rng = random.Random(4)
  band = [4_000_000_000 + rng.randrange(100_000_000) for _ in range(999)]
  values = [float(v) for v in band] + [1_000_000_000.0]
  drawn = _draw(
      _numeric_profile(values, integral=True), 2000, use_numpy=use_numpy)
  in_band = sum(1 for v in drawn if str(int(v)).startswith("40")) / len(drawn)
  assert in_band >= 0.99, f"banded mass {in_band} broke the 40-prefix"
  assert all(isinstance(v, int) for v in drawn)


@pytest.mark.parametrize("use_numpy", [True, False])
def test_decile_ks_against_observed_is_small(use_numpy: bool) -> None:
  # The stats_diff decile-KS oracle: empirical inverse-CDF must reproduce
  # the observed deciles at sample resolution (KS << the 0.2 warn gate).
  rng = random.Random(12)
  values = sorted(rng.lognormvariate(6.0, 1.2) for _ in range(3000))
  drawn = sorted(_draw(_numeric_profile(values), 4000, use_numpy=use_numpy))
  ks = 0.0
  for q in range(1, 10):
    src = values[int(q / 10 * (len(values) - 1))]
    below = sum(1 for v in drawn if v <= src) / len(drawn)
    ks = max(ks, abs(below - q / 10))
  assert ks < 0.06, f"decile KS {ks} vs observed sample"


@pytest.mark.parametrize("use_numpy", [True, False])
def test_empty_observed_falls_back_to_uniform_range(use_numpy: bool) -> None:
  profile = ColumnProfile(
      name="n",
      bq_type="FLOAT64",
      kind=ColumnKind.NUMERIC,
      nullable=False,
      null_fraction=0.0,
      numeric_min=2.0,
      numeric_max=5.0,
      observed_values=(),
  )
  drawn = _draw(profile, 200, use_numpy=use_numpy)
  assert all(2.0 <= v <= 5.0 for v in drawn)


@pytest.mark.parametrize("use_numpy", [True, False])
def test_seeded_draws_are_reproducible(use_numpy: bool) -> None:
  values = [float(i) for i in range(50)] + [900.0] * 5
  profile = _numeric_profile(values)
  assert _draw(
      profile, 64, use_numpy=use_numpy) == _draw(
          profile, 64, use_numpy=use_numpy)
