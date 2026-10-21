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
"""Relational integrity and fan-out: for every foreign-key edge
`child(cols) → parent(ref_cols)`, on the source side and on the synthetic
side, the orphan rate and the children-per-parent distribution.

    EncodedBatch (every table, every side)
      │  ChildKeysFn: per live (edge, side), the child's fk_hash[:, e]
      │  (edge.cols, in order). A tuple with a NULL part is COUNTED
      │  (null_keys), never joined — SQL MATCH SIMPLE
      ▼
    (hash, count) per distinct key of a bundle ─► CombinePerKey(sum)
      │  = Count.PerKey of the child keys, pre-summed per bundle (flushed
      │  at FLUSH_CODES held keys, as membership.RowKeysFn does)
      │
      │   parent rows: read ONCE per (parent, side) through `Sources`,
      │   projected to the edges' referenced columns ─► ParentKeysFn:
      │   key_hashes(rows, edge.ref_cols) PER EDGE's ref_cols — never the
      │   parent's pk_hash (ref_cols may be another unique key, the PK in
      │   another order, or a widened pair list)
      ▼
    ┌─ side input (≤ SIDE_INPUT_MAX_KEYS parent rows): the parent keys as
    │  ONE sorted uint64 array (CombineGlobally) ─► AsSingleton; the child
    │  counts, batched, looked up with np.searchsorted
    └─ CoGroupByKey({child counts, parent (hash, rows)}) otherwise
      ▼
    FanoutAcc parts (vectorised numpy) + NULL / row counts of both sides
      │  CombineGlobally(FanoutCombineFn), one per (edge, side)
      ▼
    EdgeSummary: the capped histogram (bins 0 .. 49 and ≥ 50; a parent
      │  with no child is fan-out 0), exact mean / min / max, orphans
      ▼  GroupByKey(edge) with a seed per edge and every tagged failure
    edge_outputs ─► MetricValue per OWNED_METRIC_ID (table = the CHILD,
                    edge = edge.label(child), detail["enforced"])

The join switch. A CoGroupByKey shuffles both sides by key; a parent side
small enough to broadcast is cheaper as a side input, joined map-side:

    parent rows on that side (TablePlan.rows_read,      join
      sampling applied; rows ≥ distinct keys)
    ─────────────────────────────────────────────────   ──────────────────
    ≤ SIDE_INPUT_MAX_KEYS (10 M)                        side input: a
                                                        sorted uint64 set,
                                                        8 B a key (≤ 80 MB),
                                                        np.searchsorted
                                                        over the child
                                                        counts; the parent
                                                        keys are never
                                                        shuffled by key
    > 10 M, or unknown — a read-only (`external`)       CoGroupByKey
      parent is never counted at planning

Both paths feed the same FanoutAcc and give identical metrics (a test
pins it). A parent side above the bound would make every worker hold the
whole set, so it shuffles instead. Sizing the side-input cache to the
sets broadcast (8 B a parent key, from the planned rows) is Task 26's
job; here a runtime guard fails an edge (not_evaluated, with the reason)
whose parent set holds more than twice the rows its plan expects — the
plan, and the cache sized from it, no longer describe the data.

Counting. Every parent is a DISTINCT non-NULL referenced key tuple
(`parents`): a key held by several parent rows (a duplicate in a non-PK
`ref_cols`) is one parent, and `parent_rows - parents` is reported; a
parent row whose key has a NULL part is no parent (`parent_null_keys`),
not a childless one. A child tuple is `matched` when a parent holds its
key, an `orphan`
otherwise, a `null_key` when any part is NULL. The fan-out of a parent is
its matched children (0 when none): the histogram, TVD, W1, the
childless share, adherence and coverage read those. The mean ratio does
not (Ruling R82): it is the catalogue formula over ROWS,

    FMR = (n_child / n_parent)_syn / (n_child / n_parent)_src
    n_child   every child row read — matched, orphaned, NULL-keyed
    n_parent  every parent row read — a NULL-part key included

so a source with 100 of its 400 orders NULL-keyed (guest checkouts)
against a synthetic side with none scores 1.0, and extra NULL-keyed or
orphaned synthetic child rows move it (matched-only means would read
4/3 and 1.0 there). A parent row sample at rate r scales a side's rows
per parent by 1/r; the ratio is evaluated only when both parent sides
share one rate (they cancel), `not_evaluated` otherwise.

    metric                         reads                    noise (R41)
    ─────────────────────────────  ───────────────────────  ─────────────
    relationship.orphan_rate       synthetic child, parent  —
    relationship.orphan_rate_      source child, parent     —
      source
    relationship.fanout_tvd        both sides' histograms   tvd_null
    relationship.fanout_w1         (stats.relational.       —
    relationship.fanout_mean_ratio  fanout_metrics)         —
    relationship.zero_child_                                folded
      share_delta                                           Newcombe CI
    relationship.cardinality_                               Wilson CI
      adherence
    relationship.parent_coverage                            —

Documented edges (`enforced: false`, Ruling R42): every row is emitted
exactly as on an enforced edge, with `detail["enforced"]` for the
pipeline to pass to `scoring.to_metric_row(enforced=…)` — the scorer
reports only the orphan rate as INFO; the fan-out metrics stay graded.
The source's own orphan rate is the synthetic row's `source_value`.

Fewer than k parents (Ruling R83, k = RARE_COUNT = 10, the R80.3 rule
for relations): when either side holds fewer than k distinct parent keys,
every fan-out metric is `not_evaluated` with "fewer than k parents on a
side (k = 10)" and publishes no mean, no `children_*`/`parents_*` detail:
a fan-out over a handful of parents is theirs (one parent's IS its
count). The orphan rows stay evaluated — an integrity verdict must hold
on every edge — without their parent counts; their rate and n still
give the matched total, which below k bounds those few parents' summed
fan-out (accepted for the integrity gate).

Extremes (R65, R71): an exact min or max fan-out is one parent's count,
so no detail publishes one (nor an overflow mean, which can be a single
parent's); `cardinality_adherence` uses them internally only. Its
adherent count (adherence * n_synthetic) can still pin max_src when
fan-outs are dense integers — accepted and documented, as R65 accepts
the same channel for `field.range_adherence`.

A missing source twin (Ruling R84): a read-only parent whose
`<source dataset>.<name>` does not exist, or a launch parent with no
source table, leaves only the metrics that need the source side
`not_evaluated` (the source orphan rate and the six fan-out metrics);
the synthetic orphan rate is still measured against the parent's
landing table.

Key types (Ruling R113): key tuples are compared by the hash of their
canonical values (`encode.key_hash`), and equal values of two type
families have different canonical forms — an INT64 5, a NUMERIC 5 and a
FLOAT64 5 never hash alike. An edge whose child column and referenced
column are of different families (`_KEY_FAMILIES`) would read every
child as an orphan, on both sides, so every metric of that edge is
`not_evaluated` with a reason naming both columns and their types.

Sampled mode (Ruling R72, as in `beam.membership`): a side whose plan
rate is below 1 was read as a row sample. A metric that needs every row
of a side it reads is `not_evaluated` with "sampled mode cannot measure
…; run exact mode" and the observed lower bounds in `detail`, each named
`*_lower_bound`:

    metric                       needs in full           why
    ───────────────────────────  ──────────────────────  ─────────────────
    orphan_rate(_source)         that side's child and   an orphan outside
                                 parent                  the sample goes
                                                         unseen; a child of
                                                         an unsampled parent
                                                         looks orphaned
    fanout_tvd, _w1, _mean_      both child sides        a thinned child
      ratio, zero_child_share_                           side lowers every
      delta, parent_coverage                             parent's fan-out
    fanout_mean_ratio, also      equal parent rates on   a parent sample
                                 both sides              scales rows per
                                                         parent by 1/r
    cardinality_adherence        both child sides and    a sample's extremes
                                 the source parent       fall inside the
                                                         source's range

A parent row sample alone leaves each sampled parent's fan-out exact, so
the fan-out shape stays evaluated there, with `method` = sample and the
parent rate as `sample_rate`.

Failures stay per edge (as in `beam.membership`): an edge whose spec
cannot be built or whose parent cannot be read on the driver
(`TABLE_ERRORS`), or whose keys raise a data error on a worker
(`WORKER_ERRORS`), writes `not_evaluated` rows with the reason for every
owned id; the other edges and the pipeline carry on. A worker's
`MemoryError` is not a data error: it is raised, so the bundle fails and
the runner retries it (Ruling R113). Nothing is dropped silently: an
unreadable side, an unknown parent and a skipped child table are all
`not_evaluated` rows with a reason.

References (author-year, R22): ISO/IEC 9075 (1992), SQL's referential
constraint MATCH SIMPLE; Patki, Wedge & Veeramachaneni (2016) for
parent-child cardinality; Newcombe (1998); Wilson (1927); Ramdas, Garcia
Trillos & Cuturi (2017) for the fan-out W1.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import apache_beam as beam
import numpy as np
from apache_beam.transforms.window import GlobalWindows

from sdfb_evaluation.beam.encode import (
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    EncodedBatch,
    key_hashes,
)
from sdfb_evaluation.beam.dense import RARE_COUNT
from sdfb_evaluation.beam.membership import TABLE_ERRORS, WORKER_ERRORS
from sdfb_evaluation.canonical import NULL_CODE
from sdfb_evaluation.context.plan import parent_landing
from sdfb_evaluation.stats import noise
from sdfb_evaluation.stats.relational import fanout_metrics, orphan_summary
from sdfb_evaluation.types import Method, MetricValue, Side

if TYPE_CHECKING:
  from sdfb_evaluation.beam.io import Sources
  from sdfb_evaluation.context.plan import TablePlan
  from sdfb_evaluation.context.relationships import Edge

__all__ = [
    "COGROUP",
    "FANOUT_CAP",
    "FLUSH_CODES",
    "OWNED_METRIC_IDS",
    "SIDE_INPUT",
    "SIDE_INPUT_MAX_KEYS",
    "TABLE_ERRORS",
    "WORKER_ERRORS",
    "EdgeSpec",
    "EdgeSummary",
    "FanoutAcc",
    "FanoutCombineFn",
    "Relational",
    "SidePlan",
    "child_keys",
    "edge_outputs",
    "fanout_part",
    "plan_edges",
    "summarize",
]

_ORPHAN = "relationship.orphan_rate"
_ORPHAN_SOURCE = "relationship.orphan_rate_source"
_TVD = "relationship.fanout_tvd"
_W1 = "relationship.fanout_w1"
_MEAN_RATIO = "relationship.fanout_mean_ratio"
_ZERO = "relationship.zero_child_share_delta"
_ADHERENCE = "relationship.cardinality_adherence"
_COVERAGE = "relationship.parent_coverage"
OWNED_METRIC_IDS: tuple[str, ...] = (_ORPHAN, _ORPHAN_SOURCE, _TVD, _W1,
                                     _MEAN_RATIO, _ZERO, _ADHERENCE, _COVERAGE)
_FANOUT_IDS = OWNED_METRIC_IDS[2:]

FANOUT_CAP = 50  # the catalogue's bins: one per count 0 .. 49, one for >= 50
SIDE_INPUT_MAX_KEYS = 10_000_000  # parent rows of a side-input join
SIDE_INPUT, COGROUP = "side_input", "cogroup"
_SIDES = (Side.SOURCE, Side.SYNTHETIC)
_CHILD, _PARENT = "child", "parent"
_BOTH_CHILDREN = ((Side.SOURCE, _CHILD), (Side.SYNTHETIC, _CHILD))
# What a metric must read in full (module docstring, sampled mode).
_NEEDS: dict[str, tuple[tuple[Side, str], ...]] = {
    _ORPHAN: ((Side.SYNTHETIC, _CHILD), (Side.SYNTHETIC, _PARENT)),
    _ORPHAN_SOURCE: ((Side.SOURCE, _CHILD), (Side.SOURCE, _PARENT)),
    _TVD: _BOTH_CHILDREN,
    _W1: _BOTH_CHILDREN,
    _MEAN_RATIO: _BOTH_CHILDREN,
    _ZERO: _BOTH_CHILDREN,
    _COVERAGE: _BOTH_CHILDREN,
    _ADHERENCE: (*_BOTH_CHILDREN, (Side.SOURCE, _PARENT)),
}
_COMPACT_CODES = 1 << 20  # pending set codes before a combine compacts
FLUSH_CODES = 1 << 18  # distinct child keys a bundle holds before it flushes
_REASON_CHARS = 300
_KEYS, _STATS, _PARTS, _FAILED = "keys", "stats", "parts", "failed"
_SUMMARY, _SEED = "summary", "seed"
_NULL = np.uint64(NULL_CODE)
_EMPTY_CODES = np.zeros(0, dtype=np.uint64)
_MATCH_SIMPLE = ("SQL MATCH SIMPLE: a key tuple with a NULL part is counted "
                 "in null_keys, never joined and never an orphan")
_MATCHED_ONLY = ("the histogram counts matched non-null key tuples: orphans "
                 "and NULL keys are the orphan rate's, not a parent's children")
_ROWS_ONLY = ("every child row read (matched, orphaned, NULL-keyed) over "
              "every parent row read, per side (the catalogue formula, R82)")
_EXTREMES_NOTE = ("the source's exact [min, max] fan-out (each one parent's "
                  "count) bounds the count internally and is never published "
                  "as a value; the adherent count can still pin max_src when "
                  "fan-outs are dense integers — accepted, as R65 accepts it "
                  "for field.range_adherence")
_BELOW_K = f"fewer than k parents on a side (k = {RARE_COUNT})"
# The canonical form a key part is hashed by (`canonical.canonical_value`;
# `encode.key_hash` hashes canonical values): equal values of two types
# hash alike only inside one family — an INT64 5 is the int 5, a NUMERIC
# 5 the text "5", a FLOAT64 5 the float 5.0. A type not listed is its own
# family (STRING, BYTES, DATE, TIME).
_KEY_FAMILIES: Mapping[str, str] = {
    **dict.fromkeys(
        ("INT64", "INTEGER", "INT", "SMALLINT", "BIGINT", "TINYINT", "BYTEINT"),
        "integer"),
    **dict.fromkeys(("NUMERIC", "BIGNUMERIC", "DECIMAL", "BIGDECIMAL"),
                    "decimal"),
    **dict.fromkeys(("FLOAT64", "FLOAT"), "float"),
    **dict.fromkeys(("BOOL", "BOOLEAN"), "boolean"),
    # a naive DATETIME is read as UTC, so both are one UTC instant's text
    **dict.fromkeys(("TIMESTAMP", "DATETIME"), "instant"),
}


def _failure(exc: BaseException) -> str:
  """A failed edge's reason: the error class and its message (bounded;
  numpy and encoder messages name columns and types, never values)."""
  text = ("relational metrics could not be computed for this edge "
          f"({type(exc).__name__}: {exc})")
  return text[:_REASON_CHARS]


def _rate(rate: float | None) -> float:
  return 1.0 if rate is None or rate >= 1.0 else float(rate)


def _signed(codes: np.ndarray) -> list[int]:
  """uint64 codes as the int64s with the same 64 bits: a Beam key then
  takes the coder's varint path instead of its pickle fallback."""
  signed: list[int] = codes.astype(np.uint64).view(np.int64).tolist()
  return signed


