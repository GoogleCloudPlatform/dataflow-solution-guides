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
"""The keyed value census: every metric and profile that needs one count
per distinct value of a column — value distributions, diversity, copies,
memorization lifts, top-k lists and shape mixes.

    EncodedBatch (source | synthetic)
      │  ParDo(CensusPreAggregateFn)   one dict per bundle, flushed in
      │                                finish_bundle, or before a column
      │                                could take it past MAX_PREAGG_KEYS
      │                                (never dropped)
      ├─ values   ((t, j, code), counts6)         census values (a head /
      │                                           tail sample, see below)
      ├─ masks    ((t, j, mask code), (c_src, c_syn, mask))   every value
      └─ literals ((t, j), {code: text})          literal-ok columns
    values ─► CombinePerKey(CountsCombineFn)   one entry per value (keys
      │                                        cannot be hot: each bundle
      │                                        sends a key at most once)
      ├─► Map ((t, j), (code, counts)) ─► CombinePerKey(CensusCombineFn)
      │     = the Task 8 CensusAccumulator per column ──► summarize
      │     └─► Chao-Shen coverage per column ─── side input ──┐
      └─► Map(value_contribution, totals, refs, coverage) ◄─────┘
            ─► CombinePerKey(sum): TVD/JSD/w², their floors and
               baselines, Chao-Shen, rarefaction, Horvitz-Thompson sums
               and tail cluster sums, R/H/E/H_E lift counts
    masks ─► CombinePerKey(MaskCountsCombineFn)
          ─► Map(mask_part, totals, refs) ─► CombinePerKey(MaskSummary)
    parts + one seed per table ─► GroupByKey(table) ─► table_outputs
          ─► MetricValue (OWNED_METRIC_IDS), ProfileValue (topk, shape_mix)

The per-column combines keep Beam's combiner lifting (a column is a hot
key; lifting sends one partial accumulator per bundle).

`counts6` = (c_src, c_syn, c_src_m, c_syn_m, c_src_r, flags): full-n
counts, matched-n counts (`subsample_m`, R59: only the n-dependent
diversity metrics read them), the source count inside a Bernoulli
subsample matched to |R| rows (the entropy/distinct baselines, D4), and
bit 0 = the value is NOT substantive (empty after trim, or a
0001-/9999- date sentinel — the probe's rule). The FlatMap the brief
draws before the pre-aggregation DoFn is fused into it (`accumulate`
counts a batch with numpy and folds it into the bundle dict), so no
per-value element is emitted before the dict has merged the batch.
Nulls are never keys.

Side inputs:

    totals    the dense pass's per-(table, side) accumulators (Task 21),
              slimmed to non-null / matched non-null / non-empty counts
              per plan column: the exact denominators of p, q and the
              shape shares, known before the census has summed anything
    refs      per census column, from the R/H panel (D3), built on the
              driver: R value counts, H/E/H_E value sets, R masks and
              the free-text pool (`census_refs`)
    coverage  the first pass's Chao-Shen sample coverage (Ruling R3; an
              exact census only)

Value sampling (`census: value_sampled`, rate = K / M with M =
`VALUE_SAMPLE_MODULUS`) is a stratified Horvitz-Thompson design (Ruling
R67). The certainty stratum is the plan's `census_head` — the source's
and the synthetic side's top-254 values — included with probability 1
(weight 1), so no heavy hitter is left to the hash. The tail is
Poisson-sampled by the value hash: a value enters iff `code mod M < K`,
a Bernoulli(rate) draw per VALUE independent of its counts, side and
R/H membership (weight w = 1 / rate). Every retained value's counts are
exact. Estimators (w_v = 1 in the head, 1 / rate in the tail):

    distinct counts     Σ_sampled w_v                           unbiased
    Σ_v f(counts_v)     Σ_sampled w_v f — TVD, JSD, w², the     unbiased
      (totals exact)    tvd_null floor, entropy's Σ c ln c,     (Horvitz &
                        rarefaction, baselines                  Thompson)
    shares (copy rate,  Σ w_v y_v / Σ w_v x_v, the combined      ratio,
      adherence,        ratio of two Horvitz-Thompson totals    unbiased to
      coverage, novelty)                                        first order
    lifts               values in the hash range only (head or not): a
                        uniform draw, R, H, E, H_E thinned alike, so the
                        rates' ratio is unchanged

The copy rate (and category adherence) on a value-sampled column carries
a cluster-robust interval with the values as the sampling units
(`noise.stratified_ratio_interval`, R70): the head is counted exactly,
so only the tail's ratio q̂ is uncertain; its tail row total is exact too
(the dense non-null count for adherence; for the copy rate the census
counts substantive synthetic rows over every present value, before the
value sample), so R̂ = (Y_head + X_tail q̂) / (X_head + X_tail). q̂ gets a
Korn & Graubard (1998) Clopper-Pearson interval on its effective sample
size — linearised variance with the (1 - rate) factor, capped at the
tail's effective cluster count (Σx)² / Σx², with K&G's t degrees-of-
freedom factor — mapped through that function with the head as a known
constant, so a sample without a copy still bounds the rate. Novelty is
1 - adherence there (one estimator). The Horvitz-Thompson view is
validated before use — Σ c ln c within [0, n ln n] on each side, shares
and TVD/JSD at most 1, a positive source entropy — and a metric whose
view fails is `not_evaluated` with the reason, never a crash. Chao-Shen
(detail only) is not estimated on a value-sampled column, and
`column.distinct_ceiling_hit` is `not_evaluated` there (the exact K_syn
is not measured); its top-k profile is exact on the head and carries
`value_sample_rate`.

Rare values and lifts. A value is rare when 0 < c_src < 10 in the census
(the full source read; the repo's k-anonymity floor). With V_S the rare,
substantive values held by panel set S and not by its exchangeable twin
(D3), `field.value_memorization_lift` compares m_R / |V_R| with
m_H / |V_H|, m_S = the values of V_S the synthetic side reproduces at
least once (distinct values: one value copied many times is one
event, which keeps the counts close to Poisson), via the conditional
rate-ratio test with a Clopper-Pearson interval at alpha / m — Bonferroni
over the m columns of the table whose lift has exposure on both sides
(`detail.bonferroni_family`); status gates on ci_low (R1, R12, R38).
`detail.exposure` repeats it for E vs H_E (not gated).
`field.pool_memorization_lift` counts m_S = |P ∩ V_S| over the free-text
pool P of the run's (reference digest, model) — a side input the CLI
fills from `freetext_pools_table` (`read_pools`); absent, it is
`not_evaluated` with the reason; each pooled column is its own test at
an uncorrected alpha. An unverified reference, or an empty synthetic
side, makes both lifts `not_evaluated` (Review Focus 5); fidelity is
still computed.

Shapes follow ADR 0026 as `stats.shapes` implements it, over EVERY
present value — the mask pass is never value-sampled (R67), so
`column.shape_head_tv` and `field.shape_adherence` are exact. Masks are
keyed by their hash and pre-aggregated like values. A mask is a head
mask when it holds at least 2 % of the source's non-empty values (a
per-mask decision from its own count and the dense total, so no global
head set has to be broadcast); every other mask pools into the tail.
Masks longer than 256 characters pool into one `<long>` bucket at
count time, which is never a head and never counts toward adherence.

Literals (D6, R56, R64): a top-k label is the value itself only when the
column is `literal_ok`, `hash64(name, v)` is in its (source-built)
`detection_dictionary` and the census counts it at least 10 times in the
source; everything else — every synthetic-only value — is
`canonical.hashed_label(code, key=label_key)`, keyed BLAKE2b, so a label
cannot be reversed by enumerating a small domain. A shape mask is shown
when it holds at least 10 source values and contains a class
placeholder (9, A, a, ␣); a mask that is only literal characters is the
value itself, so it is labelled.

`field.substantive_copy_rate` is gated only on `text` columns: `scoring`
reports it as INFO on every other kind, from `column_kind` (numeric and
temporal values collide by domain size; reusing a rare real category or
identifier is not memorisation — Ruling R66). `detail.day_granularity`
still records a temporal column's granularity.

Sampled mode (Ruling R72, as in `beam.membership`): a side whose plan
rate is below 1 was read as a row sample (`CensusSpec.rate_source` /
`rate_synthetic`). A metric defined by the value SET of a side — or by
an exact count of it — is `not_evaluated` there with "sampled mode
cannot measure …; run exact mode", what the sample did see in `detail`
(named `*_lower_bound` where it bounds the full count) and
`detail.sample_rates`:

    metric                        needs in full   why
    ────────────────────────────  ──────────────  ────────────────────────
    field.category_adherence,     the source      a synthetic value whose
      column.novelty_mass                         source rows were not
                                                  sampled looks invented
    field.substantive_copy_rate   the source      a copy of an unsampled
                                                  source value goes unseen
                                                  and a value's source
                                                  count (rare below 10) is
                                                  thinned
    field.shape_adherence         the source      a synthetic shape whose
                                                  source rows were not
                                                  sampled looks unseen
    column.coverage_mass          the synthetic   a source value whose
                                                  synthetic rows were not
                                                  sampled looks uncovered
    column.distinct_ceiling_hit   the synthetic   the exact synthetic
                                                  distinct count is not
                                                  measured
    column.distinct_ratio,        both sides      defined at m = min(n_src,
      column.entropy_ratio                        n_syn); a row sample
                                                  thins repeats and the
                                                  ratio drifts toward 1

Every other row that reads a sampled side is an estimate over the rows
read: `method` = sample, `sample_rate` = the lowest rate among the
sampled sides it reads, `detail.sample_rates` naming each (a
value-sampled column's own rate moves to `detail.value_sample_rate`).
Its totals are the dense pass's counts of the rows READ, so every
interval and noise floor is the sample's. The lifts stay evaluated: R
and H are thinned alike by a sample drawn on row content, so the ratio
keeps its null; "rare" is then judged on the sample's counts. A profile
of a sampled side carries `sample_rate` in its payload.

References (author-year, R22): Horvitz & Thompson (1952); Woodruff
(1971); Särndal, Swensson & Wretman (1992); Korn & Graubard (1998); Good
(1953); Chao & Shen
(2003); Miller (1955); Hurlbert (1971); Lin (1991); Cohen (1988); Wilson
(1927); Newcombe (1998); Przyborowski & Wilenski (1940); Clopper &
Pearson (1934); Dunn (1961) for Bonferroni; Sweeney (2002).

Design: docs/designs/2026-07-07-evaluation-framework-design.md

Design: docs/DESIGN.md §5 Fidelity
(ADR 0018, 0020, 0022, 0026).
"""

from __future__ import annotations

import functools
import hashlib
import math
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NamedTuple

import apache_beam as beam
import numpy as np
from apache_beam.transforms.window import GlobalWindows

from sdfb_evaluation.beam import staged
from sdfb_evaluation.beam.encode import BatchLayout, EncodedBatch
from sdfb_evaluation.beam.encode import cell_text
from sdfb_evaluation.canonical import NULL_CODE, hash64, hashed_label
from sdfb_evaluation.catalogue import Catalogue, load_catalogue
from sdfb_evaluation.context.bq import quote_fqn
from sdfb_evaluation.context.budget import VALUE_SAMPLE_MODULUS
from sdfb_evaluation.stats import noise, shapes
from sdfb_evaluation.stats.noise import folded_abs_interval
from sdfb_evaluation.stats.diversity import (
    CensusAccumulator,
    chao_shen_term,
    summarize,
    tvd_jsd_from_census,
)
from sdfb_evaluation.types import (
    ColumnKind,
    Method,
    MetricValue,
    ProfileValue,
    Side,
)

if TYPE_CHECKING:
  from sdfb_evaluation.beam.dense import DenseProfile
  from sdfb_evaluation.context.plan import ColumnPlan, TablePlan

__all__ = [
    "APPLIES_TO",
    "MAX_PREAGG_KEYS",
    "NEEDS_FULL_SIDE",
    "OWNED_METRIC_IDS",
    "POOL_CAP",
    "RARE_COUNT",
    "SHAPE_HEAD_FLOOR",
    "TOPK_ITEMS",
    "VALUE_SAMPLE_MODULUS",
    "BatchCounts",
    "CensusColumn",
    "CensusCombineFn",
    "CensusMetrics",
    "CensusPreAggregateFn",
    "CensusResult",
    "CensusSpec",
    "ColumnParts",
    "ColumnRef",
    "ColumnSizes",
    "ContributionCombineFn",
    "CountsCombineFn",
    "FreeTextPool",
    "MaskCountsCombineFn",
    "MaskSummary",
    "SideTotals",
    "accumulate",
    "batch_counts",
    "census_outputs",
    "census_refs",
    "mask_part",
    "pools_from_rows",
    "pools_sql",
    "read_pools",
    "shape_masks",
    "stratified_share",
    "table_outputs",
    "value_contribution",
]

OWNED_METRIC_IDS: tuple[str, ...] = (
    "field.category_adherence",
    "field.shape_adherence",
    "field.substantive_copy_rate",
    "field.value_memorization_lift",
    "field.pool_memorization_lift",
    "column.jsd",
    "column.tvd",
    "column.cohens_w",
    "column.top1_share_delta",
    "column.coverage_mass",
    "column.novelty_mass",
    "column.entropy_ratio",
    "column.distinct_ratio",
    "column.distinct_ceiling_hit",
    "column.shape_head_tv",
)

MAX_PREAGG_KEYS = 100_000
TOPK_ITEMS = 50  # = ADR 0022's literal column cap: a literal column fits
RARE_COUNT = 10  # the repo's k-anonymity floor (e2e_gcp_probe.py)
POOL_CAP = 512  # the generator's free-text pool cap (ADR 0018)
SHAPE_HEAD_FLOOR = 0.02  # ADR 0026 (`shapes.shape_head_tv`'s default)
MASK_MAX_CHARS = 256
LIFT_ALPHA = 0.05
_SIG_DIGITS = 12
_REL_TOL = 1e-9  # float slack on range guards (a share at exactly 1)
_DIVERSE_K = 2  # a side with this many values has a positive entropy

_K = ColumnKind
_CODED = frozenset({_K.CATEGORICAL, _K.BOOLEAN})
_TOPK_KINDS = frozenset({_K.CATEGORICAL, _K.BOOLEAN, _K.TEXT, _K.IDENTIFIER})
_SHAPE_KINDS = frozenset({_K.TEXT, _K.IDENTIFIER})
_SENTINEL_TYPES = frozenset({"DATE", "DATETIME", "TIMESTAMP"})
_SENTINEL_PREFIXES = ("0001-", "9999-")
_PLACEHOLDERS = frozenset("9Aa␣")
_LONG_MASK = "<long>"  # letters never survive shape_of: no mask spells it
_TAIL = "<tail>"
_MASK_LABEL = "sdfb:shape-mask"
_COUNTED_SIDES = frozenset({Side.SOURCE, Side.SYNTHETIC})
_SOURCE, _SYNTHETIC = Side.SOURCE.value, Side.SYNTHETIC.value
_EITHER = "either"  # NEEDS_FULL_SIDE: needs both sides in full
_VALUES, _MASKS, _LITERALS = "values", "masks", "literals"
_SUBSTANTIVE = "substantive"
_METRICS, _PROFILES = "metrics", "profiles"
_SEED = "seed"
_TWO_64 = 2**64
_MAX_U64 = _TWO_64 - 1
_MAX_I64 = 2**63 - 1
_R_STREAM = np.uint64(0xD1B54A32D192ED03)  # the |R|-matched draw's stream
_SPLITMIX = (np.uint64(0x9E3779B97F4A7C15), np.uint64(0xBF58476D1CE4E5B9),
             np.uint64(0x94D049BB133111EB))
