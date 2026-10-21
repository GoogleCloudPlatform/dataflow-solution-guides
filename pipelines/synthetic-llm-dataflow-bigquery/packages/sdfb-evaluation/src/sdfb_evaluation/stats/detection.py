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
"""Detection: a classifier two-sample test and the propensity-score MSE,
feeding `table.detection_auc` and `table.pmse_ratio`
(`src/sdfb_evaluation/catalogue/metrics.yaml`).

Both ask one question: can a model tell a synthetic row from a source row?
The caller (Task 24) takes a deterministic sample of each side, and
`featurize` turns the two into one labelled matrix (`y = 1` for synthetic),
trimmed to equal n per class (the first rows of the larger side), with
columns taken from the evaluation plan, never from the rows themselves:

  ====================  ===============================================
  column kind           features (`NaN` = NULL; both models take it)
  ====================  ===============================================
  numeric, temporal     the SOURCE grid's mid-CDF PIT `u` in `[0, 1]`
                        (`stats.privacy.pit_mid_cdf`, Rulings R16/R24)
  categorical, boolean  the `hash64` code's index in the top-254 source
                        dictionary, 254 for any other value (categorical)
  text, identifier      length; head-mask index (`shapes.shape_of` in
                        `head_masks`, else `len(head_masks)`, categorical);
                        five character-class presence flags
  key, nested           none: a key differs between the tables by
                        construction, so it would detect them trivially
  ====================  ===============================================

`c2st_auc` is the classifier two-sample test of Lopez-Paz & Oquab (2017): a
gradient-boosted classifier (`HistGradientBoostingClassifier`, native
categorical splits and missing values) predicts every row out of fold under
stratified k-fold, and the metric is the ROC AUC of the pooled out-of-fold
P(synthetic). Rank-based and tie-aware, it is the Mann-Whitney probability
that a synthetic row outscores a source row, ties counting one half; 0.5
means the classifier cannot tell the tables apart. Its interval is DeLong's:
the AUC variance from the two samples' placement values (DeLong, DeLong &
Clarke-Pearson 1988), computed from midranks in O(N log N) (Sun & Xu 2014),
with a normal interval clipped to `[0, 1]`.

`pmse_ratio` is the propensity-score mean squared error of Woo et al.
(2009), standardised by its null expectation as Snoke et al. (2018) do: a
logistic regression on all rows estimates each row's propensity `p_i` of
being synthetic; `pMSE = mean((p_i - c)^2)` with `c = n_syn / N`, divided
by `E0 = (k - 1) c (1 - c) / N` for a model with `k` parameters (Ruling
R31). That `E0` is the null for our sampling regime, where the source
sample and the synthetic rows are two independent draws from one
distribution: the fitted propensities' spread `sum_i (p_i - c)^2` is the
explained sum of squares of a `k`-parameter model of a label with variance
`c (1 - c)`, which averages `(k - 1) c (1 - c)` under the null. Snoke et
al.'s `(k - 1)(1 - c)^2 c / N` is the null when the synthetic rows are
drawn from the source sample itself (only the synthetic side is random);
here it would put a perfect generator at `1 / (1 - c)`, i.e. 2 at the
equal n `featurize` builds. `test_pmse_null_calibration` pins both
regimes: the ratio averages 1 for independent draws and `1 - c` for
synthetic rows resampled from the source rows. The design matrix:

  - a numeric feature (every column not in `cat_idx`) is standardised, with
    a NULL imputed at the column mean (0 after standardising), plus a 0/1
    missing indicator when some but not all of its values are NULL;
  - a categorical feature is one-hot coded over its present codes, NULL a
    level of its own; codes with fewer than `max(20, ceil(N / 1000))` rows
    (both sides together) share one pooled "rare" level (Ruling R33); the
    lowest remaining code (else the rare level) is the reference level the
    intercept absorbs;
  - constant columns and columns identical to an earlier one are dropped
    (a text column's seven features share one NULL pattern, so its missing
    indicators and the mask's NULL level collapse onto one column).

`k` is the intercept plus the design's columns, exactly the number of
parameters the model fits. Collinearity beyond exact duplicates (one
column a function of another) is not detected; it overstates `k`, which
lowers the ratio, the safe direction for a lower-better gate. The ratio
has a hard ceiling: `pMSE <= c (1 - c)` (every propensity at 0 or 1), so
`pMSE / E0 <= N / (k - 1)`, and a wide design can make even a perfectly
separable table read below the fail threshold. Pooling rare levels caps
`k`, and `pmse_ratio` returns `k` and the ceiling so the scorer can mark
the metric not evaluated when the ceiling sits below the fail threshold
(Ruling R33). A light
ridge (scikit-learn's default `C = 1`) keeps quasi-separated codes (a value
only one table has) finite; against the Fisher information of the
thousands of rows the evaluation samples its shrinkage is negligible.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy import sparse
from scipy.stats import norm, rankdata
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold

from sdfb_evaluation.canonical import canonical_value, hash64
from sdfb_evaluation.stats.privacy import pit_mid_cdf
from sdfb_evaluation.stats.shapes import char_class_presence, shape_of
from sdfb_evaluation.types import ColumnKind

# Dictionary codes 0..253 plus the shared "other" code 254: at most 255
# non-NULL categories, HistGradientBoostingClassifier's categorical
# cardinality limit at its default `max_bins = 255`.
_MAX_CODEBOOK = 254
_OTHER_CODE = 254
# The five character classes, in `shapes.char_class_presence` order.
_TEXT_CLASSES = ("digit", "upper", "lower", "space", "punct")
# A text/identifier column's features: length, head-mask index, 5 flags.
_TEXT_WIDTH = 2 + len(_TEXT_CLASSES)
_MASK_OFFSET = 1

_HGB_MAX_ITER = 200
_MIN_FOLDS = 2
# Rows per class below which the test does not run. Beyond `2 * folds`
# (two rows of each class per fold), 20 keeps every training fold at >= 20
# rows, so early stopping's stratified 10 % validation split still holds
# >= 2 rows (one per class) at any `folds >= 2`.
_MIN_ROWS_PER_CLASS = 20
_ALPHA = 0.05
# DeLong's sample variances of the placement values need two of each.
_MIN_DELONG_ROWS = 2
_PMSE_MAX_ITER = 1000
# A categorical/head-mask level keeps its own pMSE parameter only with at
# least max(20, ceil(N / 1000)) rows over both sides; rarer levels share one
# pooled "rare" level (Ruling R33), which caps k. NULL keeps its own level.
_MIN_LEVEL_ROWS = 20
_ROWS_PER_LEVEL_ROW = 1000
_NULL_LEVEL = -1.0
_RARE_LEVEL = -2.0
_MIN_ROC_POINTS = 2
_MATRIX_NDIM = 2

_NUMERIC_KINDS = frozenset({ColumnKind.NUMERIC, ColumnKind.TEMPORAL})
_CATEGORICAL_KINDS = frozenset({ColumnKind.CATEGORICAL, ColumnKind.BOOLEAN})


def _codebook(column: str, label: str,
              entries: Sequence[Any] | None) -> dict[Any, int]:
  """`{entry: index}` for a dictionary or head-mask list, validated."""
  if entries is None:
    raise ValueError(f"detection column {column!r} needs its {label} "
                     "from the evaluation plan")
  if len(entries) > _MAX_CODEBOOK:
    raise ValueError(
        f"detection column {column!r}: {label} has {len(entries)} entries, "
        f"at most {_MAX_CODEBOOK} (code {_OTHER_CODE} is 'other')")
  index = {entry: i for i, entry in enumerate(entries)}
  if len(index) != len(entries):
    raise ValueError(
        f"detection column {column!r}: {label} has duplicate entries")
  return index


def _as_text(value: Any) -> str | None:
  """A cell as text: strings as-is, anything else via `canonical_value`."""
  if isinstance(value, str):
    return value
  canonical = canonical_value(value)
  return None if canonical is None else str(canonical)


@dataclass(frozen=True, eq=False)
class DetectionColumn:
  """One plan column as detection sees it (Ruling R30; Task 24 adapts the
  plan's columns to it).

  `grid` is the source quantile grid of a numeric/temporal column (temporal
  in UNIX microseconds, the plan's scale — Ruling R54; a TIME column in
  microseconds since midnight), `dictionary` the `hash64` codes of a
  categorical/boolean column's top source values (at most 254, most
  frequent first), `head_masks` the `shapes.shape_of` masks a
  text/identifier column keeps as their own category (at most 254); cells
  are read on the same scale (`stats.privacy.pit_mid_cdf`, through
  `canonical.numeric_value`). A key or nested column is never a
  feature and needs none of them; a feature column without the one its kind
  needs raises `ValueError`. Equality is identity (`eq=False`: comparing
  grids elementwise is not a truth value).
  """
  name: str
  kind: ColumnKind
  is_key: bool
  grid: np.ndarray | None = None
  dictionary: tuple[int, ...] | None = None
  head_masks: tuple[str, ...] | None = None
  _index: Mapping[Any, int] = field(
      init=False, repr=False, default_factory=dict)

  def __post_init__(self) -> None:
    kind = ColumnKind(self.kind)
    object.__setattr__(self, "kind", kind)
    if not self.is_feature:
      return
    if kind in _NUMERIC_KINDS:
      if self.grid is None:
        raise ValueError(f"detection column {self.name!r} ({kind}) needs the "
                         "source quantile grid")
    elif kind in _CATEGORICAL_KINDS:
      object.__setattr__(self, "_index",
                         _codebook(self.name, "dictionary", self.dictionary))
    else:
      object.__setattr__(self, "_index",
                         _codebook(self.name, "head_masks", self.head_masks))

  @property
  def is_feature(self) -> bool:
    """Whether the column contributes features (not a key, not nested)."""
    return not self.is_key and self.kind is not ColumnKind.NESTED

  @property
  def categorical_offsets(self) -> tuple[int, ...]:
    """The categorical positions within this column's `encode` block."""
    if self.kind in _NUMERIC_KINDS:
      return ()
    if self.kind in _CATEGORICAL_KINDS:
      return (0,)
    return (_MASK_OFFSET,)

  def encode(self, values: Sequence[Any]) -> np.ndarray:
    """The `(len(values), width)` float64 features of this column's cells."""
    if self.kind in _NUMERIC_KINDS:
      return pit_mid_cdf(values, self.grid, column=self.name)[:, np.newaxis]
    if self.kind in _CATEGORICAL_KINDS:
      codes = [
          math.nan if v is None else self._index.get(
              hash64(self.name, v), _OTHER_CODE) for v in values
      ]
      return np.array(codes, dtype=np.float64)[:, np.newaxis]
    out = np.full((len(values), _TEXT_WIDTH), np.nan)
    other = len(self._index)
    for i, value in enumerate(values):
      text = _as_text(value)
      if text is None:
        continue
      flags = char_class_presence(text)
      out[i, 0] = len(text)
      out[i, _MASK_OFFSET] = self._index.get(shape_of(text), other)
      out[i, _MASK_OFFSET + 1:] = [flags[c] for c in _TEXT_CLASSES]
    return out