def _member(sorted_codes: np.ndarray, codes: np.ndarray) -> np.ndarray:
  """`codes[i] in sorted_codes`, vectorised (`sorted_codes` sorted)."""
  if sorted_codes.size == 0 or codes.size == 0:
    return np.zeros(len(codes), dtype=bool)
  pos = np.minimum(np.searchsorted(sorted_codes, codes), len(sorted_codes) - 1)
  found: np.ndarray = sorted_codes[pos] == codes
  return found


# --------------------------------------------------------------------------
# the per-edge spec (driver-built, pickled: slim)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SidePlan:
  """One side (source or synthetic) of one edge. `reason` says why the
  side cannot be evaluated (None: it can); `parent` is the parent's
  landing table (the key of its one read); the rates are the plan's
  row-sample rates (1.0 when read in full); `path` is SIDE_INPUT or
  COGROUP, `path_note` why; `planned_keys` the parent rows the plan
  expects that side to read (the side-input set's size bound; None when
  the parent was not counted)."""
  reason: str | None = None
  parent: str | None = None
  child_rate: float = 1.0
  parent_rate: float = 1.0
  path: str = COGROUP
  path_note: str = ""
  planned_keys: float | None = None

  def rate(self, role: str) -> float:
    return self.child_rate if role == _CHILD else self.parent_rate