_YEAR_2_US = -62_104_060_800_000_000  # 0002-01-01T00:00:00Z
_YEAR_9999_US = 253_370_764_800_000_000  # 9999-01-01T00:00:00Z

# counts6 slots
_SRC, _SYN, _SRC_M, _SYN_M, _SRC_R, _FLAGS = range(6)
COUNT_WIDTH = 6
_NONSUBSTANTIVE = 1

_COPY_KINDS = frozenset(
    {_K.CATEGORICAL, _K.TEXT, _K.IDENTIFIER, _K.NUMERIC, _K.TEMPORAL})
_NOVEL_KINDS = frozenset({_K.CATEGORICAL, _K.TEXT, _K.IDENTIFIER})

# Sampled mode (module docstring, R72): the side each of these needs in
# full, what a row sample of it cannot measure, and why.
NEEDS_FULL_SIDE: Mapping[str, tuple[str, str, str]] = {
    "field.category_adherence":
        (_SOURCE, "category adherence",
         "a synthetic value whose source rows were not sampled looks "
         "invented"),
    "column.novelty_mass":
        (_SOURCE, "novelty", "a synthetic value whose source rows were not "
         "sampled looks novel"),
    "field.substantive_copy_rate":
        (_SOURCE, "copies of rare source values",
         "a copy of an unsampled source value goes unseen and a value's "
         "source count (the rare-below-10 rule) is thinned"),
    "field.shape_adherence": (_SOURCE, "shape adherence",
                              "a synthetic shape whose source rows were not "
                              "sampled looks unseen"),
    "column.coverage_mass":
        (_SYNTHETIC, "coverage", "a source value whose synthetic rows were not "
         "sampled looks uncovered"),
    "column.distinct_ceiling_hit":
        (_SYNTHETIC, "a pool-cap hit", "the exact synthetic distinct count is "
         "not measured"),
    # defined at m = min(n_src, n_syn): a row sample thins repeats and the
    # ratio drifts toward 1, whichever side was sampled
    "column.distinct_ratio":
        (_EITHER, "the distinct ratio", "thinned repeats pull it toward 1"),
    "column.entropy_ratio":
        (_EITHER, "the entropy ratio", "thinned repeats pull it toward 1"),
}
# The sides a metric reads, when not both (the pool lift counts the pool
# against the panel and the source's rare values; the synthetic side
# only has to exist).
_BOTH_SIDES = (_SOURCE, _SYNTHETIC)
_SIDES_READ: Mapping[str, tuple[str, ...]] = {
    "field.pool_memorization_lift": (_SOURCE,),
    "column.distinct_ceiling_hit": (_SYNTHETIC,),
}

# Where each owned id reaches: the catalogue's kinds, except column.jsd,
# whose numeric/temporal side is the dense pass's (binned).
APPLIES_TO: Mapping[str, frozenset[ColumnKind]] = {
    "field.category_adherence": _CODED,
    "field.shape_adherence": _SHAPE_KINDS,
    "field.substantive_copy_rate": _COPY_KINDS,
    "field.value_memorization_lift": _COPY_KINDS,
    "field.pool_memorization_lift": frozenset({_K.TEXT}),
    "column.jsd": _CODED,
    "column.tvd": _CODED,
    "column.cohens_w": _CODED,
    "column.top1_share_delta": _CODED,
    "column.coverage_mass": _CODED,
    "column.novelty_mass": _NOVEL_KINDS,
    "column.entropy_ratio": _TOPK_KINDS,
    "column.distinct_ratio": _NOVEL_KINDS,
    "column.distinct_ceiling_hit": frozenset({_K.TEXT}),
    "column.shape_head_tv": _SHAPE_KINDS,
}


@functools.cache
def _catalogue() -> Catalogue:
  return load_catalogue()


def _reach(metric_id: str) -> frozenset[ColumnKind]:
  return APPLIES_TO[metric_id]


def _signed(code: int) -> int:
  """A uint64 code as the int64 with the same 64 bits: a Beam key then
  takes the coder's varint path instead of its pickle fallback."""
  return code - _TWO_64 if code > _MAX_I64 else code


def _unsigned(key: int) -> int:
  return key + _TWO_64 if key < 0 else key


def _sig(x: Any) -> Any:
  """A finite float at 12 significant digits (Ruling R21's rerun
  determinism: merge-tree float sums differ in the last ulps), through
  mappings and lists; anything else unchanged."""
  if isinstance(x, float) and math.isfinite(x) and x != 0.0:
    return float(f"{x:.{_SIG_DIGITS}g}")
  if isinstance(x, Mapping):
    return {key: _sig(value) for key, value in x.items()}
  if isinstance(x, (list, tuple)):
    return [_sig(item) for item in x]
  return x


# --------------------------------------------------------------------------
# free-text pools (the injectable seam for field.pool_memorization_lift)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FreeTextPool:
  """One column's persisted LLM free-text pool (ADR 0020): the values the
  generator drew that column from, for one (reference digest, model)."""
  column: str
  values: tuple[str, ...]
  target: int | None = None
  reference_digest: str | None = None
  model_uri: str | None = None


def pools_sql(pools_table: str) -> str:
  """The query for one run's pools in `freetext_pools_table`; bind
  `@reference_digest` and `@model_uri` (the pool's identity)."""
  return ("SELECT `column`, target, `values`, reference_digest, model_uri "
          f"FROM {quote_fqn(pools_table)} "
          "WHERE reference_digest = @reference_digest "
          "AND model_uri = @model_uri")


def pools_from_rows(
    rows: Iterable[Mapping[str, Any]]) -> dict[str, FreeTextPool]:
  """`freetext_pools` rows → {column: FreeTextPool} (a later row of the
  same column wins; BigQuery returns an empty REPEATED as None)."""
  pools: dict[str, FreeTextPool] = {}
  for row in rows:
    target = row.get("target")
    pools[str(row["column"])] = FreeTextPool(
        column=str(row["column"]),
        values=tuple(str(v) for v in row.get("values") or ()),
        target=None if target is None else int(target),
        reference_digest=row.get("reference_digest"),
        model_uri=row.get("model_uri"))
  return pools


def read_pools(bq: Any,
               *,
               pools_table: str,
               reference_digest: str,
               model_uri: str,
               max_bytes: int | None = None) -> dict[str, FreeTextPool]:
  """The run's pools, read through a `Bq` (driver side; Task 27 passes
  the result to `CensusMetrics(pools=...)` per table)."""
  rows = bq.query(
      pools_sql(pools_table), {
          "reference_digest": reference_digest,
          "model_uri": model_uri
      },
      max_bytes=max_bytes)
  return pools_from_rows(rows)


# --------------------------------------------------------------------------
# the plan, slimmed for the workers
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CensusColumn:  # pylint: disable=too-many-instance-attributes  # one field per encoding the census reads
  """One plan column as the census sees it. `code_block` says where its
  `hash64` codes come from: `nonkey` (`h_nonkey`, the encoder's own
  codes) or `text` (an identity column: `hash64(name, text)`); `keep` is
  K of `code mod M < K` for a value-sampled census, and `head` its
  certainty stratum (the plan's `census_head`, R67)."""
  j: int
  name: str
  kind: ColumnKind
  bq_type: str
  census: str
  rate: float
  keep: int | None
  code_block: str | None
  code_k: int
  text_k: int | None
  num_k: int | None
  literal_ok: bool
  detection: frozenset[int]
  day_granularity: bool
  head: frozenset[int] = frozenset()

  @property
  def censused(self) -> bool:
    return self.census != "none"

  @property
  def sampled(self) -> bool:
    return self.keep is not None

  @property
  def weight(self) -> float:
    """w = 1 / rate, the Horvitz-Thompson weight of a tail value."""
    return 1.0 / self.rate

  def in_hash_range(self, code: int) -> bool:
    """The tail's Bernoulli(rate) draw: `code mod M < K` (always True for
    an exact census)."""
    return self.keep is None or code % VALUE_SAMPLE_MODULUS < self.keep

  def kept(self, code: int) -> bool:
    """Whether the census counts this value: the head with certainty, the
    tail when its hash is in range (R67)."""
    return self.keep is None or code in self.head or self.in_hash_range(code)

  def weight_of(self, code: int) -> float:
    """The Horvitz-Thompson weight of a counted value: 1 for an exact
    census and for the head, 1 / rate for the tail."""
    return 1.0 if self.keep is None or code in self.head else self.weight

  def in_tail(self, code: int) -> bool:
    """A value-sampled column's hash-sampled (non-head) value."""
    return self.keep is not None and code not in self.head

  @property
  def method(self) -> Method:
    return Method.VALUE_SAMPLED if self.sampled else Method.EXACT

  @property
  def has_shapes(self) -> bool:
    return (self.censused and self.kind in _SHAPE_KINDS and
            self.text_k is not None)

  @property
  def sentinel_dates(self) -> bool:
    return (self.kind is _K.TEMPORAL and self.bq_type in _SENTINEL_TYPES and
            self.num_k is not None)

  @classmethod
  def from_plan(cls, j: int, column: ColumnPlan,
                layout: BatchLayout) -> CensusColumn:
    """Raises ValueError when a censused column has no code source."""
    name = column.name
    block: str | None = None
    k = -1
    if name in layout.nonkey_columns:
      block, k = "nonkey", layout.nonkey_columns.index(name)
    elif name in layout.text_columns:
      block, k = "text", layout.text_columns.index(name)
    if column.census != "none" and block is None:
      raise ValueError(f"{layout.table}: census column {name!r} has neither a "
                       "hash nor a text encoding")
    keep = None
    rate = 1.0
    if column.census == "value_sampled":
      keep = round(
          float(column.value_sample_rate or 0.0) * VALUE_SAMPLE_MODULUS)
      if not 1 <= keep <= VALUE_SAMPLE_MODULUS:
        raise ValueError(f"{layout.table}: column {name!r} value sample rate "
                         f"{column.value_sample_rate!r} is not K / "
                         f"{VALUE_SAMPLE_MODULUS} with K >= 1")
      rate = keep / VALUE_SAMPLE_MODULUS
    return cls(
        j=j,
        name=name,
        kind=ColumnKind(column.kind),
        bq_type=column.bq_type,
        census=column.census,
        rate=rate,
        keep=keep,
        code_block=block,
        code_k=k,
        text_k=(layout.text_columns.index(name)
                if name in layout.text_columns else None),
        num_k=(layout.num_columns.index(name)
               if name in layout.num_columns else None),
        literal_ok=bool(column.literal_ok),
        detection=frozenset(int(c) for c in column.detection_dictionary or ()),
        day_granularity=bool(column.day_granularity),
        head=frozenset(int(c) for c in column.census_head or ()))


def _row_rate(rate: float | None) -> float:
  """A side's plan row-sample rate (None or >= 1: read in full)."""
  return 1.0 if rate is None or rate >= 1.0 else float(rate)


@dataclass(frozen=True)
class CensusSpec:  # pylint: disable=too-many-instance-attributes  # the per-table facts the census needs
  """What the census needs of one `TablePlan` — never its panel rows (the
  refs side input carries what it needs of them)."""
  table: str
  encoding_plan_digest: str
  columns: tuple[CensusColumn, ...]
  string_js: tuple[int, ...]
  has_panel: bool
  verified: bool
  panel_reason: str | None
  reference_rows: int
  rate_r: float | None
  rate_source: float = 1.0
  rate_synthetic: float = 1.0

  @property
  def censused(self) -> tuple[CensusColumn, ...]:
    return tuple(c for c in self.columns if c.censused)

  def sample_rates(self,
                   sides: Sequence[str] = _BOTH_SIDES) -> dict[str, float]:
    """The row-sample rates (< 1) of `sides`, by side (R72); empty when
    each was read in full."""
    rates = {_SOURCE: self.rate_source, _SYNTHETIC: self.rate_synthetic}
    return {side: rates[side] for side in sides if rates[side] < 1.0}

  def sampled_reason(self, metric_id: str) -> str | None:
    """Why `metric_id` cannot be measured on this table's row samples
    (R72), or None when the side it needs in full was read in full."""
    needs = NEEDS_FULL_SIDE.get(metric_id)
    if needs is None:
      return None
    side, what, why = needs
    rates = self.sample_rates(_BOTH_SIDES if side == _EITHER else (side,))
    if not rates:
      return None
    where = " and ".join(
        f"the {s} side is a {r:.3g} row sample" for s, r in rates.items())
    return (f"sampled mode cannot measure {what}: {where}, so {why}; "
            "run exact mode")

  @classmethod
  def from_table(cls, table: TablePlan) -> CensusSpec:
    layout = BatchLayout.from_table(table)
    panel = table.panel
    n_r = len(panel.r_rows) if panel is not None else 0
    rows_source = table.rows_read[0] if table.rows_source is not None else 0
    rate_r = min(1.0, n_r / rows_source) if n_r and rows_source > 0 else None
    if panel is None:
      reason = "no reference panel (R/H) was read for this table"
    elif not panel.verified:
      why = panel.reason or "digest mismatch"
      reason = f"reference not verified: {why}"
    else:
      reason = None
    return cls(
        table=table.name,
        encoding_plan_digest=table.encoding_plan_digest,
        columns=tuple(
            CensusColumn.from_plan(j, c, layout)
            for j, c in enumerate(table.columns)),
        string_js=tuple(j for j, c in enumerate(table.columns)
                        if c.name in layout.text_columns),
        has_panel=panel is not None,
        verified=panel is not None and bool(panel.verified),
        panel_reason=reason,
        reference_rows=n_r,
        rate_r=rate_r,
        rate_source=_row_rate(table.sample_rate_source),
        rate_synthetic=_row_rate(table.sample_rate_synthetic))


