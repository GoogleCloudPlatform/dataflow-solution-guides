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
"""Tests for `sdfb_evaluation.stats.privacy` (Task 11).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import math
import tracemalloc
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import pytest

from sdfb_evaluation.canonical import NULL_CODE
from sdfb_evaluation.stats.privacy import GowerSpace
from sdfb_evaluation.stats.privacy import NNPrivacyResult
from sdfb_evaluation.stats.privacy import density_coverage
from sdfb_evaluation.stats.privacy import effective_chunk
from sdfb_evaluation.stats.noise import wilson_interval
from sdfb_evaluation.stats.privacy import gower_knn
from sdfb_evaluation.stats.privacy import holdout_mass
from sdfb_evaluation.stats.privacy import nn_privacy
from sdfb_evaluation.stats.privacy import nn_privacy_encoded
from sdfb_evaluation.stats.privacy import permutation_se
from sdfb_evaluation.stats.privacy import pit_mid_cdf
from sdfb_evaluation.stats.privacy import summarize_nn

_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _micros(moment: datetime) -> float:
  """A timestamp on the planning scale: UNIX microseconds (Ruling R54)."""
  return float((moment - _UNIX_EPOCH) // timedelta(microseconds=1))


_EPOCH_2020 = _micros(datetime(2020, 1, 1, tzinfo=UTC))
_FOUR_YEARS_US = 4 * 365 * 86_400 * 1_000_000
_STATUSES = ("new", "paid", "shipped", "returned")
_STATUS_P = (0.1, 0.5, 0.3, 0.1)

# ---------------------------------------------------------------------------
# Helpers: a mixed-kind thelook-like generator and a GowerSpace on its grids
# ---------------------------------------------------------------------------


def _draw_rows(rng: np.random.Generator, n: int) -> list[dict[str, Any]]:
  """`n` mixed rows: continuous/atomic numerics, a timestamp, categoricals, nulls."""
  amount = rng.lognormal(3.0, 1.0, n)
  amount_null = rng.random(n) < 0.05
  age = rng.integers(18, 90, n)
  created = _EPOCH_2020 + rng.random(n) * _FOUR_YEARS_US
  status = rng.choice(len(_STATUSES), size=n, p=_STATUS_P)
  city = rng.integers(0, 40, n)
  city_null = rng.random(n) < 0.05
  return [{
      "amount": None if amount_null[i] else float(amount[i]),
      "age": int(age[i]),
      "created_at": _UNIX_EPOCH + timedelta(microseconds=float(created[i])),
      "status": _STATUSES[int(status[i])],
      "city": None if city_null[i] else f"city_{int(city[i])}",
  } for i in range(n)]


def _grid(values: Sequence[float]) -> np.ndarray:
  """A 1,001-point quantile grid, the shape `APPROX_QUANTILES(x, 1000)` gives."""
  return np.quantile(np.asarray(values, dtype=float), np.linspace(0, 1, 1001))


def _space_for(source: Sequence[dict[str, Any]]) -> GowerSpace:
  amounts = [row["amount"] for row in source if row["amount"] is not None]
  ages = [row["age"] for row in source]
  created = [_micros(row["created_at"]) for row in source]
  return GowerSpace(
      num_grids=(_grid(amounts), _grid(ages), _grid(created)),
      num_names=("amount", "age", "created_at"),
      cat_names=("status", "city"),
  )


@pytest.fixture(name="space", scope="module")
def _space() -> GowerSpace:
  return _space_for(_draw_rows(np.random.default_rng(12345), 20_000))


def _naive_gower(q_num: np.ndarray, q_cat: np.ndarray, r_num: np.ndarray,
                 r_cat: np.ndarray) -> np.ndarray:
  """The plain double loop, feature by feature, in float64."""
  d = q_num.shape[1] + q_cat.shape[1]
  out = np.empty((q_num.shape[0], r_num.shape[0]))
  for i in range(q_num.shape[0]):
    for j in range(r_num.shape[0]):
      total = 0.0
      for f in range(q_num.shape[1]):
        a, b = float(q_num[i, f]), float(r_num[j, f])
        if math.isnan(a) and math.isnan(b):
          continue
        total += 1.0 if math.isnan(a) or math.isnan(b) else abs(a - b)
      for f in range(q_cat.shape[1]):
        total += 0.0 if q_cat[i, f] == r_cat[j, f] else 1.0
      out[i, j] = total / d
  return out


def _naive_knn(dist: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
  """The k smallest per row, ties broken by the lower reference index."""
  idx = np.array([
      sorted(range(dist.shape[1]), key=lambda j, i=i: (dist[i, j], j))[:k]
      for i in range(dist.shape[0])
  ])
  return np.take_along_axis(dist, idx, axis=1), idx


def _mixed_blocks(rng: np.random.Generator, n: int, *,
                  dyadic: bool) -> tuple[np.ndarray, np.ndarray]:
  """`(n, 4)` numeric (NaN nulls) and `(n, 3)` categorical (NULL_CODE nulls) blocks."""
  if dyadic:
    num = rng.integers(0, 9, size=(n, 4)).astype(np.float32) / 8
  else:
    num = rng.random((n, 4)).astype(np.float32)
  num[rng.random((n, 4)) < 0.15] = np.nan
  cat = rng.integers(0, 3, size=(n, 3)).astype(np.uint64)
  cat[rng.random((n, 3)) < 0.15] = NULL_CODE
  return num, cat


# ---------------------------------------------------------------------------
# 1. gower_knn equals a naive double loop on 30 x 40 mixed rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("k", [1, 5, 12, 40])
def test_gower_knn_equals_naive_double_loop_exactly_on_dyadic_values(k):
  # Multiples of 1/8 with d = 7 features: every float32 sum is exact, so
  # distances, the many ties and their index tie-breaks must match exactly.
  # k <= 8 runs the argmin selection, larger k the partition selection.
  rng = np.random.default_rng(1)
  q_num, q_cat = _mixed_blocks(rng, 30, dyadic=True)
  r_num, r_cat = _mixed_blocks(rng, 40, dyadic=True)
  dist, idx = gower_knn(q_num, q_cat, r_num, r_cat, k=k)
  want_dist, want_idx = _naive_knn(_naive_gower(q_num, q_cat, r_num, r_cat), k)
  np.testing.assert_array_equal(idx, want_idx)
  np.testing.assert_array_equal(dist, want_dist)


def test_argmin_and_partition_selections_agree():
  rng = np.random.default_rng(21)
  q_num, q_cat = _mixed_blocks(rng, 200, dyadic=True)
  r_num, r_cat = _mixed_blocks(rng, 300, dyadic=True)
  small = gower_knn(q_num, q_cat, r_num, r_cat, k=8)
  large = gower_knn(q_num, q_cat, r_num, r_cat, k=9)
  np.testing.assert_array_equal(large[0][:, :8], small[0])
  np.testing.assert_array_equal(large[1][:, :8], small[1])


def test_gower_knn_equals_naive_double_loop_on_encoded_rows(space):
  rng = np.random.default_rng(2)
  q_num, q_cat = space.encode(_draw_rows(rng, 30))
  r_num, r_cat = space.encode(_draw_rows(rng, 40))
  dist, idx = gower_knn(q_num, q_cat, r_num, r_cat, k=4)
  full = _naive_gower(q_num, q_cat, r_num, r_cat)
  want_dist, _ = _naive_knn(full, 4)
  np.testing.assert_allclose(dist, want_dist, rtol=0, atol=1e-6)
  # Every returned index really sits at the distance reported for it.
  np.testing.assert_allclose(
      np.take_along_axis(full, idx, axis=1), dist, rtol=0, atol=1e-6)
  assert all(len(set(row)) == 4 for row in idx.tolist())


def test_gower_knn_breaks_ties_by_reference_index():
  r_num = np.full((10, 2), 0.5, dtype=np.float32)
  r_cat = np.ones((10, 1), dtype=np.uint64)
  q_num = np.full((3, 2), 0.25, dtype=np.float32)
  q_cat = np.ones((3, 1), dtype=np.uint64)
  dist, idx = gower_knn(q_num, q_cat, r_num, r_cat, k=4)
  np.testing.assert_array_equal(idx, np.tile(np.arange(4), (3, 1)))
  np.testing.assert_array_equal(dist, np.full((3, 4), 0.5 / 3))


def test_gower_knn_returns_sorted_neighbours_and_validates_k():
  rng = np.random.default_rng(3)
  q_num, q_cat = _mixed_blocks(rng, 20, dyadic=False)
  r_num, r_cat = _mixed_blocks(rng, 15, dyadic=False)
  dist, idx = gower_knn(q_num, q_cat, r_num, r_cat, k=15)
  assert dist.shape == idx.shape == (20, 15)
  assert np.all(np.diff(dist, axis=1) >= 0)
  np.testing.assert_array_equal(
      np.sort(idx, axis=1), np.tile(np.arange(15), (20, 1)))
  with pytest.raises(ValueError, match="k"):
    gower_knn(q_num, q_cat, r_num, r_cat, k=16)
  with pytest.raises(ValueError, match="k"):
    gower_knn(q_num, q_cat, r_num, r_cat, k=0)


def test_gower_knn_rejects_mismatched_blocks():
  rng = np.random.default_rng(4)
  q_num, q_cat = _mixed_blocks(rng, 5, dyadic=False)
  r_num, r_cat = _mixed_blocks(rng, 6, dyadic=False)
  with pytest.raises(ValueError, match="feature"):
    gower_knn(q_num[:, :3], q_cat, r_num, r_cat, k=1)
  with pytest.raises(ValueError, match="rows"):
    gower_knn(q_num[:4], q_cat, r_num, r_cat, k=1)
  with pytest.raises(ValueError, match=r"\[0, 1\]"):
    gower_knn(q_num * 3, q_cat, r_num, r_cat, k=1)


# ---------------------------------------------------------------------------
# Null convention and rare categorical values
# ---------------------------------------------------------------------------


def test_null_convention_numeric_and_categorical():
  # One numeric + one categorical feature; d = 2.
  r_num = np.array([[np.nan], [0.25]], dtype=np.float32)
  r_cat = np.array([[NULL_CODE], [7]], dtype=np.uint64)
  q_num = np.array([[np.nan]], dtype=np.float32)
  q_cat = np.array([[NULL_CODE]], dtype=np.uint64)
  dist, idx = gower_knn(q_num, q_cat, r_num, r_cat, k=2)
  # null vs null = 0 on both features; null vs value = 1 on both features.
  np.testing.assert_array_equal(idx, [[0, 1]])
  np.testing.assert_array_equal(dist, [[0.0, 1.0]])


def test_null_convention_through_encode(space):
  rows = [
      {
          "amount": None,
          "age": 30,
          "created_at": _EPOCH_2020,
          "status": "new",
          "city": None
      },
      {
          "amount": None,
          "age": 30,
          "created_at": _EPOCH_2020,
          "status": "new",
          "city": None
      },
      {
          "amount": 20.0,
          "age": 30,
          "created_at": _EPOCH_2020,
          "status": "new",
          "city": "city_1"
      },
  ]
  num, cat = space.encode(rows)
  dist, idx = gower_knn(num[:1], cat[:1], num[1:], cat[1:], k=2)
  np.testing.assert_array_equal(idx, [[0, 1]])
  np.testing.assert_array_equal(dist, [[0.0, 2.0 / 5.0]])


def test_distinct_rare_categorical_values_are_distance_one_apart():
  # 200 distinct values, each seen once: no top-K / "other" collapse, so every
  # pair of distinct values is exactly 1 apart and only the value itself is 0.
  space = GowerSpace(num_grids=(), num_names=(), cat_names=("sku",))
  rows = [{"sku": f"rare_{i:04d}"} for i in range(200)]
  num, cat = space.encode(rows)
  assert num.shape == (200, 0)
  assert len(set(cat[:, 0].tolist())) == 200
  dist, idx = gower_knn(num, cat, num, cat, k=3)
  np.testing.assert_array_equal(dist[:, 0], np.zeros(200))
  np.testing.assert_array_equal(idx[:, 0], np.arange(200))
  np.testing.assert_array_equal(dist[:, 1:], np.ones((200, 2)))


# ---------------------------------------------------------------------------
# GowerSpace: mid-CDF PIT, value readings, digest, validation
# ---------------------------------------------------------------------------


def test_mid_cdf_pit_on_a_grid_with_atoms():
  # 11 points at probabilities 0, 0.1, ..., 1.0: an atom at 0 (grid indices
  # 0-4), an atom at 2 (indices 6-8), single points elsewhere.
  grid = np.array([0, 0, 0, 0, 0, 1, 2, 2, 2, 3, 4], dtype=float)
  space = GowerSpace(num_grids=(grid,), num_names=("x",), cat_names=())
  values = [-5, 0, 0.5, 1, 1.5, 2, 2.5, 4, 9, None]
  num, _ = space.encode([{"x": v} for v in values])
  want = [
      0.0,  # below the grid clamps to 0
      0.2,  # atom run p in [0.0, 0.4] -> midpoint
      0.45,  # between 0 (top of its run, p=0.4) and 1 (p=0.5)
      0.5,  # a single grid point
      0.55,  # between 1 (p=0.5) and the bottom of the atom at 2 (p=0.6)
      0.7,  # atom run p in [0.6, 0.8] -> midpoint
      0.85,  # between 2 (top of its run, p=0.8) and 3 (p=0.9)
      1.0,  # the grid maximum
      1.0,  # above the grid clamps to 1
  ]
  assert num.dtype == np.float32
  np.testing.assert_allclose(num[:-1, 0], want, rtol=0, atol=1e-7)
  assert math.isnan(float(num[-1, 0]))


def test_constant_source_grid_maps_its_value_to_one_half():
  space = GowerSpace(
      num_grids=(np.full(1001, 7.0),), num_names=("x",), cat_names=())
  num, _ = space.encode([{"x": 7}, {"x": 6}, {"x": 8}])
  np.testing.assert_array_equal(num[:, 0],
                                np.array([0.5, 0.0, 1.0], dtype=np.float32))


def test_temporal_and_numeric_readings_agree_across_representations():
  moment = datetime(2023, 5, 17, 12, 30, tzinfo=UTC)
  grid = np.linspace(_micros(moment) - 1e12, _micros(moment) + 1e12, 1001)
  space = GowerSpace(
      num_grids=(grid, np.linspace(0, 100, 1001)),
      num_names=("ts", "amount"),
      cat_names=())
  rows = [
      {
          "ts": moment,
          "amount": Decimal("10.50")
      },
      {
          "ts": _micros(moment),
          "amount": 10.5
      },
      {
          "ts": moment.replace(tzinfo=None),
          "amount": "10.5"
      },
      {
          "ts": moment.isoformat(timespec="microseconds"),
          "amount": 10.5
      },
  ]
  num, _ = space.encode(rows)
  for i in range(1, 4):
    np.testing.assert_array_equal(num[i], num[0])
  nulls, _ = space.encode([{"ts": float("nan"), "amount": float("inf")}])
  assert np.isnan(nulls).all()


def test_time_of_day_readings_are_micros_since_midnight():
  # Ruling R54: TIME is TIME_DIFF(x, '00:00:00', MICROSECOND), as planned;
  # a canonicalised row's "HH:MM:SS[.ffffff]" text reads the same.
  grid = np.linspace(0.0, 86_400_000_000.0, 1001)
  u = pit_mid_cdf([time(12, 0), "12:00:00", "12:00:00.000000", None],
                  grid,
                  column="pickup_time")
  np.testing.assert_allclose(u[:3], [0.5, 0.5, 0.5])
  assert math.isnan(u[3])


def test_unreadable_numeric_value_raises_naming_the_column():
  space = GowerSpace(
      num_grids=(np.linspace(0, 1, 11),), num_names=("amount",), cat_names=())
  with pytest.raises(ValueError, match="'amount'"):
    space.encode([{"amount": "not a number"}])


def test_encode_shapes_and_categorical_codes(space):
  rows = _draw_rows(np.random.default_rng(5), 25)
  num, cat = space.encode(rows)
  assert num.shape == (25, 3) and num.dtype == np.float32
  assert cat.shape == (25, 2) and cat.dtype == np.uint64
  finite = num[~np.isnan(num)]
  assert finite.min() >= 0.0 and finite.max() <= 1.0
  null_city = [i for i, row in enumerate(rows) if row["city"] is None]
  assert all(cat[i, 1] == NULL_CODE for i in null_city)
  assert space.n_features == 5


def test_digest_is_stable_and_sensitive_to_names_kinds_and_grids():
  grid = np.linspace(0, 10, 1001)
  base = GowerSpace((grid,), ("a",), ("b",))
  assert base.digest == GowerSpace((grid.copy(),), ("a",), ("b",)).digest
  assert len(base.digest) == 32
  int(base.digest, 16)
  bumped = grid.copy()
  bumped[500] += 1e-9
  variants = [
      GowerSpace((bumped,), ("a",), ("b",)),
      GowerSpace((grid,), ("a2",), ("b",)),
      GowerSpace((grid,), ("a",), ("c",)),
      GowerSpace((grid, grid), ("a", "b"), ()),
      GowerSpace((), (), ("a", "b")),
  ]
  assert len({base.digest, *(v.digest for v in variants)}) == 6


@pytest.mark.parametrize(
    ("grids", "num_names", "cat_names", "match"),
    [
        ((np.linspace(0, 1, 5),), ("a", "b"), (), "num_grids"),
        ((np.array([1.0]),), ("a",), (), "at least 2"),
        ((np.array([0.0, 2.0, 1.0]),), ("a",), (), "non-decreasing"),
        ((np.array([0.0, np.inf]),), ("a",), (), "finite"),
        ((np.linspace(0, 1, 5),), ("a",), ("a",), "duplicate"),
        ((), (), (), "no features"),
    ],
)
def test_gower_space_validates_its_plan(grids, num_names, cat_names, match):
  with pytest.raises(ValueError, match=match):
    GowerSpace(grids, num_names, cat_names)


# ---------------------------------------------------------------------------
# 5. Chunking: memory bound and exactness
# ---------------------------------------------------------------------------


def test_chunked_memory_bound_holds_at_the_defaults():
  n_ref, d = 10_000, 50
  chunk = effective_chunk(n_ref, d)
  assert 1 <= chunk <= 64
  assert chunk * n_ref * d * 4 <= 64_000_000
  rng = np.random.default_rng(6)
  r_num = rng.random((n_ref, 25)).astype(np.float32)
  r_cat = rng.integers(0, 20, size=(n_ref, 25)).astype(np.uint64)
  q_num = rng.random((96, 25)).astype(np.float32)
  q_cat = rng.integers(0, 20, size=(96, 25)).astype(np.uint64)
  tracemalloc.start()
  try:
    tracemalloc.reset_peak()
    gower_knn(q_num, q_cat, r_num, r_cat, k=5)
    _, peak = tracemalloc.get_traced_memory()
  finally:
    tracemalloc.stop()
  assert peak <= 64_000_000


def test_chunked_result_equals_unchunked_exactly():
  rng = np.random.default_rng(7)
  q_num, q_cat = _mixed_blocks(rng, 150, dyadic=False)
  r_num, r_cat = _mixed_blocks(rng, 120, dyadic=False)
  whole = gower_knn(q_num, q_cat, r_num, r_cat, k=6, chunk=10_000)
  for chunk in (1, 7, 64):
    part = gower_knn(q_num, q_cat, r_num, r_cat, k=6, chunk=chunk)
    np.testing.assert_array_equal(part[0], whole[0])
    np.testing.assert_array_equal(part[1], whole[1])
  # Query rows are independent: a shuffled query set gives shuffled rows.
  perm = rng.permutation(150)
  shuffled = gower_knn(q_num[perm], q_cat[perm], r_num, r_cat, k=6, chunk=7)
  np.testing.assert_array_equal(shuffled[0], whole[0][perm])
  np.testing.assert_array_equal(shuffled[1], whole[1][perm])


# ---------------------------------------------------------------------------
# 2./3. The holdout DCR test
# ---------------------------------------------------------------------------


def test_copies_of_reference_give_share_one_and_zero_dcr_ratio(space):
  rng = np.random.default_rng(8)
  r_rows = _draw_rows(rng, 600)
  h_rows = _draw_rows(rng, 600)
  res = nn_privacy(space, r_rows, h_rows, list(r_rows))
  summary = summarize_nn(res)
  assert res.closer_to_r == pytest.approx(1.0, abs=0.01)
  np.testing.assert_array_equal(res.dcr_syn_r, np.zeros(600))
  assert summary["dcr_p5_ratio"] == 0.0
  assert summary["dcr_train_holdout_share_ci_low"] > 0.95
  assert summary["dcr_h_r_p5"] > 0.0


def test_same_generator_synthetic_share_is_one_half(space):
  rng = np.random.default_rng(9)
  r_rows, h_rows, syn_rows = (_draw_rows(rng, 4000) for _ in range(3))
  res = nn_privacy(space, r_rows, h_rows, syn_rows)
  summary = summarize_nn(res)
  assert 0.45 <= res.closer_to_r <= 0.55
  assert summary["dcr_train_holdout_share_ci_low"] < 0.55
  assert 0.8 <= summary["dcr_p5_ratio"] <= 1.25
  assert 0.8 <= summary["nndr_p5_ratio"] <= 1.25


def test_ties_count_one_half():
  space = GowerSpace((), (), ("c",))
  r_rows = [{"c": "a"}, {"c": "b"}]
  h_rows = [{"c": "a"}, {"c": "z"}]
  syn_rows = [{"c": "a"}, {"c": "b"}, {"c": "q"}, {"c": "z"}]
  res = nn_privacy(space, r_rows, h_rows, syn_rows)
  # a: tie (1/2); b: closer to R (1); q: tie at distance 1 (1/2); z: H (0).
  assert res.closer_to_r == pytest.approx(2.0 / 4.0)
  assert res.n_syn == 4


def test_nn_mass_follows_the_tie_rule():
  space = GowerSpace((), (), ("c",))
  r_rows = [{"c": "a"}, {"c": "b"}]
  h_rows = [{"c": "c"}, {"c": "d"}]
  syn_rows = [{"c": "a"}, {"c": "a"}, {"c": "b"}, {"c": "c"}, {"c": "x"}]
  res = nn_privacy(space, r_rows, h_rows, syn_rows)
  # a, a -> R0 (decisive, 1 each); b -> R1; c -> H0; x is 1 from everything:
  # a tie between the lowest-index nearest rows R0 and H0, 1/2 each.
  np.testing.assert_array_equal(res.nn_mass, [2.5, 1.0, 1.5, 0.0])
  assert res.closer_to_r == 3.5 / 5
  assert res.nn_mass[:2].sum() == res.closer_to_r * res.n_syn
  assert res.nn_mass.sum() == res.n_syn


def test_holdout_mass_merges_by_sum_across_synthetic_chunks():
  rng = np.random.default_rng(22)
  r, h, syn = (_mixed_blocks(rng, n, dyadic=True) for n in (60, 60, 500))
  whole = nn_privacy_encoded(r, h, syn)
  parts = [
      nn_privacy_encoded(r, h, (syn[0][s], syn[1][s]))
      for s in (slice(0, 170), slice(170, 500))
  ]
  np.testing.assert_array_equal(parts[0].nn_mass + parts[1].nn_mass,
                                whole.nn_mass)
  # The closer counts are half-integers: exact, and they add up too.
  counts = [part.nn_mass[:60].sum() for part in (*parts, whole)]
  assert counts[0] + counts[1] == counts[2]
  assert counts[2] == pytest.approx(whole.closer_to_r * 500, abs=1e-9)
  d_r, i_r = gower_knn(*syn, *r, k=1)
  d_h, i_h = gower_knn(*syn, *h, k=1)
  closer, mass = holdout_mass(
      d_r[:, 0], i_r[:, 0], d_h[:, 0], i_h[:, 0], n_r=60, n_h=60)
  np.testing.assert_array_equal(mass, whole.nn_mass)
  assert closer == counts[2]
  with pytest.raises(ValueError, match="indices"):
    holdout_mass(d_r[:, 0], i_r[:, 0], d_h[:, 0], i_h[:, 0], n_r=60, n_h=10)
  with pytest.raises(ValueError, match="aligned"):
    holdout_mass(d_r[:, 0], i_r[:5, 0], d_h[:, 0], i_h[:, 0], n_r=60, n_h=60)


def test_reference_and_holdout_are_trimmed_to_equal_size(space):
  rng = np.random.default_rng(10)
  r_rows, h_rows = _draw_rows(rng, 50), _draw_rows(rng, 30)
  res = nn_privacy(space, r_rows, h_rows, _draw_rows(rng, 20))
  assert res.dcr_h_r.shape == res.nndr_h.shape == (30,)
  trimmed = nn_privacy(space, r_rows[:30], h_rows, _draw_rows(rng, 5))
  np.testing.assert_array_equal(trimmed.dcr_h_r, res.dcr_h_r)


def test_holdout_and_synthetic_sides_are_symmetric(space):
  rng = np.random.default_rng(11)
  r_rows, h_rows, syn_rows = (_draw_rows(rng, 300) for _ in range(3))
  # Synthetic = H: syn -> R is exactly H -> R, so both p5 ratios are 1.
  as_holdout = nn_privacy(space, r_rows, h_rows, list(h_rows))
  np.testing.assert_array_equal(as_holdout.dcr_syn_r, as_holdout.dcr_h_r)
  np.testing.assert_array_equal(as_holdout.nndr_syn, as_holdout.nndr_h)
  summary = summarize_nn(as_holdout)
  assert summary["dcr_p5_ratio"] == 1.0
  assert summary["nndr_p5_ratio"] == 1.0
  # Swapping R and H swaps the share around one half.
  forward = nn_privacy(space, r_rows, h_rows, syn_rows)
  swapped = nn_privacy(space, r_rows=h_rows, h_rows=r_rows, syn_rows=syn_rows)
  assert forward.closer_to_r + swapped.closer_to_r == pytest.approx(
      1.0, abs=1e-12)
  np.testing.assert_array_equal(forward.dcr_syn_r, swapped.dcr_syn_h)


def test_nndr_is_one_when_the_second_neighbour_is_at_distance_zero():
  space = GowerSpace((), (), ("c",))
  r_rows = [{"c": "a"}, {"c": "a"}, {"c": "b"}]
  res = nn_privacy(space, r_rows, [{
      "c": "b"
  }, {
      "c": "x"
  }, {
      "c": "y"
  }], [{
      "c": "a"
  }, {
      "c": "b"
  }])
  np.testing.assert_array_equal(res.nndr_syn, [1.0, 0.0])


def test_nn_privacy_is_deterministic(space):
  rows = [_draw_rows(np.random.default_rng(12), 400) for _ in range(3)]
  first = nn_privacy(space, *rows)
  second = nn_privacy(space, *rows)
  for name in ("dcr_syn_r", "dcr_syn_h", "dcr_h_r", "nndr_syn", "nndr_h",
               "nn_mass"):
    np.testing.assert_array_equal(getattr(first, name), getattr(second, name))
  assert first.closer_to_r == second.closer_to_r
  assert (first.density, first.coverage) == (second.density, second.coverage)
  assert summarize_nn(first) == summarize_nn(second)


# ---------------------------------------------------------------------------
# 6. Density and coverage
# ---------------------------------------------------------------------------


def test_density_and_coverage_for_identical_distributions(space):
  rng = np.random.default_rng(13)
  r_rows, h_rows, syn_rows = (_draw_rows(rng, 2000) for _ in range(3))
  res = nn_privacy(space, r_rows, h_rows, syn_rows, k=5)
  assert res.density == pytest.approx(1.0, abs=0.1)
  assert res.coverage >= 0.9


def test_density_and_coverage_on_a_collapsed_synthetic_side():
  rng = np.random.default_rng(14)
  real_num = rng.random((500, 3)).astype(np.float32)
  real_cat = np.zeros((500, 0), dtype=np.uint64)
  # Every fake row sits on one point: few real balls contain it (low coverage).
  fake_num = np.full((500, 3), 0.5, dtype=np.float32)
  density, coverage = density_coverage(
      real_num, real_cat, fake_num, real_cat, k=5)
  assert coverage < 0.05
  assert density >= 0.0


def test_density_coverage_hand_example():
  # Real points on a line at 0, 1/8, 2/8, 3/8 (one numeric feature), k = 1:
  # every radius is 1/8; the end points have K = 1 other row in their ball,
  # the middle ones K = 2 (both neighbours at exactly 1/8). The fake at 1/16
  # falls in the balls of 0 and 1/8 (closed balls); the fake at 7/8 in none.
  # density = (1 / (1 * 2)) * (1 * 1/1 + 1 * 1/2) = 3/4, coverage = 2/4.
  real = np.array([[0.0], [0.125], [0.25], [0.375]], dtype=np.float32)
  fake = np.array([[0.0625], [0.875]], dtype=np.float32)
  empty = np.zeros((4, 0), dtype=np.uint64)
  density, coverage = density_coverage(real, empty, fake, empty[:2], k=1)
  assert density == 0.75
  assert coverage == 0.5


def test_density_tie_correction_hand_example():
  # One categorical feature, k = 1. Real a, a, a, b: each a has radius 0 and
  # K = 2 other rows in its ball; b has radius 1 and K = 3. Fake a lands in
  # all four balls, fake c only in b's. Tie-corrected density
  # = (1/(k M)) sum_i hits_i * k / K_i = (1/2)(1/2 + 1/2 + 1/2 + 2/3) = 13/12
  # (plain closed-ball counting would give 5/2); coverage = 4/4.
  real = np.array([[1], [1], [1], [2]], dtype=np.uint64)
  fake = np.array([[1], [3]], dtype=np.uint64)
  empty = np.zeros((4, 0), dtype=np.float32)
  density, coverage = density_coverage(empty, real, empty[:2], fake, k=1)
  assert density == pytest.approx(13 / 12, abs=1e-15)
  assert coverage == 1.0


def _naive_density_coverage(real: tuple[np.ndarray, np.ndarray],
                            fake: tuple[np.ndarray, np.ndarray],
                            k: int) -> tuple[float, float]:
  """R28 by definition, on a full naive distance matrix."""
  d_rr = _naive_gower(*real, *real)
  d_fr = _naive_gower(*fake, *real)
  n = d_rr.shape[0]
  others = [np.delete(d_rr[i], i) for i in range(n)]
  radii = np.array([np.sort(o)[k - 1] for o in others])
  big_k = np.array(
      [np.count_nonzero(o <= r) for o, r in zip(others, radii, strict=True)])
  inside = d_fr <= radii[np.newaxis, :]
  density = float(np.sum(inside.sum(axis=0) * k / big_k)) / (k * len(d_fr))
  return density, float(inside.any(axis=0).mean())


@pytest.mark.parametrize("k", [1, 5, 9])
def test_density_matches_the_tie_corrected_definition(k):
  # Dyadic values: exact sums, many ties at every radius. k + 1 = 10 runs
  # the partition selection for the radii, smaller k the argmin one.
  rng = np.random.default_rng(23)
  real = _mixed_blocks(rng, 40, dyadic=True)
  fake = _mixed_blocks(rng, 30, dyadic=True)
  got = density_coverage(*real, *fake, k=k)
  want = _naive_density_coverage(real, fake, k)
  assert got[0] == pytest.approx(want[0], rel=1e-12)
  assert got[1] == want[1]


def test_density_without_ties_is_exactly_naeem():
  rng = np.random.default_rng(24)
  real = (rng.random((300, 3)).astype(np.float32), np.zeros((300, 0),
                                                            np.uint64))
  fake = (rng.random((300, 3)).astype(np.float32), np.zeros((300, 0),
                                                            np.uint64))
  k = 5
  radii = gower_knn(*real, *real, k=k + 1)[0][:, k]
  dist, idx = gower_knn(*fake, *real, k=300)
  naeem = np.count_nonzero(dist <= radii[idx]) / (k * 300)
  density, _ = density_coverage(*real, *fake, k=k)
  assert density == naeem


@pytest.mark.parametrize("cards", [(2, 3, 5), (5, 26, 2)])
def test_density_is_one_for_identical_categorical_only_distributions(cards):
  # A lattice: most radii are 0 and each ball holds many more than k real
  # rows. Plain closed-ball density reads several times 1 here.
  rng = np.random.default_rng(25)

  def draw(n: int) -> tuple[np.ndarray, np.ndarray]:
    codes = np.stack([rng.integers(0, c, n) for c in cards], axis=1)
    return np.zeros((n, 0), np.float32), codes.astype(np.uint64)

  density, coverage = density_coverage(*draw(2000), *draw(2000), k=5)
  assert 0.85 <= density <= 1.15
  assert coverage >= 0.9


def test_density_and_coverage_are_none_when_too_few_rows_for_k(space):
  rng = np.random.default_rng(15)
  res = nn_privacy(
      space, _draw_rows(rng, 4), _draw_rows(rng, 4), _draw_rows(rng, 4), k=5)
  assert res.density is None and res.coverage is None
  summary = summarize_nn(res)
  assert summary["density"] is None and summary["coverage"] is None


# ---------------------------------------------------------------------------
# 7. Degenerate inputs
# ---------------------------------------------------------------------------


def test_fewer_than_two_rows_raise_with_a_message(space):
  rng = np.random.default_rng(16)
  one, many = _draw_rows(rng, 1), _draw_rows(rng, 10)
  with pytest.raises(ValueError, match="at least 2 reference"):
    nn_privacy(space, one, many, many)
  with pytest.raises(ValueError, match="at least 2 holdout"):
    nn_privacy(space, many, one, many)
  with pytest.raises(ValueError, match="synthetic"):
    nn_privacy(space, many, many, [])
  with pytest.raises(ValueError, match="k"):
    nn_privacy(space, many, many, many, k=0)
  with pytest.raises(ValueError, match="k"):
    density_coverage(
        np.zeros((3, 1), np.float32),
        np.zeros((3, 0), np.uint64),
        np.zeros((3, 1), np.float32),
        np.zeros((3, 0), np.uint64),
        k=3)


# ---------------------------------------------------------------------------
# summarize_nn
# ---------------------------------------------------------------------------


def _result(**overrides: Any) -> NNPrivacyResult:
  fields: dict[str, Any] = {
      "dcr_syn_r": np.array([0.0, 0.1, 0.2, 0.3]),
      "dcr_syn_h": np.array([0.1, 0.1, 0.1, 0.1]),
      "dcr_h_r": np.array([0.2, 0.2, 0.4, 0.4]),
      "nndr_syn": np.array([0.0, 0.5, 1.0, 1.0]),
      "nndr_h": np.array([0.5, 0.5, 1.0, 1.0]),
      # R rows first: 1.5 of the 4 synthetic rows sit nearest R (= 0.375).
      "nn_mass": np.array([1.0, 0.5, 0.0, 0.0, 1.0, 0.5, 1.0, 0.0]),
      "closer_to_r": 0.375,
      "n_syn": 4,
      "density": 1.02,
      "coverage": 0.96,
  }
  fields.update(overrides)
  return NNPrivacyResult(**fields)


def test_summarize_nn_values_and_wilson_interval():
  summary = summarize_nn(_result())
  assert summary["n_synthetic"] == 4
  assert summary["n_reference"] == 4
  assert summary["dcr_train_holdout_share"] == 0.375
  z = 1.959964
  w_lo, w_hi = wilson_interval(1.5, 4)  # type: ignore[arg-type]
  se_wilson = (w_hi - w_lo) / (2 * z)
  # sum_x (w_x - 4/8)^2 = 1.5 over 2n = 8 pooled rows.
  se_perm = math.sqrt((8 / 7) * 1.5 / (4 * 4 * 4))
  assert summary["dcr_train_holdout_share_se_wilson"] == pytest.approx(
      se_wilson)
  assert summary["dcr_train_holdout_share_se_perm"] == pytest.approx(se_perm)
  se = max(se_wilson, se_perm)
  assert summary["dcr_train_holdout_share_ci_low"] == pytest.approx(
      max(0.0, 0.375 - z * se))
  assert summary["dcr_train_holdout_share_ci_high"] == pytest.approx(
      min(1.0, 0.375 + z * se))
  assert summary["dcr_syn_r_p5"] == pytest.approx(0.015)
  assert summary["dcr_syn_r_p50"] == pytest.approx(0.15)
  assert summary["dcr_h_r_p5"] == pytest.approx(0.2)
  assert summary["dcr_p5_ratio"] == pytest.approx(0.075)
  assert summary["nndr_syn_p5"] == pytest.approx(0.075)
  assert summary["nndr_h_p5"] == pytest.approx(0.5)
  assert summary["nndr_p5_ratio"] == pytest.approx(0.15)
  assert summary["density"] == 1.02 and summary["coverage"] == 0.96


def test_summarize_nn_ratios_are_none_when_the_holdout_quantile_is_zero():
  summary = summarize_nn(
      _result(dcr_h_r=np.zeros(4), nndr_h=np.array([0.0, 0.0, 0.0, 1.0])))
  assert summary["dcr_p5_ratio"] is None
  assert summary["nndr_p5_ratio"] is None
  assert summary["dcr_h_r_p5"] == 0.0


def test_summarize_nn_histogram_payloads_match_the_profile_contract():
  profiles = summarize_nn(_result())["profiles"]
  assert set(profiles) == {"dcr_hist", "nndr_hist"}
  for kind in ("dcr_hist", "nndr_hist"):
    assert set(profiles[kind]) == {"synthetic", "holdout"}
    for payload in profiles[kind].values():
      assert set(payload) == {"edges", "counts", "p5", "p50", "n"}
      assert len(payload["edges"]) == 49
      assert payload["edges"][0] == pytest.approx(0.02)
      assert payload["edges"][-1] == pytest.approx(0.98)
      assert len(payload["counts"]) == 50
      assert sum(payload["counts"]) == payload["n"] == 4
      assert all(isinstance(c, int) for c in payload["counts"])
  syn_dcr = profiles["dcr_hist"]["synthetic"]
  # 0.0 -> bin 0; 0.1 -> (0.08, 0.10] = bin 4; 0.2 -> bin 9; 0.3 -> bin 14.
  assert [i for i, c in enumerate(syn_dcr["counts"]) if c] == [0, 4, 9, 14]
  assert profiles["nndr_hist"]["holdout"]["p50"] == pytest.approx(0.75)


def test_share_interval_uses_the_larger_of_wilson_and_permutation_se():
  # Synthetic mass spread evenly over every pooled row: any split gives
  # exactly one half, se_perm = 0, and the Wilson se sets the interval.
  spread = summarize_nn(_result(nn_mass=np.full(8, 0.5), closer_to_r=0.5))
  assert spread["dcr_train_holdout_share_se_perm"] == 0.0
  se_wilson = spread["dcr_train_holdout_share_se_wilson"]
  assert se_wilson > 0.0
  assert spread["dcr_train_holdout_share_ci_low"] == pytest.approx(0.5 -
                                                                   1.959964 *
                                                                   se_wilson)
  # All synthetic mass on one R row: clumped, the permutation se dominates.
  clumped = summarize_nn(
      _result(
          nn_mass=np.array([4.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
          closer_to_r=1.0))
  assert clumped["dcr_train_holdout_share_se_perm"] > clumped[
      "dcr_train_holdout_share_se_wilson"]
  assert clumped["dcr_train_holdout_share_ci_high"] == 1.0
  z = 1.959964
  assert clumped["dcr_train_holdout_share_ci_low"] == pytest.approx(
      max(0.0, 1.0 - z * clumped["dcr_train_holdout_share_se_perm"]))
  assert permutation_se(np.zeros(8), 0) == 0.0


def _null_blocks(rng: np.random.Generator,
                 n: int) -> tuple[np.ndarray, np.ndarray]:
  """A mixed generator on encoded blocks: continuous and atomic PIT values,
  a skewed and a long-tailed categorical, 5 % NULLs everywhere."""
  num = np.empty((n, 2), np.float32)
  num[:, 0] = rng.random(n)
  levels = np.array([0.1, 0.35, 0.6, 0.85], np.float32)
  num[:, 1] = levels[rng.choice(4, size=n, p=[0.2, 0.3, 0.3, 0.2])]
  num[rng.random((n, 2)) < 0.05] = np.nan
  cat = np.stack(
      [rng.choice(3, size=n, p=[0.6, 0.3, 0.1]),
       rng.zipf(1.6, n) % 20], axis=1).astype(np.uint64)
  cat[rng.random((n, 2)) < 0.05] = NULL_CODE
  return num, cat


@pytest.mark.slow
def test_share_se_matches_the_null_spread_of_the_share():
  # Under the null (R, H and synthetic from one generator), with 10 synthetic
  # rows per source row: the share's replicate SD is what se_perm reports
  # (120 replicates measured SD / mean se_perm = 0.92), while the Wilson
  # binomial se is about half of it (a gate on it fires far too often).
  shares, se_perm, se_wilson = [], [], []
  for seed in range(40):
    rng = np.random.default_rng(31_000 + seed)
    res = nn_privacy_encoded(
        _null_blocks(rng, 2000), _null_blocks(rng, 2000),
        _null_blocks(rng, 20_000))
    summary = summarize_nn(res)
    shares.append(summary["dcr_train_holdout_share"])
    se_perm.append(summary["dcr_train_holdout_share_se_perm"])
    se_wilson.append(summary["dcr_train_holdout_share_se_wilson"])
  empirical_sd = float(np.std(shares, ddof=1))
  reported_se = float(np.mean(np.maximum(se_perm, se_wilson)))
  assert 0.65 * reported_se <= empirical_sd <= 1.35 * reported_se
  assert all(p > w for p, w in zip(se_perm, se_wilson, strict=True))
  assert abs(float(np.mean(shares)) - 0.5) < 3 * reported_se / math.sqrt(40)