def _check_dictionary_hits(column: DetectionColumn,
                           source_codes: np.ndarray) -> None:
  """`ValueError` when a categorical column's non-empty dictionary matches
  none of its non-NULL source values (a plan/`hash64` mismatch)."""
  if column.kind not in _CATEGORICAL_KINDS or not column.dictionary:
    return
  present = source_codes[~np.isnan(source_codes)]
  if present.size and np.all(present == _OTHER_CODE):
    raise ValueError(
        f"detection column {column.name!r}: none of its {present.size} "
        "non-NULL source values is in its dictionary; the plan's hashes do "
        "not match hash64(column, value)")


def featurize(
    rows_src: Sequence[Mapping[str, Any]],
    rows_syn: Sequence[Mapping[str, Any]],
    columns: Sequence[DetectionColumn],
) -> tuple[np.ndarray, np.ndarray, list[int]]:
  """`(x, y, cat_idx)`: float64 features, int labels (1 = synthetic) and the
  categorical feature indices, over the first `min(len(rows_src),
  len(rows_syn))` rows of each side (source rows first).

  Trimming keeps the FIRST rows of the larger side, so callers must pass
  rows in an order unrelated to their content (e.g. Task 24's BottomK hash
  order), never sorted by a column. Every key column must be flagged
  `is_key`, foreign keys included: a synthetic foreign key points at
  synthetic parent keys and a source one at source keys, so either would
  detect the tables trivially.

  Key and nested columns are skipped; a column absent from a row reads as
  NULL; a numeric cell with no numeric reading raises `ValueError` (see
  `privacy.pit_mid_cdf`), as does an invalid grid. So does a categorical
  column whose non-empty dictionary no non-NULL source value hits: the
  plan's hashes do not match `hash64(column, value)`.
  """
  n = min(len(rows_src), len(rows_syn))
  rows = [*rows_src[:n], *rows_syn[:n]]
  blocks: list[np.ndarray] = []
  cat_idx: list[int] = []
  width = 0
  for column in columns:
    if not column.is_feature:
      continue
    block = column.encode([row.get(column.name) for row in rows])
    _check_dictionary_hits(column, block[:n, 0])
    cat_idx.extend(width + offset for offset in column.categorical_offsets)
    blocks.append(block)
    width += block.shape[1]
  x = np.hstack(blocks) if blocks else np.empty((2 * n, 0))
  y = np.concatenate([np.zeros(n, dtype=np.int64), np.ones(n, dtype=np.int64)])
  return x, y, cat_idx