@dataclass(frozen=True)
class SideTotals:
  """One (table, side)'s exact per-column totals from the dense pass: the
  denominators of every census share (plan column order; `nonempty` is
  None for a column outside the dense string block)."""
  rows: int
  rows_m: int
  nonnull: tuple[int, ...]
  nonnull_m: tuple[int, ...]
  nonempty: tuple[int | None, ...]

  @classmethod
  def empty(cls, spec: CensusSpec) -> SideTotals:
    d = len(spec.columns)
    return cls(0, 0, (0,) * d, (0,) * d,
               tuple(0 if j in spec.string_js else None for j in range(d)))

  @classmethod
  def from_profile(cls, spec: CensusSpec, profile: DenseProfile) -> SideTotals:
    """Raises ValueError when the profile is not of this spec's table."""
    if (profile.table != spec.table or
        len(profile.nulls) != len(spec.columns) or
        len(profile.str_nonempty) != len(spec.string_js)):
      raise ValueError(f"the {profile.table}/{profile.side} dense profile does "
                       f"not match the census spec of {spec.table!r}")
    nonempty: list[int | None] = [None] * len(spec.columns)
    for si, j in enumerate(spec.string_js):
      nonempty[j] = int(profile.str_nonempty[si])
    return cls(
        rows=int(profile.rows),
        rows_m=int(profile.rows_m),
        nonnull=tuple(int(profile.rows) - int(n) for n in profile.nulls),
        nonnull_m=tuple(int(profile.rows_m) - int(n) for n in profile.nulls_m),
        nonempty=tuple(nonempty))


class ColumnSizes(NamedTuple):
  """One column's exact totals: non-null, matched non-null, non-empty."""
  n_src: int
  n_syn: int
  n_src_m: int
  n_syn_m: int
  ne_src: int
  ne_syn: int


def _sizes(src: SideTotals, syn: SideTotals, j: int) -> ColumnSizes:
  return ColumnSizes(src.nonnull[j], syn.nonnull[j], src.nonnull_m[j],
                     syn.nonnull_m[j], src.nonempty[j] or 0, syn.nonempty[j] or
                     0)


# --------------------------------------------------------------------------
# the R/H panel side input
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ColumnRef:  # pylint: disable=too-many-instance-attributes  # one field per panel set
  """One census column's panel view (D3), built on the driver. For a
  value-sampled column the code sets keep the values the census counts
  (head and hash range; it sees nothing else), and `r_mass_ht` is the
  Horvitz-Thompson estimate of R's mass on them (1 when exact) — the
  same draw the census's baseline sums use, so their difference is R's
  mass outside the census without sampling noise. R's masks, like the
  mask pass, cover every value; `r_nonnull`/`r_nonempty` stay full."""
  r_counts: Mapping[int, int]
  h_codes: frozenset[int]
  e_codes: frozenset[int]
  he_codes: frozenset[int]
  r_nonnull: int
  r_nonempty: int
  r_masks: Mapping[str, int]
  r_mass_ht: float = 1.0
  pool: FreeTextPool | None = None
  pool_codes: frozenset[int] = frozenset()
  pool_reason: str | None = None

  @property
  def r_short(self) -> int:
    """R's masked values (the `<long>` bucket excluded)."""
    return sum(c for m, c in self.r_masks.items() if m != _LONG_MASK)


def _panel_code(col: CensusColumn, value: Any) -> int | None:
  """The census code of one panel cell: the encoder's `hash64(name, v)`
  for a non-key column, `hash64(name, text)` for an identity column."""
  if value is None:
    return None
  if col.code_block == "text":
    return hash64(col.name, cell_text(value))
  return hash64(col.name, value)


def _mask_of(text: str) -> str:
  return _LONG_MASK if len(text) > MASK_MAX_CHARS else shapes.shape_of(text)


def _pool_reason(pools: Mapping[str, FreeTextPool] | None) -> str:
  if pools is None:
    return ("no free-text pool side input: the CLI reads the launch's "
            "freetext_pools_table for this reference digest and model")
  return ("no free-text pool for this column in the run's freetext_pools "
          "(reference digest and model)")


def _column_ref(col: CensusColumn, panel: Any,
                pools: Mapping[str, FreeTextPool] | None) -> ColumnRef:
  r_codes: Counter = Counter()
  r_masks: Counter = Counter()
  r_nonnull = r_nonempty = 0
  for row in panel.r_rows:
    value = row.get(col.name)
    code = _panel_code(col, value)
    if code is None:
      continue
    r_nonnull += 1
    text = cell_text(value) if col.has_shapes else None
    if text:
      r_nonempty += 1
      r_masks[_mask_of(text)] += 1
    if col.kept(code):
      r_codes[code] += 1

  def codes(rows: Sequence[Mapping[str, Any]]) -> frozenset[int]:
    found = (_panel_code(col, row.get(col.name)) for row in rows)
    return frozenset(c for c in found if c is not None and col.kept(c))

  head = sum(c for code, c in r_codes.items() if not col.in_tail(code))
  tail = sum(c for code, c in r_codes.items() if col.in_tail(code))

  pool = None if pools is None else pools.get(col.name)
  return ColumnRef(
      r_counts=dict(r_codes),
      h_codes=codes(panel.h_rows),
      e_codes=codes(panel.r_rows[:panel.e_n]),
      he_codes=codes(panel.h_rows[:panel.he_n]),
      r_nonnull=r_nonnull,
      r_nonempty=r_nonempty,
      r_masks=dict(r_masks),
      r_mass_ht=(head + col.weight * tail) / r_nonnull if r_nonnull else 0.0,
      pool=pool,
      pool_codes=frozenset(
          c for v in (pool.values if pool else ())
          if (c := _panel_code(col, v)) is not None),
      pool_reason=None if pool is not None else _pool_reason(pools))


def census_refs(
    table: TablePlan,
    *,
    pools: Mapping[str, FreeTextPool] | None = None) -> dict[int, ColumnRef]:
  """{plan column index: ColumnRef} for every census column of `table`
  (empty without a panel). `pools` maps column → pool for this table;
  None means no pool side input at all (both cases give the pool lift a
  reason)."""
  if table.panel is None:
    return {}
  spec = CensusSpec.from_table(table)
  return {col.j: _column_ref(col, table.panel, pools) for col in spec.censused}


# --------------------------------------------------------------------------
# one batch
# --------------------------------------------------------------------------
_ASCII_SHAPES = np.array([ord(shapes.shape_of(chr(c))) for c in range(128)],
                         dtype=np.uint32)


def shape_masks(values: Sequence[str]) -> list[str]:
  """`shapes.shape_of` of every value, vectorised: `shape_of` maps each
  character on its own, so the batch's code points are mapped at once
  (a lookup for ASCII, one `shape_of` call per distinct other code
  point) and split back by length."""
  if not values:
    return []
  points = np.frombuffer(
      "".join(values).encode("utf-32-le", "surrogatepass"), dtype="<u4")
  mapped = np.empty_like(points)
  ascii_mask = points < _ASCII_SHAPES.size
  mapped[ascii_mask] = _ASCII_SHAPES[points[ascii_mask]]
  if not ascii_mask.all():
    unique, inverse = np.unique(points[~ascii_mask], return_inverse=True)
    table = np.fromiter((ord(shapes.shape_of(chr(c))) for c in unique.tolist()),
                        dtype=np.uint32,
                        count=unique.size)
    mapped[~ascii_mask] = table[inverse]
  text = mapped.astype("<u4").tobytes().decode("utf-32-le", "surrogatepass")
  out, start = [], 0
  for value in values:
    out.append(text[start:start + len(value)])
    start += len(value)
  return out


def _splitmix(x: np.ndarray) -> np.ndarray:
  """SplitMix64's finaliser, vectorised (uint64 wraps by design)."""
  with np.errstate(over="ignore"):
    z = x + _SPLITMIX[0]
    z = (z ^ (z >> np.uint64(30))) * _SPLITMIX[1]
    z = (z ^ (z >> np.uint64(27))) * _SPLITMIX[2]
    return z ^ (z >> np.uint64(31))


def _r_subsample(row_hash: np.ndarray, rate: float) -> np.ndarray:
  """A Bernoulli(`rate`) draw per source row keyed on its (salted) row
  hash, on a stream independent of `subsample_m`: the source matched to
  |R| rows for the n-dependent baselines (D4, D5)."""
  if rate >= 1.0:
    return np.ones(len(row_hash), dtype=bool)
  limit = np.uint64(min(int(rate * _TWO_64), _MAX_U64))
  return _splitmix(row_hash.astype(np.uint64) ^ _R_STREAM) < limit


def _column_codes(col: CensusColumn, batch: EncodedBatch) -> np.ndarray:
  if col.code_block == "nonkey":
    return batch.h_nonkey[:, col.code_k]
  texts = batch.text[col.code_k]
  return np.fromiter(
      (NULL_CODE if t is None else hash64(col.name, t) for t in texts),
      dtype=np.uint64,
      count=len(texts))


def _blank_or_sentinel(text: str) -> bool:
  return not text.strip() or text.startswith(_SENTINEL_PREFIXES)


def _nonsubstantive(col: CensusColumn, batch: EncodedBatch,
                    rows: np.ndarray) -> np.ndarray:
  """Per row: empty after trim or a 0001-/9999- sentinel (the probe's
  `SAFE_CAST(x AS STRING)` regex; for DATE/DATETIME/TIMESTAMP the year).
  BYTES text is base64, so only its emptiness is judged."""
  if col.text_k is not None:
    texts = batch.text[col.text_k]
    if col.bq_type == "BYTES":
      return np.fromiter((not texts[i] for i in rows.tolist()),
                         dtype=bool,
                         count=rows.size)
    return np.fromiter((_blank_or_sentinel(texts[i]) for i in rows.tolist()),
                       dtype=bool,
                       count=rows.size)
  if col.sentinel_dates:
    assert col.num_k is not None
    micros = batch.num[rows, col.num_k]
    sentinel: np.ndarray = (micros < _YEAR_2_US) | (micros >= _YEAR_9999_US)
    return sentinel
  return np.zeros(rows.size, dtype=bool)


@dataclass
class BatchCounts:
  """A bundle's pre-aggregated census: `values[(t, j, code)]` = counts6
  (lists, merged in place), `masks[(t, j, mask code)]` = [c_src, c_syn,
  mask], `literals[(t, j)]` = {code: text} of a literal-ok column's
  dictionary values, `substantive[(t, j)]` = a value-sampled column's
  substantive synthetic rows over EVERY present value (the copy rate's
  exact denominator, R70)."""
  values: dict[tuple[str, int, int], list[int]] = field(default_factory=dict)
  masks: dict[tuple[str, int, int], list[Any]] = field(default_factory=dict)
  literals: dict[tuple[str, int], dict[int, str]] = field(default_factory=dict)
  substantive: dict[tuple[str, int], int] = field(default_factory=dict)

  def __len__(self) -> int:
    return len(self.values) + len(self.masks)


def _add_values(out: BatchCounts, key_head: tuple[str, int], src: bool,
                uniq: np.ndarray, counts: Sequence[np.ndarray],
                nonsub: np.ndarray) -> None:
  """Fold one column's per-value batch counts into the bundle dict."""
  full, matched, in_r = counts
  first, second = (_SRC, _SRC_M) if src else (_SYN, _SYN_M)
  table, j = key_head
  values = out.values
  for code, c, cm, cr, ns in zip(
      uniq.tolist(),
      full.tolist(),
      matched.tolist(),
      in_r.tolist(),
      nonsub.tolist(),
      strict=True):
    slot = values.get((table, j, code))
    if slot is None:
      slot = values[(table, j, code)] = [0] * COUNT_WIDTH
    slot[first] += c
    slot[second] += cm
    slot[_SRC_R] += cr
    if ns:
      slot[_FLAGS] |= _NONSUBSTANTIVE


_MASK_PREFIX = f"{_MASK_LABEL}\x1f".encode()


def mask_code(mask: str) -> int:
  """The key of one mask in the mask pass (and its hashed label's code): a
  64-bit BLAKE2b of the mask's text. A mask is already a plain string, so
  it skips `hash64`'s canonical JSON (the mask pass's hottest call, R75);
  the label built from it is keyed (R64), so the code itself reveals
  nothing."""
  digest = hashlib.blake2b(
      _MASK_PREFIX + mask.encode("utf-8", "surrogatepass"),
      digest_size=8).digest()
  return int.from_bytes(digest, "big")


def _add_masks(out: BatchCounts, key_head: tuple[str, int], src: bool,
               texts: Sequence[str | None], counts: np.ndarray) -> None:
  """Fold the batch's masks — of EVERY present value, never sampled (R67)
  — into the bundle dict, keyed by mask hash."""
  slot_i = 0 if src else 1
  table, j = key_head
  short: list[str] = []
  short_counts: list[int] = []
  long_count = 0
  for text, c in zip(texts, counts.tolist(), strict=True):
    if not text:
      continue
    if len(text) > MASK_MAX_CHARS:
      long_count += c
    else:
      short.append(text)
      short_counts.append(c)
  per_mask: dict[str, int] = {}
  for mask, c in zip(shape_masks(short), short_counts, strict=True):
    per_mask[mask] = per_mask.get(mask, 0) + c
  if long_count:
    per_mask[_LONG_MASK] = long_count
  for mask, c in per_mask.items():
    key = (table, j, mask_code(mask))
    slot = out.masks.get(key)
    if slot is None:
      slot = out.masks[key] = [0, 0, mask]
    slot[slot_i] += c


def _add_literals(out: BatchCounts, key_head: tuple[str,
                                                    int], col: CensusColumn,
                  uniq: np.ndarray, texts: Sequence[str | None]) -> None:
  found = out.literals.setdefault(key_head, {})
  for code, text in zip(uniq.tolist(), texts, strict=True):
    if code in col.detection and code not in found and text is not None:
      found[code] = text


class _BatchFlags(NamedTuple):
  """A batch's per-row inputs shared by every census column."""
  src: bool
  matched: np.ndarray
  in_r: np.ndarray


def _batch_flags(spec: CensusSpec, batch: EncodedBatch) -> _BatchFlags | None:
  """None for a batch the census does not count (reference/holdout: the
  panel reaches it as the refs side input; or empty).

  Raises:
    ValueError: the batch belongs to another table.
  """
  side = Side(batch.side)
  if side not in _COUNTED_SIDES or not batch.n:
    return None
  if batch.table != spec.table:
    raise ValueError(f"a batch of {batch.table!r} reached the census spec of "
                     f"{spec.table!r}")
  src = side is Side.SOURCE
  in_r = (
      _r_subsample(batch.row_hash, spec.rate_r)
      if src and spec.rate_r is not None else np.zeros(batch.n, dtype=bool))
  return _BatchFlags(src, batch.subsample_m, in_r)


def _kept_mask(col: CensusColumn, uniq: np.ndarray) -> np.ndarray:
  """Which distinct values the census counts: every one when exact; the
  head and the hash range when value-sampled (R67)."""
  if col.keep is None:
    return np.ones(uniq.size, dtype=bool)
  in_range = (uniq % np.uint64(VALUE_SAMPLE_MODULUS)) < np.uint64(col.keep)
  if not col.head:
    return in_range
  head = np.fromiter(col.head, dtype=np.uint64, count=len(col.head))
  kept: np.ndarray = in_range | np.isin(uniq, head)
  return kept