@dataclass(frozen=True)
class EdgeSpec:  # pylint: disable=too-many-instance-attributes  # one field per fact the passes read
  """What the relational pass needs of one edge. `key` identifies it in
  the pipeline, `index` is its column in the child's `fk_hash`, `reason`
  (when set) makes the whole edge `not_evaluated`."""
  key: int
  child: str
  index: int
  label: str
  ref_cols: tuple[str, ...]
  enforced: bool
  digest: str
  parent_role: str | None = None
  source: SidePlan = SidePlan()
  synthetic: SidePlan = SidePlan()
  reason: str | None = None

  def side(self, side: Side | str) -> SidePlan:
    return self.source if Side(side) is Side.SOURCE else self.synthetic


def _side_plan(side: Side, child: TablePlan, parent: TablePlan, edge: Edge,
               max_keys: int) -> SidePlan:
  """One side of an edge whose parent is planned (module docstring)."""
  source = side is Side.SOURCE
  child_rate = _rate(
      child.sample_rate_source if source else child.sample_rate_synthetic)
  parent_rate = _rate(
      parent.sample_rate_source if source else parent.sample_rate_synthetic)
  rows = parent.rows_source if source else parent.rows_synthetic
  read: float | None = None
  if rows is None:
    path, note = COGROUP, ("parent row count unknown (a read-only parent is "
                           "not counted at planning): CoGroupByKey")
  else:
    read = parent.rows_read[0 if source else 1]
    within = read <= max_keys
    path = SIDE_INPUT if within else COGROUP
    # Hoisted: a quoted literal inside the f-string would nest the quote
    # the py3.14 pylint gate reads as inconsistent (W1405).
    bound, join = (("<=", "a sorted key set as a side input") if within else
                   (">", "CoGroupByKey"))
    note = f"{read:.0f} parent rows {bound} {max_keys}: {join}"
  plan = SidePlan(
      parent=parent.landing_table,
      child_rate=child_rate,
      parent_rate=parent_rate,
      path=path,
      path_note=note,
      planned_keys=read)
  if parent.role != "external" and parent.skip_reason is not None:
    why = f"the parent table is not evaluated: {parent.skip_reason}"
  elif source and not parent.source_read_table and parent.role == "external":
    why = (f"no source-side parent: the read-only parent {parent.name} has no "
           f"source-dataset twin (<source dataset>.{parent.name} was not "
           "found)")
  elif source and not parent.source_read_table:
    why = (f"no source-side parent: the parent table {parent.landing_table} "
           "has no source table to read")
  elif not source and not (parent.synthetic_read_table or
                           parent.scope.read_table):
    unread = parent.scope.reason or "no read table was planned"
    why = f"the parent {parent.landing_table} cannot be read: {unread}"
  else:
    planned = {c.name for c in parent.columns}
    missing = [c for c in edge.ref_cols if c not in planned]
    if not missing:
      return plan
    why = (f"referenced column(s) {missing} are not among the parent's "
           "planned columns")
  return dataclasses.replace(plan, reason=why)


def _key_family(bq_type: str) -> str:
  name = bq_type.upper()
  return _KEY_FAMILIES.get(name, name)


def _type_mismatch(child: TablePlan, parent: TablePlan,
                   edge: Edge) -> str | None:
  """Why this edge's key tuples can never match (Ruling R113), or None: a
  child column and the column it references are of different type
  families, so equal values hash apart and every child would read as an
  orphan (the fan-out metrics then pass on nothing). The first such pair
  is named with both types. A column the plan does not hold is left to
  the side plans, which say so."""
  child_types = {c.name: c.bq_type for c in child.columns}
  parent_types = {c.name: c.bq_type for c in parent.columns}
  for col, ref_col in zip(edge.cols, edge.ref_cols, strict=True):
    a, b = child_types.get(col), parent_types.get(ref_col)
    if a is None or b is None or _key_family(a) == _key_family(b):
      continue
    return (f"the key columns differ in type: {child.name}.{col} is "
            f"{a.upper()} and {parent.name}.{ref_col} is {b.upper()}. Key "
            f"tuples are compared by canonical value, and a {_key_family(a)} "
            f"never equals a {_key_family(b)}, so every child would read as "
            "an orphan: the edge is not evaluated (cast one side to the "
            "other's type, or correct the relationship model)")
  return None


def _edge_spec(key: int, child: TablePlan, index: int, edge: Edge,
               tables: Sequence[TablePlan],
               max_keys: int) -> tuple[EdgeSpec, TablePlan | None]:
  """The spec of edge `index` of `child`, and its parent's plan (None when
  the edge cannot be evaluated at all)."""
  spec = EdgeSpec(
      key=key,
      child=child.name,
      index=index,
      label=edge.label(child.name),
      ref_cols=tuple(edge.ref_cols),
      enforced=edge.enforced,
      digest=child.encoding_plan_digest)
  if not child.evaluated:
    why = child.skip_reason or "it is a read-only table"
    return dataclasses.replace(
        spec, reason=f"the child table is not evaluated: {why}"), None
  if len(edge.cols) != len(edge.ref_cols):
    raise ValueError(f"{spec.label}: {len(edge.cols)} child column(s) against "
                     f"{len(edge.ref_cols)} referenced column(s)")
  launch = {t.name: t for t in tables if t.role != "external"}
  parent = launch.get(edge.ref) if not edge.external else None
  if parent is None:
    landing = parent_landing(child.landing_table, edge)
    parent = next((t for t in tables
                   if t.role == "external" and t.landing_table == landing),
                  None)
    if parent is None:
      return dataclasses.replace(
          spec,
          reason=(f"the parent {landing} is not in the evaluation plan (not a "
                  "launch table, not a read-only parent)")), None
  mismatch = _type_mismatch(child, parent, edge)
  if mismatch is not None:
    return dataclasses.replace(spec, reason=mismatch), None
  return dataclasses.replace(
      spec,
      parent_role="external" if parent.role == "external" else "launch",
      source=_side_plan(Side.SOURCE, child, parent, edge, max_keys),
      synthetic=_side_plan(Side.SYNTHETIC, child, parent, edge,
                           max_keys)), parent


