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
"""Tests for `sdfb_evaluation.stats.detection` (Task 12).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest
from scipy.stats import norm, rankdata
from sklearn.metrics import roc_auc_score, roc_curve

from sdfb_evaluation.canonical import hash64
from sdfb_evaluation.stats.detection import DetectionColumn
from sdfb_evaluation.stats.detection import c2st_auc
from sdfb_evaluation.stats.detection import delong_ci
from sdfb_evaluation.stats.detection import featurize
from sdfb_evaluation.stats.detection import pmse_ratio
from sdfb_evaluation.stats.detection import roc_points
from sdfb_evaluation.stats.privacy import pit_mid_cdf
from sdfb_evaluation.stats.shapes import shape_of
from sdfb_evaluation.types import ColumnKind

_Z95 = float(norm.ppf(0.975))
_STATUSES = ("new", "paid", "shipped", "returned")
_STATUS_P = (0.1, 0.5, 0.3, 0.1)
_OTHER = 254.0

# ---------------------------------------------------------------------------
# Helpers: a small thelook-like `orders` table and its detection columns
# ---------------------------------------------------------------------------


def _orders(rng: np.random.Generator,
            n: int,
            *,
            shift_sd: float = 0.0,
            null_rate: float = 0.05,
            id_offset: int = 0,
            status_p: Sequence[float] = _STATUS_P) -> list[dict[str, Any]]:
  """`n` rows: a key, a normal amount (sd 10, optionally shifted by
  `shift_sd` sds), an integer age, a categorical status; amount has NULLs."""
  amount = rng.normal(50.0 + 10.0 * shift_sd, 10.0, n)
  amount_null = rng.random(n) < null_rate
  age = rng.integers(18, 90, n)
  status = rng.choice(len(_STATUSES), size=n, p=status_p)
  return [{
      "order_id": id_offset + i,
      "amount": None if amount_null[i] else float(amount[i]),
      "age": int(age[i]),
      "status": _STATUSES[status[i]],
  } for i in range(n)]


def _grid(rows: Sequence[dict[str, Any]], column: str) -> np.ndarray:
  """A 101-point source quantile grid for `column` (NULLs skipped)."""
  values = np.array([r[column] for r in rows if r[column] is not None],
                    dtype=np.float64)
  return np.quantile(values, np.linspace(0.0, 1.0, 101))


def _order_columns(src: Sequence[dict[str, Any]]) -> list[DetectionColumn]:
  return [
      DetectionColumn("order_id", ColumnKind.IDENTIFIER, is_key=True),
      DetectionColumn(
          "amount", ColumnKind.NUMERIC, is_key=False, grid=_grid(src,
                                                                 "amount")),
      DetectionColumn(
          "age", ColumnKind.NUMERIC, is_key=False, grid=_grid(src, "age")),
      DetectionColumn(
          "status",
          ColumnKind.CATEGORICAL,
          is_key=False,
          dictionary=tuple(hash64("status", s) for s in _STATUSES)),
  ]


def _naive_delong(y: np.ndarray, p: np.ndarray) -> tuple[float, float]:
  """DeLong et al. (1988) straight from the definition: the O(m·n) kernel
  psi(x, y) = 1[x > y] + 1/2·1[x = y] and its two structural components."""
  pos, neg = p[y == 1], p[y == 0]
  psi = (pos[:, None] > neg[None, :]) + 0.5 * (pos[:, None] == neg[None, :])
  v10, v01 = psi.mean(axis=1), psi.mean(axis=0)
  var = v10.var(ddof=1) / pos.size + v01.var(ddof=1) / neg.size
  return float(psi.mean()), float(var)


@pytest.fixture(name="null_table", scope="module")
def _null_table() -> tuple[np.ndarray, np.ndarray, list[int]]:
  """Source and synthetic drawn from the same generator, 1,000 rows each."""
  src = _orders(np.random.default_rng(11), 1000)
  syn = _orders(np.random.default_rng(12), 1000, id_offset=1_000_000)
  return featurize(src, syn, _order_columns(src))


@pytest.fixture(name="null_c2st", scope="module")
def _null_c2st(
    null_table: tuple[np.ndarray, np.ndarray, list[int]]
) -> tuple[float, float, float, np.ndarray]:
  x, y, cat_idx = null_table
  return c2st_auc(x, y, cat_idx, seed=3)


# ---------------------------------------------------------------------------
# The brief's list
# ---------------------------------------------------------------------------


def test_identical_distributions_are_not_detected(
    null_table: tuple[np.ndarray, np.ndarray, list[int]],
    null_c2st: tuple[float, float, float, np.ndarray]) -> None:
  x, y, cat_idx = null_table
  auc, lo, hi, oof = null_c2st
  assert 0.45 <= auc <= 0.58
  assert lo <= 0.5 <= hi
  assert oof.shape == y.shape
  assert np.all((oof >= 0.0) & (oof <= 1.0))
  # One null draw's pMSE ratio is chi^2_{k-1} / (k - 1) with k - 1 = 6 here
  # (sd 0.58), too wide for a fixed band; the mean of 8 independent null
  # pairs has sd ~0.2 around 1 (R31 E0), well inside [0.5, 1.6].
  ratios = [pmse_ratio(x, y, cat_idx, seed=3)[1]]
  for pair in range(7):
    src = _orders(np.random.default_rng(201 + 2 * pair), 1000)
    syn = _orders(np.random.default_rng(202 + 2 * pair), 1000)
    xp, yp, cp = featurize(src, syn, _order_columns(src))
    ratios.append(pmse_ratio(xp, yp, cp, seed=3)[1])
  assert 0.5 <= float(np.mean(ratios)) <= 1.6


def test_two_sd_shift_is_detected() -> None:
  # No NULLs here: a NULL amount carries no signal and would only dilute the
  # shift's Bayes-optimal AUC of Phi(2 / sqrt(2)) = 0.92.
  src = _orders(np.random.default_rng(21), 1000, null_rate=0.0)
  syn = _orders(np.random.default_rng(22), 1000, shift_sd=2.0, null_rate=0.0)
  x, y, cat_idx = featurize(src, syn, _order_columns(src))
  auc, lo, hi, _ = c2st_auc(x, y, cat_idx, seed=3)
  assert auc > 0.85
  assert lo < auc < hi
  ratio = pmse_ratio(x, y, cat_idx, seed=3)[1]
  assert ratio > 10.0


def test_key_column_is_excluded(
    null_table: tuple[np.ndarray, np.ndarray, list[int]],
    null_c2st: tuple[float, float, float, np.ndarray]) -> None:
  # The null table's keys differ trivially (source 0..999, synthetic from
  # 1,000,000), yet the key contributes no feature and the AUC stays near 1/2.
  x, _, cat_idx = null_table
  assert x.shape[1] == 3  # amount, age, status — no order_id
  assert cat_idx == [2]
  assert null_c2st[0] < 0.6
  # The same column declared a non-key numeric separates the tables outright
  # (300 rows a side: a perfectly separable fit never stops early).
  src = _orders(np.random.default_rng(11), 300)
  syn = _orders(np.random.default_rng(12), 300, id_offset=1_000_000)
  leaky = [
      DetectionColumn(
          "order_id",
          ColumnKind.NUMERIC,
          is_key=False,
          grid=_grid(src, "order_id")),
      *_order_columns(src)[1:],
  ]
  x2, y2, cat2 = featurize(src, syn, leaky)
  assert c2st_auc(x2, y2, cat2, seed=3)[0] > 0.99


def test_fixed_seed_is_deterministic(
    null_table: tuple[np.ndarray, np.ndarray, list[int]],
    null_c2st: tuple[float, float, float, np.ndarray]) -> None:
  x, y, cat_idx = null_table
  again = c2st_auc(x, y, cat_idx, seed=3)
  assert again[:3] == null_c2st[:3]
  assert np.array_equal(again[3], null_c2st[3])
  assert pmse_ratio(x, y, cat_idx, seed=3) == pmse_ratio(x, y, cat_idx, seed=3)
  src = _orders(np.random.default_rng(11), 1000)
  syn = _orders(np.random.default_rng(12), 1000, id_offset=1_000_000)
  x2, y2, _ = featurize(src, syn, _order_columns(src))
  assert np.array_equal(x, x2, equal_nan=True)
  assert np.array_equal(y, y2)
  # A different seed reshuffles the folds, so the out-of-fold scores move.
  assert not np.array_equal(c2st_auc(x, y, cat_idx, seed=4)[3], null_c2st[3])


def test_too_few_rows_raise() -> None:
  rng = np.random.default_rng(5)
  x = rng.random((30, 2))
  y = np.r_[np.zeros(15, dtype=int), np.ones(15, dtype=int)]
  with pytest.raises(ValueError, match="rows per class"):
    c2st_auc(x, y, [], seed=1)
  x40 = rng.random((40, 2))
  y40 = np.r_[np.zeros(20, dtype=int), np.ones(20, dtype=int)]
  with pytest.raises(ValueError, match="rows per class"):
    c2st_auc(x40, y40, [], folds=11, seed=1)
  with pytest.raises(ValueError, match="both classes"):
    c2st_auc(x40, np.ones(40, dtype=int), [], seed=1)
  with pytest.raises(ValueError, match="folds"):
    c2st_auc(x40, y40, [], folds=1, seed=1)
  with pytest.raises(ValueError, match="both classes"):
    pmse_ratio(x40, np.zeros(40, dtype=int), [], seed=1)


def test_minimum_table_runs() -> None:
  # 20 rows per class is the floor: every training fold still leaves a
  # stratified early-stopping validation split of >= 2 rows.
  rng = np.random.default_rng(6)
  x = rng.random((40, 2))
  y = np.r_[np.zeros(20, dtype=int), np.ones(20, dtype=int)]
  for folds in (2, 3, 5, 10):
    auc, lo, hi, oof = c2st_auc(x, y, [], folds=folds, seed=1)
    assert 0.0 <= lo <= auc <= hi <= 1.0
    assert oof.shape == (40,)


# ---------------------------------------------------------------------------
# AUC and DeLong
# ---------------------------------------------------------------------------


def test_delong_hand_computed_example() -> None:
  # m = n = 2: AUC = 3/4; placements V10 = (1/2, 1), V01 = (1, 1/2); both
  # sample variances are 1/8, so Var = 1/8/2 + 1/8/2 = 1/8.
  y = np.array([0, 0, 1, 1])
  p = np.array([0.1, 0.4, 0.35, 0.8])
  lo, hi = delong_ci(y, p)
  half = _Z95 * math.sqrt(0.125)
  assert lo == pytest.approx(0.75 - half, abs=1e-12)
  assert hi == 1.0  # 0.75 + 0.693 clipped to 1


def test_delong_matches_the_definition_with_ties() -> None:
  rng = np.random.default_rng(8)
  y = np.r_[np.zeros(50, dtype=int), np.ones(40, dtype=int)]
  p = np.round(np.r_[rng.normal(0.0, 1.0, 50), rng.normal(0.7, 1.0, 40)], 1)
  auc, var = _naive_delong(y, p)
  lo, hi = delong_ci(y, p, alpha=0.1)
  z90 = float(norm.ppf(0.95))
  assert lo == pytest.approx(max(0.0, auc - z90 * math.sqrt(var)), abs=1e-12)
  assert hi == pytest.approx(min(1.0, auc + z90 * math.sqrt(var)), abs=1e-12)
  assert auc == pytest.approx(roc_auc_score(y, p), abs=1e-12)


def test_delong_agrees_with_a_stratified_bootstrap() -> None:
  rng = np.random.default_rng(9)
  m = n = 300
  pos, neg = rng.normal(1.0, 1.0, m), rng.normal(0.0, 1.0, n)
  y = np.r_[np.zeros(n, dtype=int), np.ones(m, dtype=int)]
  lo, hi = delong_ci(y, np.r_[neg, pos])
  sd_delong = (hi - lo) / (2.0 * _Z95)
  boot = 2000
  scores = np.concatenate(
      [pos[rng.integers(0, m, (boot, m))], neg[rng.integers(0, n, (boot, n))]],
      axis=1)
  ranks = rankdata(scores, axis=1)
  aucs = (ranks[:, :m].sum(axis=1) - m * (m + 1) / 2.0) / (m * n)
  assert sd_delong == pytest.approx(aucs.std(ddof=1), rel=0.15)


def test_c2st_auc_is_the_pooled_out_of_fold_auc(
    null_table: tuple[np.ndarray, np.ndarray, list[int]],
    null_c2st: tuple[float, float, float, np.ndarray]) -> None:
  _, y, _ = null_table
  auc, lo, hi, oof = null_c2st
  assert auc == pytest.approx(roc_auc_score(y, oof), abs=1e-12)
  assert (lo, hi) == delong_ci(y, oof)


def test_delong_rejects_a_single_class() -> None:
  with pytest.raises(ValueError, match="both classes"):
    delong_ci(np.ones(5, dtype=int), np.linspace(0, 1, 5))
  with pytest.raises(ValueError, match="alpha"):
    delong_ci(np.array([0, 0, 1, 1]), np.array([.1, .2, .3, .4]), alpha=1.5)


# ---------------------------------------------------------------------------
# Featurization
# ---------------------------------------------------------------------------


def test_numeric_and_temporal_use_the_source_grid_mid_cdf() -> None:
  grid = np.array([0.0, 10.0, 20.0, 20.0, 40.0])
  t0 = datetime(2024, 1, 1, tzinfo=UTC)
  # Temporal grids are UNIX microseconds, like the plan's (Ruling R54).
  t0_us = (t0 - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)
  t_grid = np.array([t0_us + 86_400_000_000.0 * i for i in range(5)])
  src: list[dict[str, Any]] = [{
      "v": 0,
      "t": t0
  }, {
      "v": 20.0,
      "t": None
  }, {
      "v": 5.0,
      "t": datetime(2024, 1, 3, tzinfo=UTC)
  }]
  syn: list[dict[str, Any]] = [{
      "v": None,
      "t": t0
  }, {
      "v": 100,
      "t": t0
  }, {
      "v": -3.0,
      "t": t0
  }]
  cols = [
      DetectionColumn("v", ColumnKind.NUMERIC, is_key=False, grid=grid),
      DetectionColumn("t", ColumnKind.TEMPORAL, is_key=False, grid=t_grid),
  ]
  x, y, cat_idx = featurize(src, syn, cols)
  assert not cat_idx
  assert y.tolist() == [0, 0, 0, 1, 1, 1]
  # 20 sits on the atom grid[2..3] -> (0.5 + 0.75) / 2; 5 interpolates.
  expected_v = [0.0, 0.625, 0.125, math.nan, 1.0, 0.0]
  np.testing.assert_allclose(x[:, 0], expected_v, equal_nan=True)
  np.testing.assert_allclose(
      x[:, 1], [0.0, math.nan, 0.5, 0.0, 0.0, 0.0], equal_nan=True)
  # The one PIT implementation: privacy's public entry point.
  np.testing.assert_array_equal(
      x[:, 0], pit_mid_cdf([0, 20.0, 5.0, None, 100, -3.0], grid, column="v"))


def test_categorical_only_table_uses_dictionary_codes() -> None:
  dictionary = (hash64("status", "paid"), hash64("status", "new"))
  cols = [
      DetectionColumn(
          "status", ColumnKind.CATEGORICAL, is_key=False,
          dictionary=dictionary),
      DetectionColumn(
          "gift",
          ColumnKind.BOOLEAN,
          is_key=False,
          dictionary=(hash64("gift", False), hash64("gift", True))),
  ]
  src = [{"status": "new", "gift": True}, {"status": "paid", "gift": None}]
  syn = [{"status": "lost", "gift": False}, {"status": None, "gift": True}]
  x, y, cat_idx = featurize(src, syn, cols)
  assert cat_idx == [0, 1]
  np.testing.assert_array_equal(x[:, 0], [1.0, 0.0, _OTHER, math.nan])
  np.testing.assert_array_equal(x[:, 1], [1.0, math.nan, 0.0, 1.0])
  assert y.tolist() == [0, 0, 1, 1]


def test_dictionary_that_no_source_value_hits_raises() -> None:
  # Hashed under the wrong column name: a plan/hash64 mismatch.
  wrong = DetectionColumn(
      "status",
      ColumnKind.CATEGORICAL,
      is_key=False,
      dictionary=tuple(hash64("state", s) for s in _STATUSES))
  src = [{"status": "new"}, {"status": "paid"}, {"status": None}]
  syn = [{"status": "new"}, {"status": "paid"}, {"status": "lost"}]
  with pytest.raises(ValueError, match=r"'status'.*dictionary"):
    featurize(src, syn, [wrong])
  # No evidence either way: an all-NULL source side, or an empty dictionary.
  nulls = [{"status": None}] * 3
  x, _, _ = featurize(nulls, syn, [wrong])
  assert np.isnan(x[:3, 0]).all()
  empty = DetectionColumn(
      "status", ColumnKind.CATEGORICAL, is_key=False, dictionary=())
  x, _, _ = featurize(src, syn, [empty])
  np.testing.assert_array_equal(x[:, 0],
                                [_OTHER, _OTHER, math.nan] + [_OTHER] * 3)


def test_categorical_only_c2st_separates_a_mix_shift() -> None:
  status_dict = tuple(hash64("status", s) for s in _STATUSES)
  cols = [
      DetectionColumn(
          "status",
          ColumnKind.CATEGORICAL,
          is_key=False,
          dictionary=status_dict)
  ]
  src = _orders(np.random.default_rng(31), 800)
  same = _orders(np.random.default_rng(32), 800)
  moved = _orders(np.random.default_rng(33), 800, status_p=(0.6, 0.2, 0.1, 0.1))
  x0, y0, c0 = featurize(src, same, cols)
  assert c0 == [0]
  assert c2st_auc(x0, y0, c0, seed=1)[0] < 0.58
  x1, y1, c1 = featurize(src, moved, cols)
  auc, lo, _, _ = c2st_auc(x1, y1, c1, seed=1)
  assert auc > 0.70
  assert lo > 0.5
  assert pmse_ratio(x1, y1, c1, seed=1)[1] > 10.0


def test_text_and_identifier_features() -> None:
  masks = (shape_of("ab12@example.com"), shape_of("12345"))
  cols = [
      DetectionColumn("email", ColumnKind.TEXT, is_key=False, head_masks=masks),
      DetectionColumn(
          "sku", ColumnKind.IDENTIFIER, is_key=False, head_masks=masks),
  ]
  src = [{"email": "ab12@example.com", "sku": 12345}]
  syn = [{"email": "Jo Ann@example.com", "sku": None}]
  x, y, cat_idx = featurize(src, syn, cols)
  assert x.shape == (2, 14)
  assert cat_idx == [1, 8]
  # [length, head-mask index, digit, upper, lower, space, punct]
  np.testing.assert_array_equal(x[0, :7], [16, 0, 1, 0, 1, 0, 1])
  np.testing.assert_array_equal(x[1, :7], [18, 2, 0, 1, 1, 1, 1])  # 2 = other
  np.testing.assert_array_equal(x[0, 7:],
                                [5, 1, 1, 0, 0, 0, 0])  # int -> "12345"
  assert np.isnan(x[1, 7:]).all()  # NULL -> every feature missing
  assert y.tolist() == [0, 1]


def test_equal_n_trims_to_the_first_rows() -> None:
  rng = np.random.default_rng(41)
  src = _orders(rng, 30)
  syn = _orders(rng, 20)
  cols = _order_columns(src)
  x, y, _ = featurize(src, syn, cols)
  assert x.shape == (40, 3)
  assert y.tolist() == [0] * 20 + [1] * 20
  x_head, _, _ = featurize(src[:20], syn, cols)
  np.testing.assert_array_equal(x, x_head)
  x_short, y_short, _ = featurize(src, syn[:5], cols)
  assert x_short.shape == (10, 3)
  assert y_short.tolist() == [0] * 5 + [1] * 5


def test_nested_and_key_columns_need_no_plan_data() -> None:
  cols = [
      DetectionColumn("items", ColumnKind.NESTED, is_key=False),
      DetectionColumn("user_id", ColumnKind.NUMERIC, is_key=True),
      DetectionColumn(
          "age", ColumnKind.NUMERIC, is_key=False, grid=np.array([0.0, 100.0])),
  ]
  rows = [{"items": [{"a": 1}], "user_id": 7, "age": 50}]
  x, _, cat_idx = featurize(rows, rows, cols)
  assert x.shape == (2, 1)
  assert not cat_idx


def test_detection_column_validates_its_plan_data() -> None:
  with pytest.raises(ValueError, match="grid"):
    DetectionColumn("amount", ColumnKind.NUMERIC, is_key=False)
  with pytest.raises(ValueError, match="dictionary"):
    DetectionColumn("status", ColumnKind.CATEGORICAL, is_key=False)
  with pytest.raises(ValueError, match="254"):
    DetectionColumn(
        "status",
        ColumnKind.CATEGORICAL,
        is_key=False,
        dictionary=tuple(range(255)))
  with pytest.raises(ValueError, match="duplicate"):
    DetectionColumn(
        "status", ColumnKind.CATEGORICAL, is_key=False, dictionary=(1, 1))
  with pytest.raises(ValueError, match="head_masks"):
    DetectionColumn("email", ColumnKind.TEXT, is_key=False)
  bad_grid = DetectionColumn(
      "amount", ColumnKind.NUMERIC, is_key=False, grid=np.array([3.0, 1.0]))
  with pytest.raises(ValueError, match="non-decreasing"):
    featurize([{"amount": 1.0}], [{"amount": 2.0}], [bad_grid])


# ---------------------------------------------------------------------------
# NaN handling and the pMSE ratio
# ---------------------------------------------------------------------------


def test_missingness_shift_is_detected() -> None:
  src = _orders(np.random.default_rng(51), 1000, null_rate=0.05)
  syn = _orders(np.random.default_rng(52), 1000, null_rate=0.4)
  x, y, cat_idx = featurize(src, syn, _order_columns(src))
  assert np.isnan(x[:, 0]).any()
  assert pmse_ratio(x, y, cat_idx, seed=1)[1] > 10.0
  assert c2st_auc(x, y, cat_idx, seed=1)[0] > 0.6


def _check_ratio_and_ceiling(result: tuple[float, float, int, float],
                             y: np.ndarray) -> None:
  """ratio = pMSE / E0, E0 = (k - 1) c (1 - c) / N, ceiling = N / (k - 1)."""
  pmse, ratio, k, ceiling = result
  n = y.size
  c = y.sum() / n
  assert ratio == pytest.approx(pmse / ((k - 1) * c * (1.0 - c) / n), rel=1e-12)
  assert ceiling == pytest.approx(n / (k - 1), rel=1e-12)
  assert ratio <= ceiling


def test_pmse_parameter_count() -> None:
  rng = np.random.default_rng(61)
  n = 400
  num1 = rng.normal(size=n)
  num2 = rng.normal(size=n)
  num2[rng.random(n) < 0.2] = np.nan
  cat = rng.integers(0, 3, n).astype(float)
  const = np.full(n, 7.0)
  x = np.c_[num1, num2, cat, const]
  y = np.r_[np.zeros(300, dtype=int), np.ones(100, dtype=int)]
  result = pmse_ratio(x, y, [2], seed=1)
  # intercept + num1 + num2 + num2's missing indicator + (3 - 1) cat levels;
  # the constant column carries no parameter.
  assert result[2] == 6
  _check_ratio_and_ceiling(result, y)
  assert result[0] == pytest.approx(pmse_ratio(x[:, :3], y, [2], seed=1)[0])


def test_pmse_shared_null_pattern_counts_once() -> None:
  # A text column's seven features share one null pattern: the length, a
  # flag and the mask index are all NaN on the same rows.
  rng = np.random.default_rng(62)
  n = 400
  null = rng.random(n) < 0.3
  length = rng.integers(3, 30, n).astype(float)
  flag = (rng.random(n) < 0.5).astype(float)
  mask = rng.integers(0, 2, n).astype(float)
  for col in (length, flag, mask):
    col[null] = np.nan
  x = np.c_[length, mask, flag]
  y = np.r_[np.zeros(200, dtype=int), np.ones(200, dtype=int)]
  result = pmse_ratio(x, y, [1], seed=1)
  # intercept + length + flag + ONE missing indicator + mask level 1 (level 0
  # is the reference; the NULL level duplicates the indicator).
  assert result[2] == 5
  _check_ratio_and_ceiling(result, y)


def test_pmse_pools_rare_levels() -> None:
  # N = 400, so a level needs max(20, ceil(400 / 1000)) = 20 rows: codes
  # 0..2 keep their own level (120 rows each), the 35 single-row codes share
  # one "rare" level, and NULL keeps its own level even though it is rare.
  rng = np.random.default_rng(63)
  codes = np.r_[np.repeat([0.0, 1.0, 2.0], 120),
                np.arange(3.0, 38.0),
                np.full(5, np.nan)]
  x = np.c_[rng.permutation(codes), rng.normal(size=400)]
  y = np.r_[np.zeros(200, dtype=int), np.ones(200, dtype=int)]
  result = pmse_ratio(x, y, [0], seed=1)
  # intercept + codes 1, 2 (0 is the reference) + rare + NULL + the numeric;
  # without pooling it would be 1 + 37 + 1 + 1 = 40.
  assert result[2] == 6
  _check_ratio_and_ceiling(result, y)


def test_pmse_rare_floor_scales_with_n() -> None:
  # Two 25-row codes keep their own levels at N = 2,000 (floor 20) and are
  # pooled into one at N = 30,000 (floor ceil(30,000 / 1,000) = 30).
  for n, expected_k in ((2000, 4), (30_000, 3)):
    rng = np.random.default_rng(64)
    half = (n - 50) // 2
    codes = np.r_[np.zeros(half),
                  np.ones(n - 50 - half),
                  np.full(25, 2.0),
                  np.full(25, 3.0)]
    y = np.r_[np.zeros(n // 2, dtype=int), np.ones(n - n // 2, dtype=int)]
    result = pmse_ratio(rng.permutation(codes)[:, np.newaxis], y, [0], seed=1)
    assert result[2] == expected_k, n
    _check_ratio_and_ceiling(result, y)


def _separable_probe(levels: int) -> tuple[np.ndarray, np.ndarray, list[int]]:
  """1,000 rows a side separated by a 10-sd shift in one numeric feature,
  plus 20 categoricals drawn uniformly from `levels` codes on both sides."""
  rng = np.random.default_rng(65)
  numeric = np.r_[rng.normal(0.0, 1.0, 1000), rng.normal(10.0, 1.0, 1000)]
  cats = rng.integers(0, levels, (2000, 20)).astype(float)
  y = np.r_[np.zeros(1000, dtype=int), np.ones(1000, dtype=int)]
  return np.c_[numeric, cats], y, list(range(1, 21))


def test_pmse_ceiling_on_a_separable_table() -> None:
  # 20 x 255 levels would be k - 1 = 5,081 > N and a ceiling of 0.39: the
  # separable table would read "pass". Every level has ~8 rows, below the
  # floor of 20, so pooling leaves k tiny and the ratio fails loudly.
  x, y, cat_idx = _separable_probe(255)
  result = pmse_ratio(x, y, cat_idx, seed=1)
  assert result[2] <= 10
  assert result[1] >= 10.0 and result[3] >= 10.0
  _check_ratio_and_ceiling(result, y)
  # 50 levels of ~40 rows each survive pooling: k - 1 = 1 + 20 * 49 = 981
  # and the ceiling N / (k - 1) = 2.04 is below the fail threshold of 10,
  # which the scorer turns into "not evaluated" (Ruling R33).
  x, y, cat_idx = _separable_probe(50)
  result = pmse_ratio(x, y, cat_idx, seed=1)
  assert result[2] == 982
  assert result[3] < 10.0
  _check_ratio_and_ceiling(result, y)


def _null_ratios(regime: str, reps: int = 60) -> np.ndarray:
  """pMSE ratios of `reps` null tables: 500 source rows of 10 normal
  features, and 500 synthetic rows either drawn independently from the same
  distribution or resampled from the source rows themselves."""
  ratios = []
  for rep in range(reps):
    rng = np.random.default_rng(1000 + rep)
    src = rng.normal(size=(500, 10))
    syn = (
        rng.normal(size=(500, 10))
        if regime == "independent" else src[rng.integers(0, 500, 500)])
    y = np.r_[np.zeros(500, dtype=int), np.ones(500, dtype=int)]
    ratios.append(pmse_ratio(np.r_[src, syn], y, [], seed=1)[1])
  return np.array(ratios)


def test_pmse_null_calibration() -> None:
  # E0 = (k - 1) c (1 - c) / N is the null for two independent draws from
  # one distribution: the ratio averages 1 (Ruling R31). Synthetic rows
  # resampled from the source rows themselves (only the synthetic side
  # random, Snoke et al.'s regime) average 1 - c = 0.5 instead.
  # (k - 1 = 10; each band spans about +-3.4 standard errors of its mean.)
  assert 0.8 <= _null_ratios("independent").mean() <= 1.2
  assert 0.4 <= _null_ratios("resampled").mean() <= 0.6


def test_pmse_needs_an_informative_feature() -> None:
  x = np.c_[np.full(40, 2.0), np.full(40, np.nan)]
  y = np.r_[np.zeros(20, dtype=int), np.ones(20, dtype=int)]
  with pytest.raises(ValueError, match="non-constant"):
    pmse_ratio(x, y, [], seed=1)


# ---------------------------------------------------------------------------
# ROC profile
# ---------------------------------------------------------------------------


def test_roc_points_endpoints_and_monotone() -> None:
  rng = np.random.default_rng(71)
  y = np.r_[np.zeros(500, dtype=int), np.ones(500, dtype=int)]
  p = np.r_[rng.normal(0.0, 1.0, 500), rng.normal(1.0, 1.0, 500)]
  pts = roc_points(y, p)
  assert len(pts) <= 101
  assert pts[0] == (0.0, 0.0)
  assert pts[-1] == (1.0, 1.0)
  fpr = np.array([a for a, _ in pts])
  tpr = np.array([b for _, b in pts])
  assert np.all(np.diff(fpr) >= 0) and np.all(np.diff(tpr) >= 0)
  # Every thinned point lies on the exact curve.
  full_fpr, full_tpr, _ = roc_curve(y, p, drop_intermediate=False)
  exact = set(zip(full_fpr.tolist(), full_tpr.tolist(), strict=True))
  assert all(pt in exact for pt in pts)
  assert len(roc_points(y, p, max_points=5)) <= 5


def test_roc_points_small_input_is_the_exact_curve() -> None:
  y = np.array([0, 0, 1, 1, 0, 1])
  p = np.array([0.1, 0.4, 0.4, 0.8, 0.2, 0.9])
  full_fpr, full_tpr, _ = roc_curve(y, p, drop_intermediate=False)
  assert roc_points(y, p) == list(
      zip(full_fpr.tolist(), full_tpr.tolist(), strict=True))
  perfect = roc_points(np.array([0, 0, 1, 1]), np.array([.1, .2, .8, .9]))
  assert (0.0, 1.0) in perfect
  with pytest.raises(ValueError, match="max_points"):
    roc_points(y, p, max_points=1)
  with pytest.raises(ValueError, match="both classes"):
    roc_points(np.zeros(3, dtype=int), np.array([.1, .2, .3]))