def _accumulate_column(spec: CensusSpec, col: CensusColumn, batch: EncodedBatch,
                       flags: _BatchFlags, out: BatchCounts) -> None:
  """One column of one batch: masks and literals over every present
  value, census counts over the kept ones."""
  codes = _column_codes(col, batch)
  rows = np.flatnonzero(codes != np.uint64(NULL_CODE))
  if not rows.size:
    return
  uniq, first, inverse, full = np.unique(
      codes[rows], return_index=True, return_inverse=True, return_counts=True)
  first_rows = rows[first]
  key_head = (spec.table, col.j)
  if col.text_k is not None:
    texts = [batch.text[col.text_k][i] for i in first_rows.tolist()]
    if col.literal_ok and col.detection:
      _add_literals(out, key_head, col, uniq, texts)
    if col.has_shapes:
      _add_masks(out, key_head, flags.src, texts, full)
  kept = _kept_mask(col, uniq)
  # the substantive flag of every present value is needed only for the
  # copy rate's exact denominator: the synthetic side of a value-sampled
  # column (R70); everywhere else only the counted values' flags (R75)
  if col.sampled and not flags.src:
    nonsub = _nonsubstantive(col, batch, first_rows)
    out.substantive[key_head] = (
        out.substantive.get(key_head, 0) + int(full[~nonsub].sum()))
    kept_nonsub = nonsub[kept]
  else:
    kept_nonsub = _nonsubstantive(col, batch, first_rows[kept])
  if not kept.any():
    return
  size = uniq.size
  per_value = (full.astype(np.int64)[kept],
               np.bincount(
                   inverse, weights=flags.matched[rows],
                   minlength=size).astype(np.int64)[kept],
               np.bincount(inverse, weights=flags.in_r[rows],
                           minlength=size).astype(np.int64)[kept])
  _add_values(out, key_head, flags.src, uniq[kept], per_value, kept_nonsub)


def accumulate(spec: CensusSpec, batch: EncodedBatch, out: BatchCounts) -> None:
  """Fold one encoded batch of `spec`'s table into `out` (the fused
  FlatMap of the module docstring). Reference/holdout batches are not
  counted: the panel reaches the census as the refs side input.

  Raises:
    ValueError: the batch belongs to another table.
  """
  flags = _batch_flags(spec, batch)
  if flags is None:
    return
  for col in spec.censused:
    _accumulate_column(spec, col, batch, flags, out)


def batch_counts(spec: CensusSpec, batch: EncodedBatch) -> BatchCounts:
  """One batch's census counts, on their own."""
  out = BatchCounts()
  accumulate(spec, batch, out)
  return out


# --------------------------------------------------------------------------
# combiners
# --------------------------------------------------------------------------
class CountsCombineFn(beam.CombineFn):
  """Sums fixed-width count tuples slot by slot; the `flags_index` slot
  is ORed instead (the census's sum4, plus the matched-to-R count and the
  substantive flag). Exact and order-free."""

  def __init__(self,
               width: int = COUNT_WIDTH,
               flags_index: int | None = _FLAGS):
    super().__init__()
    self._width = width
    self._flags = flags_index

  def create_accumulator(self) -> list[int]:
    return [0] * self._width

  def add_input(self, mutable_accumulator: list[int],
                element: Sequence[int]) -> list[int]:
    for i, value in enumerate(element):
      if i == self._flags:
        mutable_accumulator[i] |= int(value)
      else:
        mutable_accumulator[i] += int(value)
    return mutable_accumulator

  def merge_accumulators(self, accumulators: Iterable[list[int]]) -> list[int]:
    merged = self.create_accumulator()
    for accumulator in accumulators:
      self.add_input(merged, accumulator)
    return merged

  def extract_output(self, accumulator: list[int]) -> tuple[int, ...]:
    return tuple(accumulator)


class MaskCountsCombineFn(beam.CombineFn):
  """Sums one mask's (c_src, c_syn, mask) entries; the mask text is the
  same for every entry of a mask hash (the smallest is kept, order-free)."""

  def create_accumulator(self) -> list[Any]:
    return [0, 0, None]

  def add_input(self, mutable_accumulator: list[Any],
                element: Sequence[Any]) -> list[Any]:
    mutable_accumulator[0] += int(element[0])
    mutable_accumulator[1] += int(element[1])
    mask = element[2]
    if mask is not None and (mutable_accumulator[2] is None or
                             mask < mutable_accumulator[2]):
      mutable_accumulator[2] = mask
    return mutable_accumulator

  def merge_accumulators(self, accumulators: Iterable[list[Any]]) -> list[Any]:
    merged = self.create_accumulator()
    for accumulator in accumulators:
      self.add_input(merged, accumulator)
    return merged

  def extract_output(self, accumulator: list[Any]) -> tuple[Any, ...]:
    return tuple(accumulator)


def _add_census(acc: CensusAccumulator, code: int,
                counts: Sequence[int]) -> CensusAccumulator:
  acc.add(
      code,
      counts[_SRC],
      counts[_SYN],
      counts[_SRC_M],
      counts[_SYN_M],
      substantive=not counts[_FLAGS] & _NONSUBSTANTIVE)
  return acc


class CensusCombineFn(beam.CombineFn):
  """One column's Task 8 `CensusAccumulator` from its per-value entries
  (each value exactly once, after the per-value sum: the accumulator's
  precondition)."""

  def create_accumulator(self) -> CensusAccumulator:
    return CensusAccumulator()

  def add_input(self, mutable_accumulator: CensusAccumulator,
                element: tuple[int, Sequence[int]]) -> CensusAccumulator:
    code, counts = element
    return _add_census(mutable_accumulator, code, counts)

  def merge_accumulators(
      self, accumulators: Iterable[CensusAccumulator]) -> CensusAccumulator:
    merged = CensusAccumulator()
    for accumulator in accumulators:
      merged = merged.merge(accumulator)
    return merged

  def extract_output(self, accumulator: CensusAccumulator) -> CensusAccumulator:
    return accumulator


class _Contribution(NamedTuple):  # pylint: disable=too-many-instance-attributes  # one field per additive sum
  """One column's additive second-pass sums, each value weighted by its
  Horvitz-Thompson weight w_v (1 when exact or in the head, 1 / rate in
  the tail). `ht_*` are the weighted counts a value-sampled view reads;
  `ca_*`/`cr_*` the adherence / copy-rate ratios' plain sums as
  `noise.StratifiedShare` holds them (head y/x, tail y/x, the tail's
  Σy², Σxy, Σx² and cluster count); lift counts are plain counts over
  the hash range."""
  tvd: float = 0.0
  jsd: float = 0.0
  tvd_floor: float = 0.0
  w2: float = 0.0
  q_on_p0: float = 0.0
  cs_src: float = 0.0
  cs_syn: float = 0.0
  rf_src: float = 0.0
  rf_syn: float = 0.0
  b_tvd: float = 0.0
  b_jsd: float = 0.0
  b_r_mass: float = 0.0
  b_w2: float = 0.0
  b_r_on_p0: float = 0.0
  b_cov: float = 0.0
  n_src_r: float = 0.0
  clc_src_r: float = 0.0
  k_src_r: float = 0.0
  v_r: float = 0.0
  m_r: float = 0.0
  rows_r: float = 0.0
  v_h: float = 0.0
  m_h: float = 0.0
  rows_h: float = 0.0
  v_e: float = 0.0
  m_e: float = 0.0
  v_he: float = 0.0
  m_he: float = 0.0
  pool_r: float = 0.0
  pool_h: float = 0.0
  ht_n_src: float = 0.0
  ht_n_syn: float = 0.0
  ht_k_src: float = 0.0
  ht_k_syn: float = 0.0
  ht_k_src_m: float = 0.0
  ht_k_syn_m: float = 0.0
  ht_clc_src_m: float = 0.0
  ht_clc_syn_m: float = 0.0
  ht_f1_src: float = 0.0
  ht_f1_src_m: float = 0.0
  ht_f1_syn_m: float = 0.0
  ht_cov: float = 0.0
  ht_novel: float = 0.0
  ht_copies: float = 0.0
  ht_subst: float = 0.0
  ca_hy: float = 0.0
  ca_hx: float = 0.0
  ca_ty: float = 0.0
  ca_tx: float = 0.0
  ca_yy: float = 0.0
  ca_xy: float = 0.0
  ca_xx: float = 0.0
  ca_tc: float = 0.0
  cr_hy: float = 0.0
  cr_hx: float = 0.0
  cr_ty: float = 0.0
  cr_tx: float = 0.0
  cr_yy: float = 0.0
  cr_xy: float = 0.0
  cr_xx: float = 0.0
  cr_tc: float = 0.0


def _add_contributions(a: _Contribution, b: _Contribution) -> _Contribution:
  return _Contribution._make(x + y for x, y in zip(a, b, strict=True))


class ContributionCombineFn(beam.CombineFn):
  """Sums `_Contribution`s (float sums: associative up to rounding)."""

  def create_accumulator(self) -> _Contribution:
    return _Contribution()

  def add_input(self, mutable_accumulator: _Contribution,
                element: _Contribution) -> _Contribution:
    return _add_contributions(mutable_accumulator, element)

  def merge_accumulators(
      self, accumulators: Iterable[_Contribution]) -> _Contribution:
    merged = _Contribution()
    for accumulator in accumulators:
      merged = _add_contributions(merged, accumulator)
    return merged

  def extract_output(self, accumulator: _Contribution) -> _Contribution:
    return accumulator


@dataclass
class MaskSummary:  # pylint: disable=too-many-instance-attributes  # one field per shape statistic
  """One column's shape census: head masks (≥ `SHAPE_HEAD_FLOOR` of the
  source's non-empty values) with their raw counts, the raw TV over every
  mask, and adherence counts (`<long>` excluded)."""
  head: dict[str, tuple[int, int]] = field(default_factory=dict)
  seen_src: int = 0
  seen_syn: int = 0
  long_src: int = 0
  long_syn: int = 0
  raw_tv: float = 0.0
  adherent_syn: int = 0
  adherent_ref: int = 0
  masks_src: int = 0
  masks_syn: int = 0

  def merge(self, other: MaskSummary) -> MaskSummary:
    return MaskSummary(
        head={
            **self.head,
            **other.head
        },
        seen_src=self.seen_src + other.seen_src,
        seen_syn=self.seen_syn + other.seen_syn,
        long_src=self.long_src + other.long_src,
        long_syn=self.long_syn + other.long_syn,
        raw_tv=self.raw_tv + other.raw_tv,
        adherent_syn=self.adherent_syn + other.adherent_syn,
        adherent_ref=self.adherent_ref + other.adherent_ref,
        masks_src=self.masks_src + other.masks_src,
        masks_syn=self.masks_syn + other.masks_syn)


class MaskSummaryCombineFn(beam.CombineFn):
  """Merges `MaskSummary`s of one column (each mask exactly once)."""

  def create_accumulator(self) -> MaskSummary:
    return MaskSummary()

  def add_input(self, mutable_accumulator: MaskSummary,
                element: MaskSummary) -> MaskSummary:
    return mutable_accumulator.merge(element)

  def merge_accumulators(self,
                         accumulators: Iterable[MaskSummary]) -> MaskSummary:
    merged = MaskSummary()
    for accumulator in accumulators:
      merged = merged.merge(accumulator)
    return merged

  def extract_output(self, accumulator: MaskSummary) -> MaskSummary:
    return accumulator


def _merge_literals(a: Mapping[int, str], b: Mapping[int, str]) -> dict:
  merged = dict(a)
  for code, text in b.items():
    merged[code] = min(merged[code], text) if code in merged else text
  return merged


class LiteralsCombineFn(beam.CombineFn):
  """Merges {code: text} maps (the smallest text per code: order-free)."""

  def create_accumulator(self) -> dict[int, str]:
    return {}

  def add_input(self, mutable_accumulator: dict[int, str],
                element: Mapping[int, str]) -> dict[int, str]:
    return _merge_literals(mutable_accumulator, element)

  def merge_accumulators(
      self, accumulators: Iterable[dict[int, str]]) -> dict[int, str]:
    merged: dict[int, str] = {}
    for accumulator in accumulators:
      merged = _merge_literals(merged, accumulator)
    return merged

  def extract_output(self, accumulator: dict[int, str]) -> dict[int, str]:
    return accumulator


# --------------------------------------------------------------------------
# the second pass (per value, per mask)
# --------------------------------------------------------------------------
def column_coverage(col: CensusColumn, acc: CensusAccumulator,
                    sizes: ColumnSizes) -> tuple[float | None, float | None]:
  """(source, synthetic) Chao-Shen sample coverage at matched n; (None,
  None) on a value-sampled column, where the census does not estimate
  it (Chao-Shen is a detail, R3)."""
  del sizes  # the exact census's own totals are the dense ones
  if col.sampled:
    return None, None
  view = summarize(acc)
  src, syn = view["chao_shen_coverage_src_m"], view["chao_shen_coverage_syn_m"]
  return (src if isinstance(src, float) else None,
          syn if isinstance(syn, float) else None)


def _present_probability(n: int, c: int, m: int) -> float:
  """P(a value held c times among n appears in a random m-subsample):
  1 - C(n - c, m) / C(n, m) (Hurlbert, 1971)."""
  if c <= 0 or m <= 0:
    return 0.0
  if n - c < m:
    return 1.0
  log_absent = (
      math.lgamma(n - c + 1) - math.lgamma(n - c - m + 1) - math.lgamma(n + 1) +
      math.lgamma(n - m + 1))
  return -math.expm1(log_absent)


def _distribution_terms(f: dict[str, float], w: float, counts: Sequence[int],
                        sizes: ColumnSizes) -> None:
  cs, cy = counts[_SRC], counts[_SYN]
  n_src, n_syn = sizes.n_src, sizes.n_syn
  tvd, jsd = tvd_jsd_from_census(((cs, cy),), n_src, n_syn)
  p, q = cs / n_src, cy / n_syn
  f["tvd"] = w * tvd
  f["jsd"] = w * jsd
  f["tvd_floor"] = w * noise.tvd_null_expectation((p,), n_src, n_syn)
  if p > 0:
    f["w2"] = w * (q - p)**2 / p
  else:
    f["q_on_p0"] = w * q
  m = min(n_src, n_syn)
  f["rf_src"] = w * _present_probability(n_src, cs, m)
  f["rf_syn"] = w * _present_probability(n_syn, cy, m)


def _baseline_terms(f: dict[str, float], w: float, cs: int, cr: int, n_src: int,
                    n_ref: int) -> None:
  btvd, bjsd = tvd_jsd_from_census(((cs, cr),), n_src, n_ref)
  p, r = cs / n_src, cr / n_ref
  f["b_tvd"] = w * btvd
  f["b_jsd"] = w * bjsd
  f["b_r_mass"] = w * r
  if p > 0:
    f["b_w2"] = w * (r - p)**2 / p
    if r > 0:
      f["b_cov"] = w * p
  else:
    f["b_r_on_p0"] = w * r