def plan_edges(
    tables: Sequence[TablePlan],
    *,
    side_input_max_keys: int = SIDE_INPUT_MAX_KEYS
) -> list[tuple[EdgeSpec, TablePlan | None]]:
  """Every foreign-key edge of every launch table as this pass plans it:
  the edge's spec — the parent each side resolves to, the join each side
  takes (`SidePlan.path`) and why a side or the whole edge is not
  evaluated — and the parent's plan (None when the edge cannot be
  evaluated at all). `Relational` builds its graph from this list and the
  pipeline sizes the side-input cache from it, so the two cannot disagree
  on which parent key sets are broadcast. An edge whose spec cannot be
  built (`TABLE_ERRORS`) is planned not evaluated, with the reason."""
  planned: list[tuple[EdgeSpec, TablePlan | None]] = []
  for child in tables:
    if child.role == "external":
      continue
    for index, edge in enumerate(child.edges):
      key = len(planned)
      try:
        planned.append(
            _edge_spec(key, child, index, edge, tables, side_input_max_keys))
      except TABLE_ERRORS as exc:
        planned.append((EdgeSpec(
            key=key,
            child=child.name,
            index=index,
            label=edge.label(child.name),
            ref_cols=tuple(edge.ref_cols),
            enforced=edge.enforced,
            digest=child.encoding_plan_digest,
            reason=_failure(exc)), None))
  return planned


# --------------------------------------------------------------------------
# the additive counts
# --------------------------------------------------------------------------
def _low(a: int | None, b: int | None) -> int | None:
  return b if a is None else a if b is None else min(a, b)


def _high(a: int | None, b: int | None) -> int | None:
  return b if a is None else a if b is None else max(a, b)


@dataclass
class FanoutAcc:  # pylint: disable=too-many-instance-attributes  # one field per additive count
  """One (edge, side)'s additive counts. `parents` are distinct non-NULL
  parent keys, `parent_rows`/`parent_nulls` the parent rows with a
  non-NULL / NULL-part key; `matched_keys` the parents with at least one
  child, `children` their children, `hist` those parents per fan-out
  (capped; bin 0 stays empty until `summarize`), `min_`/`max_matched`
  their exact extremes, `overflow_children` the children of the parents
  at or past the cap; `orphans`/`orphan_keys` the child tuples / distinct
  keys no parent holds; `child_nulls` the child tuples with a NULL part."""
  parents: int = 0
  parent_rows: int = 0
  parent_nulls: int = 0
  matched_keys: int = 0
  children: int = 0
  hist: np.ndarray | None = None
  min_matched: int | None = None
  max_matched: int | None = None
  overflow_children: int = 0
  orphans: int = 0
  orphan_keys: int = 0
  child_nulls: int = 0

  def merge(self, other: FanoutAcc) -> FanoutAcc:
    if self.hist is None:
      hist = other.hist
    elif other.hist is None:
      hist = self.hist
    else:
      hist = self.hist + other.hist
    return FanoutAcc(
        parents=self.parents + other.parents,
        parent_rows=self.parent_rows + other.parent_rows,
        parent_nulls=self.parent_nulls + other.parent_nulls,
        matched_keys=self.matched_keys + other.matched_keys,
        children=self.children + other.children,
        hist=hist,
        min_matched=_low(self.min_matched, other.min_matched),
        max_matched=_high(self.max_matched, other.max_matched),
        overflow_children=self.overflow_children + other.overflow_children,
        orphans=self.orphans + other.orphans,
        orphan_keys=self.orphan_keys + other.orphan_keys,
        child_nulls=self.child_nulls + other.child_nulls)


def fanout_part(counts: np.ndarray,
                matched: np.ndarray,
                cap: int = FANOUT_CAP) -> FanoutAcc:
  """The counts of distinct child keys: `counts[i]` child tuples hold key
  i, and `matched[i]` says whether a parent holds it (a matched key is one
  parent's fan-out; an unmatched one is an orphan key). A key with no
  child (count 0) adds nothing here: the parents are counted apart."""
  counts = np.asarray(counts, dtype=np.int64)
  matched = np.asarray(matched, dtype=bool)
  held = counts > 0
  fan = counts[matched & held]
  lost = ~matched & held
  hist = np.bincount(np.minimum(fan, cap), minlength=cap + 1)[:cap + 1]
  return FanoutAcc(
      matched_keys=int(fan.size),
      children=int(fan.sum()),
      hist=hist.astype(np.int64),
      min_matched=int(fan.min()) if fan.size else None,
      max_matched=int(fan.max()) if fan.size else None,
      overflow_children=int(fan[fan >= cap].sum()),
      orphans=int(counts[lost].sum()),
      orphan_keys=int(lost.sum()))


class FanoutCombineFn(beam.CombineFn):
  """Merges one (edge, side)'s `FanoutAcc` parts (sums, min and max)."""

  def create_accumulator(self) -> FanoutAcc:
    return FanoutAcc()

  def add_input(self, mutable_accumulator: FanoutAcc,
                element: FanoutAcc) -> FanoutAcc:
    return mutable_accumulator.merge(element)

  def merge_accumulators(self, accumulators: Iterable[FanoutAcc]) -> FanoutAcc:
    return functools.reduce(FanoutAcc.merge, accumulators, FanoutAcc())

  def extract_output(self, accumulator: FanoutAcc) -> FanoutAcc:
    return accumulator


@dataclass(frozen=True, eq=False)
class EdgeSummary:  # pylint: disable=too-many-instance-attributes  # one field per finished count
  """One (edge, side), finished: `hist` counts EVERY parent by fan-out
  (bin 0 the childless ones, bin `cap` the rest past the cap); the exact
  `min_fanout`/`max_fanout` span every parent; `overflow_mean` is the mean
  fan-out of the parents in the cap bin (None when it is empty)."""
  hist: np.ndarray
  parents: int
  parent_rows: int
  parent_nulls: int
  with_children: int
  children: int
  min_fanout: int
  max_fanout: int
  overflow_mean: float | None
  orphans: int
  orphan_keys: int
  nulls: int

  @property
  def nonnull(self) -> int:
    """Child tuples with every part non-NULL (matched + orphans)."""
    return self.children + self.orphans

  @property
  def child_rows(self) -> int:
    """Every child row read: matched, orphaned and NULL-keyed."""
    return self.nonnull + self.nulls

  @property
  def all_parent_rows(self) -> int:
    """Every parent row read, one with a NULL-part key included."""
    return self.parent_rows + self.parent_nulls

  @property
  def row_mean(self) -> float:
    """The catalogue's n_child / n_parent (R82): child rows over parent
    rows (0 when no parent row was read)."""
    rows = self.all_parent_rows
    return self.child_rows / rows if rows else 0.0


def summarize(acc: FanoutAcc, cap: int = FANOUT_CAP) -> EdgeSummary:
  """`acc` finished: its childless parents (`parents - matched_keys`) go
  to bin 0, fan-out 0.

  Raises:
    ValueError: more parents with children than parents (the parts of
      two different edges or sides were merged).
  """
  hist = (
      np.zeros(cap + 1, dtype=np.int64)
      if acc.hist is None else acc.hist.astype(np.int64).copy())
  childless = acc.parents - acc.matched_keys
  if hist.shape != (cap + 1,) or childless < 0 or hist[0] != 0:
    raise ValueError(f"{acc.matched_keys} parents with children against "
                     f"{acc.parents} parents: inconsistent fan-out parts")
  hist[0] = childless
  overflow = int(hist[cap])
  low = 0 if childless > 0 or acc.min_matched is None else acc.min_matched
  return EdgeSummary(
      hist=hist,
      parents=acc.parents,
      parent_rows=acc.parent_rows,
      parent_nulls=acc.parent_nulls,
      with_children=acc.matched_keys,
      children=acc.children,
      min_fanout=low,
      max_fanout=acc.max_matched or 0,
      overflow_mean=acc.overflow_children / overflow if overflow else None,
      orphans=acc.orphans,
      orphan_keys=acc.orphan_keys,
      nulls=acc.child_nulls)


