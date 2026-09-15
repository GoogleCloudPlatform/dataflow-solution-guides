"""B.2 inverse-CDF sampling over empirical deciles (ADR 0022).

Uniform-in-range flattened every skewed marginal: a 90/10 heavy-tailed
column landed ~99% of its draws above the true p90. Inverse transform
sampling over the profile's decile vector keeps the observed CDF shape
while every draw stays novel and in-range.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=protected-access

from datetime import date

import numpy as np
from sdfb_core.contracts.schema import TableSchema
from sdfb_core.engines.b2_library.backends import EmpiricalBackend
from sdfb_core.engines.b2_library.fidelity import ColumnKind, profile_column


def _field(type_: str):
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t"
      },
      "schema": [{
          "name": "C",
          "type": type_,
          "mode": "NULLABLE"
      }],
  }).columns[0]


def test_numeric_profile_carries_decile_vector():
  rows = [{"C": float(v)} for v in range(101)]
  prof = profile_column(_field("FLOAT64"), rows)
  assert len(prof.quantiles) == 11
  assert prof.quantiles[0] == 0.0
  assert prof.quantiles[5] == 50.0
  assert prof.quantiles[-1] == 100.0


def test_skewed_numeric_marginal_survives_sampling():
  # 90% of mass in [0, 10], 10% at 1000. Uniform over [0, 1000] would put
  # ~1% of draws at or below 10; the empirical CDF puts ~90% there.
  rows = [{"C": float(i % 11)} for i in range(900)]
  rows += [{"C": 1000.0}] * 100
  prof = profile_column(_field("FLOAT64"), rows)
  draws = EmpiricalBackend._sample_one(prof, 2000, np.random.default_rng(7),
                                       0.5)
  nums = [v for v in draws if v is not None]
  low = sum(1 for v in nums if v <= 10.0) / len(nums)
  assert 0.75 <= low <= 0.98, f"low-mass fraction {low} lost the skew"
  assert min(nums) >= 0.0 and max(nums) <= 1000.0


def test_integer_rounding_still_applies():
  rows = [{"C": i} for i in range(200)]
  prof = profile_column(_field("INT64"), rows)
  draws = EmpiricalBackend._sample_one(prof, 100, np.random.default_rng(3), 0.5)
  assert all(isinstance(v, int) for v in draws if v is not None)


def test_temporal_burst_density_survives_sampling():
  # 28 June days carry 10 rows each; 20 January days carry 1 each →
  # 48 distinct (> the 20-category enum cap → TEMPORAL). ~93% of the
  # mass is June; uniform over Jan..June would spread it evenly.
  rows = [{
      "C": date(2026, 6, 1 + (i % 28))
  } for i in range(280)] + [{
      "C": date(2026, 1, 2 + i)
  } for i in range(20)]
  prof = profile_column(_field("DATE"), rows)
  assert prof.kind is ColumnKind.TEMPORAL
  assert len(prof.quantiles) == 11
  draws = EmpiricalBackend._sample_one(prof, 1000, np.random.default_rng(11),
                                       0.5)
  dates = [v for v in draws if v is not None]
  june = sum(1 for d in dates if d.month >= 5) / len(dates)
  assert june >= 0.7, f"June mass {june} — burst density flattened"