def _lift_terms(f: dict[str, float], code: int, counts: Sequence[int],
                ref: ColumnRef) -> None:
  """The rare, substantive value's membership in the exclusive panel sets
  (D3): V_R vs V_H, V_E vs V_H_E, and the pool."""
  cy = counts[_SYN]
  hit = 1.0 if cy > 0 else 0.0
  in_r, in_h = code in ref.r_counts, code in ref.h_codes
  in_pool = code in ref.pool_codes
  if in_r != in_h:
    side = "r" if in_r else "h"
    f[f"v_{side}"] = 1.0
    f[f"m_{side}"] = hit
    f[f"rows_{side}"] = float(cy)
    f[f"pool_{side}"] = 1.0 if in_pool else 0.0
  in_e, in_he = code in ref.e_codes, code in ref.he_codes
  if in_e != in_he:
    side = "e" if in_e else "he"
    f[f"v_{side}"] = 1.0
    f[f"m_{side}"] = hit


def _ht_counts(f: dict[str, float], w: float, counts: Sequence[int]) -> None:
  """The weighted counts of values and rows (the value-sampled view)."""
  cs, cy, csm, cym = (counts[_SRC], counts[_SYN], counts[_SRC_M],
                      counts[_SYN_M])
  f["ht_n_src"] = w * cs
  f["ht_n_syn"] = w * cy
  if cs:
    f["ht_k_src"] = w
    f["ht_f1_src"] = w if cs == 1 else 0.0
  if cy:
    f["ht_k_syn"] = w
  if csm:
    f["ht_k_src_m"] = w
    f["ht_clc_src_m"] = w * csm * math.log(csm)
    f["ht_f1_src_m"] = w if csm == 1 else 0.0
  if cym:
    f["ht_k_syn_m"] = w
    f["ht_clc_syn_m"] = w * cym * math.log(cym)
    f["ht_f1_syn_m"] = w if cym == 1 else 0.0


def _ht_shares(f: dict[str, float], w: float, counts: Sequence[int],
               tail: bool) -> None:
  """The ratio numerators/denominators (coverage, novelty, adherence,
  copies) and, for a tail value, its cluster sums."""
  cs, cy = counts[_SRC], counts[_SYN]
  adherent = cy if cs > 0 else 0
  substantive = cy if not counts[_FLAGS] & _NONSUBSTANTIVE else 0
  copies = substantive if 0 < cs < RARE_COUNT else 0
  f["ht_cov"] = w * cs if cy > 0 else 0.0
  f["ht_novel"] = w * (cy - adherent)
  f["ht_copies"] = w * copies
  f["ht_subst"] = w * substantive
  for prefix, y, x in (("ca", adherent, cy), ("cr", copies, substantive)):
    if not tail:
      f[f"{prefix}_hy"], f[f"{prefix}_hx"] = float(y), float(x)
      continue
    f[f"{prefix}_ty"], f[f"{prefix}_tx"] = float(y), float(x)
    f[f"{prefix}_yy"], f[f"{prefix}_xy"], f[f"{prefix}_xx"] = (float(y * y),
                                                               float(y * x),
                                                               float(x * x))
    f[f"{prefix}_tc"] = 1.0 if x > 0 else 0.0


def stratified_share(c: _Contribution, share: str) -> noise.StratifiedShare:
  """One share's sums (`adherence` or `copy`) as the interval reads them."""
  prefix = {"adherence": "ca", "copy": "cr"}[share]
  return noise.StratifiedShare(*(getattr(c, f"{prefix}_{name}")
                                 for name in ("hy", "hx", "ty", "tx", "yy",
                                              "xy", "xx", "tc")))


def value_contribution(
    col: CensusColumn, code: int, counts: Sequence[int], sizes: ColumnSizes,
    ref: ColumnRef | None, coverage: tuple[float | None,
                                           float | None]) -> _Contribution:
  """One distinct value's share of every additive second-pass sum, at its
  Horvitz-Thompson weight (`CensusColumn.weight_of`)."""
  w = col.weight_of(code)
  f: dict[str, float] = {}
  _ht_counts(f, w, counts)
  _ht_shares(f, w, counts, col.in_tail(code))
  cs, csm, cym, csr = counts[_SRC], counts[_SRC_M], counts[_SYN_M], counts[
      _SRC_R]
  if sizes.n_src > 0 and sizes.n_syn > 0:
    _distribution_terms(f, w, counts, sizes)
  cov_src, cov_syn = coverage
  if cov_src is not None and sizes.n_src_m > 0:
    f["cs_src"] = w * chao_shen_term(csm, sizes.n_src_m, cov_src)
  if cov_syn is not None and sizes.n_syn_m > 0:
    f["cs_syn"] = w * chao_shen_term(cym, sizes.n_syn_m, cov_syn)
  if csr > 0:
    f["n_src_r"] = float(csr)
    f["clc_src_r"] = csr * math.log(csr)
    f["k_src_r"] = 1.0
  if ref is not None:
    if ref.r_nonnull > 0 and sizes.n_src > 0:
      _baseline_terms(f, w, cs, ref.r_counts.get(code, 0), sizes.n_src,
                      ref.r_nonnull)
    substantive = not counts[_FLAGS] & _NONSUBSTANTIVE
    # lifts read the hash range only (a uniform draw; R, H, E, H_E thinned
    # alike), never the head's certainty
    if substantive and 0 < cs < RARE_COUNT and col.in_hash_range(code):
      _lift_terms(f, code, counts, ref)
  return _Contribution(**f)


def mask_part(mask: str, counts: Sequence[int], sizes: ColumnSizes,
              ref: ColumnRef | None) -> MaskSummary:
  """One mask's share of its column's `MaskSummary` (exact: the mask pass
  counts every value, R67)."""
  cs, cy = int(counts[0]), int(counts[1])
  summary = MaskSummary(
      seen_src=cs, seen_syn=cy, masks_src=int(cs > 0), masks_syn=int(cy > 0))
  if sizes.ne_src > 0 and sizes.ne_syn > 0:
    summary.raw_tv = 0.5 * abs(cs / sizes.ne_src - cy / sizes.ne_syn)
  if mask == _LONG_MASK:
    summary.long_src, summary.long_syn = cs, cy
    return summary
  if cs > 0:
    summary.adherent_syn = cy
    if ref is not None:
      summary.adherent_ref = int(ref.r_masks.get(mask, 0))
  if sizes.ne_src > 0 and cs / sizes.ne_src >= SHAPE_HEAD_FLOOR:
    summary.head = {mask: (cs, cy)}
  return summary


# --------------------------------------------------------------------------
# one table's metrics and profiles
# --------------------------------------------------------------------------
@dataclass
class ColumnParts:
  """What the census produced for one column (None: nothing counted)."""
  acc: CensusAccumulator | None = None
  contrib: _Contribution | None = None
  masks: MaskSummary | None = None
  literals: dict[int, str] = field(default_factory=dict)
  substantive_syn: int | None = None


class _Emitter:
  """Collects one table's `MetricValue`s with the catalogue's baseline
  rule (as the dense pass: a baseline-true metric without a baseline
  says why)."""

  def __init__(self, spec: CensusSpec, refs: Mapping[int, ColumnRef]):
    self.spec = spec
    self.refs = refs
    self.rows: list[MetricValue] = []

  def value(self,
            metric_id: str,
            col: CensusColumn,
            value: float | None,
            *,
            sizes: tuple[int | None, int | None],
            baseline: float | None = None,
            baseline_reason: str | None = None,
            detail: Mapping[str, Any] | None = None,
            method: Method | None = None,
            **fields: Any) -> None:
    metric = _catalogue().get(metric_id)
    notes = dict(_sig(dict(detail or {})))
    method, rate = self._estimate(metric_id, col, method, notes)
    if not metric.baseline:
      baseline = None
    elif baseline is None:
      notes["baseline_reason"] = baseline_reason or (
          "no reference sample (the R panel)"
          if col.j not in self.refs else "undefined on the reference sample")
    self.rows.append(
        MetricValue(
            metric_id=metric_id,
            table=self.spec.table,
            value=None if value is None else _sig(float(value)),
            column=col.name,
            column_kind=col.kind.value,
            baseline_value=None if baseline is None else _sig(float(baseline)),
            n_source=sizes[0],
            n_synthetic=sizes[1],
            method=method,
            sample_rate=rate,
            encoding_plan_digest=self.spec.encoding_plan_digest,
            detail=notes,
            **{
                name: _sig(field_value) for name, field_value in fields.items()
            }))

  def skip(self,
           metric_id: str,
           col: CensusColumn,
           reason: str,
           *,
           sizes: tuple[int | None, int | None] = (None, None),
           method: Method | None = None,
           detail: Mapping[str, Any] | None = None) -> None:
    notes: dict[str, Any] = {"reason": reason, **(detail or {})}
    method, rate = self._estimate(metric_id, col, method, notes)
    self.rows.append(
        MetricValue(
            metric_id=metric_id,
            table=self.spec.table,
            value=None,
            column=col.name,
            column_kind=col.kind.value,
            n_source=sizes[0],
            n_synthetic=sizes[1],
            method=method,
            sample_rate=rate,
            encoding_plan_digest=self.spec.encoding_plan_digest,
            detail=notes))

  def _estimate(self, metric_id: str, col: CensusColumn, method: Method | None,
                notes: dict[str, Any]) -> tuple[Method, float | None]:
    """A row's `(method, sample_rate)`: the column's own (exact, or
    value-sampled at its rate) unless a side the metric reads is a row
    sample — then `sample` at the lowest such rate, `notes` naming each
    side's rate and keeping the value-sample rate (R72)."""
    method = method or col.method
    rates = self.spec.sample_rates(_SIDES_READ.get(metric_id, _BOTH_SIDES))
    if not rates:
      return method, (col.rate if method is Method.VALUE_SAMPLED else None)
    notes["sample_rates"] = rates
    if method is Method.VALUE_SAMPLED:
      notes["estimator"] = method.value
      notes["value_sample_rate"] = col.rate
    return Method.SAMPLE, min(rates.values())

  def withheld(self, metric_id: str, v: _View, **seen: Any) -> bool:
    """Whether `metric_id` is withheld in sampled mode (R72) — then its
    `not_evaluated` row is written here, with `seen` (what the sample
    did observe; an exact census only: a value sample's counts are
    partial twice over) in `detail`."""
    reason = self.spec.sampled_reason(metric_id)
    if reason is None:
      return False
    self.skip(
        metric_id,
        v.col,
        reason,
        sizes=v.pair,
        detail=None if v.col.sampled else seen)
    return True


@dataclass
class _View:  # pylint: disable=too-many-instance-attributes  # everything one column's metrics read
  """One census column's parts and totals, as the metric functions read
  them. `summary`/`entropy` are `summarize` of the census when exact; on
  a value-sampled column `summary` holds the Horvitz-Thompson shares,
  `entropy` `summarize` of the HT view (None with `ht_problem` saying
  why when that view is out of range), and `shares` the adherence and
  copy-rate ratios with their cluster-robust intervals."""
  col: CensusColumn
  acc: CensusAccumulator
  summary: dict[str, Any]
  entropy: dict[str, Any] | None
  c: _Contribution
  masks: MaskSummary | None
  literals: dict[int, str]
  sizes: ColumnSizes
  ref: ColumnRef | None
  shares: dict[str, tuple[float, float, float] | None] | None = None
  ht_problem: str | None = None
  substantive_syn: int | None = None

  def distinct(self, matched: bool) -> tuple[float, float]:
    """(source, synthetic) distinct values, full or matched n: counted
    when exact, Horvitz-Thompson estimates when value-sampled."""
    if self.col.sampled:
      c = self.c
      return ((c.ht_k_src_m, c.ht_k_syn_m) if matched else
              (c.ht_k_src, c.ht_k_syn))
    acc = self.acc
    return ((float(acc.k_src_m), float(acc.k_syn_m)) if matched else
            (float(acc.k_src), float(acc.k_syn)))

  @property
  def pair(self) -> tuple[int, int]:
    return (self.sizes.n_src, self.sizes.n_syn)

  def missing(self, what: str = "non-null values") -> str | None:
    if self.sizes.n_src <= 0:
      return f"no {what} on the source side"
    if self.sizes.n_syn <= 0:
      return f"no {what} on the synthetic side"
    return None

  def missing_matched(self) -> str | None:
    if self.sizes.n_src_m <= 0:
      return "no matched-n values on the source side"
    if self.sizes.n_syn_m <= 0:
      return "no matched-n values on the synthetic side"
    return None

  def missing_masked(self) -> str | None:
    if self.sizes.ne_src <= 0:
      return "no non-empty strings on the source side"
    if self.sizes.ne_syn <= 0:
      return "no non-empty strings on the synthetic side"
    return None

  def ref_baseline(self) -> bool:
    return self.ref is not None and self.ref.r_nonnull > 0

  def ref_missing_mass(self) -> float:
    """Half R's mass on counted values the census never saw (a source
    that moved): the driver's R mass on the census's draw less the part
    the census found, both on the same draw (R67 M5)."""
    if self.ref is None:
      return 0.0
    return 0.5 * max(0.0, self.ref.r_mass_ht - self.c.b_r_mass)


_NO_CENSUS = ("no value census for this column (census: none — the planner "
              "censuses every non-key, non-nested column)")

_SAMPLED_INTERVAL = ("cluster-robust: values are the sampling units; the head "
                     "exact, a Korn-Graubard Clopper-Pearson interval on the "
                     "tail's ratio over its exact row total")

_UNOBSERVED = ("value-sampled census: none in the head or the sampled hash "
               "range, so there is nothing to estimate from")


def _share_row(e: _Emitter, v: _View, metric_id: str, share: str,
               counts: tuple[int, int], detail: dict[str, Any],
               unobserved: str) -> None:
  """A share with its interval: Wilson on the exact census's rows; on a
  value-sampled column the stratified ratio and its cluster-robust
  interval (`noise.stratified_ratio_interval`) — not evaluated, with
  `unobserved` as the reason, when no denominator row was observed
  (R75: never a fabricated 0)."""
  if v.col.sampled:
    assert v.shares is not None
    ratio = v.shares.get(share)
    if ratio is None:
      e.skip(metric_id, v.col, f"{unobserved} ({_UNOBSERVED})", sizes=v.pair)
      return
    value, lo, hi = ratio  # in [0, 1]: a mix of the head's and tail's shares
    detail = {**detail, "interval": _SAMPLED_INTERVAL}
  else:
    k, n = counts
    value = k / n
    lo, hi = noise.wilson_interval(k, n)
  e.value(
      metric_id,
      v.col,
      value,
      synthetic_value=value,
      ci_low=lo,
      ci_high=hi,
      sizes=v.pair,
      detail=detail)


def _category_adherence(e: _Emitter, v: _View) -> None:
  metric_id = "field.category_adherence"
  acc = v.acc
  if e.withheld(
      metric_id, v, matched_rows_lower_bound=acc.n_syn - acc.novelty_syn):
    return
  reason = v.missing()
  if not reason and not v.col.sampled and not acc.n_syn:
    reason = "no counted synthetic value"
  if reason:
    e.skip(metric_id, v.col, reason, sizes=v.pair)
    return
  detail = ({} if v.col.sampled else {
      "invented_rows": acc.novelty_syn,
      "counted_rows": acc.n_syn
  })
  _share_row(e, v, metric_id, "adherence",
             (acc.n_syn - acc.novelty_syn, acc.n_syn), detail,
             "no counted synthetic value")