def child_keys(batch: EncodedBatch,
               index: int) -> tuple[np.ndarray, np.ndarray, int]:
  """One child batch's key hashes on edge `index` (its `fk_hash` column,
  built from `edge.cols` in order): (distinct non-NULL hashes, how many
  tuples hold each, how many tuples have a NULL part)."""
  fk = batch.fk_hash[:, index]
  null = fk == _NULL
  codes, counts = np.unique(fk[~null], return_counts=True)
  return codes, counts, int(null.sum())


def _union(parts: Sequence[np.ndarray]) -> np.ndarray:
  union: np.ndarray = np.unique(np.concatenate([_EMPTY_CODES, *parts]))
  return union.astype(np.uint64)


class _KeySetCombineFn(beam.CombineFn):
  """The sorted distinct union of uint64 key arrays; compacts lazily."""

  def create_accumulator(self) -> list[np.ndarray]:
    return []

  def add_input(self, mutable_accumulator: list[np.ndarray],
                element: np.ndarray) -> list[np.ndarray]:
    mutable_accumulator.append(element)
    if sum(len(a) for a in mutable_accumulator) > _COMPACT_CODES:
      return [_union(mutable_accumulator)]
    return mutable_accumulator

  def merge_accumulators(
      self, accumulators: Iterable[list[np.ndarray]]) -> list[np.ndarray]:
    merged = [part for acc in accumulators for part in acc]
    return [_union(merged)] if len(merged) > 1 else merged

  def compact(self, accumulator: list[np.ndarray]) -> list[np.ndarray]:
    return [_union(accumulator)] if len(accumulator) > 1 else accumulator

  def extract_output(self, accumulator: list[np.ndarray]) -> np.ndarray:
    return _union(accumulator)


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------
class _Emitter:
  """Collects one edge's `MetricValue`s."""

  def __init__(self, spec: EdgeSpec):
    self.spec = spec
    self.rows: list[MetricValue] = []

  def value(self, metric_id: str, value: float, *, detail: Mapping[str, Any],
            **fields_: Any) -> None:
    fields_.setdefault("method", Method.EXACT)
    self.rows.append(
        MetricValue(
            metric_id=metric_id,
            table=self.spec.child,
            value=value,
            edge=self.spec.label,
            encoding_plan_digest=self.spec.digest,
            detail={
                "enforced": self.spec.enforced,
                **detail
            },
            **fields_))

  def skip(self, metric_id: str, reason: str, **detail: Any) -> None:
    self.rows.append(_skipped(self.spec, metric_id, reason, detail))


def _skipped(spec: EdgeSpec, metric_id: str, reason: str,
             detail: Mapping[str, Any]) -> MetricValue:
  return MetricValue(
      metric_id=metric_id,
      table=spec.child,
      value=None,
      edge=spec.label,
      method=Method.EXACT,
      encoding_plan_digest=spec.digest,
      detail={
          "reason": reason,
          "enforced": spec.enforced,
          **detail
      })


def _failed_metrics(spec: EdgeSpec, reason: str) -> list[MetricValue]:
  """Every owned id `not_evaluated` with a failed or skipped edge's
  reason."""
  return [_skipped(spec, i, reason, {}) for i in OWNED_METRIC_IDS]


def _sampled_reason(metric_id: str, side: Side, role: str, rate: float) -> str:
  if metric_id in (_ORPHAN, _ORPHAN_SOURCE):
    what = "orphans"
    why = ("an orphan outside the sample goes unseen" if role == _CHILD else
           "a child whose parent was not sampled looks orphaned")
  elif role == _CHILD:
    what = "fan-out"
    why = ("every parent loses its children outside the sample (fan-out is "
           "biased low)")
  else:
    what = "the source's fan-out range"
    why = "a sample's extremes fall inside the full source's"
  return (f"sampled mode cannot measure {what}: the {side} {role} side is a "
          f"{rate:.3g} row sample, so {why}; run exact mode")


def _sampled(spec: EdgeSpec, metric_id: str) -> str | None:
  """Why `metric_id` cannot be measured on this edge's row samples, or
  None when every side it needs in full was read in full."""
  for side, role in _NEEDS[metric_id]:
    rate = spec.side(side).rate(role)
    if rate < 1.0:
      return _sampled_reason(metric_id, side, role, rate)
  return None


def _sample_rates(spec: EdgeSpec) -> dict[str, float]:
  """The row-sample rates (< 1) of this edge's reads, by side and role."""
  return {
      f"{side}_{role}": spec.side(side).rate(role) for side in _SIDES
      for role in (_CHILD, _PARENT)
      if spec.side(side).rate(role) < 1.0
  }


def _orphan_metric(e: _Emitter, side: Side, summary: EdgeSummary | None,
                   source_rate: float | None) -> float | None:
  """`relationship.orphan_rate` (synthetic) or `_source`; the rate, or None
  when not evaluated."""
  spec = e.spec
  synthetic = side is Side.SYNTHETIC
  metric_id = _ORPHAN if synthetic else _ORPHAN_SOURCE
  plan = spec.side(side)
  if plan.reason is not None or summary is None:
    e.skip(metric_id, plan.reason or f"the {side} side was not computed")
    return None
  few = summary.parents < RARE_COUNT  # R83: no parent counts below k
  sampled = _sampled(spec, metric_id)
  if sampled is not None:
    bounds: dict[str, Any] = {"null_keys_lower_bound": summary.nulls}
    if not few:
      bounds["matched_lower_bound"] = summary.children
    if plan.parent_rate >= 1.0:  # every observed orphan is a real one
      bounds["orphans_lower_bound"] = summary.orphans
    e.skip(metric_id, sampled, **bounds, sample_rates=_sample_rates(spec))
    return None
  result = orphan_summary(summary.nonnull, summary.orphans, summary.nulls)
  detail: dict[str, Any] = {
      "orphans": summary.orphans,
      "orphan_keys": summary.orphan_keys,
      "nonnull_keys": summary.nonnull,
      "null_keys": summary.nulls,
      "parent_role": spec.parent_role,
      "semantics": _MATCH_SIMPLE,
      "path": plan.path,
      "path_note": plan.path_note,
  }
  if few:
    detail["parent_counts"] = _BELOW_K + ": not published"
  else:
    detail.update(
        parents=summary.parents,
        parent_rows=summary.parent_rows,
        parent_duplicate_rows=summary.parent_rows - summary.parents,
        parent_null_keys=summary.parent_nulls)
  rate = result["rate"]
  if rate is None:
    e.skip(
        metric_id, f"no {side} child key tuple without a NULL part was read "
        f"({summary.nulls} with one)", **detail)
    return None
  sizes = {"n_synthetic" if synthetic else "n_source": summary.nonnull}
  e.value(
      metric_id,
      rate,
      detail=detail,
      source_value=source_rate if synthetic else rate,
      synthetic_value=rate if synthetic else None,
      **sizes)
  return float(rate)


def _unequal_parent_samples(spec: EdgeSpec) -> str | None:
  """Why the mean fan-out ratio cannot be measured on these parent row
  samples (R82), or None: a parent sample at rate r scales that side's
  rows per parent by 1/r, which cancels only at equal rates."""
  r_src, r_syn = spec.source.parent_rate, spec.synthetic.parent_rate
  if r_src == r_syn:
    return None
  return ("sampled mode cannot measure the mean fan-out ratio: the parent "
          f"sides are row samples at different rates (source {r_src:.3g}, "
          f"synthetic {r_syn:.3g}), and a parent sample at rate r scales "
          "that side's rows per parent by 1/r, which cancels only at equal "
          "rates; run exact mode")


