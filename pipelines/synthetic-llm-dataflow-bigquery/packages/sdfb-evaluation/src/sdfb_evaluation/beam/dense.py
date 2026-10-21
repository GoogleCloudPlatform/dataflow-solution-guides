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
"""Dense, mergeable profile accumulators: every column-, pair- and
row-pattern-level metric that needs no value census, and the profile
payloads the GUI draws.

    EncodedBatch of (table, side)
      │  Map: DenseProfile.from_batch(spec, batch)     vectorised numpy
      ▼
    CombinePerKey((table, side), DenseProfileCombineFn)
      │  .with_hot_key_fanout(8)                      one DenseProfile per
      ▼                                               (table, side)
    GroupByKey(table) ──► dense_outputs(spec, {side: profile}, label_key=)
                            ▲ label_key: AsSingleton(LabelKey), made on a
                            │ worker, never in the job graph (R68)
                            ├─► MetricValue   (OWNED_METRIC_IDS)
                            └─► ProfileValue  (histogram, quantiles, …)

One `DenseProfile` per (table, side) — source, synthetic, reference,
holdout — holds, on grids fixed at plan time (so it is a pure counter; no
second pass):

    numeric, temporal   counts on union_edges(q_src + q_syn + atoms)
                        (KS bracket, PIT-W1, W1, decile quantiles),
                        on profile_edges(q_src) (the 100-bin histogram)
                        and on decile_edges(q_src) (JSD, PSI); Moments;
                        values inside the source grid's [q0, q1000];
                        day-of-week / month / hour counts (UTC)
    categorical, text,  non-NULL and empty (whitespace-only) counts; a
    identifier          length histogram 0..255 + ≥ 256; how many values
                        hold each of 6 character classes
    every column        NULL count, over all rows and over `subsample_m`
                        (the census's matched-n totals; R59: nothing here
                        is computed on the subsample)
    rows                null_bits pattern counts, at most 4096 patterns
    plan pairs          BivariateAccumulator (Rulings R23/R24)

Counts are int64 and merge by addition (exact, in any order); Moments and
co-moments merge by the Chan-Golub-LeVeque / Pébay (2008) update (up to
float rounding); the pattern cap and the literal map are order-free (see
below), so a merge tree never changes a result beyond the last ulps.

    metric                    per side                   noise (R41)
    ────────────────────────  ─────────────────────────  ──────────────────
    field.type_validity       non-finite FLOAT64 cells   —
    field.range_adherence     in [q0, q1000] of source   Wilson CI
    column.null/empty/zero_   shares                     folded Newcombe CI
      rate_delta
    column.ks, length_ks      union / length bins        ks_two_sample
    column.pit_w1,            union bins (R16 mid-CDF)   —
      wasserstein
    column.decile_ks_legacy   exact min/max + 9 inner    —
                              deciles off the union bins
    column.jsd, psi           source-decile bins         jsd_null / —
    column.smd, std_ratio,    Moments (exact)            —
      range_coverage
    column.dow/month/hour_tvd calendar counts            tvd_null
    column.char_class_l1      class presence shares      —
    pair.pearson/spearman_    centered co-moments        fisher_z
      delta
    pair.cramers_v/nmi_delta, joint table, 10 cells +    — / mi_bias /
      contingency_tvd         NULL per axis              tvd_null (R63)
    row.null_pattern_tvd      top-64 source patterns +   tvd_null
                              a tail
    table.corr_rms/max_delta  Pearson deltas             —

Every fidelity metric also carries `baseline_value` = the same metric on
(reference, source) (D4); a missing or empty reference leaves it NULL with
`detail.baseline_reason`. Interval methods put their CI in `ci_low`/
`ci_high` (an absolute difference folds Newcombe's signed interval),
scalar methods put their floor in `noise_floor` (Ruling R41). Categorical
and boolean value distributions (tvd, jsd, cohens_w, top-1, coverage,
novelty, entropy, distinct counts, adherence, shape mixes) belong to the
keyed census (Task 22), not here.

Degenerate columns (R20, Review Focus 3): a metric that divides by a
source spread (SMD, std_ratio, range_coverage) is `not_evaluated` with a
reason when the source column is constant — unless both sides hold the
same constant, which is the "same" case (SMD 0, ratio 1, coverage 1, W1
0). A synthetic side that collapses to a constant scores through the
formula (std_ratio 0) and, on a pair, as "no association" (correlation,
V and NMI 0 on that side), while a constant SOURCE pair column is
`not_evaluated`. No catalogue metric divides by the IQR, so a zero-IQR
column with a real spread is evaluated in full.

Sampled mode (Ruling R72, as in `beam.membership`): a side whose plan
rate is below 1 was read as a row sample (`DenseSpec.rate_source` /
`rate_synthetic`), and a `DenseProfile` counts the rows READ. Every
metric row that reads such a side is an estimate over those rows:
`method` = sample, `sample_rate` = the lowest rate among the sampled
sides, `detail.sample_rates` naming each and `detail.estimator` keeping
"binned" where it applied; its sizes, intervals and noise floors are the
sample's own counts. One metric needs every row of both sides and is
`not_evaluated` there with "sampled mode cannot measure …; run exact
mode": `column.range_coverage` compares exact extremes, and a sample's
extremes fall inside its side's range (the relational pass withholds
`cardinality_adherence` for the same reason). So is `field.type_validity`
when the SYNTHETIC side is a sample (an integrity pass never comes from a
sample: the invalid cells may all lie outside the rows read); a sampled
source alone leaves it evaluated on the full synthetic side, unstamped.
`field.range_adherence`
stays evaluated: its bounds are the planning grid's q0 and q1000, read
from the whole source before any sample is drawn. A profile of a sampled
side carries `sample_rate` in its payload.

The null-pattern cap is a bottom-k over the priority (number of NULLs,
pattern): keeping the 4096 lowest-priority patterns is order-free, so
every retained count is exact (Ruling R34's argument) and the rest go to
an overflow counter. The metric picks the top-64 source patterns among
those known exactly on both sides; a table past 64 columns has no
complete patterns, so the metric is `not_evaluated` (R60).

Units: every temporal value is in UNIX microseconds (R54), so metric
values and details on temporal columns are micros; profile payloads
follow the GUI contract (R27) and carry temporal values in epoch seconds
(TIME: seconds since midnight, drawn on 1970-01-01), labelled by `unit`.
Profile payloads are bounded: at most 100 histogram bins and 99
quantiles (fewer on a small column, see the count rule), ≤ 4096
null patterns, contingency tables for the 5 most divergent pairs only,
and a KEYED hashed label `h:<8 hex>` (`canonical.hashed_label`, R64) for
every dictionary value D6 keeps out of a payload.

Extremes (R65, R71): an exact min or max is a single record's value, so
no payload or detail shows one, on ANY side — a source-drawn side's
(source, reference, holdout) is a source record, and a generator that
clamps to the source range would publish the source extremes through the
synthetic side. range_adherence and range_coverage compare against them
internally; payloads and details carry each side's p0.5/p99.5 instead
(`extremes: "p0.5_p99.5"`).

Count rule (R69, R77; k = RARE_COUNT = 10): which edges may be published
depends ONLY on publishable statistics — the EVALUATED source counts —
never on a value or on which grid contributed an edge. An edge is kept
iff at least k source records lie at or below it AND at least k at or
above it (both counts exact, from the right- and left-closed twins of
the profile and union bins). Both counts are monotone in the edge, so
the kept edges are one contiguous range [first kept, last kept]: every
edge outside it is dropped, synthetic-grid, atom and source-grid points
alike, and every interior edge stays (an interior point is no extreme;
R69 protects extremes). In sampled mode the sample's counts are a lower
bound of the full source's, so the rule stays safe. A common end atom
qualifies, a lone extreme never does.
  - Histogram and pair-axis edges are the profile edges that rule keeps,
    THINNED for publication (Ruling R113): going up from the first kept
    edge, an edge is published only once at least k source records lie
    in the bin since the last published one, so no two published edges
    are closer than k source records apart (`_spaced`; the pair axis
    thins its decile edges the same way). On a column of a few hundred
    rows the plan's percentiles are single source records one or two
    apart — unthinned, a 120-row column published 85 of its 120 values.
    The same edges serve every side, as they all come from the source
    grid, and a dropped edge's two bins merge (counts stay sums);
    `edges_digest` is the digest of the edges published. This is the
    publication step only: every metric is computed on the full plan
    grid.
  - Every side's quantiles and bounds are inverted from its own exact CDF
    at the kept union edges (right- and left-closed twins, merged across
    the dropped edges): a p inside an edge's jump is that edge, a p
    between jumps interpolates over the mass strictly inside the bin, and
    a probability outside (F⁻(first kept edge), F(last kept edge)] is
    WITHHELD, never clamped: nothing outside the kept range is ever
    published as a value on any side (R77.2). Each side's histogram and
    quantile payloads carry `below_mass`/`above_mass` instead, the share
    of that side's non-null values below the first / above the last kept
    edge, so the GUI can say how much of a wider synthetic lies outside
    the source's publishable range. A quantile payload also keeps only
    the p with p * n >= k and (1 - p) * n >= k, neighbouring p at least
    k / n apart (R113: k of the side's own values between two published
    quantiles; n >= 1,000 keeps all 99 percentiles), and bounds need
    n * 0.005 >= k (n the side's own).
  - With no evaluated source value in a column (no source side, or a
    NULL column), the kept edges are exactly the synthetic grid's own
    points — including any a source value coincides with — and the
    points only the source grid or the atoms contributed are dropped, as
    plan-time source records. The kept set is a function of the synthetic
    alone, so the payloads carry nothing about the unevaluated source
    (R77.3, amended by R80.1: a provenance-selective drop of the
    coinciding points mapped the source's value set on INT64/DATE).
  - Moments payloads (mean, std, skewness, kurtosis) are withheld on a
    side with fewer than k values: n <= 4 values determine them (R74.5).
    smd and std_ratio are not_evaluated when either side holds fewer
    than k values — their value with the synthetic's published moments
    would give the source's mean and std (R80.3) — and a metric's
    `baseline_value` is withheld when the reference holds fewer than k
    (R77.6), `detail.withheld` naming exactly what is withheld (R80.5).
  - The counts include any ±inf a hand-built batch carries (the encoder
    turns non-finite values into NULL-like NaN) while Moments.n does not;
    such a value only ever changes which edges carry k records, never
    a published value.

References (author-year, R22): Pébay (2008); Chan, Golub & LeVeque (1983);
Czado, Gneiting & Held (2009) for the mid-CDF PIT; Wilson (1927);
Newcombe (1998); Fisher (1915); Bergsma (2013); Treves & Panzeri (1995).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import itertools
import json
import operator
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

import apache_beam as beam
import numpy as np
from scipy.stats import entropy

from sdfb_evaluation.beam.encode import BatchLayout, EncodedBatch
from sdfb_evaluation.canonical import NULL_CODE, hash64, hashed_label
from sdfb_evaluation.catalogue import Catalogue, load_catalogue
from sdfb_evaluation.stats import binned, dependence, distances, noise, shapes
from sdfb_evaluation.stats.moments import Moments
from sdfb_evaluation.stats.noise import folded_abs_interval
from sdfb_evaluation.types import (
    ColumnKind,
    Method,
    MetricValue,
    ProfileValue,
    Side,
)

if TYPE_CHECKING:
  from sdfb_evaluation.context.plan import ColumnPlan, TablePlan

__all__ = [
    "APPLIES_TO",
    "CHAR_CLASSES",
    "CONTINGENCY_TOP_PAIRS",
    "HOT_KEY_FANOUT",
    "LENGTH_BINS",
    "NOT_EVALUATED_BELOW_K",
    "NULL_PATTERN_CAP",
    "OWNED_METRIC_IDS",
    "PAIR_BINS",
    "RARE_COUNT",
    "WITHHELD_BELOW_K",
    "DenseMetrics",
    "DenseProfile",
    "DenseProfileCombineFn",
    "DenseSpec",
    "calendar_counts",
    "char_class_masks",
    "dense_outputs",
    "pit_from_grid",
]

# The catalogue ids this pass emits (a row per column/pair/table each, a
# value or `not_evaluated` with a reason).
OWNED_METRIC_IDS: tuple[str, ...] = (
    "field.type_validity",
    "field.range_adherence",
    "column.null_rate_delta",
    "column.empty_rate_delta",
    "column.ks",
    "column.pit_w1",
    "column.wasserstein",
    "column.decile_ks_legacy",
    "column.jsd",
    "column.psi",
    "column.smd",
    "column.std_ratio",
    "column.zero_rate_delta",
    "column.range_coverage",
    "column.dow_tvd",
    "column.month_tvd",
    "column.hour_tvd",
    "column.length_ks",
    "column.char_class_l1",
    "pair.pearson_delta",
    "pair.spearman_delta",
    "pair.cramers_v_delta",
    "pair.nmi_delta",
    "pair.contingency_tvd",
    "row.null_pattern_tvd",
    "table.corr_rms_delta",
    "table.corr_max_delta",
)

_NUMERIC = ColumnKind.NUMERIC
_TEMPORAL = ColumnKind.TEMPORAL
_GRID_KINDS = frozenset({_NUMERIC, _TEMPORAL})
_STRING_KINDS = frozenset(
    {ColumnKind.CATEGORICAL, ColumnKind.TEXT, ColumnKind.IDENTIFIER})
_CODED_KINDS = frozenset({ColumnKind.CATEGORICAL, ColumnKind.BOOLEAN})
_FLOAT_TYPES = frozenset({"FLOAT64", "FLOAT"})  # the only non-finite values

# Where each field/column id reaches in this pass (the census covers the
# categorical and boolean side of column.jsd); always within the
# catalogue's kinds.
APPLIES_TO: Mapping[str, frozenset[ColumnKind]] = {
    "field.type_validity": frozenset(ColumnKind),
    "column.null_rate_delta": frozenset(ColumnKind),
    "field.range_adherence": _GRID_KINDS,
    "column.ks": _GRID_KINDS,
    "column.pit_w1": _GRID_KINDS,
    "column.wasserstein": _GRID_KINDS,
    "column.jsd": _GRID_KINDS,
    "column.psi": _GRID_KINDS,
    "column.smd": _GRID_KINDS,
    "column.std_ratio": _GRID_KINDS,
    "column.range_coverage": _GRID_KINDS,
    "column.decile_ks_legacy": frozenset({_NUMERIC}),
    "column.zero_rate_delta": frozenset({_NUMERIC}),
    "column.dow_tvd": frozenset({_TEMPORAL}),
    "column.month_tvd": frozenset({_TEMPORAL}),
    "column.hour_tvd": frozenset({_TEMPORAL}),
    "column.empty_rate_delta": _STRING_KINDS,
    "column.length_ks": _STRING_KINDS,
    "column.char_class_l1": _STRING_KINDS,
}

HOT_KEY_FANOUT = 8
NULL_PATTERN_CAP = 4096
NULL_PATTERN_TOP = 64  # row.null_pattern_tvd's head (the catalogue's 64)
CONTINGENCY_TOP_PAIRS = 5
# The pair grid: context.plan.PAIR_GRID_CELLS cells (deciles, or the top-9
# dictionary + "other") and one NULL slot, on both axes.
PAIR_BINS = 10
_OTHER_SLOT = PAIR_BINS - 1
LENGTH_OVERFLOW = 256
LENGTH_BINS = LENGTH_OVERFLOW + 1  # lengths 0..255, then >= 256
CHAR_CLASSES: tuple[str, ...] = ("digit", "upper", "lower", "space", "punct",
                                 "other")
# column.char_class_l1's classes (shapes' five; "other" is profile only)
_L1_CLASSES = CHAR_CLASSES[:5]
_SPACE_BIT = 1 << CHAR_CLASSES.index("space")
_ASCII = 128
# code points classified per vectorised chunk (4 B each)
_CHUNK_CODEPOINTS = 1 << 22
_DAY_US = 86_400_000_000
_HOUR_US = 3_600_000_000
_EPOCH_THURSDAY = 3  # 1970-01-01 is a Thursday (Monday = 0)
_MIN_CORRELATION_ROWS = 3
_MIN_OCCUPIED = 2  # rows/columns a joint table needs for an association
_MICROS_PER_SECOND = 1e6
_QUANTILE_PROBS = tuple(round(p / 100, 2) for p in range(1, 100))
# R113: two published quantile probabilities are at least this many of the
# side's own records apart (k; compared in basis points, exactly)
_QUANTILE_GAP = 10
_BASIS_POINTS = 10_000
_INNER_DECILE_PROBS = tuple(k / 10 for k in range(1, 10))
# R65/R71: every side's profile shows these quantiles, never its exact
# min/max (each a single record's value)
_BOUND_PROBS = (0.005, 0.995)
# R69: a published bound, end edge or tail quantile has at least this many
# records at or beyond it (D6/R56's k-anonymity floor, plan.LITERAL_MIN_COUNT)
RARE_COUNT = 10
# R77.6: a reference with fewer than k values withholds a row's baseline
WITHHELD_BELOW_K = f"fewer than {RARE_COUNT} values on that side (R77)"
# R80.3: smd / std_ratio with a side below k are not evaluated at all —
# their value with the synthetic's published moments would give the
# source's mean and std (both records at n = 2)
NOT_EVALUATED_BELOW_K = f"fewer than k records on a side (k = {RARE_COUNT}, R80)"
_MOMENT_VALUE_IDS = frozenset({"column.smd", "column.std_ratio"})
_EDGE_DIGITS = range(6, 18)  # 17 significant digits tell any two floats apart
_TIME_UNITS: tuple[Literal["s"], Literal["ms"],
                   Literal["us"]] = ("s", "ms", "us")
_SOURCE, _SYNTHETIC, _REFERENCE = (Side.SOURCE.value, Side.SYNTHETIC.value,
                                   Side.REFERENCE.value)
_SIDE_ORDER = tuple(side.value for side in Side)
_PROFILES_TAG = "profiles"
_METRICS_TAG = "metrics"


@functools.cache
def _catalogue() -> Catalogue:
  return load_catalogue()


# --------------------------------------------------------------------------
# vectorised building blocks
# --------------------------------------------------------------------------
def pit_from_grid(grid: np.ndarray, x: np.ndarray) -> np.ndarray:
  """The mid-CDF probability integral transform of `x` through a quantile
  `grid` (Rulings R16/R24): `F(x) - p(x) / 2` read off the grid.

  `grid` is `q_0..q_m` at probabilities `i / m` (the planning scan's
  1,001-point grid). A value on a run of equal grid points `q_a..q_b` (a
  point mass) maps to the middle of its mass, `(a + b) / (2m)`; a single
  grid point `q_a` to `a / m`; a value between two points interpolates
  linearly; one below `q_0` is 0 and one above `q_m` is 1 (Czado, Gneiting
  & Held, 2009). NaN (NULL) stays NaN, as does everything for an empty
  grid. A one-point grid holds all the mass there, so its mid-CDF is 1/2.
  """
  q = np.asarray(grid, dtype=np.float64)
  values = np.asarray(x, dtype=np.float64)
  out = np.full(values.shape, np.nan)
  present = ~np.isnan(values)
  if q.size == 0 or not present.any():
    return out
  v = values[present]
  lo = np.searchsorted(q, v, side="left")
  hi = np.searchsorted(q, v, side="right")
  steps = q.size - 1
  if steps == 0:
    out[present] = np.where(v < q[0], 0.0, np.where(v > q[0], 1.0, 0.5))
    return out
  on_grid = hi > lo
  inner = ~on_grid & (lo > 0) & (lo < q.size)
  left = np.clip(lo - 1, 0, steps)
  right = np.clip(lo, 0, steps)
  width = q[right] - q[left]
  with np.errstate(divide="ignore", invalid="ignore"):
    frac = np.where(width > 0, (v - q[left]) / width, 0.0)
  result = np.where(lo >= q.size, 1.0, 0.0)
  result = np.where(inner, (left + frac) / steps, result)
  result = np.where(on_grid, (lo + hi - 1) / (2.0 * steps), result)
  out[present] = result
  return out


def _class_bit(ch: str) -> int:
  """The one character class `ch` belongs to, as a bit. The five classes
  of `shapes.char_class_presence` are mutually exclusive per character,
  so presence of a class in a string is the OR of its characters' bits;
  "other" is what none of them claims (a caseless letter, a non-digit
  numeral)."""
  if ch.isdigit():
    name = "digit"
  elif ch.isalpha() and ch.isupper():
    name = "upper"
  elif ch.isalpha() and ch.islower():
    name = "lower"
  elif ch.isspace():
    name = "space"
  elif not ch.isalnum():
    name = "punct"
  else:
    name = "other"
  return 1 << CHAR_CLASSES.index(name)


_ASCII_BITS = np.array([_class_bit(chr(c)) for c in range(_ASCII)],
                       dtype=np.uint8)


def _codepoint_bits(codepoints: np.ndarray) -> np.ndarray:
  """Class bits per code point: a lookup for ASCII, one Python call per
  DISTINCT non-ASCII code point for the rest."""
  bits = np.empty(codepoints.size, dtype=np.uint8)
  ascii_mask = codepoints < _ASCII
  bits[ascii_mask] = _ASCII_BITS[codepoints[ascii_mask]]
  if not ascii_mask.all():
    high = codepoints[~ascii_mask]
    unique, inverse = np.unique(high, return_inverse=True)
    table = np.fromiter((_class_bit(chr(c)) for c in unique.tolist()),
                        dtype=np.uint8,
                        count=unique.size)
    bits[~ascii_mask] = table[inverse]
  return bits


def char_class_masks(values: Sequence[str],
                     *,
                     chunk_codepoints: int = _CHUNK_CODEPOINTS) -> np.ndarray:
  """A uint8 class mask per string (bit i = `CHAR_CLASSES[i]` present),
  equal to `shapes.char_class_presence` on each value, computed over the
  batch's code points at once: the strings are joined, read as UTF-32 code
  points, classified by lookup and OR-reduced per string. Chunked so about
  `chunk_codepoints` code points are held at a time (a longer string is a
  chunk of its own); an empty string has mask 0.
  """
  masks = np.zeros(len(values), dtype=np.uint8)
  lengths = np.fromiter(map(len, values), dtype=np.int64, count=len(values))
  has = lengths > 0
  index = np.flatnonzero(has)
  texts = list(itertools.compress(values, has))
  widths = lengths[index]
  ends = np.cumsum(widths)
  start = 0
  while start < len(texts):
    before = int(ends[start - 1]) if start else 0
    stop = int(np.searchsorted(ends, before + chunk_codepoints, side="right"))
    stop = max(stop, start + 1)
    codepoints = np.frombuffer(
        "".join(texts[start:stop]).encode("utf-32-le", "surrogatepass"),
        dtype="<u4")
    offsets = ends[start:stop] - widths[start:stop] - before
    masks[index[start:stop]] = np.bitwise_or.reduceat(
        _codepoint_bits(codepoints), offsets)
    start = stop
  return masks


def calendar_counts(
    micros: np.ndarray, *,
    time_only: bool) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
  """(day-of-week 7, month 12, hour 24) counts of UNIX-microsecond values
  (UTC; Monday = 0 as `datetime.weekday()`, January = 0). A TIME column
  (`time_only`, microseconds since midnight) has only hours; its
  day-of-week and month counts stay zero."""
  whole = np.asarray(micros, dtype=np.float64).astype(np.int64)
  hours = np.bincount(
      np.remainder(whole, _DAY_US) // _HOUR_US, minlength=24)[:24]
  if time_only:
    return (np.zeros(7, dtype=np.int64), np.zeros(12, dtype=np.int64),
            hours.astype(np.int64))
  days = np.floor_divide(whole, _DAY_US)
  dow = np.bincount((days + _EPOCH_THURSDAY) % 7, minlength=7)
  months = whole.astype("datetime64[us]").astype("datetime64[M]").astype(
      np.int64) % 12
  return (dow.astype(np.int64), np.bincount(months,
                                            minlength=12).astype(np.int64),
          hours.astype(np.int64))


class _StringBatch(NamedTuple):
  nonnull: int
  empty: int
  nonempty: int
  lengths: np.ndarray  # (LENGTH_BINS,)
  classes: np.ndarray  # (len(CHAR_CLASSES),)


def _string_batch(column: Sequence[str | None]) -> _StringBatch:
  """One text column of a batch: lengths, emptiness, class presence."""
  present = np.fromiter(
      map(operator.is_not, column, itertools.repeat(None)),
      dtype=bool,
      count=len(column))
  values = cast(list[str], list(itertools.compress(column, present)))
  lengths = np.fromiter(map(len, values), dtype=np.int64, count=len(values))
  histogram = np.bincount(
      np.minimum(lengths, LENGTH_OVERFLOW), minlength=LENGTH_BINS)
  masks = char_class_masks(values)
  nonempty = int(np.count_nonzero(lengths))
  blank = int(np.count_nonzero(masks == _SPACE_BIT))
  classes = np.array(
      [np.count_nonzero(masks & (1 << i)) for i in range(len(CHAR_CLASSES))],
      dtype=np.int64)
  return _StringBatch(
      nonnull=len(values),
      empty=len(values) - nonempty + blank,
      nonempty=nonempty,
      lengths=histogram.astype(np.int64),
      classes=classes)


def _pattern_priority(pattern: int) -> tuple[int, int]:
  return pattern.bit_count(), pattern


def _capped(patterns: dict[int, int],
            overflow: int) -> tuple[dict[int, int], int]:
  """At most `NULL_PATTERN_CAP` patterns: the lowest (NULL count, pattern)
  are kept, the others' rows move to `overflow`. Order-free: a pattern
  evicted anywhere had `NULL_PATTERN_CAP` lower-priority patterns seen
  with it, which every later union still holds, so it is never retained
  in the end, and every retained count is exact."""
  if len(patterns) <= NULL_PATTERN_CAP:
    return patterns, overflow
  keep = sorted(patterns, key=_pattern_priority)[:NULL_PATTERN_CAP]
  kept = {pattern: patterns[pattern] for pattern in keep}
  return kept, overflow + sum(patterns.values()) - sum(kept.values())


# --------------------------------------------------------------------------
# the plan, slimmed for the workers
# --------------------------------------------------------------------------
@dataclass(frozen=True, eq=False)
class _Grid:  # pylint: disable=too-many-instance-attributes  # one field per grid the column is counted on
  """One numeric or temporal column and its plan-time grids;
  `synthetic_edge` marks the union edges that are points of the
  synthetic's own quantile grid (whether or not a source value coincides
  with them), which only the no-source case consults (R77.3, R80.1)."""
  j: int
  name: str
  kind: ColumnKind
  bq_type: str
  num_k: int
  union: np.ndarray
  synthetic_edge: np.ndarray
  profile: np.ndarray
  deciles: np.ndarray
  lo: float | None
  hi: float | None
  day_granularity: bool

  @property
  def time_only(self) -> bool:
    return self.bq_type == "TIME"

  @property
  def scale(self) -> float:
    """Payload unit factor: temporal micros → seconds (the GUI's unit)."""
    return 1.0 / _MICROS_PER_SECOND if self.kind is _TEMPORAL else 1.0

  @property
  def unit(self) -> str:
    """The metric-side unit, for a row's detail."""
    if self.kind is not _TEMPORAL:
      return "value"
    return "micros_since_midnight" if self.time_only else "epoch_micros"


@dataclass(frozen=True)
class _String:
  """One text-block column (categorical, text or identifier)."""
  j: int
  name: str
  kind: ColumnKind
  text_k: int


@dataclass(frozen=True, eq=False)
class _PairColumn:  # pylint: disable=too-many-instance-attributes  # a pair axis carries both encodings
  """One column of the plan's pairs, on the 10-cell pair grid."""
  j: int
  name: str
  kind: ColumnKind
  grid: bool
  num_k: int | None
  cat_k: int | None
  text_k: int | None
  mean: float
  scale: float
  deciles: np.ndarray
  q_src: np.ndarray
  q_syn: np.ndarray
  dictionary: tuple[int, ...]
  dict_sorted: np.ndarray
  dict_order: np.ndarray
  literal_ok: bool
  bool_labels: Mapping[int, str]


def _null_source(name: str, layout: BatchLayout) -> tuple[str, int]:
  """Where a column past the 64 null bits shows its NULLs in a batch."""
  for block, names in (("cat", layout.cat_columns), ("text",
                                                     layout.text_columns),
                       ("nonkey", layout.nonkey_columns), ("num",
                                                           layout.num_columns)):
    if name in names:
      return block, names.index(name)
  raise ValueError(f"{layout.table}: column {name!r} is in no encoded block")


def _pair_column(j: int, column: ColumnPlan,
                 layout: BatchLayout) -> _PairColumn:
  name = column.name
  grid = column.kind in _GRID_KINDS and name in layout.num_columns
  coded = column.kind in _CODED_KINDS and name in layout.cat_columns
  if not grid and not coded:
    raise ValueError(f"{layout.table}: pair column {name!r} ({column.kind}) "
                     "has no pair-grid encoding")
  dictionary = tuple(int(c) for c in column.dictionary or ())
  codes = np.array(dictionary, dtype=np.uint64)
  order = np.argsort(codes, kind="stable")
  q_src = np.asarray(column.quantiles_src or (), dtype=np.float64)
  std = column.std_src
  return _PairColumn(
      j=j,
      name=name,
      kind=column.kind,
      grid=grid,
      num_k=layout.num_columns.index(name) if grid else None,
      cat_k=layout.cat_columns.index(name) if coded else None,
      text_k=(layout.text_columns.index(name)
              if coded and name in layout.text_columns else None),
      mean=float(column.mean_src or 0.0),
      scale=float(std) if std is not None and std > 0 else 1.0,
      deciles=binned.decile_edges(column.quantiles_src or ()),
      q_src=q_src,
      q_syn=np.asarray(column.quantiles_syn or (), dtype=np.float64),
      dictionary=dictionary,
      dict_sorted=codes[order],
      dict_order=order.astype(np.int64),
      literal_ok=column.literal_ok,
      bool_labels=({
          hash64(name, True): "true",
          hash64(name, False): "false"
      } if column.kind is ColumnKind.BOOLEAN else {}))


def _grid(j: int, column: ColumnPlan, layout: BatchLayout) -> _Grid:
  q_src = column.quantiles_src or ()
  q_syn = np.asarray(column.quantiles_syn or (), dtype=np.float64)
  union = binned.union_edges(q_src, column.quantiles_syn or (), column.atoms)
  return _Grid(
      j=j,
      name=column.name,
      kind=column.kind,
      bq_type=column.bq_type,
      num_k=layout.num_columns.index(column.name),
      union=union,
      synthetic_edge=np.isin(union, q_syn),
      profile=binned.profile_edges(q_src),
      deciles=binned.decile_edges(q_src),
      lo=float(q_src[0]) if q_src else None,
      hi=float(q_src[-1]) if q_src else None,
      day_granularity=column.day_granularity)


def _row_rate(rate: float | None) -> float:
  """A side's plan row-sample rate (None or >= 1: read in full)."""
  return 1.0 if rate is None or rate >= 1.0 else float(rate)


@dataclass(frozen=True, eq=False)
class DenseSpec:
  """What the dense pass needs of one `TablePlan`: the batch layout, each
  column's grids and the pair grid — never the R/H panel rows, so it is
  cheap to ship with every DoFn."""
  table: str
  encoding_plan_digest: str
  layout: BatchLayout
  null_sources: tuple[tuple[str, int], ...]
  grids: tuple[_Grid, ...]
  strings: tuple[_String, ...]
  pair_columns: tuple[_PairColumn, ...]
  pairs: tuple[tuple[int, int], ...]
  rate_source: float = 1.0
  rate_synthetic: float = 1.0

  def sample_rates(
      self, sides: Sequence[str] = (_SOURCE, _SYNTHETIC)) -> dict[str, float]:
    """The row-sample rates (< 1) of `sides`, by side (R72); empty when
    each was read in full. The panel sides are always read in full."""
    rates = {_SOURCE: self.rate_source, _SYNTHETIC: self.rate_synthetic}
    return {side: rates[side] for side in sides if rates.get(side, 1.0) < 1.0}

  @classmethod
  def from_table(cls, table: TablePlan) -> DenseSpec:
    """The spec of `table`'s plan columns and pairs.

    Raises:
      ValueError: a key column is not planned (`BatchLayout`), or a pair
        column has no pair-grid encoding.
    """
    layout = BatchLayout.from_table(table)
    columns = table.columns
    grids = tuple(
        _grid(j, c, layout)
        for j, c in enumerate(columns)
        if c.kind in _GRID_KINDS and c.name in layout.num_columns)
    strings = tuple(
        _String(j, c.name, c.kind, layout.text_columns.index(c.name))
        for j, c in enumerate(columns)
        if c.name in layout.text_columns)
    used = sorted({j for pair in table.pairs for j in pair})
    local = {j: i for i, j in enumerate(used)}
    return cls(
        table=table.name,
        encoding_plan_digest=table.encoding_plan_digest,
        layout=layout,
        null_sources=tuple(_null_source(c.name, layout) for c in columns),
        grids=grids,
        strings=strings,
        pair_columns=tuple(_pair_column(j, columns[j], layout) for j in used),
        pairs=tuple((local[a], local[b]) for a, b in table.pairs),
        rate_source=_row_rate(table.sample_rate_source),
        rate_synthetic=_row_rate(table.sample_rate_synthetic))


# --------------------------------------------------------------------------
# one batch
# --------------------------------------------------------------------------
def _null_matrix(spec: DenseSpec, batch: EncodedBatch) -> np.ndarray:
  """`(n, d)` bool: plan column j is NULL in row i. The first 64 columns
  read `null_bits` (SQL NULL, an empty REPEATED); the rest read their
  encoded block (NULL_CODE, None, or NaN for a key numeric)."""
  layout = spec.layout
  n, d = batch.n, len(layout.columns)
  null = np.empty((n, d), dtype=bool)
  covered = layout.null_bits_columns
  if covered:
    shifts = np.arange(covered, dtype=np.uint64)
    null[:, :covered] = ((batch.null_bits[:, np.newaxis] >> shifts)
                         & np.uint64(1)).astype(bool)
  for j in range(covered, d):
    block, k = spec.null_sources[j]
    if block == "cat":
      null[:, j] = batch.cat[:, k] == np.uint64(NULL_CODE)
    elif block == "text":
      null[:, j] = np.fromiter(
          map(operator.is_, batch.text[k], itertools.repeat(None)),
          dtype=bool,
          count=n)
    elif block == "nonkey":
      null[:, j] = batch.h_nonkey[:, k] == np.uint64(NULL_CODE)
    else:
      null[:, j] = np.isnan(batch.num[:, k])
  return null


def _dictionary_slots(column: _PairColumn, codes: np.ndarray) -> np.ndarray:
  """Pair-grid slots of a coded column: its dictionary rank, "other", or
  NULL."""
  slots = np.full(codes.shape, _OTHER_SLOT, dtype=np.int64)
  size = column.dict_sorted.size
  if size:
    pos = np.searchsorted(column.dict_sorted, codes)
    clipped = np.minimum(pos, size - 1)
    hit = (pos < size) & (column.dict_sorted[clipped] == codes)
    slots = np.where(hit, column.dict_order[clipped], slots)
  slots[codes == np.uint64(NULL_CODE)] = PAIR_BINS
  return slots


def _pair_inputs(spec: DenseSpec, batch: EncodedBatch,
                 side: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
  """`(z_std, z_pit, codes)`, each `(n, d_pairs)`, for the co-moments and
  the joint tables: the plan-standardised value, the side's own mid-CDF
  PIT (R24; the synthetic grid for the synthetic side, the source grid
  for the source-drawn sides), and the pair-grid slot."""
  n, d = batch.n, len(spec.pair_columns)
  z_std = np.full((n, d), np.nan)
  z_pit = np.full((n, d), np.nan)
  codes = np.empty((n, d), dtype=np.int64)
  for c, column in enumerate(spec.pair_columns):
    if column.num_k is not None:
      x = batch.num[:, column.num_k]
      z_std[:, c] = (x - column.mean) / column.scale
      grid = column.q_syn if side == _SYNTHETIC else column.q_src
      z_pit[:, c] = pit_from_grid(grid, x)
      codes[:, c] = np.where(
          np.isnan(x), PAIR_BINS,
          np.searchsorted(column.deciles, x, side="left"))
    elif column.cat_k is not None:
      codes[:, c] = _dictionary_slots(column, batch.cat[:, column.cat_k])
  return z_std, z_pit, codes


def _batch_literals(spec: DenseSpec,
                    batch: EncodedBatch) -> dict[int, dict[int, str]]:
  """The text of each dictionary value the batch shows, for the pair
  columns whose values D6 lets a payload show literally."""
  literals: dict[int, dict[int, str]] = {}
  for c, column in enumerate(spec.pair_columns):
    if not column.literal_ok or column.text_k is None or column.cat_k is None:
      continue
    cat = batch.cat[:, column.cat_k]
    texts = batch.text[column.text_k]
    found: dict[int, str] = {}
    for code in column.dictionary:
      rows = np.flatnonzero(cat == np.uint64(code))
      text = texts[int(rows[0])] if rows.size else None
      if text is not None:
        found[code] = text
    if found:
      literals[c] = found
  return literals


def _merge_literals(
    a: Mapping[int, Mapping[int, str]],
    b: Mapping[int, Mapping[int, str]]) -> dict[int, dict[int, str]]:
  merged: dict[int, dict[int, str]] = {c: dict(v) for c, v in a.items()}
  for c, found in b.items():
    target = merged.setdefault(c, {})
    for code, text in found.items():
      target[code] = min(target[code], text) if code in target else text
  return merged


def _add(a: Sequence[np.ndarray], b: Sequence[np.ndarray]) -> list[np.ndarray]:
  return [x + y for x, y in zip(a, b, strict=True)]


def _left_counts(x: np.ndarray, edges: np.ndarray) -> np.ndarray:
  """`binned.bin_counts`' left-closed twin: `edges.size + 1` bins
  `(-inf, e0), [e0, e1), ..., [e_last, +inf)`, so a cumulative sum gives
  count(x < e) exactly (and, against the right-closed counts,
  count(x == e))."""
  return np.bincount(
      np.searchsorted(edges, x, side="right"), minlength=edges.size + 1)


# --------------------------------------------------------------------------
# the accumulator
# --------------------------------------------------------------------------
@dataclass(eq=False)
class DenseProfile:  # pylint: disable=too-many-instance-attributes  # one field per dense statistic
  """One (table, side)'s mergeable dense profile (module docstring).

  Arrays follow the spec's order: `nulls`/`nulls_m` the plan columns,
  `union`/`union_left`/`profile`/`profile_left`/`deciles`/`moments`/
  `in_range`/`dow`/`month`/`hour` `spec.grids` (`union_left` and
  `profile_left` count the left-closed twins of the union and profile
  bins, so count(x >= edge) and count(x == edge) are exact, R69/R74),
  `str_*`/`lengths`/`classes` `spec.strings` (class columns in
  `CHAR_CLASSES` order); `null_patterns` maps a null_bits pattern to
  its row count (at most `NULL_PATTERN_CAP`, the rest in `null_overflow`);
  `literals` maps a pair column to {dictionary code: text} where D6 allows
  a literal. Task 22 reads its matched-n totals from `rows_m`/`nulls_m`.
  `eq=False`: arrays have no truth value; compare fields.
  """
  table: str
  side: str
  rows: int
  rows_m: int
  nulls: np.ndarray
  nulls_m: np.ndarray
  union: list[np.ndarray]
  union_left: list[np.ndarray]
  profile: list[np.ndarray]
  profile_left: list[np.ndarray]
  deciles: list[np.ndarray]
  moments: list[Moments]
  in_range: np.ndarray
  dow: np.ndarray
  month: np.ndarray
  hour: np.ndarray
  str_nonnull: np.ndarray
  str_empty: np.ndarray
  str_nonempty: np.ndarray
  lengths: np.ndarray
  classes: np.ndarray
  null_patterns: dict[int, int] = field(default_factory=dict)
  null_overflow: int = 0
  bivariate: dependence.BivariateAccumulator | None = None
  literals: dict[int, dict[int, str]] = field(default_factory=dict)

  @classmethod
  def empty(cls, spec: DenseSpec, side: Side | str) -> DenseProfile:
    """The profile of no rows: every count zero."""
    grids, strings = len(spec.grids), len(spec.strings)
    columns = len(spec.layout.columns)

    def zeros(*shape: int) -> np.ndarray:
      return np.zeros(shape, dtype=np.int64)

    return cls(
        table=spec.table,
        side=Side(side).value,
        rows=0,
        rows_m=0,
        nulls=zeros(columns),
        nulls_m=zeros(columns),
        union=[zeros(g.union.size + 1) for g in spec.grids],
        union_left=[zeros(g.union.size + 1) for g in spec.grids],
        profile=[zeros(g.profile.size + 1) for g in spec.grids],
        profile_left=[zeros(g.profile.size + 1) for g in spec.grids],
        deciles=[zeros(g.deciles.size + 1) for g in spec.grids],
        moments=[Moments() for _ in spec.grids],
        in_range=zeros(grids),
        dow=zeros(grids, 7),
        month=zeros(grids, 12),
        hour=zeros(grids, 24),
        str_nonnull=zeros(strings),
        str_empty=zeros(strings),
        str_nonempty=zeros(strings),
        lengths=zeros(strings, LENGTH_BINS),
        classes=zeros(strings, len(CHAR_CLASSES)),
        bivariate=(dependence.BivariateAccumulator(
            pairs=spec.pairs, bins=PAIR_BINS) if spec.pairs else None))

  @classmethod
  def from_batch(cls, spec: DenseSpec, batch: EncodedBatch) -> DenseProfile:
    """The profile of one encoded batch (vectorised per column).

    Raises:
      ValueError: the batch belongs to another table.
    """
    if batch.table != spec.table:
      raise ValueError(f"a batch of {batch.table!r} reached the dense spec of "
                       f"{spec.table!r}")
    side = Side(batch.side).value
    profile = cls.empty(spec, side)
    null = _null_matrix(spec, batch)
    profile.rows = batch.n
    profile.rows_m = int(np.count_nonzero(batch.subsample_m))
    profile.nulls = null.sum(axis=0, dtype=np.int64)
    profile.nulls_m = null[batch.subsample_m].sum(axis=0, dtype=np.int64)
    profile._add_grids(spec, batch, null)
    profile._add_strings(spec, batch)
    if spec.layout.null_bits_complete:
      patterns, counts = np.unique(batch.null_bits, return_counts=True)
      profile.null_patterns, profile.null_overflow = _capped(
          dict(zip(patterns.tolist(), counts.tolist(), strict=True)), 0)
    if profile.bivariate is not None:
      profile.bivariate.add_batch(*_pair_inputs(spec, batch, side))
      profile.literals = _batch_literals(spec, batch)
    return profile

  def _add_grids(self, spec: DenseSpec, batch: EncodedBatch,
                 null: np.ndarray) -> None:
    for gi, grid in enumerate(spec.grids):
      values = batch.num[:, grid.num_k][~null[:, grid.j]]
      self.moments[gi].add_array(values)  # non-finite → Moments.nonfinite
      # NaN out; a ±inf (never from the encoder, which maps non-finite to
      # NaN) would land in the end bins and not in Moments.n (module
      # docstring: it only bears on which edges carry k records)
      finite = values[~np.isnan(values)]
      self.union[gi] += binned.bin_counts(finite, grid.union)
      self.profile[gi] += binned.bin_counts(finite, grid.profile)
      # left-closed twins [e_{i-1}, e_i): count(x >= e) and count(x == e)
      # are exact too (R69, R74)
      self.union_left[gi] += _left_counts(finite, grid.union)
      self.profile_left[gi] += _left_counts(finite, grid.profile)
      self.deciles[gi] += binned.bin_counts(finite, grid.deciles)
      if grid.lo is not None:
        self.in_range[gi] = np.count_nonzero((finite >= grid.lo)
                                             & (finite <= grid.hi))
      if grid.kind is _TEMPORAL and finite.size:
        self.dow[gi], self.month[gi], self.hour[gi] = calendar_counts(
            finite, time_only=grid.time_only)

  def _add_strings(self, spec: DenseSpec, batch: EncodedBatch) -> None:
    for si, column in enumerate(spec.strings):
      stats = _string_batch(batch.text[column.text_k])
      self.str_nonnull[si] = stats.nonnull
      self.str_empty[si] = stats.empty
      self.str_nonempty[si] = stats.nonempty
      self.lengths[si] = stats.lengths
      self.classes[si] = stats.classes

  def merge(self, other: DenseProfile) -> DenseProfile:
    """`self` and `other` combined, as a new profile (pure: neither operand
    changes). Counts add exactly; Moments and co-moments merge by Pébay;
    the pattern cap and literals merge order-free.

    Raises:
      ValueError: the two profiles are of different tables, sides or
        specs.
    """
    if (self.table, self.side) != (other.table, other.side):
      raise ValueError(f"cannot merge the {other.table}/{other.side} profile "
                       f"into {self.table}/{self.side}: another table or side")
    if (self.nulls.shape != other.nulls.shape or
        len(self.union) != len(other.union) or
        self.lengths.shape != other.lengths.shape):
      raise ValueError(f"{self.table}/{self.side}: profiles of different "
                       "specs cannot merge")
    patterns = dict(self.null_patterns)
    for pattern, count in other.null_patterns.items():
      patterns[pattern] = patterns.get(pattern, 0) + count
    patterns, overflow = _capped(patterns,
                                 self.null_overflow + other.null_overflow)
    return DenseProfile(
        table=self.table,
        side=self.side,
        rows=self.rows + other.rows,
        rows_m=self.rows_m + other.rows_m,
        nulls=self.nulls + other.nulls,
        nulls_m=self.nulls_m + other.nulls_m,
        union=_add(self.union, other.union),
        union_left=_add(self.union_left, other.union_left),
        profile=_add(self.profile, other.profile),
        profile_left=_add(self.profile_left, other.profile_left),
        deciles=_add(self.deciles, other.deciles),
        moments=[
            a.merge(b)
            for a, b in zip(self.moments, other.moments, strict=True)
        ],
        in_range=self.in_range + other.in_range,
        dow=self.dow + other.dow,
        month=self.month + other.month,
        hour=self.hour + other.hour,
        str_nonnull=self.str_nonnull + other.str_nonnull,
        str_empty=self.str_empty + other.str_empty,
        str_nonempty=self.str_nonempty + other.str_nonempty,
        lengths=self.lengths + other.lengths,
        classes=self.classes + other.classes,
        null_patterns=patterns,
        null_overflow=overflow,
        bivariate=_merge_bivariate(self.bivariate, other.bivariate),
        literals=_merge_literals(self.literals, other.literals))


def _merge_bivariate(
    a: dependence.BivariateAccumulator | None,
    b: dependence.BivariateAccumulator | None
) -> dependence.BivariateAccumulator | None:
  if a is None or b is None:
    if a is not b:
      raise ValueError("profiles of different specs cannot merge (pairs)")
    return None
  return a.merge(b)


class DenseProfileCombineFn(beam.CombineFn):
  """Merges `DenseProfile`s of one (table, side). The empty accumulator is
  `None` (no spec needed); `merge` is pure, so an input profile can
  become the accumulator as it is, never mutated."""

  def create_accumulator(self) -> DenseProfile | None:
    return None

  def add_input(self, mutable_accumulator: DenseProfile | None,
                element: DenseProfile) -> DenseProfile:
    if mutable_accumulator is None:
      return element
    return mutable_accumulator.merge(element)

  def merge_accumulators(
      self, accumulators: Iterable[DenseProfile | None]) -> DenseProfile | None:
    merged: DenseProfile | None = None
    for accumulator in accumulators:
      if accumulator is None:
        continue
      merged = accumulator if merged is None else merged.merge(accumulator)
    return merged

  def extract_output(self,
                     accumulator: DenseProfile | None) -> DenseProfile | None:
    return accumulator


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------
class _Scope(NamedTuple):
  column: str | None
  column_2: str | None
  kind: str | None


class _Sizes(NamedTuple):
  """What a row was computed over, per side (`n_source`/`n_synthetic`)."""
  n_source: int | None
  n_synthetic: int | None


_TABLE_SCOPE = _Scope(None, None, None)


@dataclass
class _Sides:
  src: DenseProfile
  syn: DenseProfile
  ref: DenseProfile | None


def _number(x: Any) -> float | None:
  return None if x is None else float(x)


def _missing(what: str, n_source: int, n_synthetic: int) -> str | None:
  """Why a two-sided metric cannot compare, or None when it can."""
  if n_source <= 0:
    return f"no {what} on the source side"
  if n_synthetic <= 0:
    return f"no {what} on the synthetic side"
  return None


class _Emitter:
  """Collects one table's `MetricValue`s, attaching the catalogue-driven
  baseline rule and the table-wide fields."""

  def __init__(self, spec: DenseSpec, has_reference: bool):
    self.spec = spec
    self.has_reference = has_reference
    self.rows: list[MetricValue] = []

  def value(self,
            metric_id: str,
            value: float,
            scope: _Scope,
            *,
            sizes: _Sizes,
            baseline: float | None = None,
            method: Method = Method.EXACT,
            detail: Mapping[str, Any] | None = None,
            reads: Sequence[str] = (_SOURCE, _SYNTHETIC),
            **fields: float | str | None) -> None:
    metric = _catalogue().get(metric_id)
    notes = dict(detail or {})
    if not metric.baseline:
      baseline = None
    elif baseline is None:
      notes.setdefault(
          "baseline_reason", "undefined on the reference sample"
          if self.has_reference else "no reference sample (the R panel)")
    method, rate = self._estimate(method, notes, reads)
    self.rows.append(
        MetricValue(
            metric_id=metric_id,
            table=self.spec.table,
            value=float(value),
            column=scope.column,
            column_2=scope.column_2,
            column_kind=scope.kind,
            baseline_value=_number(baseline),
            n_source=_count(sizes.n_source),
            n_synthetic=_count(sizes.n_synthetic),
            method=method,
            sample_rate=rate,
            encoding_plan_digest=self.spec.encoding_plan_digest,
            detail=notes,
            **{
                k: _plain(v) for k, v in fields.items()
            }))

  def skip(self,
           metric_id: str,
           reason: str,
           scope: _Scope,
           *,
           sizes: _Sizes,
           detail: Mapping[str, Any] | None = None) -> None:
    notes: dict[str, Any] = {"reason": reason, **(detail or {})}
    method, rate = self._estimate(Method.EXACT, notes)
    self.rows.append(
        MetricValue(
            metric_id=metric_id,
            table=self.spec.table,
            value=None,
            column=scope.column,
            column_2=scope.column_2,
            column_kind=scope.kind,
            n_source=_count(sizes.n_source),
            n_synthetic=_count(sizes.n_synthetic),
            method=method,
            sample_rate=rate,
            encoding_plan_digest=self.spec.encoding_plan_digest,
            detail=notes))

  def _estimate(
      self,
      method: Method,
      notes: dict[str, Any],
      reads: Sequence[str] = (_SOURCE, _SYNTHETIC)
  ) -> tuple[Method, float | None]:
    """A row's `(method, sample_rate)`: its own method unless a side it
    `reads` was read as a row sample — then `sample` at the lowest rate,
    `notes` naming each sampled side's rate and keeping a binned
    estimator's name (R72). The dense metrics read both sides, except
    `field.type_validity`, whose value reads the synthetic side only
    (`field.range_adherence`'s value does too, its bounds being the
    planning grid's; it keeps the both-sides stamp, a conservative one)."""
    rates = self.spec.sample_rates(reads)
    if not rates:
      return method, None
    notes["sample_rates"] = rates
    if method is not Method.EXACT:
      notes["estimator"] = method.value
    return Method.SAMPLE, min(rates.values())

  def sampled_extremes(self, what: str) -> str | None:
    """Why a metric of exact extremes cannot be measured on this table's
    row samples (R72), or None when both sides were read in full."""
    rates = self.spec.sample_rates()
    if not rates:
      return None
    side, rate = next(iter(rates.items()))  # the source first
    return (f"sampled mode cannot measure {what}: the {side} side is a "
            f"{rate:.3g} row sample, so its extremes fall inside the full "
            "side's range; run exact mode")


def _count(x: Any) -> int | None:
  return None if x is None else int(x)


def _plain(x: Any) -> Any:
  """numpy scalars as Python values (MetricValue fields are plain)."""
  return x.item() if isinstance(x, np.generic) else x


def _share_delta(e: _Emitter, metric_id: str, scope: _Scope,
                 counts: tuple[int, int, int | None],
                 totals: tuple[int, int, int | None], what: str) -> None:
  """|p_syn - p_src| with the folded Newcombe CI; baseline |p_ref - p_src|."""
  (k_src, k_syn, k_ref), (n_src, n_syn, n_ref) = counts, totals
  sizes = _Sizes(n_src, n_syn)
  why = _missing(what, n_src, n_syn)
  if why:
    e.skip(metric_id, why, scope, sizes=sizes)
    return
  p_src, p_syn = k_src / n_src, k_syn / n_syn
  lo, hi = folded_abs_interval(
      *noise.newcombe_diff_interval(k_syn, n_syn, k_src, n_src))
  baseline = (
      abs(k_ref / n_ref - p_src) if k_ref is not None and n_ref else None)
  e.value(
      metric_id,
      abs(p_syn - p_src),
      scope,
      baseline=baseline,
      source_value=p_src,
      synthetic_value=p_syn,
      ci_low=lo,
      ci_high=hi,
      sizes=sizes)


def _column_basics(e: _Emitter, spec: DenseSpec, s: _Sides) -> None:
  """field.type_validity and column.null_rate_delta, every column."""
  grid_of = {g.j: gi for gi, g in enumerate(spec.grids)}
  ref = s.ref
  syn_rate = spec.sample_rates((_SYNTHETIC,)).get(_SYNTHETIC)
  for j, name in enumerate(spec.layout.columns):
    scope = _Scope(name, None, spec.layout.kinds[j].value)
    _share_delta(e, "column.null_rate_delta", scope,
                 (int(s.src.nulls[j]), int(s.syn.nulls[j]),
                  None if ref is None else int(ref.nulls[j])),
                 (s.src.rows, s.syn.rows, None if ref is None else ref.rows),
                 "rows")
    gi = grid_of.get(j)

    def invalid(p: DenseProfile, gi: int | None = gi) -> int:
      return 0 if gi is None else p.moments[gi].nonfinite

    nonnull_src = s.src.rows - int(s.src.nulls[j])
    nonnull_syn = s.syn.rows - int(s.syn.nulls[j])
    sizes = _Sizes(nonnull_src, nonnull_syn)
    if nonnull_syn <= 0:
      e.skip(
          "field.type_validity",
          "no non-null values on the synthetic side",
          scope,
          sizes=sizes)
      continue
    if syn_rate is not None:
      # an integrity pass never comes from a sample (R72): the invalid
      # cells may all be outside the rows read
      e.skip(
          "field.type_validity",
          "sampled mode cannot measure type validity: the synthetic side "
          f"is a {syn_rate:.3g} row sample, so invalid cells it did not "
          "read go unseen; run exact mode",
          scope,
          sizes=sizes,
          detail={
              "invalid_in_sample": invalid(s.syn),
              "note": "the sample's count, not the table's"
          })
      continue
    bad = invalid(s.syn)
    rule = ("a non-finite FLOAT64 value (NaN, ±Inf) is invalid"
            if spec.layout.bq_types[j] in _FLOAT_TYPES else
            "BigQuery storage enforces the declared type on every value")
    e.value(
        "field.type_validity", (nonnull_syn - bad) / nonnull_syn,
        scope,
        source_value=((nonnull_src - invalid(s.src)) /
                      nonnull_src if nonnull_src else None),
        synthetic_value=(nonnull_syn - bad) / nonnull_syn,
        detail={
            "invalid": bad,
            "rule": rule
        },
        reads=(_SYNTHETIC,),
        sizes=sizes)


def _grid_ids(grid: _Grid) -> list[str]:
  """The ids a numeric/temporal column's grid block emits, in order."""
  return [
      metric_id for metric_id, reach in APPLIES_TO.items()
      if reach <= _GRID_KINDS and grid.kind in reach
  ]


def _w1(edges: np.ndarray, a: np.ndarray, b: np.ndarray, ma: Moments,
        mb: Moments) -> float | None:
  """W1 on the union bins; two constant sides with too few edges are two
  point masses, |b - a| apart (R20)."""
  if edges.size >= 2:  # noqa: PLR2004 — an interval needs two edges
    return binned.w1_from_bins(edges, a, b)
  if ma.n and mb.n and ma.min == ma.max and mb.min == mb.max:
    return abs(mb.min - ma.min)
  return None


def _legacy_deciles(edges: np.ndarray, counts: np.ndarray,
                    m: Moments) -> list[float] | None:
  """A side's 11 deciles as the legacy rule reads them (APPROX_QUANTILES(x,
  10)): the side's EXACT min and max at 0 and 1, the nine inner deciles
  from its union bins (clipped into [min, max], where every true decile
  lies). None without values or bin edges."""
  if not m.n:
    return None
  inner = binned.quantiles_from_bins(edges, counts, _INNER_DECILE_PROBS)
  if any(np.isnan(inner)):
    return None
  clipped = np.clip(inner, m.min, m.max).tolist()
  return [float(m.min), *clipped, float(m.max)]


def _decile_ks(edges: np.ndarray, a: tuple[np.ndarray, Moments],
               b: tuple[np.ndarray, Moments]) -> float | None:
  qa, qb = _legacy_deciles(edges, *a), _legacy_deciles(edges, *b)
  if qa is None or qb is None:
    return None
  return binned.decile_ks_legacy(qa, qb)


def _binned_metrics(e: _Emitter, gi: int, grid: _Grid, s: _Sides, scope: _Scope,
                    sizes: _Sizes) -> None:
  """ks, pit_w1, wasserstein, jsd, psi and decile_ks_legacy. wasserstein and
  decile_ks_legacy are not_evaluated when either side holds fewer than
  RARE_COUNT values (Ruling R81, extending R80.3): `_w1` and
  `_legacy_deciles` both read the source's raw Moments.min/max with no k
  gate of their own, so a tiny side's exact extremes could otherwise
  constrain published values."""
  cs, cy = s.src.union[gi], s.syn.union[gi]
  cr = None if s.ref is None else s.ref.union[gi]
  ms, my = s.src.moments[gi], s.syn.moments[gi]
  n_src, n_syn = sizes.n_source, sizes.n_synthetic
  tiny = min(ms.n, my.n) < RARE_COUNT
  bracket = binned.ks_bracket(cs, cy)
  assert bracket is not None  # both sides hold values (guarded)
  base = None if cr is None else binned.ks_bracket(cs, cr)
  e.value(
      "column.ks",
      bracket[0],
      scope,
      baseline=None if base is None else base[0],
      method=Method.BINNED,
      noise_floor=noise.noise_floor("ks_two_sample", n=n_src, m=n_syn),
      detail={
          "d_lo": bracket[0],
          "d_hi": bracket[1],
          "edges": int(grid.union.size)
      },
      sizes=sizes)
  pit = binned.pit_w1(cs, cy)
  assert pit is not None
  e.value(
      "column.pit_w1",
      pit,
      scope,
      baseline=None if cr is None else binned.pit_w1(cs, cr),
      method=Method.BINNED,
      sizes=sizes)
  mr = None if s.ref is None else s.ref.moments[gi]
  if tiny:
    e.skip("column.wasserstein", NOT_EVALUATED_BELOW_K, scope, sizes=sizes)
  else:
    w1 = _w1(grid.union, cs, cy, ms, my)
    if w1 is None:
      e.skip(
          "column.wasserstein",
          "fewer than 2 bin edges for a non-constant column",
          scope,
          sizes=sizes)
    else:
      tails = binned.tail_masses(cs, cy)
      e.value(
          "column.wasserstein",
          w1,
          scope,
          baseline=(None if cr is None or mr is None else _w1(
              grid.union, cs, cr, ms, mr)),
          method=Method.BINNED,
          detail={
              **tails, "unit": grid.unit
          },
          sizes=sizes)
  ds, dy = s.src.deciles[gi].tolist(), s.syn.deciles[gi].tolist()
  dr = None if s.ref is None else s.ref.deciles[gi].tolist()
  jsd = distances.jsd_bits(ds, dy)
  assert jsd is not None
  e.value(
      "column.jsd",
      jsd,
      scope,
      baseline=None if dr is None else distances.jsd_bits(ds, dr),
      method=Method.BINNED,
      noise_floor=noise.noise_floor("jsd_null", k=len(ds), n=n_src, m=n_syn),
      detail={"bins": len(ds)},
      sizes=sizes)
  psi = distances.psi(ds, dy)
  assert psi is not None
  e.value(
      "column.psi",
      psi,
      scope,
      baseline=None if dr is None else distances.psi(ds, dr),
      method=Method.BINNED,
      detail={"bins": len(ds)},
      sizes=sizes)
  if grid.kind is _NUMERIC:
    if tiny:
      e.skip(
          "column.decile_ks_legacy", NOT_EVALUATED_BELOW_K, scope, sizes=sizes)
    else:
      legacy = _decile_ks(grid.union, (cs, ms), (cy, my))
      if legacy is None:
        e.skip(
            "column.decile_ks_legacy",
            "no bin edges to read deciles from",
            scope,
            sizes=sizes)
      else:
        e.value(
            "column.decile_ks_legacy",
            legacy,
            scope,
            baseline=(None if cr is None or mr is None else _decile_ks(
                grid.union, (cs, ms), (cr, mr))),
            method=Method.BINNED,
            sizes=sizes)


def _constant(m: Moments) -> bool:
  return m.n > 0 and m.min == m.max


def _same_constant(a: Moments, b: Moments | None) -> bool:
  return b is not None and _constant(a) and _constant(b) and a.min == b.min


def _smd(ms: Moments, other: Moments | None) -> float | None:
  if other is None or other.n == 0:
    return None
  if not _constant(ms):
    assert ms.std is not None
    return abs(other.mean - ms.mean) / ms.std
  return 0.0 if _same_constant(ms, other) else None


def _std_ratio(ms: Moments, other: Moments | None) -> float | None:
  if other is None or other.n == 0 or other.std is None:
    return None
  if not _constant(ms):
    assert ms.std is not None
    return other.std / ms.std
  return 1.0 if _constant(other) else None


def _coverage(ms: Moments, other: Moments | None) -> float | None:
  if other is None or other.n == 0:
    return None
  span = ms.max - ms.min
  if span > 0:
    return max(0.0, min(other.max, ms.max) - max(other.min, ms.min)) / span
  return 1.0 if _same_constant(ms, other) else None


_CONSTANT_SOURCE = {
    "column.smd": "the source column is constant (std = 0): SMD needs a "
                  "source spread",
    "column.std_ratio": "the source column is constant (std = 0) and the "
                        "synthetic one is not: the spread ratio is undefined",
    "column.range_coverage": "the source column is constant (zero range): "
                             "coverage needs a source range",
}


def _moment_metrics(e: _Emitter, gi: int, grid: _Grid, s: _Sides, scope: _Scope,
                    sizes: _Sizes) -> None:
  """smd, std_ratio and range_coverage (exact, from Moments) and, numeric,
  zero_rate_delta. range_coverage compares each side's exact extremes, so
  it is not_evaluated when a side was read as a row sample (R72). smd and
  std_ratio are not_evaluated when either side
  holds fewer than RARE_COUNT values: their value with the synthetic's
  published moments would give the source's mean and std (R80.3). A
  reference with fewer than RARE_COUNT values withholds `baseline_value`
  (R77.6), and `detail.withheld` names exactly that (R80.5)."""
  ms, my = s.src.moments[gi], s.syn.moments[gi]
  mr = None if s.ref is None else s.ref.moments[gi]
  tiny = min(ms.n, my.n) < RARE_COUNT
  baseline_withheld = mr is not None and mr.n < RARE_COUNT
  functions: tuple[tuple[str, Callable[[Moments, Moments | None],
                                       float | None]],
                   ...] = (("column.smd", _smd), ("column.std_ratio",
                                                  _std_ratio),
                           ("column.range_coverage", _coverage))
  side_values = {
      "column.smd": (ms.mean, my.mean),
      "column.std_ratio": (ms.std, my.std),
      "column.range_coverage": (None, None),
  }
  for metric_id, fn in functions:
    sampled = (
        e.sampled_extremes("range coverage")
        if metric_id == "column.range_coverage" else None)
    if sampled is not None:  # R72: exact extremes need every row
      e.skip(metric_id, sampled, scope, sizes=sizes)
      continue
    if tiny and metric_id in _MOMENT_VALUE_IDS:
      e.skip(metric_id, NOT_EVALUATED_BELOW_K, scope, sizes=sizes)
      continue
    value = fn(ms, my)
    if value is None:
      e.skip(metric_id, _CONSTANT_SOURCE[metric_id], scope, sizes=sizes)
      continue
    source_value, synthetic_value = side_values[metric_id]
    detail: dict[str, Any] = (
        _coverage_detail(grid, gi, s)
        if metric_id == "column.range_coverage" else {
            "unit": grid.unit
        })
    if baseline_withheld:
      detail["withheld"] = {"baseline_value": WITHHELD_BELOW_K}
      detail["baseline_reason"] = WITHHELD_BELOW_K
    e.value(
        metric_id,
        value,
        scope,
        baseline=None if baseline_withheld else fn(ms, mr),
        source_value=source_value,
        synthetic_value=synthetic_value,
        detail=detail,
        sizes=sizes)
  if grid.kind is _NUMERIC:
    _share_delta(e, "column.zero_rate_delta", scope,
                 (ms.zeros, my.zeros, None if mr is None else mr.zeros),
                 (ms.n, my.n, None if mr is None else mr.n),
                 "finite non-null values")


def _source_bounds(grid: _Grid, gi: int, s: _Sides) -> dict[str, Any]:
  """The source's p0.5/p99.5 (R65: in place of its exact extremes)."""
  lo, hi = _profile_bounds(grid, s.src, gi, _kept_range(grid, s.src, gi))
  return {"source_p0_5": lo, "source_p99_5": hi}


def _coverage_detail(grid: _Grid, gi: int, s: _Sides) -> dict[str, Any]:
  lo, hi = _profile_bounds(grid, s.syn, gi, _kept_range(grid, s.src, gi))
  return {
      **_source_bounds(grid, gi, s),
      "synthetic_p0_5": lo,
      "synthetic_p99_5": hi,
      "unit": grid.unit,
      "bounds": "each side's exact min and max (not shown, R65/R71)",
  }


def _range_adherence(e: _Emitter, gi: int, grid: _Grid, s: _Sides,
                     scope: _Scope, sizes: _Sizes) -> None:
  if grid.lo is None or grid.hi is None:
    e.skip(
        "field.range_adherence",
        "no source quantile grid (planning saw no finite source value)",
        scope,
        sizes=sizes)
    return
  ms, my = s.src.moments[gi], s.syn.moments[gi]
  inside = int(s.syn.in_range[gi])
  lo, hi = noise.wilson_interval(inside, my.n)
  e.value(
      "field.range_adherence",
      inside / my.n,
      scope,
      method=Method.BINNED,
      source_value=int(s.src.in_range[gi]) / ms.n,
      synthetic_value=inside / my.n,
      ci_low=lo,
      ci_high=hi,
      detail={
          "outside":
              my.n - inside,
          **_source_bounds(grid, gi, s),
          "unit":
              grid.unit,
          "bounds": ("the planning grid's source q0 and q1000, the source "
                     "extremes (not shown, R65)"),
      },
      sizes=sizes)


def _calendar_metrics(e: _Emitter, gi: int, grid: _Grid, s: _Sides,
                      scope: _Scope, sizes: _Sizes) -> None:
  for part in ("dow", "month", "hour"):
    metric_id = f"column.{part}_tvd"
    cs, cy = getattr(s.src, part)[gi], getattr(s.syn, part)[gi]
    if grid.time_only and part != "hour":
      e.skip(metric_id, "a TIME column has no date part", scope, sizes=sizes)
      continue
    if part == "hour" and grid.day_granularity:
      e.skip(
          metric_id,
          "the column is day-granular (no time part)",
          scope,
          sizes=sizes)
      continue
    cr = None if s.ref is None else getattr(s.ref, part)[gi]
    value = distances.tvd(cs, cy)
    assert value is not None  # both sides hold values (guarded)
    e.value(
        metric_id,
        value,
        scope,
        baseline=None if cr is None else distances.tvd(cs, cr),
        noise_floor=noise.noise_floor(
            "tvd_null",
            p=(cs / cs.sum()).tolist(),
            n=sizes.n_source,
            m=sizes.n_synthetic),
        detail={"timezone": "UTC"},
        sizes=sizes)


def _grid_metrics(e: _Emitter, spec: DenseSpec, s: _Sides) -> None:
  for gi, grid in enumerate(spec.grids):
    scope = _Scope(grid.name, None, grid.kind.value)
    ms, my = s.src.moments[gi], s.syn.moments[gi]
    sizes = _Sizes(ms.n, my.n)
    why = _missing("finite non-null values", ms.n, my.n)
    if why:
      for metric_id in _grid_ids(grid):
        e.skip(metric_id, why, scope, sizes=sizes)
      continue
    _range_adherence(e, gi, grid, s, scope, sizes)
    _binned_metrics(e, gi, grid, s, scope, sizes)
    _moment_metrics(e, gi, grid, s, scope, sizes)
    if grid.kind is _TEMPORAL:
      _calendar_metrics(e, gi, grid, s, scope, sizes)


def _class_fractions(p: DenseProfile, si: int) -> dict[str, float]:
  """`shapes.char_class_fractions` from counts (all 0 without values)."""
  nonempty = int(p.str_nonempty[si])
  return {
      name: (int(p.classes[si, k]) / nonempty if nonempty else 0.0)
      for k, name in enumerate(_L1_CLASSES)
  }


def _string_metrics(e: _Emitter, spec: DenseSpec, s: _Sides) -> None:
  ref = s.ref
  for si, column in enumerate(spec.strings):
    scope = _Scope(column.name, None, column.kind.value)
    _share_delta(e, "column.empty_rate_delta", scope,
                 (int(s.src.str_empty[si]), int(s.syn.str_empty[si]),
                  None if ref is None else int(ref.str_empty[si])),
                 (s.src.rows, s.syn.rows, None if ref is None else ref.rows),
                 "rows")
    n_src, n_syn = int(s.src.str_nonnull[si]), int(s.syn.str_nonnull[si])
    sizes = _Sizes(n_src, n_syn)
    why = _missing("non-null values", n_src, n_syn)
    if why:
      e.skip("column.length_ks", why, scope, sizes=sizes)
      e.skip("column.char_class_l1", why, scope, sizes=sizes)
      continue
    hs, hy = s.src.lengths[si], s.syn.lengths[si]
    bracket = binned.ks_bracket(hs, hy)
    assert bracket is not None
    base = None if ref is None else binned.ks_bracket(hs, ref.lengths[si])
    e.value(
        "column.length_ks",
        bracket[0],
        scope,
        baseline=None if base is None else base[0],
        method=Method.BINNED,
        noise_floor=noise.noise_floor("ks_two_sample", n=n_src, m=n_syn),
        detail={
            "d_lo": bracket[0],
            "d_hi": bracket[1]
        },
        sizes=sizes)
    if not s.src.str_nonempty[si]:
      e.skip(
          "column.char_class_l1",
          "no non-empty strings on the source side",
          scope,
          sizes=sizes)
      continue
    fs, fy = _class_fractions(s.src, si), _class_fractions(s.syn, si)
    e.value(
        "column.char_class_l1",
        shapes.char_class_l1(fs, fy),
        scope,
        baseline=(None if ref is None or not ref.str_nonnull[si] else
                  shapes.char_class_l1(fs, _class_fractions(ref, si))),
        detail={
            "source": fs,
            "synthetic": fy
        },
        sizes=_Sizes(int(s.src.str_nonempty[si]), int(s.syn.str_nonempty[si])))


# ---- pairs ----------------------------------------------------------------
class _Side(NamedTuple):
  """One side's statistic for a pair: its value (a degenerate side
  resolved per R20) and state: ok | constant | empty | undefined."""
  value: float | None
  state: str


def _correlation(row: np.ndarray | None) -> _Side:
  if row is None or row[0] <= 0:
    return _Side(None, "empty")
  if row[0] < _MIN_CORRELATION_ROWS:
    return _Side(None, "undefined")
  rho = float(dependence.pearson_from_comoments(row[np.newaxis, :])[0])
  return _Side(0.0, "constant") if np.isnan(rho) else _Side(rho, "ok")


def _association(fn: Callable[[np.ndarray], float | None],
                 table: np.ndarray | None) -> _Side:
  if table is None or table.sum() <= 0:
    return _Side(None, "empty")
  occupied = (np.count_nonzero(table.sum(axis=1)),
              np.count_nonzero(table.sum(axis=0)))
  if min(occupied) < _MIN_OCCUPIED:
    return _Side(0.0, "constant")
  value = fn(table)
  return _Side(None, "undefined") if value is None else _Side(value, "ok")


_STATE_REASON = {
    "empty": "no {what} on the {side} side",
    "undefined": "the statistic is undefined on the {side} side ({why})",
}


def _pair_value(src: _Side, syn: _Side, what: str,
                why: str) -> tuple[float | None, str | None, dict]:
  """|src - syn| under R20, or (None, reason, {})."""
  for side, state in ((_SOURCE, src), (_SYNTHETIC, syn)):
    if state.state in _STATE_REASON:
      return None, _STATE_REASON[state.state].format(
          what=what, side=side, why=why), {}
  if src.state == "constant" and syn.state != "constant":
    return None, ("a pair column is constant on the source side: the "
                  "association is undefined"), {}
  assert src.value is not None and syn.value is not None
  notes = {}
  if syn.state == "constant":
    notes["synthetic_constant"] = True
  return abs(src.value - syn.value), None, notes


def _pair_baseline(src: _Side, ref: _Side | None) -> float | None:
  if ref is None or ref.value is None or src.value is None:
    return None
  if src.state == "constant" and ref.state != "constant":
    return None
  return abs(src.value - ref.value)


def _mi_floor(table: np.ndarray) -> float:
  """The plug-in MI bias (Treves & Panzeri, 1995) of one side, on the NMI
  scale: `(r-1)(c-1)/(2n)` nats over min(H_X, H_Y) nats."""
  rows, cols = table.sum(axis=1), table.sum(axis=0)
  r, c = np.count_nonzero(rows), np.count_nonzero(cols)
  if min(r, c) < _MIN_OCCUPIED:
    return 0.0
  bias = noise.noise_floor("mi_bias", r=int(r), c=int(c), n=int(table.sum()))
  h = min(float(entropy(rows[rows > 0])), float(entropy(cols[cols > 0])))
  return 0.0 if bias is None or h <= 0 else bias / h


class _PairTables(NamedTuple):
  src: dependence.BivariateAccumulator
  syn: dependence.BivariateAccumulator
  ref: dependence.BivariateAccumulator | None


def _correlation_metric(e: _Emitter, metric_id: str,
                        rows: tuple[np.ndarray, np.ndarray,
                                    np.ndarray | None], scope: _Scope,
                        method: Method) -> tuple[float, float | None] | None:
  """A Pearson/Spearman delta row; returns the signed (syn, ref) deltas."""
  src, syn = _correlation(rows[0]), _correlation(rows[1])
  ref = None if rows[2] is None else _correlation(rows[2])
  n_src, n_syn = int(rows[0][0]), int(rows[1][0])
  sizes = _Sizes(n_src, n_syn)
  value, reason, notes = _pair_value(src, syn, "pairwise-complete rows",
                                     "fewer than 3 pairwise-complete rows")
  if value is None:
    e.skip(metric_id, reason or "undefined", scope, sizes=sizes)
    return None
  baseline = _pair_baseline(src, ref)
  e.value(
      metric_id,
      value,
      scope,
      baseline=baseline,
      method=method,
      source_value=src.value,
      synthetic_value=syn.value,
      noise_floor=noise.noise_floor("fisher_z", n=n_src, m=n_syn),
      detail=notes,
      sizes=sizes)
  assert src.value is not None and syn.value is not None
  signed_ref = (None if baseline is None or ref is None or ref.value is None
                else src.value - ref.value)
  return src.value - syn.value, signed_ref


def _association_metrics(e: _Emitter, p: int, tables: _PairTables,
                         scope: _Scope) -> float | None:
  """cramers_v_delta, nmi_delta and contingency_tvd of pair p; returns the
  contingency TVD (for the top-divergent contingency profiles)."""
  ts, ty = tables.src.counts2d[p], tables.syn.counts2d[p]
  tr = None if tables.ref is None else tables.ref.counts2d[p]
  n_src, n_syn = int(ts.sum()), int(ty.sum())
  sizes = _Sizes(n_src, n_syn)
  for metric_id, fn in (("pair.cramers_v_delta",
                         dependence.cramers_v_bias_corrected),
                        ("pair.nmi_delta", dependence.nmi_min)):
    src, syn = _association(fn, ts), _association(fn, ty)
    value, reason, notes = _pair_value(src, syn, "rows",
                                       "too few rows for the bias correction")
    if value is None:
      e.skip(metric_id, reason or "undefined", scope, sizes=sizes)
      continue
    extra: dict[str, Any] = {}
    if metric_id == "pair.nmi_delta":
      extra["noise_floor"] = _mi_floor(ts) + _mi_floor(ty)
      notes["noise_floor_basis"] = ("Σ over both sides of (r-1)(c-1)/(2n) "
                                    "nats / min(H_X, H_Y)")
    e.value(
        metric_id,
        value,
        scope,
        baseline=_pair_baseline(src,
                                None if tr is None else _association(fn, tr)),
        source_value=src.value,
        synthetic_value=syn.value,
        detail=notes,
        **extra,
        sizes=sizes)
  tvd = dependence.contingency_tvd(ts, ty)
  if tvd is None:
    e.skip(
        "pair.contingency_tvd",
        _missing("rows", n_src, n_syn) or "empty joint table",
        scope,
        sizes=sizes)
    return None
  e.value(
      "pair.contingency_tvd",
      tvd,
      scope,
      baseline=None if tr is None else dependence.contingency_tvd(ts, tr),
      noise_floor=noise.noise_floor(
          "tvd_null", p=(ts / n_src).ravel().tolist(), n=n_src, m=n_syn),
      sizes=sizes)
  return tvd


def _feature_digest(names: Iterable[str]) -> str:
  text = json.dumps(sorted(names), separators=(",", ":"))
  return hashlib.blake2b(text.encode(), digest_size=16).hexdigest()


def _pair_metrics(e: _Emitter, spec: DenseSpec, s: _Sides) -> dict[int, float]:
  """Every pair metric, then table.corr_rms_delta / corr_max_delta;
  returns each pair's contingency TVD."""
  tvds: dict[int, float] = {}
  deltas: list[float] = []
  bases: list[float] = []
  numeric: set[str] = set()
  if s.src.bivariate is None or s.syn.bivariate is None:
    _corr_summary(e, s, deltas, bases, numeric)  # no pairs: say so
    return tvds
  tables = _PairTables(s.src.bivariate, s.syn.bivariate,
                       None if s.ref is None else s.ref.bivariate)
  for p, (a, b) in enumerate(spec.pairs):
    ca, cb = spec.pair_columns[a], spec.pair_columns[b]
    scope = _Scope(ca.name, cb.name, ca.kind.value)
    if ca.grid and cb.grid:
      numeric.update((ca.name, cb.name))
      ref_std = None if tables.ref is None else tables.ref.com_std[p]
      ref_pit = None if tables.ref is None else tables.ref.com_pit[p]
      signed = _correlation_metric(
          e, "pair.pearson_delta",
          (tables.src.com_std[p], tables.syn.com_std[p], ref_std), scope,
          Method.EXACT)
      deltas.append(np.nan if signed is None else signed[0])
      bases.append(np.nan if signed is None or signed[1] is None else signed[1])
      _correlation_metric(
          e, "pair.spearman_delta",
          (tables.src.com_pit[p], tables.syn.com_pit[p], ref_pit), scope,
          Method.BINNED)
    tvd = _association_metrics(e, p, tables, scope)
    if tvd is not None:
      tvds[p] = tvd
  _corr_summary(e, s, deltas, bases, numeric)
  return tvds


def _corr_summary(e: _Emitter, s: _Sides, deltas: Sequence[float],
                  bases: Sequence[float], numeric: set[str]) -> None:
  sizes = _Sizes(s.src.rows, s.syn.rows)
  why = _missing("rows", s.src.rows, s.syn.rows)
  if why is None and not deltas:
    why = "fewer than two numeric or temporal columns among the plan's pairs"
  rms, largest = (None, None) if why else dependence.corr_rms_max(
      np.array(deltas, dtype=np.float64))
  if why is None and rms is None:
    why = "no numeric pair has a correlation on both sides"
  if why is not None or rms is None or largest is None:
    reason = why or "undefined"
    e.skip("table.corr_rms_delta", reason, _TABLE_SCOPE, sizes=sizes)
    e.skip("table.corr_max_delta", reason, _TABLE_SCOPE, sizes=sizes)
    return
  base_rms, base_max = dependence.corr_rms_max(
      np.array(bases, dtype=np.float64))
  digest = _feature_digest(numeric)
  finite = int(np.count_nonzero(np.isfinite(deltas)))
  for metric_id, value, base in (("table.corr_rms_delta", rms, base_rms),
                                 ("table.corr_max_delta", largest, base_max)):
    e.value(
        metric_id,
        value,
        _TABLE_SCOPE,
        baseline=base,
        feature_set_digest=digest,
        detail={
            "pairs": finite,
            "columns": sorted(numeric)
        },
        sizes=sizes)


# ---- row patterns ---------------------------------------------------------
def _exact_on(p: DenseProfile) -> Callable[[int], bool]:
  """Whether a pattern's count on side p is exact: always without an
  overflow, else when it ranks within the retained bottom-k."""
  if not p.null_overflow:
    return lambda pattern: True
  horizon = max(map(_pattern_priority, p.null_patterns))
  return lambda pattern: _pattern_priority(pattern) <= horizon


def _pattern_vector(p: DenseProfile, head: Sequence[int]) -> list[int]:
  counts = [p.null_patterns.get(pattern, 0) for pattern in head]
  return [*counts, p.rows - sum(counts)]


def _null_pattern_metric(e: _Emitter, spec: DenseSpec, s: _Sides) -> None:
  metric_id = "row.null_pattern_tvd"
  sizes = _Sizes(s.src.rows, s.syn.rows)
  layout = spec.layout
  if not layout.null_bits_complete:
    e.skip(
        metric_id, f"the table has {len(layout.columns)} columns and null "
        "patterns cover the first 64 only (null_bits), so the pattern "
        "mix is not evaluated (R60)",
        _TABLE_SCOPE,
        sizes=sizes)
    return
  why = _missing("rows", s.src.rows, s.syn.rows)
  if why:
    e.skip(metric_id, why, _TABLE_SCOPE, sizes=sizes)
    return
  exact_syn = _exact_on(s.syn)
  candidates = [p for p in s.src.null_patterns if exact_syn(p)]
  head = sorted(
      candidates, key=lambda p: (-s.src.null_patterns[p], p))[:NULL_PATTERN_TOP]
  vs, vy = _pattern_vector(s.src, head), _pattern_vector(s.syn, head)
  baseline = None
  if s.ref is not None and s.ref.rows and all(map(_exact_on(s.ref), head)):
    baseline = distances.tvd(vs, _pattern_vector(s.ref, head))
  value = distances.tvd(vs, vy)
  assert value is not None
  capped = bool(s.src.null_overflow or s.syn.null_overflow)
  notes: dict[str, Any] = {"capped_head": capped}
  if capped:
    notes["note"] = (
        "the null-pattern cap was hit: the head is the top source patterns "
        "among those counted exactly on both sides, which can deviate from "
        "the catalogue's top-64 source patterns")
  e.value(
      metric_id,
      value,
      _TABLE_SCOPE,
      baseline=baseline,
      noise_floor=noise.noise_floor(
          "tvd_null",
          p=[c / s.src.rows for c in vs],
          n=s.src.rows,
          m=s.syn.rows),
      detail={
          "patterns": len(head),
          "tail_share_source": vs[-1] / s.src.rows,
          "tail_share_synthetic": vy[-1] / s.syn.rows,
          **notes,
      },
      sizes=sizes)


def _metrics(spec: DenseSpec,
             s: _Sides) -> tuple[list[MetricValue], dict[int, float]]:
  e = _Emitter(spec, has_reference=s.ref is not None)
  _column_basics(e, spec, s)
  _grid_metrics(e, spec, s)
  _string_metrics(e, spec, s)
  tvds = _pair_metrics(e, spec, s)
  _null_pattern_metric(e, spec, s)
  return e.rows, tvds


# --------------------------------------------------------------------------
# profiles
# --------------------------------------------------------------------------
def _json_number(x: Any) -> float | None:
  if x is None:
    return None
  number = float(x)
  return number if np.isfinite(number) else None


def _edges_digest(edges: np.ndarray, unit: str) -> str:
  payload = unit.encode() + np.asarray(edges, dtype="<f8").tobytes()
  return hashlib.blake2b(payload, digest_size=16).hexdigest()


def _kept_edges(right: np.ndarray, left: np.ndarray) -> np.ndarray:
  """Which edges may be shown or interpolated over, decided on the SOURCE
  side's evaluated counts alone (R69, R77.1): an edge is kept iff at
  least RARE_COUNT source records lie at or below it AND at least
  RARE_COUNT at or above it. `right`/`left` are the source's right- and
  left-closed bin counts on the edges, so count(x <= e) and count(x >= e)
  are exact. Both counts are monotone in the edge, so the kept edges are
  one contiguous range: everything outside it is dropped whatever grid
  contributed it, everything inside stays. A common end atom qualifies,
  a lone extreme never does; an empty source keeps nothing."""
  below = np.cumsum(right)[:-1]  # count(x <= e)
  under = np.cumsum(left)[:-1]  # count(x < e)
  above = int(right.sum()) - under  # count(x >= e)
  kept: np.ndarray = (below >= RARE_COUNT) & (above >= RARE_COUNT)
  return kept


def _spaced(kept: np.ndarray, below: np.ndarray) -> np.ndarray:
  """The kept edges thinned for publication (Ruling R113): the first kept
  edge, then each next one once at least RARE_COUNT source records lie in
  the bin since the last published edge — `below[i]` is the source's
  exact count(x <= edge i), so the bin (last, e] holds `below[i] -
  below[last]`. No two published edges are then closer than k source
  records apart: on a small column the plan's grid points are single
  source records a record or two apart, and publishing them all would
  publish most of the column. A dropped edge's two bins merge (counts
  stay sums). Greedy from the left over publishable counts only, as the
  kept range is, so every side shares the result and a rerun repeats it;
  a column whose bins already hold k source records is unchanged."""
  out = np.zeros(kept.size, dtype=bool)
  last: int | None = None
  for i in np.flatnonzero(kept).tolist():
    count = int(below[i])
    if last is None or count - last >= RARE_COUNT:
      out[i] = True
      last = count
  return out


def _published(p: DenseProfile, gi: int) -> np.ndarray:
  """The profile (histogram) edges published for the source profile `p`:
  those `_kept_edges` keeps, thinned so that neighbouring ones are at
  least k source records apart (`_spaced`); the same mask serves every
  side, whose edges all come from the source grid. The publication step
  only: no metric reads it."""
  right = p.profile[gi]
  return _spaced(_kept_edges(right, p.profile_left[gi]), np.cumsum(right)[:-1])


def _kept_union(src: DenseProfile, gi: int) -> np.ndarray:
  """The union edges every side's quantiles and bounds may interpolate
  over: `_kept_edges` on the source's union counts. No kept edge is a
  source value with fewer than k records at or beyond it, so no plateau
  can land on one, and nothing outside [first kept, last kept] is ever
  published as a value (R77.1)."""
  return _kept_edges(src.union[gi], src.union_left[gi])


def _kept_range(grid: _Grid, src: DenseProfile, gi: int) -> np.ndarray:
  """`_kept_union`, except that a column with no evaluated source value
  (no source side, or a NULL column) keeps exactly the synthetic grid's
  own points — every one of them, including a point a source value
  coincides with — and drops the points only the source grid or the
  atoms contributed (plan-time source records). The kept set is then a
  function of the synthetic alone, so the payloads cannot depend on the
  unevaluated source (R77.3 as amended by R80.1)."""
  if not src.union[gi].sum():
    return grid.synthetic_edge
  return _kept_union(src, gi)


def _tail_masses(p: DenseProfile, gi: int,
                 kept: np.ndarray) -> tuple[float | None, float | None]:
  """`(below_mass, above_mass)`: the share of side `p`'s non-null values
  below the first kept edge and above the last (R77.2) — what the payload
  says about the mass whose values are withheld; None with no kept edge
  or no values."""
  total = float(p.union[gi].sum())
  if not total or not kept.any():
    return None, None
  first = int(np.argmax(kept))
  last = kept.size - 1 - int(np.argmax(kept[::-1]))
  below = float(np.cumsum(p.union_left[gi])[first]) / total  # count(x < e)
  above = 1.0 - float(np.cumsum(p.union[gi])[last]) / total  # count(x > e)
  return below, above


def _inverted(edges: np.ndarray, f_at: np.ndarray, f_before: np.ndarray,
              probs: Sequence[float]) -> list[float]:
  """Q(p) from a side's exact CDF at the kept edges: `f_at[i]` is the
  share at or below `edges[i]` and `f_before[i]` the share below it (the
  left-closed twin), so a p within an edge's jump `(F⁻(e), F(e)]` is that
  edge exactly (an atom's mass sits on it) and a p between two jumps
  interpolates linearly over the mass strictly inside the bin. The caller
  passes only p within `(F⁻(first edge), F(last edge)]`, so nothing is
  ever extrapolated (R74.3)."""
  p = np.asarray(probs, dtype=np.float64)
  if not p.size:
    return []
  i = np.searchsorted(f_at, p, side="left")  # the first F(e_i) >= p
  on_edge = f_before[i] < p  # p inside the jump at e_i
  j = np.maximum(i - 1, 0)  # else F(e_j) < p <= F⁻(e_i), and i >= 1
  span = f_before[i] - f_at[j]
  frac = (p - f_at[j]) / np.where(span > 0, span, 1.0)
  inside = edges[j] + frac * (edges[i] - edges[j])
  return [float(v) for v in np.where(on_edge, edges[i], inside)]


def _side_quantiles(grid: _Grid, p: DenseProfile, gi: int, kept: np.ndarray,
                    probs: Sequence[float]) -> tuple[list[float], list[float]]:
  """`(probs, values)`: a side's quantiles over its union bins merged
  across the dropped edges, at the `probs` its merged CDF spans — a
  probability outside `(F⁻(first kept edge), F(last kept edge)]` is
  withheld, never clamped (R77.2); the jump at an edge is exact from the
  left-closed twin, so a common atom at a side's minimum is its own lower
  quantiles."""
  edges = grid.union[kept]
  right = _merged(p.union[gi], kept)
  left = _merged(p.union_left[gi], kept)
  total = float(right.sum())
  if not edges.size or total <= 0:
    return [], []
  f_at = np.cumsum(right)[:-1] / total  # F(e): share at or below e
  f_before = np.cumsum(left)[:-1] / total  # F⁻(e): share below e
  shown = [q for q in probs if f_before[0] < q <= f_at[-1]]
  return shown, _inverted(edges, f_at, f_before, shown)


def _profile_bounds(grid: _Grid, p: DenseProfile, gi: int,
                    kept: np.ndarray) -> tuple[float | None, float | None]:
  """A side's p0.5 / p99.5 over the merged union bins — what every side's
  payloads and details show instead of its exact extremes (R65, R71).
  Published only when n * 0.005 >= RARE_COUNT, so at least k records lie
  at or beyond each (R69), and only where the merged CDF spans the
  probability (R74.3); a bound outside the side's own [min, max] is an
  interpolation artifact and is withheld too."""
  m = p.moments[gi]
  if m.n * _BOUND_PROBS[0] < RARE_COUNT:
    return None, None
  probs, values = _side_quantiles(grid, p, gi, kept, _BOUND_PROBS)
  shown = {
      q: v
      for q, v in zip(probs, values, strict=True)
      if np.isfinite(v) and m.min <= v <= m.max
  }
  return shown.get(_BOUND_PROBS[0]), shown.get(_BOUND_PROBS[1])


def _merged(counts: np.ndarray, kept: np.ndarray) -> np.ndarray:
  """Bin counts on `edges + 1` bins, re-binned on the kept edges only
  (a dropped edge's two bins merge)."""
  starts = np.concatenate(([0], np.flatnonzero(kept) + 1))
  merged: np.ndarray = np.add.reduceat(counts, starts)
  return merged


def _tail_safe(probs: Sequence[float], n: int) -> list[float]:
  """The quantile probabilities a side of n values may publish: at least
  RARE_COUNT of its values at or beyond each, in either tail (R69), and
  neighbouring ones at least `_QUANTILE_GAP` of its values apart — a
  spacing of k / n in probability, greedy from the lowest (Ruling R113:
  on a 120-value column the 99 percentiles are 1.2 records apart, so
  publishing them all publishes most of the column). The gap is compared
  in basis points, exactly; n >= 1,000 keeps every percentile."""
  out: list[float] = []
  last: int | None = None
  for q in probs:
    if q * n < RARE_COUNT or (1.0 - q) * n < RARE_COUNT:
      continue
    at = round(q * _BASIS_POINTS)
    if last is not None and (at - last) * n < _QUANTILE_GAP * _BASIS_POINTS:
      continue
    out.append(q)
    last = at
  return out


def _grid_profiles(spec: DenseSpec, p: DenseProfile,
                   src: DenseProfile) -> Iterator[ProfileValue]:
  for gi, grid in enumerate(spec.grids):
    m = p.moments[gi]
    kept = _published(src, gi)
    safe = _kept_range(grid, src, gi)
    edges = grid.profile[kept]
    # One published set per column (Ruling R116): every exact source value
    # a payload can show is one of these histogram edges. A column with no
    # evaluated source value publishes none (its grid is the synthetic's).
    grid_mask = np.isin(grid.union, edges) if src.union[gi].sum() else safe
    unit = "epoch_seconds" if grid.kind is _TEMPORAL else "value"
    scale = grid.scale

    def scaled(x: Any, scale: float = scale) -> float | None:
      number = _json_number(x)
      return None if number is None else number * scale

    low, high = _profile_bounds(grid, p, gi, safe)
    below_mass, above_mass = _tail_masses(p, gi, safe)
    extremes = "p0.5_p99.5"

    yield ProfileValue(
        profile_kind="histogram",
        payload={
            "edges": [float(v) * scale for v in edges],
            "counts": _merged(p.profile[gi], kept).tolist(),
            "min": scaled(low),
            "max": scaled(high),
            "extremes": extremes,
            "below_mass": below_mass,
            "above_mass": above_mass,
            "nulls": int(p.nulls[grid.j]),
            "unit": unit,
        },
        n=m.n,
        edges_digest=_edges_digest(edges, grid.unit),
        table=spec.table,
        side=p.side,
        column=grid.name)
    probs, values = _side_quantiles(grid, p, gi, grid_mask,
                                    _tail_safe(_QUANTILE_PROBS, m.n))
    quantile_below, quantile_above = _tail_masses(p, gi, grid_mask)
    if probs and np.isfinite(values).all():  # the GUI's values are numbers
      yield ProfileValue(
          profile_kind="quantiles",
          payload={
              "probs": probs,
              "values": [scaled(v) for v in values],
              "below_mass": quantile_below,
              "above_mass": quantile_above,
              "unit": unit,
          },
          n=m.n,
          table=spec.table,
          side=p.side,
          column=grid.name)
    # R74.5: n < k values determine the moments; withheld below the floor
    shown = m.n >= RARE_COUNT
    yield ProfileValue(
        profile_kind="moments",
        payload={
            "n":
                m.n,
            "mean":
                scaled(m.mean) if shown else None,
            "std":
                scaled(m.std) if shown else None,
            "skewness":
                _json_number(m.skewness) if shown else None,
            "kurtosis_excess":
                _json_number(m.kurtosis_excess) if shown else None,
            "min":
                scaled(low),
            "max":
                scaled(high),
            "extremes":
                extremes,
            "zeros":
                m.zeros,
            "unit":
                unit,
        },
        n=m.n,
        table=spec.table,
        side=p.side,
        column=grid.name)
    if grid.kind is _TEMPORAL:
      payload: dict[str, Any] = {
          "dow": p.dow[gi].tolist(),
          "month": p.month[gi].tolist(),
          "hour": [] if grid.day_granularity else p.hour[gi].tolist(),
          "day_granularity": grid.day_granularity,
      }
      if grid.time_only:
        payload["time_only"] = True
      yield ProfileValue(
          profile_kind="temporal_mix",
          payload=payload,
          n=m.n,
          table=spec.table,
          side=p.side,
          column=grid.name)


def _string_profiles(spec: DenseSpec,
                     p: DenseProfile) -> Iterator[ProfileValue]:
  for si, column in enumerate(spec.strings):
    lengths = p.lengths[si]
    seen = np.flatnonzero(lengths[:LENGTH_OVERFLOW])
    yield ProfileValue(
        profile_kind="length_hist",
        payload={
            "lengths": seen.tolist(),
            "counts": lengths[seen].tolist(),
            "overflow": int(lengths[LENGTH_OVERFLOW]),
        },
        n=int(p.str_nonnull[si]),
        table=spec.table,
        side=p.side,
        column=column.name)
    nonempty = int(p.str_nonempty[si])
    yield ProfileValue(
        profile_kind="char_classes",
        payload={
            "classes": {
                name: (int(p.classes[si, k]) / nonempty if nonempty else 0.0)
                for k, name in enumerate(CHAR_CLASSES)
            },
            "n": nonempty,
        },
        n=nonempty,
        table=spec.table,
        side=p.side,
        column=column.name)


def _null_pattern_profile(spec: DenseSpec,
                          p: DenseProfile) -> Iterator[ProfileValue]:
  layout = spec.layout
  if not layout.null_bits_complete:
    return
  width = len(layout.columns)
  ranked = sorted(p.null_patterns.items(), key=lambda kv: (-kv[1], kv[0]))
  yield ProfileValue(
      table=spec.table,
      side=p.side,
      profile_kind="null_patterns",
      payload={
          "columns": list(layout.columns),
          "patterns": [{
              "bits":
                  "".join("1" if pattern >> i & 1 else "0"
                          for i in range(width)),
              "count":
                  count,
              "share":
                  count / p.rows if p.rows else 0.0,
          }
                       for pattern, count in ranked],
          "overflow": p.null_overflow,
      },
      n=p.rows,
      truncated=bool(p.null_overflow))


def _corr_profiles(spec: DenseSpec, p: DenseProfile) -> Iterator[ProfileValue]:
  acc = p.bivariate
  if acc is None:
    return
  pearson = dependence.pearson_from_comoments(acc.com_std)
  spearman = dependence.pearson_from_comoments(acc.com_pit)
  cramers = [
      dependence.cramers_v_bias_corrected(acc.counts2d[k])
      for k in range(len(spec.pairs))
  ]
  numeric = [c for c, pc in enumerate(spec.pair_columns) if pc.grid]
  every = list(range(len(spec.pair_columns)))
  for method, members, values in (("pearson", numeric,
                                   pearson), ("spearman", numeric, spearman),
                                  ("cramers_v", every, cramers)):
    if len(members) < _MIN_OCCUPIED:
      continue
    index = {c: i for i, c in enumerate(members)}
    matrix: list[list[float | None]] = [[
        1.0 if i == k else None for k in range(len(members))
    ] for i in range(len(members))]
    for k, (a, b) in enumerate(spec.pairs):
      if a in index and b in index:
        cell = _json_number(values[k])
        matrix[index[a]][index[b]] = matrix[index[b]][index[a]] = cell
    yield ProfileValue(
        table=spec.table,
        side=p.side,
        profile_kind="corr_matrix",
        payload={
            "columns": [spec.pair_columns[c].name for c in members],
            "method": method,
            "values": matrix,
            "n": p.rows,
        },
        n=p.rows)


def _edge_labels(edges: Sequence[float], kind: ColumnKind) -> list[str]:
  """Readable labels of a pair axis's decile edges, never two alike: the
  fewest significant digits (numbers) or the coarsest unit (timestamps,
  UTC) that tell every edge apart; `repr` (always distinct) otherwise."""
  values = [float(e) for e in edges]
  if kind is _TEMPORAL and all(e.is_integer() for e in values):
    stamps = np.array(values, dtype=np.int64).astype("datetime64[us]")
    for unit in _TIME_UNITS:
      labels = [str(v) for v in np.datetime_as_string(stamps, unit=unit)]
      if len(set(labels)) == len(values):
        return labels
  else:
    for digits in _EDGE_DIGITS:
      labels = [f"{e:.{digits}g}" for e in values]
      if len(set(labels)) == len(values):
        return labels
  return [repr(e) for e in values]


def _decile_kept(column: _PairColumn, grid: _Grid, src: DenseProfile,
                 gi: int) -> tuple[np.ndarray, np.ndarray]:
  """`(kept, shown)`: the pair axis's decile cuts a label may show, and the
  value each is labelled with. A cut is a decile that is a profile edge kept
  by the count rule (R69); it is labelled with the nearest PUBLISHED
  histogram edge at or below it (Ruling R116: the axis publishes no source
  value the histogram does not), and when several cuts fall on one edge only
  the lowest is kept — so the labels are k source records apart because the
  published edges are, and a column of fewer than 100 values gets a coarser
  axis. The cut's counts are the decile's, so a label may name an edge up
  to one bin below the cut. `shown[i]` is meaningful where `kept[i]`."""
  kept = np.zeros(column.deciles.size, dtype=bool)
  shown = np.full(column.deciles.size, np.nan)
  if not grid.profile.size:
    return kept, shown
  right = src.profile[gi]
  profile_kept = _kept_edges(right, src.profile_left[gi])
  index = np.searchsorted(grid.profile, column.deciles)
  found = index < grid.profile.size
  found[found] = grid.profile[index[found]] == column.deciles[found]
  kept[found] = profile_kept[index[found]]
  published = grid.profile[_published(src, gi)]
  at = np.searchsorted(published, column.deciles, side="right") - 1
  kept &= at >= 0  # no published edge at or below: the cut merges
  below = np.zeros(column.deciles.size, dtype=np.int64)
  below[found] = np.cumsum(right)[:-1][index[found]]
  kept = _spaced(kept, below)  # the cuts themselves k records apart
  last = -1
  for i in np.flatnonzero(kept).tolist():
    if at[i] == last:  # a lower cut already sits on this edge
      kept[i] = False
    else:
      last = int(at[i])
      shown[i] = published[last]
  return kept, shown


def _axis(
    column: _PairColumn, literals: Mapping[int, str], label_key: bytes,
    kept: tuple[np.ndarray, np.ndarray] | None
) -> tuple[list[list[int]], list[str]]:
  """The pair-grid slot groups an axis shows and their labels: decile bins
  merged across the edges R69 withholds; D6 for dictionary values (a
  literal only where `literal_ok`, else a keyed hashed label, R64)."""
  if column.grid:
    assert kept is not None
    cut, labelled = kept
    shown = np.flatnonzero(cut)
    if not shown.size:
      return [list(range(column.deciles.size + 1)),
              [PAIR_BINS]], ["any", "NULL"]
    edges = _edge_labels(labelled[shown].tolist(), column.kind)
    starts = [0, *(int(k) + 1 for k in shown), column.deciles.size + 1]
    groups = [list(range(a, b)) for a, b in itertools.pairwise(starts)]
    labels = [f"<= {edges[0]}"]
    labels += [f"({a}, {b}]" for a, b in itertools.pairwise(edges)]
    labels.append(f"> {edges[-1]}")
    return [*groups, [PAIR_BINS]], [*labels, "NULL"]
  labels = []
  for code in column.dictionary:
    literal = column.bool_labels.get(code) or literals.get(code)
    labels.append(
        literal if column.literal_ok and literal is not None else hashed_label(
            code, key=label_key))
  slots = [*range(len(column.dictionary)), _OTHER_SLOT, PAIR_BINS]
  return [[slot] for slot in slots], [*labels, "other", "NULL"]


def _grouped(table: np.ndarray, rows: Sequence[Sequence[int]],
             cols: Sequence[Sequence[int]]) -> list[list[int]]:
  return [[int(table[np.ix_(r, c)].sum()) for c in cols] for r in rows]


def _contingency_profiles(spec: DenseSpec, sides: Mapping[str, DenseProfile],
                          tvds: Mapping[int, float],
                          label_key: bytes) -> Iterator[ProfileValue]:
  src, syn = sides.get(_SOURCE), sides.get(_SYNTHETIC)
  if src is None or syn is None or src.bivariate is None:
    return
  literals = _merge_literals(src.literals, syn.literals)
  grid_of = {g.j: gi for gi, g in enumerate(spec.grids)}

  def kept(column: _PairColumn) -> tuple[np.ndarray, np.ndarray] | None:
    if not column.grid:
      return None
    gi = grid_of[column.j]
    return _decile_kept(column, spec.grids[gi], src, gi)

  top = sorted(tvds, key=lambda k: (-tvds[k], k))[:CONTINGENCY_TOP_PAIRS]
  for k in sorted(top):
    a, b = spec.pairs[k]
    ca, cb = spec.pair_columns[a], spec.pair_columns[b]
    x_groups, x_labels = _axis(ca, literals.get(a, {}), label_key, kept(ca))
    y_groups, y_labels = _axis(cb, literals.get(b, {}), label_key, kept(cb))
    for p in (src, syn):
      assert p.bivariate is not None
      counts = _grouped(p.bivariate.counts2d[k], x_groups, y_groups)
      yield ProfileValue(
          table=spec.table,
          side=p.side,
          profile_kind="contingency",
          column=ca.name,
          payload={
              "column_x": ca.name,
              "column_y": cb.name,
              "x_labels": x_labels,
              "y_labels": y_labels,
              "counts": counts,
          },
          n=int(p.bivariate.counts2d[k].sum()))


def _profiles(spec: DenseSpec, present: Mapping[str, DenseProfile],
              tvds: Mapping[int,
                            float], label_key: bytes) -> list[ProfileValue]:
  out: list[ProfileValue] = []
  src = present.get(_SOURCE) or DenseProfile.empty(spec, _SOURCE)
  for side in _SIDE_ORDER:
    p = present.get(side)
    if p is None:
      continue
    out.extend(_grid_profiles(spec, p, src))
    out.extend(_string_profiles(spec, p))
    out.extend(_null_pattern_profile(spec, p))
    out.extend(_corr_profiles(spec, p))
  out.extend(_contingency_profiles(spec, present, tvds, label_key))
  rates = spec.sample_rates()
  if not rates:
    return out
  # R72: a profile of a row-sampled side says so in its payload
  return [
      dataclasses.replace(
          value, payload={
              **value.payload, "sample_rate": rates[value.side]
          }) if value.side in rates else value for value in out
  ]


def _checked_key(label_key: Any) -> bytes:
  if not isinstance(label_key, bytes) or not label_key:
    raise ValueError("label_key must be non-empty bytes (Ruling R64)")
  return label_key


def dense_outputs(
    spec: DenseSpec, profiles: Mapping[str, DenseProfile], *,
    label_key: bytes) -> tuple[list[MetricValue], list[ProfileValue]]:
  """One table's metrics and profile payloads from its per-side profiles.

  A side with no rows (no profile) counts as empty: every metric that
  needs it is `not_evaluated` with a reason naming the side, so the set
  of (metric, scope) rows never depends on which sides arrived.
  `label_key` keys every hashed label (`canonical.hashed_label`, R64); it
  never reaches a payload or a detail.

  Raises:
    ValueError: a profile of another table, or filed under another side;
      an empty `label_key`.
  """
  label_key = _checked_key(label_key)
  for side, p in profiles.items():
    if p.table != spec.table or p.side != Side(side).value:
      raise ValueError(f"the {p.table}/{p.side} profile was given as "
                       f"{spec.table}/{side}")
  present = {Side(side).value: p for side, p in profiles.items()}
  sides = _Sides(
      src=present.get(_SOURCE) or DenseProfile.empty(spec, _SOURCE),
      syn=present.get(_SYNTHETIC) or DenseProfile.empty(spec, _SYNTHETIC),
      ref=present.get(_REFERENCE))
  metrics, tvds = _metrics(spec, sides)
  return metrics, _profiles(spec, present, tvds, label_key)


# --------------------------------------------------------------------------
# Beam
# --------------------------------------------------------------------------
def _keyed_profile(
    batch: EncodedBatch,
    specs: Mapping[str, DenseSpec]) -> tuple[tuple[str, str], DenseProfile]:
  spec = specs.get(batch.table)
  if spec is None:
    raise ValueError(f"no dense spec for table {batch.table!r}")
  return (batch.table,
          Side(batch.side).value), DenseProfile.from_batch(spec, batch)


def _by_table(
    item: tuple[tuple[str, str], DenseProfile]
) -> tuple[str, tuple[str, DenseProfile]]:
  (table, side), profile = item
  return table, (side, profile)


def _emit(item: tuple[str, Iterable[tuple[str, DenseProfile] | None]],
          specs: Mapping[str, DenseSpec], label_key: bytes) -> Iterator[Any]:
  table, entries = item
  sides: dict[str, DenseProfile] = {}
  for entry in entries:
    if entry is None:  # the table's seed: present even with no batch
      continue
    side, profile = entry
    sides[side] = profile if side not in sides else sides[side].merge(profile)
  metrics, profiles = dense_outputs(specs[table], sides, label_key=label_key)
  yield from metrics
  for profile_value in profiles:
    yield beam.pvalue.TaggedOutput(_PROFILES_TAG, profile_value)


class DenseMetrics(beam.PTransform):
  """`PCollection[EncodedBatch]` (any tables and sides) →
  `{"metrics": PCollection[MetricValue], "profiles":
  PCollection[ProfileValue], "accumulators": PCollection[((table, side),
  DenseProfile)]}` (module docstring). The accumulators are the census's
  totals side input (Task 22). Only each table's slim `DenseSpec` is
  pickled, never its panel rows. One seed per table joins the per-table
  grouping, so a table no batch reached (no rows read on any side)
  still emits its rows, each `not_evaluated` with the side it lacks as
  the reason. `label_key` is the one-element key
  PCollection `beam.label_key.LabelKey` makes on a worker; the emitter
  reads it as a side input, so the key never enters the job graph
  (Rulings R64, R68).

  Raises:
    TypeError: `label_key` is not a PCollection (bytes here would be
      pickled into the graph).
  """

  def __init__(self, tables: Sequence[TablePlan], *,
               label_key: beam.PCollection):
    super().__init__()
    if not isinstance(label_key, beam.PCollection):
      raise TypeError("label_key must be the LabelKey PCollection, never "
                      "bytes: a constructor argument is pickled into the job "
                      "graph (Ruling R68)")
    self._label_key = label_key
    self._specs = {table.name: DenseSpec.from_table(table) for table in tables}

  def expand(self, input_or_inputs: beam.PCollection) -> dict[str, Any]:
    specs = self._specs
    accumulators = (
        input_or_inputs
        | "Profile" >> beam.Map(_keyed_profile, specs)
        | "Combine" >> beam.CombinePerKey(
            DenseProfileCombineFn()).with_hot_key_fanout(HOT_KEY_FANOUT))
    seeds = input_or_inputs.pipeline | "Seeds" >> beam.Create(
        [(name, None) for name in sorted(specs)])
    emitted = (
        (accumulators | "ByTable" >> beam.Map(_by_table), seeds)
        | "Entries" >> beam.Flatten()
        | "GroupSides" >> beam.GroupByKey()
        | "Emit" >>
        beam.FlatMap(_emit, specs, beam.pvalue.AsSingleton(
            self._label_key)).with_outputs(_PROFILES_TAG, main=_METRICS_TAG))
    return {
        _METRICS_TAG: emitted[_METRICS_TAG],
        _PROFILES_TAG: emitted[_PROFILES_TAG],
        "accumulators": accumulators,
    }