def _copy_rate(e: _Emitter, v: _View) -> None:
  metric_id = "field.substantive_copy_rate"
  acc = v.acc
  # no bound here: a value rare in the source SAMPLE (count < 10) need
  # not be rare in the source, and a copy's twin may be unsampled
  if e.withheld(
      metric_id,
      v,
      copies_in_sample=acc.copies_substantive,
      substantive=acc.substantive_syn,
      note=("copies counted against the source sample: neither bound of "
            "the full count")):
    return
  substantive = v.substantive_syn if v.col.sampled else acc.substantive_syn
  reason = v.missing() or (None if substantive else (
      "no substantive synthetic values (non-null, non-empty, not a "
      "0001-/9999- date sentinel)"))
  if reason:
    e.skip(metric_id, v.col, reason, sizes=v.pair)
    return
  detail: dict[str, Any] = {
      "rare_below": RARE_COUNT,
      "substantive": substantive
  }
  if not v.col.sampled:
    detail["copies"] = acc.copies_substantive
  if v.col.kind is _K.TEMPORAL:
    detail["day_granularity"] = v.col.day_granularity
  _share_row(e, v, metric_id, "copy",
             (acc.copies_substantive, acc.substantive_syn), detail,
             "no substantive synthetic values counted")


def _bounded(x: float | None, hi: float = 1.0) -> float | None:
  """`x`, or None past `hi`: a Horvitz-Thompson estimate of a bounded
  quantity that left its range cannot be reported (R67 guards)."""
  return None if x is None or x > hi + _REL_TOL else x


_OUT_OF_RANGE = ("the Horvitz-Thompson estimate exceeds 1, the metric's "
                 "range: value sampling cannot estimate it here")


def _tvd_jsd_w(e: _Emitter, v: _View) -> None:
  reason = v.missing()
  ids = ("column.tvd", "column.jsd", "column.cohens_w")
  if reason:
    for metric_id in ids:
      e.skip(metric_id, v.col, reason, sizes=v.pair)
    return
  c, has_ref = v.c, v.ref_baseline()
  k = max(1, round(v.distinct(matched=False)[0]))
  for metric_id, value, floor, base in (("column.tvd", c.tvd, c.tvd_floor,
                                         c.b_tvd),
                                        ("column.jsd", c.jsd,
                                         noise.jsd_null_expectation_bits(
                                             k, *v.pair), c.b_jsd)):
    if _bounded(value) is None:
      e.skip(metric_id, v.col, _OUT_OF_RANGE, sizes=v.pair)
      continue
    e.value(
        metric_id,
        v.col,
        value,
        noise_floor=floor,
        baseline=_bounded(base + v.ref_missing_mass()) if has_ref else None,
        baseline_reason=(None if not has_ref else _OUT_OF_RANGE),
        sizes=v.pair,
        detail={"categories": k} if metric_id == "column.jsd" else None)
  e.value(
      "column.cohens_w",
      v.col,
      math.sqrt(c.w2),
      baseline=math.sqrt(c.b_w2) if has_ref else None,
      sizes=v.pair,
      detail={"q_mass_on_p0": c.q_on_p0})


def _top1(e: _Emitter, v: _View) -> None:
  metric_id = "column.top1_share_delta"
  if v.col.sampled and not v.col.head:
    e.skip(
        metric_id,
        v.col,
        f"value-sampled census (rate {v.col.rate:g}) with no head stratum "
        "(no top list was planned): the top-1 value may lie outside the "
        "sampled hash range",
        sizes=v.pair)
    return
  reason = v.missing()
  if reason:
    e.skip(metric_id, v.col, reason, sizes=v.pair)
    return
  n_src, n_syn = v.pair
  k_src = v.acc.top_src[0][0] if v.acc.top_src else 0
  k_syn = v.acc.top_syn[0][0] if v.acc.top_syn else 0
  p_src, p_syn = k_src / n_src, k_syn / n_syn
  lo, hi = folded_abs_interval(
      *noise.newcombe_diff_interval(k_syn, n_syn, k_src, n_src))
  baseline = None
  if v.ref is not None and v.ref.r_nonnull and v.ref.r_counts:
    baseline = abs(max(v.ref.r_counts.values()) / v.ref.r_nonnull - p_src)
  e.value(
      metric_id,
      v.col,
      abs(p_syn - p_src),
      source_value=p_src,
      synthetic_value=p_syn,
      ci_low=lo,
      ci_high=hi,
      baseline=baseline,
      sizes=v.pair,
      detail={"head_values": len(v.col.head)} if v.col.sampled else None)


def _coverage(e: _Emitter, v: _View) -> None:
  metric_id = "column.coverage_mass"
  if e.withheld(metric_id, v, covered_rows_lower_bound=v.acc.coverage_mass_num):
    return
  reason = v.missing()
  coverage = _bounded(v.summary["coverage_mass"])
  if reason or coverage is None:
    e.skip(metric_id, v.col, reason or "no counted source value", sizes=v.pair)
    return
  e.value(
      metric_id,
      v.col,
      coverage,
      baseline=_bounded(v.c.b_cov) if v.ref_baseline() else None,
      baseline_reason=_OUT_OF_RANGE if v.ref_baseline() else None,
      sizes=v.pair)


def _novelty(e: _Emitter, v: _View) -> None:
  metric_id = "column.novelty_mass"
  if e.withheld(
      metric_id, v, matched_rows_lower_bound=v.acc.n_syn - v.acc.novelty_syn):
    return
  reason = v.missing()
  novelty = _bounded(v.summary["novelty_mass"])
  if reason or novelty is None:
    unobserved = f" ({_UNOBSERVED})" if v.col.sampled else ""
    e.skip(
        metric_id,
        v.col,
        reason or f"no counted synthetic value{unobserved}",
        sizes=v.pair)
    return
  e.value(
      metric_id,
      v.col,
      novelty,
      source_value=v.summary["good_turing_unseen_src"],
      synthetic_value=novelty,
      sizes=v.pair,
      detail={"target": "Good-Turing f1/N of the source"})


def _baseline_diversity(v: _View) -> tuple[dict[str, Any] | None, str | None]:
  """`summarize` of R (as the synthetic side) against the source's |R|
  matched subsample: the entropy and distinct baselines (D4, D5)."""
  if v.ref is None or not v.ref.r_nonnull:
    return None, None
  if v.col.sampled:
    return None, ("value-sampled census: the reference-matched source "
                  "subsample is not counted")
  if not v.c.n_src_r:
    return None, ("no source row fell in the reference-matched subsample (the "
                  "source row count was unknown or zero)")
  r = v.ref.r_counts
  # Added left to right, not with the builtin sum(): CPython 3.12 made the
  # sum() of floats compensated, which moves this total's last bit.
  clc_r = 0.0
  for c in r.values():
    clc_r += c * math.log(c)
  acc = CensusAccumulator(
      n_src_m=round(v.c.n_src_r),
      clc_src_m=v.c.clc_src_r,
      k_src_m=round(v.c.k_src_r),
      n_syn_m=sum(r.values()),
      clc_syn_m=clc_r,
      k_syn_m=len(r))
  return summarize(acc), None


def _entropy(e: _Emitter, v: _View, base: dict[str, Any] | None,
             base_reason: str | None) -> None:
  metric_id = "column.entropy_ratio"
  sizes = (v.sizes.n_src_m, v.sizes.n_syn_m)
  if e.withheld(
      metric_id,
      v,
      sample_ratio=None if v.entropy is None else v.entropy["entropy_ratio"],
      note="the ratio of the rows read, not the table's"):
    return
  reason = v.missing_matched() or v.ht_problem
  ent = v.entropy
  if not reason and ent is not None and ent["entropy_ratio"] is None:
    reason = str(ent["entropy_ratio_reason"] or "no matched-n values")
    if v.col.sampled:
      reason = f"{reason} (Horvitz-Thompson view of a value-sampled census)"
  if reason or ent is None:
    e.skip(metric_id, v.col, str(reason), sizes=sizes)
    return
  ln2 = math.log(2.0)
  sampled = v.col.sampled
  detail = {
      "matched_n": min(sizes),
      "n_src_m": sizes[0],
      "n_syn_m": sizes[1],
      "miller_madow_src_m_bits": ent["miller_madow_src_m_bits"],
      "miller_madow_syn_m_bits": ent["miller_madow_syn_m_bits"],
      "chao_shen_coverage_src_m": ent["chao_shen_coverage_src_m"],
      "chao_shen_coverage_syn_m": ent["chao_shen_coverage_syn_m"],
      "chao_shen_src_m_bits": None if sampled else v.c.cs_src / ln2,
      "chao_shen_syn_m_bits": None if sampled else v.c.cs_syn / ln2,
  }
  e.value(
      metric_id,
      v.col,
      ent["entropy_ratio"],
      source_value=ent["entropy_src_m_bits"],
      synthetic_value=ent["entropy_syn_m_bits"],
      baseline=None if base is None else base["entropy_ratio"],
      baseline_reason=base_reason,
      sizes=sizes,
      detail=detail)


def _distinct(e: _Emitter, v: _View, base: dict[str, Any] | None,
              base_reason: str | None) -> None:
  metric_id = "column.distinct_ratio"
  sizes = (v.sizes.n_src_m, v.sizes.n_syn_m)
  if e.withheld(
      metric_id,
      v,
      sample_ratio=v.summary["distinct_ratio"],
      distinct_src_in_sample=round(v.distinct(matched=False)[0]),
      distinct_syn_in_sample=round(v.distinct(matched=False)[1]),
      note="the ratio and counts of the rows read, not the table's"):
    return
  ratio = v.summary["distinct_ratio"]
  reason = v.missing_matched()
  if not reason and ratio is None:
    reason = "no counted source value at the matched n"
  if not reason and v.col.sampled and not v.c.ht_k_syn_m:
    # rows exist at matched n but none was counted: an HT distinct count
    # of 0 would be a fabricated collapse (R75)
    reason = f"no counted synthetic value at the matched n ({_UNOBSERVED})"
  if reason:
    e.skip(metric_id, v.col, reason, sizes=sizes)
    return
  c = v.c
  full, matched = v.distinct(matched=False), v.distinct(matched=True)
  detail = {
      "matched_n": min(sizes),
      "distinct_src": round(full[0]),
      "distinct_syn": round(full[1]),
      "rarefaction_n": min(v.pair),
      "rarefied_distinct_src": c.rf_src,
      "rarefied_distinct_syn": c.rf_syn,
      "distinct_ratio_rarefied": c.rf_syn / c.rf_src if c.rf_src else None,
  }
  e.value(
      metric_id,
      v.col,
      ratio,
      source_value=matched[0],
      synthetic_value=matched[1],
      baseline=None if base is None else base["distinct_ratio"],
      baseline_reason=base_reason,
      sizes=sizes,
      detail=detail)


def _ceiling(e: _Emitter, v: _View) -> None:
  metric_id = "column.distinct_ceiling_hit"
  if e.withheld(metric_id, v, distinct_syn_lower_bound=v.acc.k_syn):
    return
  if v.col.sampled:
    e.skip(
        metric_id,
        v.col,
        "value-sampled census: the exact synthetic distinct count is not "
        "measured, so a pool-cap hit cannot be tested",
        sizes=v.pair)
    return
  if v.sizes.n_syn <= 0:
    e.skip(
        metric_id,
        v.col,
        "no non-null values on the synthetic side",
        sizes=v.pair)
    return
  pool = v.ref.pool if v.ref is not None else None
  target = pool.target if pool is not None else None
  hit = v.acc.k_syn == POOL_CAP or (target is not None and
                                    v.acc.k_syn == target)
  e.value(
      metric_id,
      v.col,
      1.0 if hit else 0.0,
      sizes=v.pair,
      detail={
          "distinct_syn": v.acc.k_syn,
          "pool_cap": POOL_CAP,
          "pool_target": target
      })


def _tail_share(head_counts: Iterable[int], total: int) -> float:
  """The tail's share from counts (the head's integer sum first), so no
  float residue of `1 - Σ shares` — whose size depends on summation
  order — reaches a floor or a payload."""
  return max(0, total - sum(head_counts)) / total


def _masses(masks: MaskSummary, side: int, total: int) -> dict[str, float]:
  """{head mask: share} + the tail, in sorted mask order."""
  head = {m: masks.head[m][side] / total for m in sorted(masks.head)}
  head[_TAIL] = _tail_share((c[side] for c in masks.head.values()), total)
  return head


def _shape_head(e: _Emitter, v: _View) -> None:
  metric_id = "column.shape_head_tv"
  sizes = (v.sizes.ne_src, v.sizes.ne_syn)
  masks = v.masks
  reason = v.missing_masked()
  if not reason and (masks is None or not masks.head):
    reason = (f"no source mask holds ≥ {SHAPE_HEAD_FLOOR * 100:g} % of the "
              "non-empty values (near-unique masks, e.g. long prose): the head "
              "TV would compare one tail bucket")
  if reason or masks is None:
    e.skip(metric_id, v.col, str(reason), sizes=sizes, method=Method.EXACT)
    return
  src = _masses(masks, 0, v.sizes.ne_src)
  syn = _masses(masks, 1, v.sizes.ne_syn)
  _, head, _ = shapes.shape_head_tv(src, syn, floor=SHAPE_HEAD_FLOOR)
  baseline = None
  ref = v.ref
  if ref is not None and ref.r_nonempty:
    r_mass = {
        m: ref.r_masks.get(m, 0) / ref.r_nonempty for m in sorted(masks.head)
    }
    r_mass[_TAIL] = _tail_share((ref.r_masks.get(m, 0) for m in masks.head),
                                ref.r_nonempty)
    baseline = shapes.shape_head_tv(src, r_mass, floor=SHAPE_HEAD_FLOOR)[1]
  e.value(
      metric_id,
      v.col,
      head,
      noise_floor=noise.tvd_null_expectation(list(src.values()), *sizes),
      baseline=baseline,
      sizes=sizes,
      method=Method.EXACT,
      detail={
          "raw_tv": masks.raw_tv,
          "head_masks": len(masks.head),
          "head_floor": SHAPE_HEAD_FLOOR,
          "distinct_masks_src": masks.masks_src,
          "distinct_masks_syn": masks.masks_syn,
      })