def _fanout_values(e: _Emitter, src: EdgeSummary, syn: EdgeSummary) -> None:
  """The six fan-out rows of an edge both of whose sides were computed."""
  spec = e.spec
  few = [(side, summary.parents)
         for side, summary in ((Side.SOURCE, src), (Side.SYNTHETIC, syn))
         if summary.parents < RARE_COUNT]
  if few:  # R83: the fan-out of fewer than k parents stays theirs
    side, parents = few[0]
    reason = _BELOW_K if parents else f"{_BELOW_K}; the {side} side has none"
    for metric_id in _FANOUT_IDS:
      e.skip(metric_id, reason)
    return
  fm = fanout_metrics(
      src.hist.tolist(),
      syn.hist.tolist(),
      src.row_mean,
      syn.row_mean,
      src.min_fanout,
      src.max_fanout,
      cap=FANOUT_CAP,
      mean_overflow_src=src.overflow_mean,
      mean_overflow_syn=syn.overflow_mean)
  # Type narrowing only: None means a side with no parent, excluded above.
  assert fm is not None, "unreachable: both sides hold at least k parents"
  detail: dict[str, Any] = {
      "cap": FANOUT_CAP,
      "parents_source": src.parents,
      "parents_synthetic": syn.parents,
      "children_source": src.children,
      "children_synthetic": syn.children,
      "children_counted": _MATCHED_ONLY,
      "paths": {
          str(side): spec.side(side).path for side in _SIDES
      },
  }
  fields_: dict[str, Any] = {
      "n_source": src.parents,
      "n_synthetic": syn.parents
  }
  parent_rates = {
      f"{side}_parent": spec.side(side).parent_rate
      for side in _SIDES
      if spec.side(side).parent_rate < 1.0
  }
  if parent_rates:  # a parent sample leaves each sampled fan-out exact
    detail["sample_rates"] = parent_rates
    fields_.update(method=Method.SAMPLE, sample_rate=min(parent_rates.values()))
  for metric_id in _FANOUT_IDS:
    sampled = _sampled(spec, metric_id)
    if sampled is None and metric_id == _MEAN_RATIO:
      sampled = _unequal_parent_samples(spec)
    if sampled is not None:
      e.skip(
          metric_id,
          sampled,
          parents_with_children_lower_bound_source=src.with_children,
          parents_with_children_lower_bound_synthetic=syn.with_children,
          sample_rates=_sample_rates(spec))
    else:
      _fanout_value(e, metric_id, fm, (src, syn), detail, fields_)


def _fanout_value(e: _Emitter, metric_id: str, fm: Mapping[str, Any],
                  sides: tuple[EdgeSummary, EdgeSummary],
                  detail: Mapping[str, Any], fields_: Mapping[str,
                                                              Any]) -> None:
  """One fan-out row from `stats.relational.fanout_metrics`' output."""
  src, syn = sides
  z_src, z_syn = fm["zero_child_share_source"], fm["zero_child_share_synthetic"]
  if metric_id == _TVD:
    floor = noise.tvd_null_expectation((src.hist / src.parents).tolist(),
                                       src.parents, syn.parents)
    e.value(metric_id, fm["tvd"], detail=detail, noise_floor=floor, **fields_)
  elif metric_id == _W1:
    e.value(metric_id, fm["w1"], detail=detail, **fields_)
  elif metric_id == _MEAN_RATIO:
    rows = {
        **detail,
        "child_rows_source": src.child_rows,
        "child_rows_synthetic": syn.child_rows,
        "parent_rows_source": src.all_parent_rows,
        "parent_rows_synthetic": syn.all_parent_rows,
        "ratio_counts": _ROWS_ONLY,
    }
    if fm["mean_ratio"] is None:
      e.skip(metric_id, "no source child row was read: the ratio is undefined",
             **rows)
      return
    e.value(
        metric_id,
        fm["mean_ratio"],
        detail=rows,
        source_value=src.row_mean,
        synthetic_value=syn.row_mean,
        **fields_)
  elif metric_id == _ZERO:
    e.value(
        metric_id,
        fm["zero_child_share_delta"],
        detail=detail,
        ci_low=fm["zero_child_share_delta_ci_low"],
        ci_high=fm["zero_child_share_delta_ci_high"],
        source_value=z_src,
        synthetic_value=z_syn,
        **fields_)
  elif metric_id == _ADHERENCE:
    e.value(
        metric_id,
        fm["cardinality_adherence"],
        detail={
            **detail,
            "adherent": fm["cardinality_adherence_count"],
            "range_exact": fm["cardinality_adherence_exact"],
            "range_note": _EXTREMES_NOTE,
        },
        ci_low=fm["cardinality_adherence_ci_low"],
        ci_high=fm["cardinality_adherence_ci_high"],
        **fields_)
  elif fm["parent_coverage"] is None:
    e.skip(metric_id, "no source parent has a child: the ratio is undefined",
           **detail)
  else:
    e.value(
        metric_id,
        fm["parent_coverage"],
        detail=detail,
        source_value=1.0 - z_src,
        synthetic_value=1.0 - z_syn,
        **fields_)


def edge_outputs(spec: EdgeSpec,
                 summaries: Mapping[Side, EdgeSummary]) -> list[MetricValue]:
  """One edge's relationship metrics from its finished sides: a row per
  owned id — a value, or `not_evaluated` with a reason — in
  `OWNED_METRIC_IDS` order."""
  if spec.reason is not None:
    return _failed_metrics(spec, spec.reason)
  e = _Emitter(spec)
  source_rate = _orphan_metric(e, Side.SOURCE, summaries.get(Side.SOURCE), None)
  _orphan_metric(e, Side.SYNTHETIC, summaries.get(Side.SYNTHETIC), source_rate)
  closed = [(side, spec.side(side).reason)
            for side in _SIDES
            if spec.side(side).reason is not None or side not in summaries]
  if closed:
    side, why = closed[0]
    why = why or "it was not computed"
    reason = f"needs both sides; the {side} side is not evaluated: {why}"
    for metric_id in _FANOUT_IDS:
      e.skip(metric_id, reason)
  else:
    _fanout_values(e, summaries[Side.SOURCE], summaries[Side.SYNTHETIC])
  order = {metric_id: i for i, metric_id in enumerate(OWNED_METRIC_IDS)}
  return sorted(e.rows, key=lambda mv: order[mv.metric_id])


# --------------------------------------------------------------------------
# Beam
# --------------------------------------------------------------------------
def _merged(
    parts: Sequence[tuple[np.ndarray, np.ndarray]]
) -> tuple[np.ndarray, np.ndarray]:
  """(distinct codes, summed counts) of several `child_keys` parts."""
  if len(parts) == 1:
    return parts[0]
  codes, inverse = np.unique(
      np.concatenate([c for c, _ in parts]), return_inverse=True)
  counts = np.zeros(len(codes), dtype=np.int64)
  np.add.at(counts, inverse, np.concatenate([n for _, n in parts]))
  return codes, counts


def _tagged_out(tag: str, value: Any, windowed: bool) -> Any:
  if windowed:  # finish_bundle emits windowed values only
    value = GlobalWindows.windowed_value(value)
  return beam.pvalue.TaggedOutput(tag, value)