def _class_counts(y: np.ndarray) -> tuple[int, int]:
  """`(n_source, n_synthetic)` of a 0/1 label vector."""
  n_syn = int(y.sum())
  return y.size - n_syn, n_syn


def _checked_labels(y: Any, n: int | None = None) -> np.ndarray:
  labels = np.asarray(y)
  if labels.ndim != 1 or (n is not None and labels.size != n):
    raise ValueError(
        f"y must be a 1-D label vector of length {n}, got shape {labels.shape}")
  if not np.isin(labels, (0, 1)).all():
    raise ValueError("y must hold 0 (source) and 1 (synthetic) only")
  return labels.astype(np.int64)


def _checked_xy(x: Any, y: Any) -> tuple[np.ndarray, np.ndarray]:
  features = np.asarray(x, dtype=np.float64)
  if features.ndim != _MATRIX_NDIM:
    raise ValueError(f"x must be a 2-D feature matrix, got {features.shape}")
  if np.isinf(features).any():
    raise ValueError("x must be finite (NaN marks NULL)")
  return features, _checked_labels(y, features.shape[0])


def _checked_cat_idx(cat_idx: Sequence[int], x: np.ndarray) -> list[int]:
  """`cat_idx` sorted, each a valid column holding codes 0..254 or `NaN`."""
  indices = sorted({int(j) for j in cat_idx})
  for j in indices:
    if not 0 <= j < x.shape[1]:
      raise ValueError(f"categorical index {j} outside the {x.shape[1]} "
                       "features")
    codes = x[:, j][~np.isnan(x[:, j])]
    if np.any((codes < 0) | (codes > _OTHER_CODE) | (codes != np.floor(codes))):
      raise ValueError(f"categorical feature {j} must hold integer codes "
                       f"0..{_OTHER_CODE} (NaN for NULL)")
  return indices