def _shape_adherence(e: _Emitter, v: _View) -> None:
  metric_id = "field.shape_adherence"
  masks = v.masks
  sizes = (v.sizes.ne_src, v.sizes.ne_syn)
  reason = e.spec.sampled_reason(metric_id)
  if reason is not None:  # R72; the mask pass counts every value read
    e.skip(
        metric_id,
        v.col,
        reason,
        sizes=sizes,
        method=Method.EXACT,
        detail={
            "adherent_lower_bound": 0 if masks is None else masks.adherent_syn
        })
    return
  reason = v.missing_masked()
  counted = 0 if masks is None else masks.seen_syn - masks.long_syn
  if not reason and counted <= 0:
    reason = (f"every non-empty synthetic value is longer than "
              f"{MASK_MAX_CHARS} characters (masks pooled, not compared)")
  if reason or masks is None:
    e.skip(metric_id, v.col, str(reason), sizes=sizes, method=Method.EXACT)
    return
  lo, hi = noise.wilson_interval(masks.adherent_syn, counted)
  baseline = None
  if v.ref is not None and v.ref.r_short:
    baseline = masks.adherent_ref / v.ref.r_short
  e.value(
      metric_id,
      v.col,
      masks.adherent_syn / counted,
      synthetic_value=masks.adherent_syn / counted,
      ci_low=lo,
      ci_high=hi,
      baseline=baseline,
      sizes=sizes,
      method=Method.EXACT,
      detail={"compared_values": counted})


class _LiftInput(NamedTuple):
  view: _View | None
  reason: str | None


def _lift_input(spec: CensusSpec, col: CensusColumn,
                view: _View | None) -> _LiftInput:
  if not col.censused:
    return _LiftInput(None, _NO_CENSUS)
  if not spec.verified:
    return _LiftInput(None, str(spec.panel_reason))
  if view is None or view.ref is None:
    return _LiftInput(None, "no reference values for this column")
  if view.sizes.n_syn <= 0:
    return _LiftInput(view, "no non-null values on the synthetic side")
  if not view.c.v_r or not view.c.v_h:
    return _LiftInput(
        view, "no rare (source count < 10), substantive value is held only by "
        "the reference sample R, or none only by the holdout H: the lift has "
        "no exposure on one side")
  return _LiftInput(view, None)


def _rate_ratio_row(e: _Emitter, metric_id: str, v: _View, counts: tuple[float,
                                                                         float],
                    alpha: float, detail: dict[str, Any]) -> None:
  c = v.c
  ratio, lo, hi = noise.rate_ratio(
      round(counts[0]), c.v_r, round(counts[1]), c.v_h, alpha=alpha)
  e.value(
      metric_id,
      v.col,
      ratio,
      ci_low=lo,
      ci_high=hi,
      sizes=v.pair,
      detail={
          "copies_r": round(counts[0]),
          "copies_h": round(counts[1]),
          "exposed_r": round(c.v_r),
          "exposed_h": round(c.v_h),
          "alpha": alpha,
          **detail,
      })


def _exposure_detail(c: _Contribution) -> dict[str, Any]:
  if not c.v_e or not c.v_he:
    return {"exposure": None}
  ratio, lo, hi = noise.rate_ratio(
      round(c.m_e), c.v_e, round(c.m_he), c.v_he, alpha=LIFT_ALPHA)
  return {
      "exposure": {
          "lift": ratio,
          "ci_low": lo,
          "ci_high": None if math.isinf(hi) else hi,
          "copies_e": round(c.m_e),
          "copies_he": round(c.m_he),
          "exposed_e": round(c.v_e),
          "exposed_he": round(c.v_he),
      }
  }


def _lifts(e: _Emitter, spec: CensusSpec, views: Mapping[int, _View]) -> None:
  value_id, pool_id = ("field.value_memorization_lift",
                       "field.pool_memorization_lift")
  inputs = {
      col.j: _lift_input(spec, col, views.get(col.j))
      for col in spec.columns
      if col.kind in _reach(value_id) or col.kind in _reach(pool_id)
  }
  family = sorted(
      j for j, li in inputs.items()
      if li.reason is None and spec.columns[j].kind in _reach(value_id))
  alpha = LIFT_ALPHA / max(1, len(family))
  for j, (view, reason) in sorted(inputs.items()):
    col = spec.columns[j]
    if col.kind in _reach(value_id):
      if reason is not None or view is None:
        e.skip(value_id, col, str(reason), sizes=_pair(view))
      else:
        _rate_ratio_row(
            e, value_id, view, (view.c.m_r, view.c.m_h), alpha, {
                "rows_r": round(view.c.rows_r),
                "rows_h": round(view.c.rows_h),
                "bonferroni_family": len(family),
                **_exposure_detail(view.c),
            })
    if col.kind in _reach(pool_id):
      pool_reason = reason
      if (col.censused and view is not None and view.ref is not None and
          view.ref.pool_reason is not None):
        pool_reason = view.ref.pool_reason
      if pool_reason is not None or view is None or view.ref is None:
        e.skip(pool_id, col, str(pool_reason), sizes=_pair(view))
        continue
      pool = view.ref.pool
      assert pool is not None
      _rate_ratio_row(
          e, pool_id, view, (view.c.pool_r, view.c.pool_h), LIFT_ALPHA, {
              "pool_size": len(pool.values),
              "pool_target": pool.target,
              "model_uri": pool.model_uri,
          })


def _pair(view: _View | None) -> tuple[int | None, int | None]:
  return (None, None) if view is None else view.pair


def _topk_items(v: _View, top: Sequence[tuple[int, int]], total: int,
                label_key: bytes) -> list[dict[str, Any]]:
  src_counts = dict((code, count) for count, code in v.acc.top_src)
  bool_labels = ({
      hash64(v.col.name, True): "true",
      hash64(v.col.name, False): "false"
  } if v.col.kind is _K.BOOLEAN else {})
  items = []
  for count, code in top[:TOPK_ITEMS]:
    text = bool_labels.get(code) or v.literals.get(code)
    literal = (
        v.col.literal_ok and code in v.col.detection and text is not None and
        src_counts.get(code, 0) >= RARE_COUNT)
    items.append({
        "label": text if literal else hashed_label(code, key=label_key),
        "literal": bool(literal),
        "count": int(count),
        "share": count / total if total else 0.0,
    })
  return items


def _side_sample(spec: CensusSpec, side: str) -> dict[str, float]:
  """A profile payload's `sample_rate` when its side was read as a row
  sample (R72), else nothing."""
  rate = spec.sample_rates((side,)).get(side)
  return {} if rate is None else {"sample_rate": rate}


def _topk_profiles(spec: CensusSpec, v: _View, totals: Mapping[str, SideTotals],
                   label_key: bytes) -> Iterator[ProfileValue]:
  distinct = v.distinct(matched=False)
  for side, top, k in ((_SOURCE, v.acc.top_src, distinct[0]),
                       (_SYNTHETIC, v.acc.top_syn, distinct[1])):
    side_totals = totals[side]
    total = side_totals.nonnull[v.col.j]
    items = _topk_items(v, top, total, label_key)
    payload: dict[str, Any] = {
        "items": items,
        "other_count": max(0, total - sum(i["count"] for i in items)),
        "distinct": round(k),
        "total": total,
        "nulls": side_totals.rows - total,
    }
    if v.col.sampled:
      payload["value_sample_rate"] = v.col.rate
    payload.update(_side_sample(spec, side))
    yield ProfileValue(
        table=spec.table,
        profile_kind="topk",
        side=side,
        column=v.col.name,
        payload=payload,
        n=total,
        truncated=v.col.sampled or round(k) > len(items))


def _mask_label(mask: str, src_count: int, label_key: bytes) -> str:
  if src_count >= RARE_COUNT and _PLACEHOLDERS.intersection(mask):
    return mask
  return hashed_label(mask_code(mask), key=label_key)


def _shape_profiles(spec: CensusSpec, v: _View,
                    label_key: bytes) -> Iterator[ProfileValue]:
  masks = v.masks
  if masks is None:
    return
  ranked = sorted(masks.head.items(), key=lambda kv: (-kv[1][0], kv[0]))
  for side, slot, total in ((_SOURCE, 0, v.sizes.ne_src), (_SYNTHETIC, 1,
                                                           v.sizes.ne_syn)):
    if total <= 0:
      continue
    items = [{
        "mask": _mask_label(mask, counts[0], label_key),
        "count": counts[slot],
        "share": counts[slot] / total,
    } for mask, counts in ranked]
    yield ProfileValue(
        table=spec.table,
        profile_kind="shape_mix",
        side=side,
        column=v.col.name,
        payload={
            "items":
                items,
            "tail_share":
                _tail_share((c[slot] for c in masks.head.values()), total),
            "head_floor":
                SHAPE_HEAD_FLOOR,
            **_side_sample(spec, side),
        },
        n=total)


def _ratio(num: float, den: float) -> float | None:
  return num / den if den > 0 else None


def _ht_problem(c: _Contribution, sizes: ColumnSizes) -> str | None:
  """Why a value-sampled column's Horvitz-Thompson entropy view cannot be
  used (R67), or None: each side needs a counted value at matched n and
  Σ c ln c within [0, n ln n] (a heavy value weighted by 1 / rate can
  push it past, where the entropy would turn negative)."""
  for side, clc, n, k in (("source", c.ht_clc_src_m, sizes.n_src_m,
                           c.ht_k_src_m), ("synthetic", c.ht_clc_syn_m,
                                           sizes.n_syn_m, c.ht_k_syn_m)):
    if n <= 0:
      continue
    if k <= 0:
      return (f"no counted value at the matched n on the {side} side "
              "(value-sampled census)")
    limit = n * math.log(n)
    if clc < 0.0 or clc > limit * (1.0 + _REL_TOL):
      return (f"the Horvitz-Thompson view is out of range on the {side} side "
              f"(Σ c ln c = {clc:.6g} outside [0, n ln n = {limit:.6g}]): "
              "value sampling cannot estimate the entropy here")
  return None


def _sampled_parts(
    col: CensusColumn, c: _Contribution, sizes: ColumnSizes,
    substantive_syn: int | None
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any], str | None]:
  """A value-sampled column's (summary, entropy, shares, problem): the
  Horvitz-Thompson ratios, `summarize` of the HT entropy view (validated),
  and the adherence / copy-rate ratios with their tail-only Korn-Graubard
  intervals, each tail total exact (the dense non-null count, the census's
  substantive count over every value; R70)."""
  adherence = noise.stratified_ratio_interval(
      stratified_share(c, "adherence"),
      col.rate,
      tail_total=sizes.n_syn - c.ca_hx)
  copy = noise.stratified_ratio_interval(
      stratified_share(c, "copy"),
      col.rate,
      tail_total=(None if substantive_syn is None else substantive_syn -
                  c.cr_hx))
  summary = {
      "coverage_mass": _ratio(c.ht_cov, c.ht_n_src),
      # one estimator for the two complements (adherence = 1 - novelty)
      "novelty_mass": (1.0 - adherence[0] if adherence is not None else _ratio(
          c.ht_novel, c.ht_n_syn)),
      "good_turing_unseen_src": _ratio(c.ht_f1_src, c.ht_n_src),
      "distinct_ratio": _ratio(c.ht_k_syn_m, c.ht_k_src_m),
  }
  shares = {"adherence": adherence, "copy": copy}
  problem = _ht_problem(c, sizes)
  if problem is not None:
    return summary, None, shares, problem
  entropy = summarize(
      CensusAccumulator(
          n_src=sizes.n_src,
          n_syn=sizes.n_syn,
          n_src_m=sizes.n_src_m,
          n_syn_m=sizes.n_syn_m,
          k_src=round(c.ht_k_src),
          k_syn=round(c.ht_k_syn),
          k_src_m=round(c.ht_k_src_m),
          k_syn_m=round(c.ht_k_syn_m),
          clc_src_m=c.ht_clc_src_m,
          clc_syn_m=c.ht_clc_syn_m,
          f1_src=round(c.ht_f1_src),
          f1_src_m=round(c.ht_f1_src_m),
          f1_syn_m=round(c.ht_f1_syn_m)))
  for side in ("src", "syn"):
    bits, k = entropy[f"entropy_{side}_m_bits"], entropy[f"distinct_{side}_m"]
    if isinstance(k, int) and k >= _DIVERSE_K and not (isinstance(bits, float)
                                                       and bits > 0.0):
      return summary, None, shares, (
          f"the Horvitz-Thompson entropy of the {side} side is not positive "
          f"with {k} values: value sampling cannot estimate it here")
  return summary, entropy, shares, None


def _view(col: CensusColumn, parts: ColumnParts | None, sizes: ColumnSizes,
          ref: ColumnRef | None) -> _View:
  acc = parts.acc if parts is not None and parts.acc is not None else (
      CensusAccumulator())
  c = (
      parts.contrib
      if parts is not None and parts.contrib is not None else _Contribution())
  view = _View(
      col=col,
      acc=acc,
      summary={},
      entropy=None,
      c=c,
      masks=parts.masks if parts is not None else None,
      literals=dict(parts.literals) if parts is not None else {},
      sizes=sizes,
      ref=ref)
  if parts is not None:
    view.substantive_syn = parts.substantive_syn
  if col.sampled:
    view.summary, view.entropy, view.shares, view.ht_problem = _sampled_parts(
        col, c, sizes, view.substantive_syn)
  else:
    view.summary = view.entropy = summarize(acc)
  return view


_VIEW_METRICS: tuple[tuple[str, ...], ...] = (
    ("field.category_adherence",),
    ("field.substantive_copy_rate",),
    ("column.tvd", "column.jsd", "column.cohens_w"),
    ("column.top1_share_delta",),
    ("column.coverage_mass",),
    ("column.novelty_mass",),
    ("column.distinct_ceiling_hit",),
    ("column.shape_head_tv",),
    ("field.shape_adherence",),
)
_VIEW_FNS = (_category_adherence, _copy_rate, _tvd_jsd_w, _top1, _coverage,
             _novelty, _ceiling, _shape_head, _shape_adherence)


def _column_metrics(e: _Emitter, col: CensusColumn, view: _View | None) -> None:
  for ids, fn in zip(_VIEW_METRICS, _VIEW_FNS, strict=True):
    if col.kind not in _reach(ids[0]):
      continue
    if view is None:
      for metric_id in ids:
        e.skip(metric_id, col, _NO_CENSUS)
      continue
    fn(e, view)
  diversity = ("column.entropy_ratio", "column.distinct_ratio")
  applicable = [m for m in diversity if col.kind in _reach(m)]
  if not applicable:
    return
  if view is None:
    for metric_id in applicable:
      e.skip(metric_id, col, _NO_CENSUS)
    return
  base, base_reason = _baseline_diversity(view)
  if "column.entropy_ratio" in applicable:
    _entropy(e, view, base, base_reason)
  if "column.distinct_ratio" in applicable:
    _distinct(e, view, base, base_reason)