class _ChildKeysFn(beam.DoFn):
  """Child `EncodedBatch` → per live (edge, side) `t`: tagged `keys<t>`
  (signed hash, count) per distinct non-NULL key and a tagged `stats<t>`
  `FanoutAcc` of its NULL tuples; failures `(edge key, reason)` on the
  main output. Batches of other tables pass untouched.

  The batches of a bundle are merged per key before anything is emitted
  (flushed once `FLUSH_CODES` distinct keys are held, and in
  `finish_bundle`, as `membership.RowKeysFn` does): an element per
  distinct key of a bundle, never one per row."""

  def __init__(self,
               routes: Mapping[tuple[str, str], Sequence[tuple[int, int, str,
                                                               int]]],
               flush_codes: int | None = None):
    super().__init__()
    self._routes = dict(routes)
    self._flush_at = FLUSH_CODES if flush_codes is None else flush_codes
    self._pending: dict[int, list[tuple[np.ndarray, np.ndarray]]] = {}
    self._nulls: dict[int, int] = {}
    self._held = 0

  def start_bundle(self) -> None:
    self._pending, self._nulls, self._held = {}, {}, 0

  def process(self, element: EncodedBatch) -> Iterator[Any]:
    for t, index, label, key in self._routes.get(
        (element.table, str(element.side)), ()):
      try:
        found = element.layout.edge_labels[index]
        if found != label:
          raise ValueError(f"{element.table}: fk_hash column {index} is edge "
                           f"{found!r}, expected {label!r}")
        codes, counts, nulls = child_keys(element, index)
      except WORKER_ERRORS as exc:
        yield key, _failure(exc)
        continue
      self._pending.setdefault(t, []).append((codes, counts))
      self._nulls[t] = self._nulls.get(t, 0) + nulls
      self._held += len(codes)
    if self._held >= self._flush_at:
      yield from self._flush(windowed=False)

  def finish_bundle(self) -> Iterator[Any]:
    yield from self._flush(windowed=True)

  def _flush(self, *, windowed: bool) -> Iterator[Any]:
    pending, nulls = self._pending, self._nulls
    self._pending, self._nulls, self._held = {}, {}, 0
    for t, parts in pending.items():
      codes, counts = _merged(parts)
      tag = f"{_KEYS}{t}"
      for code, count in zip(_signed(codes), counts.tolist(), strict=True):
        yield _tagged_out(tag, (code, count), windowed)
    for t, count in nulls.items():
      yield _tagged_out(f"{_STATS}{t}", FanoutAcc(child_nulls=count), windowed)


class _ParentKeysFn(beam.DoFn):
  """A batch of parent rows → `key_hashes` over one edge group's
  `ref_cols`: a tagged `stats` `FanoutAcc` (rows with a non-NULL / NULL
  key) and, on the main output, the batch's distinct keys — one sorted
  uint64 array (SIDE_INPUT) or (signed hash, rows) pairs (COGROUP).
  Failures are tagged for every edge of the group."""

  def __init__(self, ref_cols: Sequence[str], path: str,
               keys: Sequence[int]) -> None:
    super().__init__()
    self._ref_cols = tuple(ref_cols)
    self._path = path
    self._keys = tuple(keys)

  def process(self, element: Sequence[Mapping[str, Any]]) -> Iterator[Any]:
    try:
      codes = key_hashes(element, self._ref_cols)
    except WORKER_ERRORS as exc:
      reason = _failure(exc)
      for key in self._keys:
        yield beam.pvalue.TaggedOutput(_FAILED, (key, reason))
      return
    null = codes == _NULL
    present = codes[~null]
    yield beam.pvalue.TaggedOutput(
        _STATS,
        FanoutAcc(parent_rows=int(present.size), parent_nulls=int(null.sum())))
    if self._path == SIDE_INPUT:
      yield np.unique(present)
      return
    uniq, rows = np.unique(present, return_counts=True)
    yield from zip(_signed(uniq), rows.tolist(), strict=True)


def _check_set_size(size: int, planned: float) -> None:
  """Raise `ValueError` when a side-input parent set holds more than twice
  the parent rows the plan expects (rows bound distinct keys): the plan no
  longer describes the data, and the side-input cache Task 26 sizes from
  it would not hold the set."""
  if size > 2.0 * max(planned, 1.0):
    raise ValueError(f"the parent key set holds {size} keys, more than twice "
                     f"the {planned:.0f} parent rows planned for this side: "
                     "the plan is stale and the side-input cache is sized "
                     "from it")


class _KeySetCountFn(beam.DoFn):
  """The side-input path's parent count (the set's distinct keys), behind
  the size guard (`_check_set_size`): a set past twice its plan fails
  every edge of its group."""

  def __init__(self, planned: float, keys: Sequence[int]):
    super().__init__()
    self._planned = planned
    self._keys = tuple(keys)

  def process(self, element: np.ndarray) -> Iterator[Any]:
    try:
      _check_set_size(int(element.size), self._planned)
    except WORKER_ERRORS as exc:
      reason = _failure(exc)
      for key in self._keys:
        yield beam.pvalue.TaggedOutput(_FAILED, (key, reason))
      return
    yield FanoutAcc(parents=int(element.size))


class _MatchFn(beam.DoFn):
  """A batch of (signed hash, children) → the `FanoutAcc` part of the
  side-input path: np.searchsorted membership in the parent key set
  (behind the same size guard as `_KeySetCountFn`)."""

  def __init__(self, key: int, planned: float):
    super().__init__()
    self._key = key
    self._planned = planned

  def process(self, element: Sequence[tuple[int, int]],
              key_set: np.ndarray) -> Iterator[Any]:
    try:
      _check_set_size(int(key_set.size), self._planned)
      codes = np.array([code for code, _ in element],
                       dtype=np.int64).view(np.uint64)
      counts = np.array([count for _, count in element], dtype=np.int64)
      yield fanout_part(counts, _member(key_set, codes))
    except WORKER_ERRORS as exc:
      yield beam.pvalue.TaggedOutput(_FAILED, (self._key, _failure(exc)))


class _JoinFn(beam.DoFn):
  """A batch of CoGroupByKey results `(hash, {child, parent})` → the
  `FanoutAcc` part of the join path; a key with parent rows is ONE
  parent, with or without children."""

  def __init__(self, key: int):
    super().__init__()
    self._key = key

  def process(
      self, element: Sequence[tuple[int,
                                    Mapping[str,
                                            Iterable[int]]]]) -> Iterator[Any]:
    try:
      children = np.array([sum(g["child"]) for _, g in element], dtype=np.int64)
      parent_rows = np.array([sum(g["parent"]) for _, g in element],
                             dtype=np.int64)
      has_parent = parent_rows > 0
      part = fanout_part(children, has_parent)
      yield dataclasses.replace(part, parents=int(has_parent.sum()))
    except WORKER_ERRORS as exc:
      yield beam.pvalue.TaggedOutput(_FAILED, (self._key, _failure(exc)))


def _summary_entry(acc: FanoutAcc, key: int,
                   side: str) -> tuple[int, tuple[str, Any]]:
  return key, (_SUMMARY, (side, acc))


def _tagged(item: tuple[int, Any], tag: str) -> tuple[int, tuple[str, Any]]:
  key, value = item
  return key, (tag, value)


def _emit_edge(item: tuple[int, Iterable[tuple[str, Any]]],
               specs: Mapping[int, EdgeSpec]) -> list[MetricValue]:
  """One edge's metrics — or, when it failed anywhere (the driver, a
  worker, or here), every owned id not_evaluated with the reason."""
  key, entries = item
  spec = specs[key]
  accs: dict[Side, FanoutAcc] = {}
  reasons = []
  for tag, value in entries:
    if tag == _SUMMARY:
      side, acc = value
      accs[Side(side)] = acc
    elif tag == _FAILED:
      reasons.append(value)
  if reasons:
    return _failed_metrics(spec, sorted(reasons)[0])
  try:
    return edge_outputs(spec, {
        side: summarize(acc) for side, acc in accs.items()
    })
  except WORKER_ERRORS as exc:
    return _failed_metrics(spec, _failure(exc))


@dataclass(frozen=True)
class _ParentKeys:
  """One edge group's parent keys: `keys` is the sorted key set
  (SIDE_INPUT, one element) or the (hash, rows) pairs (COGROUP); `parts`
  the `FanoutAcc` counts every edge of the group adds."""
  path: str
  keys: beam.PCollection
  planned: float | None
  parts: tuple[beam.PCollection, ...]