def _checked_scores(y: Any, p: Any) -> tuple[np.ndarray, np.ndarray]:
  scores = np.asarray(p, dtype=np.float64)
  if scores.ndim != 1 or not np.isfinite(scores).all():
    raise ValueError("p must be a 1-D vector of finite scores")
  labels = _checked_labels(y, scores.size)
  if min(_class_counts(labels)) == 0:
    raise ValueError("an ROC needs both classes (source 0 and synthetic 1)")
  return labels, scores


def _delong(y: np.ndarray, p: np.ndarray) -> tuple[float, float]:
  """`(AUC, Var(AUC))` by DeLong et al. (1988), with Sun & Xu's (2014)
  midrank computation of the placement values.

  With `m` synthetic scores X and `n` source scores Y, `V10_i = (midrank of
  X_i among all - midrank among X) / n` is the share of Y below X_i (ties
  one half), `V01_j = 1 - (midrank of Y_j among all - midrank among Y) / m`
  the share of X above Y_j; AUC is the mean of either, and
  `Var = s²(V10) / m + s²(V01) / n` with unbiased sample variances.
  """
  n_neg, n_pos = _class_counts(y)
  if min(n_neg, n_pos) < _MIN_DELONG_ROWS:
    raise ValueError(
        f"DeLong's AUC variance needs at least {_MIN_DELONG_ROWS} rows in "
        f"both classes; got {n_neg} source and {n_pos} synthetic")
  pos, neg = p[y == 1], p[y == 0]
  ranks_all = rankdata(np.concatenate([pos, neg]))
  v10 = (ranks_all[:n_pos] - rankdata(pos)) / n_neg
  v01 = 1.0 - (ranks_all[n_pos:] - rankdata(neg)) / n_pos
  auc = (ranks_all[:n_pos].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
  var = v10.var(ddof=1) / n_pos + v01.var(ddof=1) / n_neg
  return float(auc), float(var)


def _normal_ci(auc: float, var: float, alpha: float) -> tuple[float, float]:
  half = float(norm.ppf(1.0 - alpha / 2.0)) * math.sqrt(max(var, 0.0))
  return max(0.0, auc - half), min(1.0, auc + half)


def delong_ci(y: np.ndarray,
              p: np.ndarray,
              alpha: float = _ALPHA) -> tuple[float, float]:
  """The two-sided `1 - alpha` DeLong interval of the AUC of scores `p`
  (higher = more likely synthetic) against labels `y` (1 = synthetic),
  clipped to `[0, 1]`. Needs two rows of each class."""
  if not 0.0 < alpha < 1.0:
    raise ValueError(f"alpha must lie in (0, 1), got {alpha}")
  labels, scores = _checked_scores(y, p)
  return _normal_ci(*_delong(labels, scores), alpha)


def c2st_auc(x: np.ndarray,
             y: np.ndarray,
             cat_idx: Sequence[int],
             *,
             folds: int = 5,
             seed: int) -> tuple[float, float, float, np.ndarray]:
  """`(AUC, ci_low, ci_high, oof)`: the out-of-fold AUC of a gradient-boosted
  classifier separating synthetic (`y = 1`) from source rows, its 95 %
  DeLong interval, and the out-of-fold P(synthetic) of every row.

  Folds are `StratifiedKFold(folds, shuffle=True, random_state=seed)`; each
  fits `HistGradientBoostingClassifier(categorical_features=cat_idx,
  max_iter=200, early_stopping=True, random_state=seed)`, so a fixed seed
  gives identical output. Raises `ValueError` for fewer than 2 folds, a
  single class, fewer than `max(2 * folds, 20)` rows in either class, no
  features, or a categorical feature not holding codes 0..254.

  The AUC pools the scores of `folds` separately fitted models, so
  calibration differences between them can depress it slightly below the
  per-fold AUCs: the lenient direction for this lower-better gate, and a
  small effect in simulation. The DeLong interval treats the pooled scores
  as one classifier's, ignoring the variance between fold models; its null
  coverage stays close to nominal in simulation.
  """
  features, labels = _checked_xy(x, y)
  if folds < _MIN_FOLDS:
    raise ValueError(f"C2ST needs at least {_MIN_FOLDS} folds, got {folds}")
  n_neg, n_pos = _class_counts(labels)
  if min(n_neg, n_pos) == 0:
    raise ValueError("C2ST needs both classes (source 0 and synthetic 1)")
  need = max(2 * folds, _MIN_ROWS_PER_CLASS)
  if min(n_neg, n_pos) < need:
    raise ValueError(
        f"C2ST with {folds} folds needs at least {need} rows per class; "
        f"got {n_neg} source and {n_pos} synthetic")
  if features.shape[1] == 0:
    raise ValueError("C2ST needs at least one feature column")
  categorical = _checked_cat_idx(cat_idx, features)
  oof = np.empty(labels.size, dtype=np.float64)
  splitter = StratifiedKFold(n_splits=folds, shuffle=True, random_state=seed)
  for train, test in splitter.split(features, labels):
    model = HistGradientBoostingClassifier(
        categorical_features=categorical or None,
        max_iter=_HGB_MAX_ITER,
        early_stopping=True,
        random_state=seed)
    model.fit(features[train], labels[train])
    # Classes are sorted, so column 1 is P(synthetic).
    oof[test] = model.predict_proba(features[test])[:, 1]
  auc, var = _delong(labels, oof)
  lo, hi = _normal_ci(auc, var, _ALPHA)
  return auc, lo, hi, oof


class _DesignColumns:
  """The pMSE design's columns, dropping constants and exact duplicates."""

  def __init__(self, n: int) -> None:
    self.n = n
    self.dense: list[np.ndarray] = []
    self.ones: list[np.ndarray] = []  # row indices of each 0/1 column's 1s
    self._seen: set[tuple[str, bytes]] = set()

  def _new(self, tag: str, data: np.ndarray) -> bool:
    key = (tag, hashlib.blake2b(data.tobytes(), digest_size=16).digest())
    if key in self._seen:
      return False
    self._seen.add(key)
    return True

  def add_dense(self, column: np.ndarray) -> None:
    if self._new("dense", column):
      self.dense.append(column)

  def add_binary(self, rows: np.ndarray) -> None:
    if 0 < rows.size < self.n and self._new("binary", rows):
      self.ones.append(rows)

  @property
  def width(self) -> int:
    return len(self.dense) + len(self.ones)

  def matrix(self) -> Any:
    """The CSR design matrix: standardised columns, then the 0/1 columns."""
    dense = (
        np.column_stack(self.dense) if self.dense else np.empty((self.n, 0)))
    sizes = [rows.size for rows in self.ones]
    binary = sparse.csc_matrix(
        (np.ones(sum(sizes)),
         np.concatenate(self.ones) if self.ones else np.empty(0, np.int64),
         np.concatenate([[0], np.cumsum(sizes, dtype=np.int64)])),
        shape=(self.n, len(self.ones)))
    return sparse.hstack([sparse.csr_matrix(dense), binary], format="csr")


def _propensity_design(x: np.ndarray,
                       categorical: Sequence[int]) -> _DesignColumns:
  """The pMSE design matrix (see the module docstring for its columns)."""
  design = _DesignColumns(x.shape[0])
  floor = max(_MIN_LEVEL_ROWS, math.ceil(x.shape[0] / _ROWS_PER_LEVEL_ROW))
  for j in range(x.shape[1]):
    column = x[:, j]
    missing = np.isnan(column)
    if j in categorical:
      keys = np.where(missing, _NULL_LEVEL, column)
      levels, counts = np.unique(keys, return_counts=True)
      rare = levels[(counts < floor) & (levels != _NULL_LEVEL)]
      keys = np.where(np.isin(keys, rare), _RARE_LEVEL, keys)
      levels = np.unique(keys)
      named = levels[levels >= 0]
      reference = named[0] if named.size else _RARE_LEVEL
      for level in levels:
        if level != reference:
          design.add_binary(np.flatnonzero(keys == level))
      continue
    observed = column[~missing]
    if observed.size:
      mean, sd = observed.mean(), observed.std()
      if sd > 0:
        design.add_dense(np.where(missing, 0.0, (column - mean) / sd))
    design.add_binary(np.flatnonzero(missing))
  return design


def pmse_ratio(x: np.ndarray, y: np.ndarray, cat_idx: Sequence[int], *,
               seed: int) -> tuple[float, float, int, float]:
  """`(pMSE, pMSE / E0, k, ceiling)` of a logistic propensity model fitted on
  all rows, with `ceiling = N / (k - 1)` the largest ratio this design can
  read (Ruling R33: the scorer marks the metric not evaluated when the
  ceiling sits below the fail threshold).

  `c = n_syn / N`, `pMSE = mean((p_i - c)^2)` and `E0 = (k - 1) c (1 - c)
  / N`, the null expectation for independent source and synthetic samples
  (Ruling R31; not Snoke et al.'s (2018) `(k - 1)(1 - c)^2 c / N`, which
  holds only for synthetic rows drawn from the source sample itself), where
  `k` counts the intercept plus every design column; the module docstring
  details the design and both null regimes. A perfect generator averages
  1. `cat_idx` marks the categorical codes (one-hot coded, rare levels
  pooled; every other feature is standardised, NULLs mean-imputed with a
  missing indicator). The fit is lbfgs, which is deterministic; `seed` is
  passed as the model's `random_state` for symmetry with `c2st_auc`.
  Raises `ValueError` for a single class or when no feature varies.
  """
  features, labels = _checked_xy(x, y)
  n_neg, n_pos = _class_counts(labels)
  if min(n_neg, n_pos) == 0:
    raise ValueError("pMSE needs both classes (source 0 and synthetic 1)")
  columns = _propensity_design(features, _checked_cat_idx(cat_idx, features))
  if columns.width == 0:
    raise ValueError("pMSE needs at least one non-constant feature")
  design = columns.matrix()
  model = LogisticRegression(
      C=1.0, solver="lbfgs", max_iter=_PMSE_MAX_ITER, random_state=seed)
  model.fit(design, labels)
  propensity = model.predict_proba(design)[:, 1]
  n = labels.size
  c = n_pos / n
  pmse = float(np.mean((propensity - c)**2))
  k = columns.width + 1
  e0 = (k - 1) * c * (1.0 - c) / n
  return pmse, pmse / e0, k, n / (k - 1)


def roc_points(y: np.ndarray,
               p: np.ndarray,
               max_points: int = 101) -> list[tuple[float, float]]:
  """The ROC as `(false positive rate, true positive rate)` points, ascending,
  from `(0, 0)` to `(1, 1)`: one vertex per distinct score (ties move both
  rates at once), thinned to at most `max_points` vertices of the exact
  curve, evenly spaced in the number of rows called synthetic, so it stays
  monotone and always keeps both end points (the `roc_curve` profile).
  """
  if max_points < _MIN_ROC_POINTS:
    raise ValueError(
        f"max_points must be at least {_MIN_ROC_POINTS}, got {max_points}")
  labels, scores = _checked_scores(y, p)
  n_neg, n_pos = _class_counts(labels)
  order = np.argsort(-scores, kind="stable")
  # The last row of each run of equal scores closes one ROC vertex.
  ends = np.append(np.flatnonzero(np.diff(scores[order])), scores.size - 1)
  tps = np.concatenate([[0], np.cumsum(labels[order])[ends]])
  called = np.concatenate([[0], ends + 1])
  fps = called - tps
  keep = np.arange(called.size)
  if called.size > max_points:
    targets = np.linspace(0.0, float(scores.size), max_points)
    keep = np.unique(np.searchsorted(called, targets, side="left"))
  return [(float(fps[i] / n_neg), float(tps[i] / n_pos)) for i in keep]