def table_outputs(
    spec: CensusSpec, parts: Mapping[int, ColumnParts],
    totals: Mapping[str, SideTotals], refs: Mapping[int, ColumnRef], *,
    label_key: bytes) -> tuple[list[MetricValue], list[ProfileValue]]:
  """One table's census metrics (a row per owned id and applicable
  column: a value, or `not_evaluated` with a reason) and its topk /
  shape_mix profiles."""
  side_totals = {
      _SOURCE: totals.get(_SOURCE) or SideTotals.empty(spec),
      _SYNTHETIC: totals.get(_SYNTHETIC) or SideTotals.empty(spec),
  }
  e = _Emitter(spec, refs)
  views: dict[int, _View] = {}
  for col in spec.columns:
    if col.censused:
      views[col.j] = _view(
          col, parts.get(col.j),
          _sizes(side_totals[_SOURCE], side_totals[_SYNTHETIC], col.j),
          refs.get(col.j))
    _column_metrics(e, col, views.get(col.j))
  _lifts(e, spec, views)
  order = {metric_id: i for i, metric_id in enumerate(OWNED_METRIC_IDS)}
  position = {col.name: col.j for col in spec.columns}
  e.rows.sort(key=lambda mv: (position[str(mv.column)], order[mv.metric_id]))
  profiles: list[ProfileValue] = []
  for view in views.values():
    if view.col.kind in _TOPK_KINDS and view.acc.n_src + view.acc.n_syn:
      profiles.extend(_topk_profiles(spec, view, side_totals, label_key))
    if view.col.has_shapes:
      profiles.extend(_shape_profiles(spec, view, label_key))
  return e.rows, profiles


class CensusResult(NamedTuple):
  """`census_outputs`' result; `summaries` maps a plan column to its
  Task 8 accumulator."""
  metrics: list[MetricValue]
  profiles: list[ProfileValue]
  summaries: dict[int, CensusAccumulator]


def census_outputs(spec: CensusSpec, batches: Iterable[EncodedBatch],
                   totals: Mapping[str, SideTotals], refs: Mapping[int,
                                                                   ColumnRef],
                   *, label_key: bytes) -> CensusResult:
  """The whole census in one process (the Beam transform's reference
  implementation; the tests hold the two equal)."""
  counts = BatchCounts()
  for batch in batches:
    accumulate(spec, batch, counts)
  src = totals.get(_SOURCE) or SideTotals.empty(spec)
  syn = totals.get(_SYNTHETIC) or SideTotals.empty(spec)
  parts: dict[int, ColumnParts] = {}
  for (_, j, code), value in sorted(counts.values.items()):
    part = parts.setdefault(j, ColumnParts(acc=CensusAccumulator()))
    assert part.acc is not None
    _add_census(part.acc, code, value)
  coverage = {
      j: column_coverage(spec.columns[j], part.acc, _sizes(src, syn, j))
      for j, part in parts.items()
      if part.acc is not None
  }
  for (_, j, code), value in sorted(counts.values.items()):
    part = parts[j]
    item = value_contribution(spec.columns[j], code, value, _sizes(src, syn, j),
                              refs.get(j), coverage[j])
    part.contrib = item if part.contrib is None else _add_contributions(
        part.contrib, item)
  for (_, j, _), slot in sorted(counts.masks.items(), key=lambda kv: kv[0]):
    part = parts.setdefault(j, ColumnParts())
    item_m = mask_part(str(slot[2]), slot, _sizes(src, syn, j), refs.get(j))
    part.masks = item_m if part.masks is None else part.masks.merge(item_m)
  for (_, j), found in counts.literals.items():
    parts.setdefault(j, ColumnParts()).literals.update(found)
  for (_, j), rows in counts.substantive.items():
    parts.setdefault(j, ColumnParts()).substantive_syn = rows
  metrics, profiles = table_outputs(
      spec, parts, {
          _SOURCE: src,
          _SYNTHETIC: syn
      }, refs, label_key=label_key)
  return CensusResult(metrics, profiles, {
      j: p.acc for j, p in parts.items() if p.acc is not None
  })


# --------------------------------------------------------------------------
# Beam
# --------------------------------------------------------------------------
def _windowed(value: Any, windowed: bool) -> Any:
  return GlobalWindows.windowed_value(value) if windowed else value


def _outputs(counts: BatchCounts, windowed: bool) -> Iterator[Any]:
  for (table, j, code), slot in counts.values.items():
    yield _windowed(((table, j, _signed(code)), tuple(slot)), windowed)
  for (table, j, code), slot in counts.masks.items():
    yield beam.pvalue.TaggedOutput(
        _MASKS, _windowed(((table, j, _signed(code)), tuple(slot)), windowed))
  for literal_key, found in counts.literals.items():
    if found:
      yield beam.pvalue.TaggedOutput(
          _LITERALS, _windowed((literal_key, dict(found)), windowed))
  for column_key, rows in counts.substantive.items():
    yield beam.pvalue.TaggedOutput(_SUBSTANTIVE,
                                   _windowed((column_key, rows), windowed))


class CensusPreAggregateFn(beam.DoFn):
  """`EncodedBatch` → pre-aggregated census entries (module docstring).

  One `BatchCounts` per bundle, flushed in `finish_bundle`, and flushed
  early before any column whose keys could take it past `max_keys`
  (values + masks; a column of a batch adds at most two keys per row), so
  it never holds more than `max_keys` keys — or one column-batch's keys
  when that alone is larger — and no count is ever dropped (R67 M1).
  Outputs: main `((t, j, code), counts6)` and tagged `masks` `((t, j,
  mask code), (c_src, c_syn, mask))`, each code as the int64 with the
  same 64 bits (a Beam key then takes the coder's varint path); tagged
  `literals` and `substantive` `((t, j), rows)`."""

  def __init__(self,
               specs: Mapping[str, CensusSpec],
               *,
               max_keys: int = MAX_PREAGG_KEYS):
    super().__init__()
    self._specs = dict(specs)
    self._max_keys = max_keys
    self._counts = BatchCounts()

  @property
  def held(self) -> int:
    """How many keys the bundle dict holds now."""
    return len(self._counts)

  def start_bundle(self) -> None:
    self._counts = BatchCounts()

  def process(self, element: EncodedBatch) -> Iterator[Any]:
    spec = self._specs.get(element.table)
    if spec is None:
      raise ValueError(f"no census spec for table {element.table!r}")
    flags = _batch_flags(spec, element)
    if flags is None:
      return
    bound = 2 * element.n  # a column's value keys + mask keys, at most
    for col in spec.censused:
      if self._counts and len(self._counts) + bound > self._max_keys:
        counts, self._counts = self._counts, BatchCounts()
        yield from _outputs(counts, windowed=False)
      _accumulate_column(spec, col, element, flags, self._counts)

  def finish_bundle(self) -> Iterator[Any]:
    counts, self._counts = self._counts, BatchCounts()
    yield from _outputs(counts, windowed=True)


def _totals_entry(item: tuple[tuple[str, str], DenseProfile],
                  specs: Mapping[str, CensusSpec]) -> Iterator[Any]:
  (table, side), profile = item
  spec = specs.get(table)
  if spec is not None and side in (_SOURCE, _SYNTHETIC):
    yield (table, side), SideTotals.from_profile(spec, profile)


def _side_totals(
    table: str, spec: CensusSpec,
    totals: Mapping[tuple[str, str],
                    SideTotals]) -> tuple[SideTotals, SideTotals]:
  return (totals.get((table, _SOURCE)) or
          SideTotals.empty(spec), totals.get(
              (table, _SYNTHETIC)) or SideTotals.empty(spec))


def _by_column(
    item: tuple[tuple[str, int, int], Sequence[int]]
) -> tuple[tuple[str, int], tuple[int, Sequence[int]]]:
  (table, j, code), counts = item
  return (table, j), (_unsigned(code), counts)


def _coverage_entry(
    item: tuple[tuple[str, int], CensusAccumulator], specs: Mapping[str,
                                                                    CensusSpec],
    totals: Mapping[tuple[str, str], SideTotals]) -> tuple[Any, Any]:
  (table, j), acc = item
  spec = specs[table]
  src, syn = _side_totals(table, spec, totals)
  return (table, j), column_coverage(spec.columns[j], acc, _sizes(src, syn, j))


def _contribution_entry(
    item: tuple[tuple[str, int, int],
                Sequence[int]], specs: Mapping[str, CensusSpec],
    totals: Mapping[tuple[str, str], SideTotals], refs: Mapping[tuple[str, int],
                                                                ColumnRef],
    coverage: Mapping[tuple[str, int], Any]) -> tuple[Any, Any]:
  (table, j, code), counts = item
  spec = specs[table]
  src, syn = _side_totals(table, spec, totals)
  return (table, j), value_contribution(spec.columns[j], _unsigned(code),
                                        counts, _sizes(src, syn, j),
                                        refs.get((table, j)),
                                        coverage.get((table, j), (None, None)))


def _mask_entry(item: tuple[tuple[str, int, int],
                            Sequence[Any]], specs: Mapping[str, CensusSpec],
                totals: Mapping[tuple[str, str], SideTotals],
                refs: Mapping[tuple[str, int], ColumnRef]) -> tuple[Any, Any]:
  (table, j, _), counts = item
  spec = specs[table]
  src, syn = _side_totals(table, spec, totals)
  return (table, j), mask_part(counts[2], counts, _sizes(src, syn, j),
                               refs.get((table, j)))


def _tagged(item: tuple[tuple[str, int], Any], kind: str) -> tuple[str, Any]:
  (table, j), value = item
  return table, (kind, j, value)


def _emit_table(item: tuple[str, Iterable[tuple[str, int, Any]]],
                specs: Mapping[str, CensusSpec], label_key: bytes,
                totals: Mapping[tuple[str, str], SideTotals],
                refs: Mapping[tuple[str, int], ColumnRef]) -> Iterator[Any]:
  table, entries = item
  if not isinstance(label_key, bytes) or not label_key:
    raise ValueError("the label key side input must be non-empty bytes (R64)")
  spec = specs[table]
  parts: dict[int, ColumnParts] = {}
  for kind, j, value in entries:
    if kind == _SEED:
      continue
    part = parts.setdefault(j, ColumnParts())
    if kind == "census":
      part.acc = value
    elif kind == "contrib":
      part.contrib = value
    elif kind == _MASKS:
      part.masks = value
    elif kind == _SUBSTANTIVE:
      part.substantive_syn = int(value)
    else:
      part.literals = dict(value)
  src, syn = _side_totals(table, spec, totals)
  metrics, profiles = table_outputs(
      spec,
      parts, {
          _SOURCE: src,
          _SYNTHETIC: syn
      }, {
          col.j: refs[
              (table, col.j)] for col in spec.censused if (table, col.j) in refs
      },
      label_key=label_key)
  yield from metrics
  for profile in profiles:
    yield beam.pvalue.TaggedOutput(_PROFILES, profile)


class CensusMetrics(beam.PTransform):
  """`{"batches": PCollection[EncodedBatch], "accumulators": the dense
  pass's ((table, side), DenseProfile)}` → `{"metrics":
  PCollection[MetricValue], "profiles": PCollection[ProfileValue],
  "summaries": PCollection[((table, j), CensusAccumulator)]}` (module
  docstring).

  `label_key` is the one-element key PCollection `beam.label_key.LabelKey`
  makes on a worker; it keys every hashed label and is read as a side
  input, so the key never enters the job graph (Rulings R64, R68).
  `pools` maps table →
  column → `FreeTextPool` (the CLI's `read_pools`); None means the pool
  side input is absent. The panel's R/H views are built here, on the
  driver, and shipped as a side input — embedded in the job graph, or,
  given a `staging`, as one object the workers read (`beam.staged`,
  Ruling R154); only each table's slim `CensusSpec` is pickled into the
  DoFns.

  Raises:
    TypeError: `label_key` is not a PCollection (bytes here would be
      pickled into the graph).
  """

  def __init__(self,
               tables: Sequence[TablePlan],
               *,
               label_key: beam.PCollection,
               pools: Mapping[str, Mapping[str, FreeTextPool]] | None = None,
               staging: staged.Staging | None = None):
    super().__init__()
    self._staging = staging
    if not isinstance(label_key, beam.PCollection):
      raise TypeError("label_key must be the LabelKey PCollection, never "
                      "bytes: a constructor argument is pickled into the job "
                      "graph (Ruling R68)")
    self._label_key = label_key
    self._specs = {table.name: CensusSpec.from_table(table) for table in tables}
    self._refs = sorted(
        ((table.name, j), ref) for table in tables for j, ref in census_refs(
            table, pools=None if pools is
            None else pools.get(table.name, {})).items())

  def expand(self, input_or_inputs: Mapping[str, Any]) -> dict[str, Any]:
    batches = input_or_inputs["batches"]
    p = batches.pipeline
    specs = self._specs
    totals = beam.pvalue.AsDict(
        input_or_inputs["accumulators"]
        | "Totals" >> beam.FlatMap(_totals_entry, specs))
    refs = beam.pvalue.AsDict(
        staged.create(p, "Refs", self._refs, self._staging))
    counted = batches | "PreAggregate" >> beam.ParDo(
        CensusPreAggregateFn(specs)).with_outputs(
            _MASKS, _LITERALS, _SUBSTANTIVE, main=_VALUES)
    # no hot-key fanout: after pre-aggregation a value key arrives at most
    # once per bundle; the per-column combines keep combiner lifting
    values = counted[_VALUES] | "SumValues" >> beam.CombinePerKey(
        CountsCombineFn())
    summaries = (
        values
        | "ByColumn" >> beam.Map(_by_column)
        | "Census" >> beam.CombinePerKey(CensusCombineFn()))
    coverage = beam.pvalue.AsDict(
        summaries | "Coverage" >> beam.Map(_coverage_entry, specs, totals))
    contributions = (
        values
        | "Contribute" >> beam.Map(_contribution_entry, specs, totals, refs,
                                   coverage)
        | "SumContributions" >> beam.CombinePerKey(ContributionCombineFn()))
    masks = (
        counted[_MASKS]
        | "SumMasks" >> beam.CombinePerKey(MaskCountsCombineFn())
        | "MaskParts" >> beam.Map(_mask_entry, specs, totals, refs)
        | "MaskSummaries" >> beam.CombinePerKey(MaskSummaryCombineFn()))
    literals = counted[_LITERALS] | "Literals" >> beam.CombinePerKey(
        LiteralsCombineFn())
    substantive = counted[_SUBSTANTIVE] | "Substantive" >> beam.CombinePerKey(
        sum)
    seeds = p | "Seeds" >> beam.Create([(table, (_SEED, -1, None))
                                        for table in sorted(specs)])
    parts = ((
        summaries | "TagCensus" >> beam.Map(_tagged, "census"),
        contributions | "TagContrib" >> beam.Map(_tagged, "contrib"),
        masks | "TagMasks" >> beam.Map(_tagged, _MASKS),
        literals | "TagLiterals" >> beam.Map(_tagged, _LITERALS),
        substantive | "TagSubstantive" >> beam.Map(_tagged, _SUBSTANTIVE),
        seeds,
    )
             | "Parts" >> beam.Flatten()
             | "ByTable" >> beam.GroupByKey())
    emitted = parts | "Emit" >> beam.FlatMap(
        _emit_table, specs, beam.pvalue.AsSingleton(self._label_key), totals,
        refs).with_outputs(
            _PROFILES, main=_METRICS)
    return {
        _METRICS: emitted[_METRICS],
        _PROFILES: emitted[_PROFILES],
        "summaries": summaries,
    }