def _projected(parent: TablePlan, columns: Iterable[str]) -> TablePlan:
  """`parent` restricted to `columns` (a DIRECT_READ bills the selected
  columns only)."""
  wanted = frozenset(columns)
  return dataclasses.replace(
      parent, columns=tuple(c for c in parent.columns if c.name in wanted))


class Relational(beam.PTransform):
  """`PCollection[EncodedBatch]` (every table, both sides) → `{"metrics":
  PCollection[MetricValue]}` (module docstring).

  `tables` is every table of the plan, read-only (`external`) parents
  included; the child side comes from the batches, the parent side is
  read here through `sources`, once per (parent, side), projected to the
  referenced columns (the encoder keeps no `ref_cols` hash of a parent).
  `side_input_max_keys` bounds the side-input join (module docstring). An
  edge whose spec or parent read cannot be built (`TABLE_ERRORS`) is not
  evaluated, with the reason; the others run.
  """

  def __init__(self,
               tables: Sequence[TablePlan],
               *,
               sources: Sources,
               side_input_max_keys: int = SIDE_INPUT_MAX_KEYS):
    super().__init__()
    self._sources = sources
    planned = plan_edges(tables, side_input_max_keys=side_input_max_keys)
    self._parents: dict[str, TablePlan] = {
        parent.landing_table: parent
        for _, parent in planned
        if parent is not None
    }
    self._specs = tuple(spec for spec, _ in planned)

  def _parent_keys(
      self, p: beam.Pipeline, live: Sequence[tuple[EdgeSpec, Side]],
      late: list[tuple[int, str]]
  ) -> tuple[dict[tuple[str, Side, tuple[str, ...]], _ParentKeys],
             list[beam.PCollection]]:
    """Each (parent, side) read once, each group of edges sharing its
    `ref_cols` hashed once; a read that cannot be built fails its edges
    into `late`."""
    groups: dict[tuple[str, Side, tuple[str, ...]], list[EdgeSpec]] = {}
    for spec, side in live:
      landing = spec.side(side).parent
      assert landing is not None  # a live side has a planned parent
      groups.setdefault((landing, side, spec.ref_cols), []).append(spec)
    reads: dict[tuple[str, Side], set[str]] = {}
    for landing, side, ref_cols in groups:
      reads.setdefault((landing, side), set()).update(ref_cols)
    batched: dict[tuple[str, Side], beam.PCollection] = {}
    for (landing, side), columns in reads.items():
      try:
        rows = self._sources.read(p, _projected(self._parents[landing],
                                                columns), side)
      except TABLE_ERRORS as exc:
        reason = (f"the parent {landing} could not be read for the {side} "
                  f"side ({type(exc).__name__}: {exc})")[:_REASON_CHARS]
        late.extend((spec.key, reason)
                    for (g_landing, g_side, _), members in groups.items()
                    if (g_landing, g_side) == (landing, side)
                    for spec in members)
        continue
      batched[(landing, side)] = rows | f"ParentBatch[{landing}/{side}]" >> (
          beam.BatchElements(
              min_batch_size=MIN_BATCH_SIZE, max_batch_size=MAX_BATCH_SIZE))
    out: dict[tuple[str, Side, tuple[str, ...]], _ParentKeys] = {}
    failures = []
    for (landing, side, cols), members in groups.items():
      if (landing, side) not in batched:
        continue
      path = members[0].side(side).path
      joined_cols = ",".join(cols)
      name = f"{landing}/{side}/{joined_cols}"
      keyed = batched[(landing, side)] | f"ParentKeys[{name}]" >> beam.ParDo(
          _ParentKeysFn(cols, path, [s.key for s in members])).with_outputs(
              _STATS, _FAILED, main=_KEYS)
      failures.append(keyed[_FAILED])
      if path == SIDE_INPUT:
        planned = members[0].side(side).planned_keys
        assert planned is not None  # the side-input path needs a row count
        key_set = keyed[_KEYS] | f"ParentSet[{name}]" >> beam.CombineGlobally(
            _KeySetCombineFn())
        count = key_set | f"ParentCount[{name}]" >> beam.ParDo(
            _KeySetCountFn(planned, [s.key for s in members])).with_outputs(
                _FAILED, main=_PARTS)
        failures.append(count[_FAILED])
        out[(landing, side, cols)] = _ParentKeys(path, key_set, planned,
                                                 (keyed[_STATS], count[_PARTS]))
      else:
        out[(landing, side, cols)] = _ParentKeys(path, keyed[_KEYS], None,
                                                 (keyed[_STATS],))
    return out, failures

  def expand(self, input_or_inputs: beam.PCollection) -> dict[str, Any]:
    batches = input_or_inputs
    p = batches.pipeline
    specs = self._specs
    late: list[tuple[int, str]] = []
    live = [(spec, side) for spec in specs if spec.reason is None
            for side in _SIDES if spec.side(side).reason is None]
    parents, failures = self._parent_keys(p, live, late)
    dead = {key for key, _ in late}
    live = [(spec, side) for spec, side in live if spec.key not in dead]
    routes: dict[tuple[str, str], list[tuple[int, int, str, int]]] = {}
    for t, (spec, side) in enumerate(live):
      routes.setdefault((spec.child, str(side)), []).append(
          (t, spec.index, spec.label, spec.key))
    tags = [f"{kind}{t}" for t in range(len(live)) for kind in (_KEYS, _STATS)]
    children = batches | "ChildKeys" >> beam.ParDo(
        _ChildKeysFn(routes)).with_outputs(
            *tags, main=_FAILED)
    failures.append(children[_FAILED])
    summaries = []
    for t, (spec, side) in enumerate(live):
      group = parents[(str(spec.side(side).parent), side, spec.ref_cols)]
      name = f"{spec.label}#{spec.key}/{side}"
      counts = children[f"{_KEYS}{t}"] | f"CountPerKey[{name}]" >> (
          beam.CombinePerKey(sum))
      batching = beam.BatchElements(
          min_batch_size=MIN_BATCH_SIZE, max_batch_size=MAX_BATCH_SIZE)
      if group.path == SIDE_INPUT:
        joined = (
            counts
            | f"MatchBatch[{name}]" >> batching
            | f"Match[{name}]" >> beam.ParDo(
                _MatchFn(spec.key, float(group.planned or 0.0)),
                beam.pvalue.AsSingleton(group.keys)).with_outputs(
                    _FAILED, main=_PARTS))
      else:
        joined = ({
            "child": counts,
            "parent": group.keys
        }
                  | f"Join[{name}]" >> beam.CoGroupByKey()
                  | f"JoinBatch[{name}]" >> batching
                  | f"Joined[{name}]" >> beam.ParDo(_JoinFn(
                      spec.key)).with_outputs(_FAILED, main=_PARTS))
      failures.append(joined[_FAILED])
      parts = (joined[_PARTS], children[f"{_STATS}{t}"], *group.parts)
      summaries.append(
          parts
          | f"Parts[{name}]" >> beam.Flatten()
          | f"Fanout[{name}]" >> beam.CombineGlobally(FanoutCombineFn())
          | f"Summary[{name}]" >> beam.Map(_summary_entry, spec.key, str(side)))
    failures.append(p | "DriverFailures" >> beam.Create(late))
    failed = (
        failures
        | "FlattenFailures" >> beam.Flatten()
        | "TagFailures" >> beam.Map(_tagged, _FAILED))
    seeds = p | "Seeds" >> beam.Create([(spec.key, (_SEED, None))
                                        for spec in specs])
    metrics = (
        (*summaries, failed, seeds)
        | "Entries" >> beam.Flatten()
        | "ByEdge" >> beam.GroupByKey()
        |
        "Emit" >> beam.FlatMap(_emit_edge, {spec.key: spec for spec in specs}))
    return {"metrics": metrics}
