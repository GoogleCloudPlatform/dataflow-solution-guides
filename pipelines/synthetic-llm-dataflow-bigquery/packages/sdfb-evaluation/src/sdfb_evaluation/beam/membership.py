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
"""Row membership: which synthetic rows reproduce a source record, whole
or all but one field, and whether the rows the generator read come back
more often than rows it never saw.

    driver: the panel (D3) ─ BatchEncoder ─► PanelRefs per table
      │   R, H record / row / key hashes in rank order (E = the first
      │   e_n ranks of R, H_E of H) and their cell-code matrices
      ▼
    EncodedBatch ─ Partition(table) ─┬─ source ─► SourceHashes ─► Combine
      │                              │   Globally: SourceSets (sorted uint64
      │                              │   record and row hashes) ── AsSingleton
      │                              └─ synthetic ─► MembershipFn ◄─ panel
      │                                   │  (Shared PanelIndex)   AsSingleton
      │                                   ├─ (table, MembershipAcc)
      │                                   │    ─► CombinePerKey ────────────┐
      │                                   └─ ((table, check), candidate)    │
      │                                        ─► CombinePerKey(top-k       │
      │                                           distinct) ─► RowFlag      │
      │                                           (label key: AsSingleton)  │
      └─ RowKeysFn (bundle-merged) ─► ((table, kind, bucket), packed counts)│
           ─► CombinePerKey (compact) ─► KeyStats ─► CombinePerKey(table) ──┤
    per-table failures (driver or worker) and one seed per table ───────────┤
                                               GroupByKey(table) ◄──────────┘
                                                 ─► MetricValue (OWNED_METRIC_IDS)

Hashes (`beam.encode`): `nonkey_hash` is the salted linear hash of the
non-key cells (no PK, identity or FK column), so a record copied under a
fresh key still matches; `row_hash` covers every column. A table's
RECORD hash is its non-key hash — or, for an all-key table (every column
a key, no content), its row hash: the key tuple is then the record, so
its copies are still flagged, though the content metrics are not
evaluated. A row whose record cells are all NULL has one constant record
hash: it carries no content and is NEVER a match, near match or internal
duplicate (Ruling R60).

Exact sets and the lifts (D3). With U_S the distinct record hashes of
panel set S, the exclusive sets are R \\ H and H \\ R — a record both
halves hold is common, and hitting it is chance — and, for the exposure
lift, E \\ H against H_E \\ R; (R \\ H) \\ E against (H \\ R) \\ H_E (the
exclusive records outside the exposed prefixes) give `detail.unexposed`,
or a reason when either is empty (a steep-tailed source can hold every
exclusive R record inside E). R and H are exchangeable halves of one
ranking, so under the null a synthetic generator hits either exclusive
set alike. The EVENT is a distinct exclusive record the synthetic side
reproduces at least once, never a synthetic row: a record hit by many
synthetic rows is one event, which keeps the counts close to Poisson.
Counting rows instead would weight every chance hit by the synthetic
rows its pattern draws, which on a skewed table puts the whole R/H
selection noise into a narrow Poisson interval (a simulation at
n_syn ≫ n missed 1 in 39-81 % of null runs, against 0-1 % for distinct
records). The row counts are in `detail` (`rows_r`/`rows_h`); a record
copied many times still shows in `row.exact_match_rate_nonkey` and in
the flags. The lift is the conditional rate-ratio test of the event
counts over the exclusive set sizes, with a Clopper-Pearson interval
(Przyborowski & Wilenski, 1940; Clopper & Pearson, 1934); status reads
ci_low (Rulings R1, R12, R38: no event on either side leaves the value
NULL with ci_low 0).

Near matches (leave one out, Ruling R2). Row x's key at non-key column j
is `canonical.loo_hashes`: its non-key hash without column j's term, so
two rows equal in every non-key column but j share it. A synthetic row
near-matches set S when, at some j, its key equals the key of an S row at
the same j AND every other non-key cell code equals that row's
(`hash64` per cell, compared exactly: a linear-hash collision is
rejected). A row that matches an R or an H record exactly is never a
near match of either set (exact matches are counted as exact). The
dropped column's all-NULL remainder is never a key (R60).
`row.near_match_rate` is the share of synthetic rows near-matching R
(Wilson, 1927); the near lift counts distinct exclusive keys (a key of R
not held by H at the same column, and vice versa), as the exact lift
does. Fewer than two non-key columns leave nothing to compare once one
is dropped.

The full source. `row.exact_match_rate` (every column) and
`row.exact_match_rate_nonkey` compare the synthetic side with every
source row read:

    source side input fits SOURCE_SET_MAX_BYTES  sorted distinct source
      (`context.budget.source_sets_fit`: 8 B a   hashes, one CombineGlobally
      source row and array; the record hashes,   per table, read with
      plus the row hashes when a keyed table     AsSingleton; membership is
      has content: 20M rows key-less or          map-side, so full-source
      all-key, 10M keyed, at 160 MB)             exact copies are flagged
    above it, or the source count unknown        the keyed count below IS the
                                                  co-grouping: Σ c_syn over
                                                  the hashes with c_src ≥ 1;
                                                  no full-source flag (panel
                                                  flags remain)

Task 26 sizes `max_cache_memory_usage_mb` (Beam 2.76 defaults to 0, no
side-input cache) to the SUM of the side inputs a worker holds at once,
so each is materialised once; the derived panel index is built once per
worker behind `apache_beam.utils.shared.Shared`, tagged by the panel's
content token.

Keyed counts (all tables, one pass). Every hash is counted exactly per
side, in `(table, kind, bucket)` keys — the hash space split by its top
bits into up to 2^MAX_BUCKET_BITS buckets of about KEY_BUCKET_CODES codes
each:

    nk     non-key hash, both sides   internal duplicates (the frequency
                                      of frequencies of each side); the
                                      keyed full-source non-key matches
    pk     PK hash, synthetic         table.pk_duplicate_rate (a NULL part is
    id     identity hash, synthetic   no key: counted as `null_key_rows`,
                                      never a duplicate)
    row    row hash, both sides       keyed mode only, keyed tables only

A value is SPARSE and packed: one side's sorted codes as little-endian
uint64 bytes and their counts at the smallest unsigned width that holds
them — primitives FastPrimitivesCoder writes without pickling. `RowKeysFn`
merges the batches of a bundle per key before it emits (flushing when it
holds FLUSH_CODES codes), and `KeyCountsCombineFn.compact` merges a
lifted accumulator to one part per side before it crosses the shuffle, so
a code costs about 9 B (budgeted as `context.budget.MEMBERSHIP_CODE_BYTES`).

Internal duplicates at matched n (Ruling R73, amending R59). The share of
rows whose content is held twice or more grows with n, so both sides are
compared at m = min(n_src, n_syn) content rows by exact rarefaction from
each side's frequency of frequencies (Hurlbert, 1971; Heck, van Belle &
Simberloff, 1975): a record held c times among N rows keeps X ~
Hypergeometric(N, c, m) of them in a subsample of m, so

    E[D_m] = Σ_c f_c · (c·m/N - P(X = 1))    rows in duplicate groups

and `row.internal_duplicate_excess` = (E[D_m]_syn - E[D_m]_src) / m (the
smaller side is not subsampled: its value is its observed share). Noise:
under the null the two sides are exchangeable, each a random m-subset of
their pooled rows, and D_m = m - F1 (the rows of records held once), so
the null variance is Var(F1) of an m-subset of the pool — exact from the
pooled frequency of frequencies with the multivariate hypergeometric
(`duplicate_null_variance`), divided by the finite-population factor
1 - m / (n_src + n_syn) so it is the variance of an m-row sample of the
population rather than of the pool (Cochran, 1977; two sides of equal
size are complementary halves of the pool, whose difference varies twice
as much as a half does alone). Rows of one duplicate group are not
independent draws and a heavy record adds no variance at all, so the
counts enter Newcombe's (1998) interval through ONE Korn & Graubard
(1998) effective sample size for both sides (Ruling R76): n* = p̄ (1 -
p̄) / Var(d) at the pooled rows' rarefied share p̄, capped at m, with
the unrounded effective counts d·n*/m — so the interval always holds the
value, and a near-unique source with a handful of pairs is not read as
a tiny sample that swallows a real excess. The catalogue's `newcombe`
noise method then reads a faithful generator's difference as noise.
`baseline_value` = the same at min(|R|, n_src) for
R against the source (D4).

Sampled mode (Ruling R72). A side read as a row sample (`--mode
sampled`, rate < 1: a salted content-hash sample that keeps identical
rows together) cannot support a verdict that needs every row of a side:
the full-source match rates (the source side), the PK/identity duplicate
rates (the synthetic side) and internal duplicates (both) are
`not_evaluated` with "sampled mode cannot measure …; run exact mode" and
the observed lower-bound count in `detail`. The panel-based rates and
lifts compare R/H/E, read in full, with the synthetic rows read, so they
stay evaluated with `method` = sample and the synthetic `sample_rate`
(also in `detail`).

Flags. A top-k of DISTINCT candidates per (table, check) for
`exact_copy` and `near_copy` (distinct by the synthetic row hash and the
source record's code, so a record copied 200 times into a key-less table
takes one slot), ranked by the set a row copies (E — an exclusive E
record — then R, H, the full source), then a salted SplitMix64 spread of
its row hash (Steele, Lea & Flood, 2014), then its synthetic handle:
deterministic for an evaluation, and a sample, not the first rows read.
A flag carries:

    synthetic_key     the synthetic row's handle: its PK tuple, else its
                      identity tuple, as JSON (NULL for a table with
                      neither). It is the synthetic table's own key, not
                      covered by D6 hashing — so for a full-row copy
                      (keys included) it IS the copied source row's key
    source_key_hash   `canonical.hashed_label` of the matched source
                      record's code with the worker-side label key
                      (Rulings R64, R68): its PK hash; else its identity
                      hash; else (no PK, no identity) its record hash —
                      the row hash itself when the table has no key
                      column. A full-source copy is labelled only in the
                      last case (map-side the source record's key is
                      unknown), NULL otherwise
    source_key        always NULL (`row_flags_source_keys=raw` is refused)

No attribute value is ever written. With an unverified reference only
full-source flags are written: the panel sets are not known to be the
generator's.

An unverified reference (`Panel.verified` False) makes every R/H/E-based
metric `not_evaluated` with `UNVERIFIED_REASON` and the panel's own
reason; the full-source metrics are still computed.

Failures stay per table. A table whose spec or panel cannot be built on
the driver (`TABLE_ERRORS`), or whose batches raise a data error on a
worker (`WORKER_ERRORS`), writes `not_evaluated` rows with the reason for
every owned id and no flags; the other tables and the pipeline carry on.
A worker's `MemoryError` is not a data error: it is raised, so the
bundle fails and the runner retries it (Ruling R113).

References (author-year, R22): Przyborowski & Wilenski (1940); Clopper &
Pearson (1934); Wilson (1927); Newcombe (1998); Hurlbert (1971); Heck,
van Belle & Simberloff (1975); Cochran (1977); Korn & Graubard (1998);
Steele, Lea & Flood (2014); Stadler, Oprisanu & Troncoso (2022) for
near copies.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple

import apache_beam as beam
import numpy as np
from apache_beam.transforms.window import GlobalWindows
from apache_beam.utils.shared import Shared

from sdfb_evaluation.beam import staged
from sdfb_evaluation.beam.encode import BatchEncoder, BatchLayout, EncodedBatch
from sdfb_evaluation.canonical import (
    NULL_CODE,
    canonical_value,
    hash64,
    hashed_label,
    json_safe,
    linear_hash,
    loo_hashes,
    multipliers,
)
from sdfb_evaluation.context.budget import (
    SOURCE_SET_MAX_BYTES,
    source_set_arrays,
    source_sets_fit,
)
from sdfb_evaluation.stats import noise
from sdfb_evaluation.types import Method, MetricValue, Side

if TYPE_CHECKING:
  from sdfb_evaluation.context.plan import TablePlan

__all__ = [
    "EXACT_COPY",
    "FLUSH_CODES",
    "KEYED_COUNT",
    "KEY_BUCKET_CODES",
    "LIFT_ALPHA",
    "MAX_BUCKET_BITS",
    "NEAR_COPY",
    "OWNED_METRIC_IDS",
    "PAIR_TABLE_CAP",
    "PANEL_SET_MAX_BYTES",
    "SIDE_INPUT",
    "SOURCE_SETS",
    "SOURCE_SET_MAX_BYTES",
    "TABLE_ERRORS",
    "UNVERIFIED_REASON",
    "WORKER_ERRORS",
    "DuplicateExcess",
    "KeyCountsCombineFn",
    "KeyStats",
    "Membership",
    "MembershipAcc",
    "MembershipCombineFn",
    "MembershipFn",
    "MembershipResult",
    "MembershipSpec",
    "PanelIndex",
    "PanelRefs",
    "PanelStats",
    "RowFlag",
    "RowKeysFn",
    "SideCounts",
    "SourceSets",
    "SourceSetsCombineFn",
    "batch_membership",
    "duplicate_excess",
    "duplicate_null_variance",
    "flag_row",
    "key_counts",
    "membership_outputs",
    "rarefied_duplicates",
    "source_hashes",
    "table_outputs",
]

OWNED_METRIC_IDS: tuple[str, ...] = (
    "row.exact_match_rate",
    "row.exact_match_rate_nonkey",
    "row.memorization_lift",
    "row.exposure_lift",
    "row.near_match_rate",
    "row.near_match_lift",
    "row.internal_duplicate_excess",
    "table.pk_duplicate_rate",
    "table.identity_duplicate_rate",
)

PANEL_SET_MAX_BYTES = 16 * 2**20  # one panel side-input array
EXACT_COPY, NEAR_COPY = "exact_copy", "near_copy"
SOURCE_SETS: tuple[str, ...] = ("E", "R", "H", "source")  # flag priority
SIDE_INPUT, KEYED_COUNT = "side_input", "keyed_count"
LIFT_ALPHA = 0.05
UNVERIFIED_REASON = "reference sample not verified"
# codes per keyed-count bucket, about: bounds a bucket's final accumulator
# (~9 B a code) while keeping buckets few enough that a bundle's element
# for one bucket carries many codes (the per-element overhead amortised)
KEY_BUCKET_CODES = 1 << 23
MAX_BUCKET_BITS = 10
FLUSH_CODES = 1 << 18  # codes a RowKeysFn holds before it flushes
# The errors that make a table not_evaluated on the DRIVER, never the run
# fail: a data error (a malformed panel row, a degenerate count) or a
# panel the driver cannot hold — no retry changes what the driver holds.
TABLE_ERRORS: tuple[type[Exception],
                    ...] = (ArithmeticError, IndexError, KeyError, MemoryError,
                            TypeError, ValueError)
# The same on a WORKER, without MemoryError: that is the worker's state,
# not the table's. It is raised, so the bundle fails and the runner
# retries it (`beam.pipeline`'s contract, Ruling R113) — swallowed, it
# was a permanent not_evaluated block that left the run SUCCEEDED.
WORKER_ERRORS: tuple[type[Exception], ...] = (ArithmeticError, IndexError,
                                              KeyError, TypeError, ValueError)
_FLUSH_PER_BUCKET = 256  # flush no sooner than this many codes per bucket
# a record class whose P(X = 1) is below this adds nothing measurable to
# the null variance (a record held ~33 times its expected share or more)
_NEGLIGIBLE = 1e-12
_PAIR_BLOCK = 1 << 20  # class pairs a variance block holds (~8 MB an array)
PAIR_TABLE_CAP = 1 << 20  # entries of the dense exact pair table (8 MB)
_WALK_CHUNK = 1 << 20  # terms a chunk of the running log sum holds
_LOG_SKIP = math.log(1e-15)  # a P(X = 1) bound below this is 0
_PAIR_OFFSET = 2  # the smallest sum of two records' counts
_UNKNOWN_BUCKET_BITS = 4
_COMPACT_CODES = 1 << 20  # pending codes before a combine compacts
_DUPLICATED = 2  # a hash counted this often or more is a duplicate
_MIN_NEAR_COLUMNS = 2  # dropping one of fewer leaves nothing to compare
# flag priority = the index of the copied set in SOURCE_SETS
_PRIO_E, _PRIO_R, _PRIO_H, _PRIO_SOURCE = range(4)
# a panel record's class (`_classes`)
_SHARED, _EXPOSED, _REST = 0, 1, 2
_SRC, _SYN = 0, 1  # the side slot of a packed keyed-count part
_MB = 1e6
_REASON_CHARS = 300
_COUNT_DTYPES = {1: "<u1", 2: "<u2", 4: "<u4", 8: "<u8"}

_NO_SYNTHETIC = "no synthetic rows were read"
_NO_SOURCE = "no source rows were read"
_NO_CONTENT = ("every column is a key (primary key, identity or foreign "
               "key): there is no non-key content to match")
_NO_PANEL = "no reference panel (R/H) was read for this table"
_FEW_COLUMNS = ("fewer than 2 non-key columns: dropping one leaves nothing "
                "to compare")
_NO_PK = "no primary key declared in the relationship model"
_NO_IDENTITY = "no identity columns declared in the relationship model"
_NO_EXPOSURE = ("no non-key record is held only by {a}, or none only by {b} "
                "(an exclusive set is empty): the lift has no exposure on one "
                "side")
_NULL_KEY_NOTE = ("rows whose key has a NULL part are not duplicates; a NULL "
                  "key is its own integrity defect")
_SAMPLED_SOURCE = ("sampled mode cannot measure matches against the full "
                   "source: the source side is a {rate:.3g} row sample, so a "
                   "copy of an unsampled source row goes unseen; run exact "
                   "mode")
_SAMPLED_KEYS = ("sampled mode cannot measure {what} duplicates: the "
                 "synthetic side is a {rate:.3g} row sample, so a duplicate "
                 "whose twin was not sampled goes unseen; run exact mode")
_SAMPLED_DUPLICATES = ("sampled mode cannot measure internal duplicates: the "
                       "{side} side is a {rate:.3g} sample of rows by content "
                       "hash, which keeps or drops identical rows together; "
                       "run exact mode")

_INT_TYPES = frozenset({"INT64", "INTEGER"})
_COUNTED_SIDES = frozenset({Side.SOURCE, Side.SYNTHETIC})
_SPLITMIX = (np.uint64(0x9E3779B97F4A7C15), np.uint64(0xBF58476D1CE4E5B9),
             np.uint64(0x94D049BB133111EB))
_SPREAD_LABEL = "sdfb:flag-spread"
_ACCS, _FLAGS, _STATS, _COUNTS, _FAILED = ("accs", "flags", "stats", "counts",
                                           "failed")
_ACC, _KEYS, _SEED = "acc", "keys", "seed"
_EMPTY_POS = np.zeros(0, dtype=np.int64)
_EMPTY_CODES = np.zeros(0, dtype=np.uint64)

FreqOfFreqs = tuple[tuple[int, int], ...]  # (count c, records held c times)


def _mix64(x: np.ndarray) -> np.ndarray:
  """SplitMix64's finaliser, vectorised (uint64 wraps by design)."""
  with np.errstate(over="ignore"):
    z = x + _SPLITMIX[0]
    z = (z ^ (z >> np.uint64(30))) * _SPLITMIX[1]
    z = (z ^ (z >> np.uint64(27))) * _SPLITMIX[2]
    return z ^ (z >> np.uint64(31))


def _member(sorted_codes: np.ndarray, codes: np.ndarray) -> np.ndarray:
  """`codes[i] in sorted_codes`, vectorised (`sorted_codes` sorted)."""
  if sorted_codes.size == 0 or codes.size == 0:
    return np.zeros(len(codes), dtype=bool)
  pos = np.minimum(np.searchsorted(sorted_codes, codes), len(sorted_codes) - 1)
  found: np.ndarray = sorted_codes[pos] == codes
  return found


def _lookup(sorted_codes: np.ndarray, codes: np.ndarray,
            eligible: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
  """(position of each code in `sorted_codes`, clipped; found & eligible)."""
  if sorted_codes.size == 0:
    return (np.zeros(len(codes),
                     dtype=np.int64), np.zeros(len(codes), dtype=bool))
  pos = np.minimum(np.searchsorted(sorted_codes, codes), len(sorted_codes) - 1)
  return pos, eligible & (sorted_codes[pos] == codes)


def _first_unique(codes: np.ndarray,
                  keep: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
  """(sorted distinct `codes[keep]`, the rank of each one's first row)."""
  ranks = np.nonzero(keep)[0]
  unique, first = np.unique(codes[ranks], return_index=True)
  return unique.astype(np.uint64), ranks[first].astype(np.int64)


def _positions(pos: np.ndarray, hit: np.ndarray) -> np.ndarray:
  """The distinct panel positions `pos[hit]`, as int64."""
  unique: np.ndarray = np.unique(pos[hit]).astype(np.int64)
  return unique


def _mb(nbytes: float) -> str:
  return f"{nbytes / _MB:.3g} MB"


def _union(a: np.ndarray, b: np.ndarray) -> np.ndarray:
  if a.size == 0:
    return b
  if b.size == 0:
    return a
  merged: np.ndarray = np.union1d(a, b)
  return merged


def _failure(exc: BaseException) -> str:
  """A failed table's reason: the error class and its message (bounded;
  encoder and numpy messages name columns and types, never values)."""
  text = f"membership could not be computed for this table ({type(exc).__name__}: {exc})"
  return text[:_REASON_CHARS]


def _freqs(counts: np.ndarray) -> FreqOfFreqs:
  """The frequency of frequencies of positive `counts`: (c, f_c) pairs."""
  values, freq = np.unique(counts[counts > 0], return_counts=True)
  return tuple((int(c), int(f))
               for c, f in zip(values.tolist(), freq.tolist(), strict=True))


def _freqs_sum(a: FreqOfFreqs, b: FreqOfFreqs) -> FreqOfFreqs:
  if not a:
    return b
  if not b:
    return a
  total = Counter(dict(a))
  total.update(dict(b))
  return tuple(sorted(total.items()))


# --------------------------------------------------------------------------
# the per-table spec (driver-built, pickled into the DoFns: slim)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class MembershipSpec:  # pylint: disable=too-many-instance-attributes  # one field per fact the passes read
  """What membership needs of one `TablePlan` — never its panel rows
  (`PanelRefs` carries their hashes, as a side input).

  `null_content`/`null_row` are the non-key / row hashes of a row whose
  cells are all NULL (R60); `handle` is the synthetic flag handle's
  columns (the PK, else the identity) and `code_kind` which source code a
  flag labels (`pk`, `identity` or `record`); `source_mode` is SIDE_INPUT
  or KEYED_COUNT (module docstring), `source_note` says why;
  `bucket_bits` splits the keyed counts; `rate_source`/`rate_synthetic`
  are the sides' row-sample rates (1.0 when read in full, R72).
  """
  table: str
  encoding_plan_digest: str
  salt: str
  nonkey_columns: tuple[str, ...]
  keyed: bool
  pk: tuple[str, ...]
  identity: tuple[str, ...]
  handle: tuple[str, ...]
  handle_text: tuple[int, ...] | None
  handle_int: tuple[bool, ...]
  code_kind: str
  null_content: int
  null_row: int
  source_mode: str
  source_note: str
  bucket_bits: int
  panel_max_bytes: int
  rate_source: float = 1.0
  rate_synthetic: float = 1.0

  @property
  def null_record(self) -> int:
    """The record hash of a row with no content (module docstring)."""
    return self.null_content if self.nonkey_columns else self.null_row

  @classmethod
  def from_table(
      cls,
      table: TablePlan,
      *,
      salt: str,
      panel_max_bytes: int = PANEL_SET_MAX_BYTES,
      source_set_max_bytes: int = SOURCE_SET_MAX_BYTES) -> MembershipSpec:
    layout = BatchLayout.from_table(table)
    nonkey = layout.nonkey_columns
    keyed = nonkey != layout.columns
    null_row = int(
        linear_hash(
            np.full((1, len(layout.columns)), NULL_CODE, dtype=np.uint64),
            multipliers(layout.columns, salt))[0])
    null_content = int(
        linear_hash(
            np.full((1, len(nonkey)), NULL_CODE, dtype=np.uint64),
            multipliers(nonkey, salt))[0]) if nonkey else NULL_CODE
    handle = layout.pk or layout.identity
    bq_type = {c.name: c.bq_type for c in table.columns}
    handle_text = (
        tuple(layout.text_columns.index(c) for c in handle)
        if handle and set(handle) <= set(layout.text_columns) else None)
    code_kind = "pk" if layout.pk else "identity" if layout.identity else (
        "record")
    mode, note = _source_mode(
        table, nonkey=bool(nonkey), keyed=keyed, max_bytes=source_set_max_bytes)
    return cls(
        table=table.name,
        encoding_plan_digest=table.encoding_plan_digest,
        salt=salt,
        nonkey_columns=nonkey,
        keyed=keyed,
        pk=layout.pk,
        identity=layout.identity,
        handle=handle,
        handle_text=handle_text,
        handle_int=tuple(bq_type[c].upper() in _INT_TYPES for c in handle),
        code_kind=code_kind,
        null_content=null_content,
        null_row=null_row,
        source_mode=mode,
        source_note=note,
        bucket_bits=_bucket_bits(table),
        panel_max_bytes=panel_max_bytes,
        rate_source=_rate(table.sample_rate_source),
        rate_synthetic=_rate(table.sample_rate_synthetic))


def _rate(rate: float | None) -> float:
  return 1.0 if rate is None or rate >= 1.0 else float(rate)


def _source_mode(table: TablePlan, *, nonkey: bool, keyed: bool,
                 max_bytes: int) -> tuple[str, str]:
  """SIDE_INPUT when the sorted source sets fit `max_bytes`
  (`context.budget.source_sets_fit`), else KEYED_COUNT."""
  if table.rows_source is None:
    return KEYED_COUNT, "source row count unknown: the keyed count"
  rows = table.rows_read[0]
  need = rows * 8 * source_set_arrays(nonkey=nonkey, keyed=keyed)
  if source_sets_fit(rows, nonkey=nonkey, keyed=keyed, max_bytes=max_bytes):
    return SIDE_INPUT, f"source sets {_mb(need)} <= {_mb(max_bytes)}"
  return KEYED_COUNT, f"source sets {_mb(need)} > {_mb(max_bytes)}"


def _bucket_bits(table: TablePlan) -> int:
  """log2 of the keyed-count buckets: about KEY_BUCKET_CODES codes each."""
  if table.rows_source is None or table.rows_synthetic is None:
    return _UNKNOWN_BUCKET_BITS
  rows = sum(table.rows_read)
  if rows <= KEY_BUCKET_CODES:
    return 0
  return min(MAX_BUCKET_BITS, math.ceil(math.log2(rows / KEY_BUCKET_CODES)))


def _record(spec: MembershipSpec, batch: EncodedBatch) -> np.ndarray:
  """A batch's record hashes (module docstring)."""
  return batch.nonkey_hash if spec.nonkey_columns else batch.row_hash


# --------------------------------------------------------------------------
# the panel: hashes (driver) and the derived index (worker, Shared)
# --------------------------------------------------------------------------
@dataclass(frozen=True, eq=False)
class PanelRefs:  # pylint: disable=too-many-instance-attributes  # one field per panel array
  """The R/H panel of one table as hashes, in rank order (the membership
  side input). E is `r_*[:e_n]`, H_E `h_*[:he_n]`. `r_record` holds the
  record hashes, `r_key` the rows' source-key codes (`spec.code_kind`),
  labelled only at flag time with the worker's key, `r_cells`/`h_cells`
  the (n, d) non-key cell codes (near matches). `lift_reason`/
  `near_reason` say why the exact / near sets are absent (their arrays
  are then empty); `r_freqs` is R's content frequency of frequencies,
  for the D4 duplicate baseline."""
  table: str
  token: str
  r_record: np.ndarray
  h_record: np.ndarray
  r_row: np.ndarray
  h_row: np.ndarray
  r_key: np.ndarray
  h_key: np.ndarray
  r_cells: np.ndarray | None
  h_cells: np.ndarray | None
  e_n: int
  he_n: int
  lift_reason: str | None
  near_reason: str | None
  r_freqs: FreqOfFreqs | None

  @classmethod
  def from_table(cls, table: TablePlan, spec: MembershipSpec) -> PanelRefs:
    """The panel's hashes (the plan's salt, `BatchEncoder`); every R/H
    reason decided here, in order: no panel, unverified (with the panel's
    own reason), the size guard (`spec.panel_max_bytes` per array), then
    for near matches too few non-key columns and the cell-matrix guard.

    Raises:
      ValueError: a panel row the encoder rejects (the caller makes the
        table not_evaluated).
    """
    panel = table.panel
    empty = cls(table.name, "", _EMPTY_CODES, _EMPTY_CODES, _EMPTY_CODES,
                _EMPTY_CODES, _EMPTY_CODES, _EMPTY_CODES, None, None, 0, 0,
                _NO_PANEL, _NO_PANEL, None)
    if panel is None:
      return empty
    r = _encode_panel(table, spec, Side.REFERENCE, panel.r_rows)
    h = _encode_panel(table, spec, Side.HOLDOUT, panel.h_rows)
    r_freqs = _record_freqs(r.record,
                            spec.null_content) if spec.nonkey_columns else None
    lift_reason = None
    if not panel.verified:
      why = panel.reason or "digest mismatch"
      lift_reason = f"{UNVERIFIED_REASON}: {why}"
    vector_bytes = max(len(r.record), len(h.record)) * 8
    if lift_reason is None and vector_bytes > spec.panel_max_bytes:
      lift_reason = (f"the reference hash side input would be "
                     f"{_mb(vector_bytes)}, above the "
                     f"{_mb(spec.panel_max_bytes)} cap per panel array")
    near_reason = lift_reason
    cell_bytes = max(r.cells.nbytes, h.cells.nbytes)
    if near_reason is None and len(spec.nonkey_columns) < _MIN_NEAR_COLUMNS:
      near_reason = _FEW_COLUMNS
    if near_reason is None and cell_bytes > spec.panel_max_bytes:
      near_reason = (f"the reference cell matrix would be {_mb(cell_bytes)}, "
                     f"above the {_mb(spec.panel_max_bytes)} cap per panel "
                     "array")
    if lift_reason is not None:
      return replace(
          empty,
          lift_reason=lift_reason,
          near_reason=near_reason,
          r_freqs=r_freqs)
    cells = (None, None) if near_reason is not None else (r.cells, h.cells)
    arrays = (r.record, r.row, r.key, h.record, h.row, h.key,
              *(c for c in cells if c is not None))
    return cls(
        table=table.name,
        token=_token(table.name, spec.salt, arrays),
        r_record=r.record,
        r_row=r.row,
        r_key=r.key,
        h_record=h.record,
        h_row=h.row,
        h_key=h.key,
        r_cells=cells[0],
        h_cells=cells[1],
        e_n=panel.e_n,
        he_n=panel.he_n,
        lift_reason=None,
        near_reason=near_reason,
        r_freqs=r_freqs)


class _PanelSide(NamedTuple):
  record: np.ndarray
  row: np.ndarray
  key: np.ndarray
  cells: np.ndarray


def _encode_panel(table: TablePlan, spec: MembershipSpec, side: Side,
                  rows: Sequence[Mapping[str, Any]]) -> _PanelSide:
  """(record hashes, row hashes, source-key codes, cell codes) of panel
  rows, in rank order."""
  if not rows:
    d = len(spec.nonkey_columns)
    return _PanelSide(_EMPTY_CODES, _EMPTY_CODES, _EMPTY_CODES,
                      np.zeros((0, d), dtype=np.uint64))
  batch = BatchEncoder.from_table(
      table, side, salt=spec.salt, subsample_rate=1.0).encode(rows)
  record = _record(spec, batch)
  key = {
      "pk": batch.pk_hash,
      "identity": batch.identity_hash
  }.get(spec.code_kind, record)
  return _PanelSide(record, batch.row_hash, key, batch.h_nonkey)


def _record_freqs(record: np.ndarray, null_content: int) -> FreqOfFreqs:
  """The frequency of frequencies of the records with content."""
  content = record[record != np.uint64(null_content)]
  if content.size == 0:
    return ()
  _, counts = np.unique(content, return_counts=True)
  return _freqs(counts)


def _token(table: str, salt: str, arrays: Iterable[np.ndarray]) -> str:
  """A digest of the panel's content: the `Shared` tag of its index."""
  digest = hashlib.blake2b(f"{table}\x1f{salt}".encode(), digest_size=16)
  for array in arrays:
    digest.update(np.ascontiguousarray(array).tobytes())
  return digest.hexdigest()


@dataclass(frozen=True, eq=False)
class _Loo:
  """One side's leave-one-out keys: per column j, the sorted distinct keys
  `keys[offsets[j]:offsets[j + 1]]`, the rank of each key's first row, the
  side's cell codes for the exact check, and `only` (the key is absent
  from the twin side's keys at the same column)."""
  keys: np.ndarray
  offsets: np.ndarray
  first: np.ndarray
  cells: np.ndarray
  only: np.ndarray

  @classmethod
  def build(cls, cells: np.ndarray, totals: np.ndarray, a: np.ndarray,
            null_loo: np.ndarray) -> _Loo:
    loo = loo_hashes(cells, a, totals) if cells.size else np.zeros(
        cells.shape, dtype=np.uint64)
    keys, firsts, offsets = [], [], [0]
    for j in range(cells.shape[1]):
      unique, first = _first_unique(loo[:, j], loo[:, j] != null_loo[j])
      keys.append(unique)
      firsts.append(first)
      offsets.append(offsets[-1] + len(unique))
    return cls(
        keys=np.concatenate(keys) if keys else _EMPTY_CODES,
        offsets=np.array(offsets, dtype=np.int64),
        first=np.concatenate(firsts) if firsts else _EMPTY_POS,
        cells=cells,
        only=np.ones(offsets[-1], dtype=bool))

  def column(self, j: int) -> np.ndarray:
    return self.keys[self.offsets[j]:self.offsets[j + 1]]

  def exclusive_of(self, twin: _Loo) -> _Loo:
    """This side with `only` set against `twin`, column by column."""
    only = np.concatenate([
        ~np.isin(self.column(j), twin.column(j))
        for j in range(len(self.offsets) - 1)
    ]) if len(self.offsets) > 1 else np.zeros(
        0, dtype=bool)
    return replace(self, only=only)


class PanelStats(NamedTuple):
  """The panel's exposures and reasons, for the emitter (slim ints)."""
  n_r: int
  n_h: int
  e_n: int
  he_n: int
  lift_reason: str | None
  near_reason: str | None
  t_e: int  # |E \ H|
  t_r_rest: int  # |(R \ H) \ E|
  t_he: int  # |H_E \ R|
  t_h_rest: int  # |(H \ R) \ H_E|
  t_near_r: int  # R's leave-one-out keys H lacks at the same column
  t_near_h: int
  r_freqs: FreqOfFreqs | None


@dataclass(eq=False)
class PanelIndex:  # pylint: disable=too-many-instance-attributes  # one field per lookup structure
  """The panel's lookup structures, built from `PanelRefs` once per worker
  (`Shared`) and on the driver for `stats` (the same function, so both
  agree). Position arrays index `u_r`/`u_h` (distinct record hashes with
  content); `r_class`/`h_class` classify each position: 0 not exclusive,
  1 exclusive and in the exposed prefix (E \\ H, H_E \\ R), 2 exclusive
  outside it."""
  exact: bool
  near: bool
  u_r: np.ndarray
  r_first: np.ndarray
  r_class: np.ndarray
  u_h: np.ndarray
  h_first: np.ndarray
  h_class: np.ndarray
  r_rows: np.ndarray
  h_rows: np.ndarray
  r_key: np.ndarray
  h_key: np.ndarray
  e_n: int
  a_nonkey: np.ndarray
  loo_r: _Loo | None
  loo_h: _Loo | None
  stats: PanelStats

  @classmethod
  def build(cls, spec: MembershipSpec, refs: PanelRefs) -> PanelIndex:
    null = np.uint64(spec.null_record)
    u_r, r_first = _first_unique(refs.r_record, refs.r_record != null)
    u_h, h_first = _first_unique(refs.h_record, refs.h_record != null)
    r_class = _classes(u_r, u_h, refs.r_record[:refs.e_n])
    h_class = _classes(u_h, u_r, refs.h_record[:refs.he_n])
    a = multipliers(spec.nonkey_columns, spec.salt)
    loo_r = loo_h = None
    if refs.r_cells is not None and refs.h_cells is not None:
      null_loo = loo_hashes(
          np.full((1, len(spec.nonkey_columns)), NULL_CODE, dtype=np.uint64), a,
          np.array([spec.null_content], dtype=np.uint64))[0]
      r_side = _Loo.build(refs.r_cells, refs.r_record, a, null_loo)
      h_side = _Loo.build(refs.h_cells, refs.h_record, a, null_loo)
      loo_r, loo_h = r_side.exclusive_of(h_side), h_side.exclusive_of(r_side)
    stats = PanelStats(
        n_r=len(refs.r_record),
        n_h=len(refs.h_record),
        e_n=refs.e_n,
        he_n=refs.he_n,
        lift_reason=refs.lift_reason,
        near_reason=refs.near_reason,
        t_e=int((r_class == _EXPOSED).sum()),
        t_r_rest=int((r_class == _REST).sum()),
        t_he=int((h_class == _EXPOSED).sum()),
        t_h_rest=int((h_class == _REST).sum()),
        t_near_r=int(loo_r.only.sum()) if loo_r is not None else 0,
        t_near_h=int(loo_h.only.sum()) if loo_h is not None else 0,
        r_freqs=refs.r_freqs)
    return cls(
        exact=refs.lift_reason is None,
        near=loo_r is not None,
        u_r=u_r,
        r_first=r_first,
        r_class=r_class,
        u_h=u_h,
        h_first=h_first,
        h_class=h_class,
        r_rows=np.unique(refs.r_row),
        h_rows=np.unique(refs.h_row),
        r_key=refs.r_key,
        h_key=refs.h_key,
        e_n=refs.e_n,
        a_nonkey=a,
        loo_r=loo_r,
        loo_h=loo_h,
        stats=stats)


def _classes(own: np.ndarray, twin: np.ndarray,
             exposed: np.ndarray) -> np.ndarray:
  """0 = also held by the twin set, 1 = exclusive and in the exposed
  prefix, 2 = exclusive outside it."""
  exclusive = ~np.isin(own, twin)
  in_prefix = np.isin(own, exposed)
  out: np.ndarray = np.where(exclusive, np.where(in_prefix, _EXPOSED, _REST),
                             _SHARED).astype(np.int8)
  return out


# --------------------------------------------------------------------------
# the full-source sets (side-input mode)
# --------------------------------------------------------------------------
@dataclass(frozen=True, eq=False)
class SourceSets:
  """The distinct source hashes of one table, sorted: the record hashes
  (rows with content) and, for a keyed table with content, the row hashes
  (all-NULL rows left out)."""
  record: np.ndarray
  row: np.ndarray | None


def source_hashes(spec: MembershipSpec, batch: EncodedBatch) -> SourceSets:
  """One source batch's distinct hashes (the combine's input)."""
  record = _record(spec, batch)
  rows = None
  if spec.keyed and spec.nonkey_columns:
    rh = batch.row_hash
    rows = np.unique(rh[rh != np.uint64(spec.null_row)])
  return SourceSets(
      np.unique(record[record != np.uint64(spec.null_record)]), rows)


def _codes_held(sets: SourceSets) -> int:
  return len(sets.record) + (0 if sets.row is None else len(sets.row))


class SourceSetsCombineFn(beam.CombineFn):
  """Distinct sorted union of `SourceSets` parts; compacts lazily."""

  def create_accumulator(self) -> list[SourceSets]:
    return []

  def add_input(self, mutable_accumulator: list[SourceSets],
                element: SourceSets) -> list[SourceSets]:
    mutable_accumulator.append(element)
    if sum(_codes_held(s) for s in mutable_accumulator) > _COMPACT_CODES:
      return [self._union(mutable_accumulator)]
    return mutable_accumulator

  def merge_accumulators(
      self, accumulators: Iterable[list[SourceSets]]) -> list[SourceSets]:
    merged = [part for acc in accumulators for part in acc]
    return [self._union(merged)] if len(merged) > 1 else merged

  def compact(self, accumulator: list[SourceSets]) -> list[SourceSets]:
    return [self._union(accumulator)] if len(accumulator) > 1 else accumulator

  def extract_output(self, accumulator: list[SourceSets]) -> SourceSets:
    return self._union(accumulator)

  @staticmethod
  def _union(parts: Sequence[SourceSets]) -> SourceSets:
    record = np.unique(
        np.concatenate([_EMPTY_CODES, *(p.record for p in parts)]))
    rows = [p.row for p in parts if p.row is not None]
    row = np.unique(np.concatenate(rows)) if rows else None
    return SourceSets(record.astype(np.uint64), row)


# --------------------------------------------------------------------------
# one synthetic batch against the panel and the source sets
# --------------------------------------------------------------------------
@dataclass
class MembershipAcc:  # pylint: disable=too-many-instance-attributes  # one field per count
  """Membership counts of synthetic rows (mergeable: sums, and unions of
  panel positions). `hits_*` are distinct exclusive panel records hit
  (positions in the index's u_r/u_h, split exposed / rest); `near_hits_*`
  distinct exclusive leave-one-out keys hit by a verified near match."""
  n: int = 0
  src_nonkey: int = 0
  src_row: int = 0
  rows_r: int = 0
  rows_h: int = 0
  rows_r_only: int = 0
  rows_h_only: int = 0
  rows_full_r: int = 0
  near_rows_r: int = 0
  near_rows_h: int = 0
  hits_e: np.ndarray = field(default_factory=lambda: _EMPTY_POS)
  hits_r_rest: np.ndarray = field(default_factory=lambda: _EMPTY_POS)
  hits_he: np.ndarray = field(default_factory=lambda: _EMPTY_POS)
  hits_h_rest: np.ndarray = field(default_factory=lambda: _EMPTY_POS)
  near_hits_r: np.ndarray = field(default_factory=lambda: _EMPTY_POS)
  near_hits_h: np.ndarray = field(default_factory=lambda: _EMPTY_POS)

  def merge(self, other: MembershipAcc) -> MembershipAcc:
    values: dict[str, Any] = {}
    for f in fields(self):
      mine, theirs = getattr(self, f.name), getattr(other, f.name)
      values[f.name] = (
          _union(mine, theirs) if isinstance(mine, np.ndarray) else mine +
          theirs)
    return MembershipAcc(**values)


@dataclass(frozen=True)
class _Candidate:  # pylint: disable=too-many-instance-attributes  # one field per flag column
  """A flag before ranking: ordered by (prio, spread, key_text), distinct
  by (spread, source_code) — the spread is a bijection of the row hash.
  A dataclass, not a NamedTuple, so Beam never infers a schema coder for
  its dict fields (it pickles)."""
  prio: int
  spread: int
  key_text: str
  source_set: str
  source_code: int | None
  synthetic_key: dict[str, Any] | None
  distance: float
  detail: dict[str, Any]


def _flag_order(candidate: _Candidate) -> tuple[int, int, str]:
  return candidate.prio, candidate.spread, candidate.key_text


def _top(candidates: Iterable[_Candidate], k: int) -> list[_Candidate]:
  """The `k` best DISTINCT candidates: one per (synthetic row hash,
  source code), so identical copies of one record take one slot."""
  best: dict[tuple[int, int | None], _Candidate] = {}
  for candidate in sorted(candidates, key=_flag_order):
    best.setdefault((candidate.spread, candidate.source_code), candidate)
  return sorted(best.values(), key=_flag_order)[:k]


class _Near(NamedTuple):
  rows: np.ndarray  # bool (n,): a verified near match
  column: np.ndarray  # the first dropped column it matched at
  rank: np.ndarray  # the matched row's rank
  key: np.ndarray  # the matched key's position (`_Loo.keys`)
  hits: np.ndarray  # distinct exclusive key positions hit


def _near(loo: _Loo, keys: np.ndarray, cells: np.ndarray,
          eligible: np.ndarray) -> _Near:
  """Verified leave-one-out matches of a batch's rows against one side."""
  n = len(eligible)
  rows = np.zeros(n, dtype=bool)
  column = np.full(n, -1, dtype=np.int64)
  rank = np.full(n, -1, dtype=np.int64)
  key = np.full(n, -1, dtype=np.int64)
  hits = []
  for j in range(keys.shape[1]):
    lo = int(loo.offsets[j])
    pos, found = _lookup(loo.column(j), keys[:, j], eligible)
    cand = np.nonzero(found)[0]
    if not cand.size:
      continue
    matched = loo.first[lo + pos[cand]]
    equal = cells[cand] == loo.cells[matched]
    equal[:, j] = True
    ok = equal.all(axis=1)
    rows_ok, ranks_ok = cand[ok], matched[ok]
    key_pos = lo + pos[rows_ok]
    hits.append(key_pos[loo.only[key_pos]])
    fresh = ~rows[rows_ok]
    rows[rows_ok[fresh]] = True
    column[rows_ok[fresh]] = j
    rank[rows_ok[fresh]] = ranks_ok[fresh]
    key[rows_ok[fresh]] = key_pos[fresh]
  merged = np.unique(np.concatenate(hits)) if hits else _EMPTY_POS
  return _Near(rows, column, rank, key, merged.astype(np.int64))


def _synthetic_key(spec: MembershipSpec, batch: EncodedBatch,
                   i: int) -> tuple[dict[str, Any] | None, str]:
  """Row i's synthetic handle (PK, else identity) as a dict — INT64 parts
  as ints — and its JSON."""
  if spec.handle_text is None:
    return None, ""
  key: dict[str, Any] = {}
  for name, slot, is_int in zip(
      spec.handle, spec.handle_text, spec.handle_int, strict=True):
    text = batch.text[slot][i]
    key[name] = int(text) if is_int and text is not None else text
  return key, json.dumps(key, sort_keys=True)


def _spread(spec: MembershipSpec, row_hash: np.ndarray) -> np.ndarray:
  return _mix64(row_hash ^ np.uint64(hash64(_SPREAD_LABEL, spec.salt)))


class _Picked(NamedTuple):
  """The chosen rows' priorities, sets, source-key codes, distance and
  details, aligned with the rows passed to `_candidates`."""
  prio: np.ndarray
  sets: Sequence[str]
  codes: Sequence[int | None]
  distance: float
  details: Sequence[dict[str, Any]]


def _choose(rows: np.ndarray, prio: np.ndarray, codes: np.ndarray,
            row_hash: np.ndarray, spread: np.ndarray, top_k: int) -> np.ndarray:
  """The positions (into `rows`) of the `top_k` best DISTINCT candidates
  by (priority, spread): the first row of each (row hash, code) pair, then
  one sort — so a batch full of copies costs no Python loop per row."""
  pairs = np.stack([row_hash[rows], codes], axis=1)
  _, first = np.unique(pairs, axis=0, return_index=True)
  first = np.sort(first)
  order = np.lexsort((spread[rows[first]], prio[first]))[:top_k]
  chosen: np.ndarray = first[order]
  return chosen


def _candidates(spec: MembershipSpec, batch: EncodedBatch, rows: np.ndarray,
                picked: _Picked, spread: np.ndarray) -> list[_Candidate]:
  """The chosen `rows` as flag candidates."""
  out = []
  for o, i in enumerate(rows.tolist()):
    key, key_text = _synthetic_key(spec, batch, i)
    out.append(
        _Candidate(
            prio=int(picked.prio[o]),
            spread=int(spread[i]),
            key_text=key_text,
            source_set=picked.sets[o],
            source_code=picked.codes[o],
            synthetic_key=key,
            distance=picked.distance,
            detail=picked.details[o]))
  return out


class _Matches(NamedTuple):
  """One batch's record lookups: panel positions and hits, source hits."""
  record: np.ndarray
  content: np.ndarray
  pos_r: np.ndarray
  in_r: np.ndarray
  pos_h: np.ndarray
  in_h: np.ndarray
  r_class: np.ndarray
  src_rec: np.ndarray
  src_row: np.ndarray | None


def _matches(spec: MembershipSpec, index: PanelIndex,
             sources: SourceSets | None, batch: EncodedBatch) -> _Matches:
  n, rh = batch.n, batch.row_hash
  record = _record(spec, batch)
  content = record != np.uint64(spec.null_record)
  src_rec = np.zeros(n, dtype=bool)
  src_row = None
  if sources is not None:
    src_rec = content & _member(sources.record, record)
    if sources.row is not None:
      src_row = _member(sources.row, rh) & (rh != np.uint64(spec.null_row))
    else:  # key-less or all-key: the record IS the row
      src_row = src_rec
  pos_r, in_r = _lookup(index.u_r, record, content & index.exact)
  pos_h, in_h = _lookup(index.u_h, record, content & index.exact)
  r_class = index.r_class[pos_r] if index.u_r.size else np.zeros(n, np.int8)
  return _Matches(record, content, pos_r, in_r, pos_h, in_h, r_class, src_rec,
                  src_row)


def batch_membership(
    spec: MembershipSpec, index: PanelIndex, sources: SourceSets | None,
    batch: EncodedBatch,
    top_k: int) -> tuple[MembershipAcc, dict[str, list[_Candidate]]]:
  """One synthetic batch's membership counts and its best flag candidates
  per check (module docstring). `sources` is None in keyed-count mode."""
  m = _matches(spec, index, sources, batch)
  acc = MembershipAcc(n=batch.n)
  if sources is not None:
    acc.src_nonkey = int(m.src_rec.sum()) if spec.nonkey_columns else 0
    acc.src_row = int(m.src_row.sum()) if m.src_row is not None else 0
  if index.exact and spec.nonkey_columns:
    h_class = index.h_class[m.pos_h] if index.u_h.size else np.zeros(
        batch.n, np.int8)
    acc.rows_r, acc.rows_h = int(m.in_r.sum()), int(m.in_h.sum())
    acc.rows_r_only = int((m.in_r & (m.r_class != _SHARED)).sum())
    acc.rows_h_only = int((m.in_h & (h_class != _SHARED)).sum())
    acc.rows_full_r = int(
        (_member(index.r_rows, batch.row_hash) & m.content).sum())
    acc.hits_e = _positions(m.pos_r, m.in_r & (m.r_class == _EXPOSED))
    acc.hits_r_rest = _positions(m.pos_r, m.in_r & (m.r_class == _REST))
    acc.hits_he = _positions(m.pos_h, m.in_h & (h_class == _EXPOSED))
    acc.hits_h_rest = _positions(m.pos_h, m.in_h & (h_class == _REST))
  spread = _spread(spec, batch.row_hash)
  out = {EXACT_COPY: _exact_candidates(spec, index, batch, m, spread, top_k)}
  if index.near and index.loo_r is not None and index.loo_h is not None:
    keys = loo_hashes(batch.h_nonkey, index.a_nonkey, batch.nonkey_hash)
    # a row matching an R or an H record exactly is exact, never near
    eligible = m.content & ~(m.in_r | m.in_h)
    near_r = _near(index.loo_r, keys, batch.h_nonkey, eligible)
    near_h = _near(index.loo_h, keys, batch.h_nonkey, eligible)
    acc.near_rows_r, acc.near_rows_h = (int(near_r.rows.sum()),
                                        int(near_h.rows.sum()))
    acc.near_hits_r, acc.near_hits_h = near_r.hits, near_h.hits
    out[NEAR_COPY] = _near_candidates(spec, index, batch, (near_r, near_h),
                                      spread, top_k)
  return acc, out


def _exact_candidates(spec: MembershipSpec, index: PanelIndex,
                      batch: EncodedBatch, m: _Matches, spread: np.ndarray,
                      top_k: int) -> list[_Candidate]:
  """The best distinct flag candidates among the synthetic rows that copy
  a record, each labelled with the first set of E (an exclusive E record),
  R, H, the full source it matches (`SOURCE_SETS`)."""
  rows = np.nonzero(m.in_r | m.in_h | m.src_rec)[0]
  if not rows.size:
    return []
  in_e = m.in_r & (m.r_class == _EXPOSED)
  prio = np.where(
      in_e, _PRIO_E,
      np.where(m.in_r, _PRIO_R, np.where(m.in_h, _PRIO_H, _PRIO_SOURCE)))[rows]
  codes = np.zeros(len(rows), dtype=np.uint64)
  from_r, from_h = prio <= _PRIO_R, prio == _PRIO_H
  if from_r.any():
    codes[from_r] = index.r_key[index.r_first[m.pos_r[rows[from_r]]]]
  if from_h.any():
    codes[from_h] = index.h_key[index.h_first[m.pos_h[rows[from_h]]]]
  labelled = spec.code_kind == "record"  # a full-source copy's own record
  if labelled:
    codes[prio == _PRIO_SOURCE] = m.record[rows[prio == _PRIO_SOURCE]]
  chosen = _choose(rows, prio, codes, batch.row_hash, spread, top_k)
  rows, prio, codes = rows[chosen], prio[chosen], codes[chosen]
  rh = batch.row_hash[rows]
  full_r = _member(index.r_rows, rh)
  full_h = _member(index.h_rows, rh)
  sets: list[str] = []
  labels: list[int | None] = []
  details: list[dict[str, Any]] = []
  for o, (i, p) in enumerate(zip(rows.tolist(), prio.tolist(), strict=True)):
    sets.append(SOURCE_SETS[p])
    if p == _PRIO_SOURCE:
      labels.append(int(codes[o]) if labelled else None)
      full = m.src_row is not None and bool(m.src_row[i])
    else:
      labels.append(int(codes[o]))
      full = bool(full_h[o] if p == _PRIO_H else full_r[o])
    details.append({"full_row": full})
  return _candidates(spec, batch, rows, _Picked(prio, sets, labels, 0.0,
                                                details), spread)


def _near_candidates(spec: MembershipSpec, index: PanelIndex,
                     batch: EncodedBatch, near: tuple[_Near, _Near],
                     spread: np.ndarray, top_k: int) -> list[_Candidate]:
  """The best distinct flag candidates among the synthetic rows that
  near-copy a panel record (an exclusive E key first, then R, then H),
  each naming the column that differs."""
  near_r, near_h = near
  assert index.loo_r is not None
  rows = np.nonzero(near_r.rows | near_h.rows)[0]
  if not rows.size:
    return []
  from_r = near_r.rows[rows]
  rank = np.where(from_r, near_r.rank[rows], near_h.rank[rows])
  column = np.where(from_r, near_r.column[rows], near_h.column[rows])
  exclusive = np.zeros(len(rows), dtype=bool)
  exclusive[from_r] = index.loo_r.only[near_r.key[rows[from_r]]]
  prio = np.where(from_r,
                  np.where(exclusive & (rank < index.e_n), _PRIO_E, _PRIO_R),
                  _PRIO_H)
  codes = np.zeros(len(rows), dtype=np.uint64)
  codes[from_r] = index.r_key[rank[from_r]]
  codes[~from_r] = index.h_key[rank[~from_r]]
  chosen = _choose(rows, prio, codes, batch.row_hash, spread, top_k)
  rows, prio, codes, column = (rows[chosen], prio[chosen], codes[chosen],
                               column[chosen])
  details = [{"differs_in": spec.nonkey_columns[j]} for j in column.tolist()]
  return _candidates(
      spec, batch, rows,
      _Picked(prio, [SOURCE_SETS[p] for p in prio.tolist()],
              [int(c) for c in codes.tolist()], 1.0 / len(spec.nonkey_columns),
              details), spread)


# --------------------------------------------------------------------------
# keyed counts (all tables): packed sparse parts
# --------------------------------------------------------------------------
Packed = tuple[int, bytes, bytes]  # (side, codes <u8 bytes, counts bytes)


def _pack(side: int, codes: np.ndarray, counts: np.ndarray) -> Packed:
  """One side's sorted codes and counts as primitives: the codes as
  little-endian uint64 bytes, the counts at the smallest unsigned width
  that holds them."""
  top = int(counts.max()) if counts.size else 0
  width = 8
  for candidate in (1, 2, 4):
    if top < 1 << (8 * candidate):
      width = candidate
      break
  return (side, np.ascontiguousarray(codes, dtype="<u8").tobytes(),
          np.ascontiguousarray(counts, dtype=_COUNT_DTYPES[width]).tobytes())


def _unpack(part: Packed) -> tuple[int, np.ndarray, np.ndarray]:
  side, codes_bytes, counts_bytes = part
  n = len(codes_bytes) // 8
  codes = np.frombuffer(codes_bytes, dtype="<u8").astype(np.uint64)
  width = len(counts_bytes) // n if n else 1
  counts = np.frombuffer(
      counts_bytes, dtype=_COUNT_DTYPES[width]).astype(np.int64)
  return side, codes, counts


def _merge_side(
    parts: Sequence[tuple[np.ndarray, np.ndarray]]
) -> tuple[np.ndarray, np.ndarray]:
  """Sorted distinct codes with their summed counts."""
  if len(parts) == 1:
    return parts[0]
  codes = np.concatenate([p[0] for p in parts])
  unique, inverse = np.unique(codes, return_inverse=True)
  counts = np.bincount(
      inverse,
      weights=np.concatenate([p[1] for p in parts]),
      minlength=len(unique))
  return unique.astype(np.uint64), counts.astype(np.int64)


def _merge_parts(
    parts: Iterable[tuple[int, np.ndarray, np.ndarray]]) -> list[Packed]:
  """Raw (side, codes, counts) parts merged to one packed part per side."""
  by_side: dict[int, list[tuple[np.ndarray, np.ndarray]]] = {}
  for side, codes, counts in parts:
    if codes.size:
      by_side.setdefault(side, []).append((codes, counts))
  return [
      _pack(side, *_merge_side(items))
      for side, items in sorted(by_side.items())
  ]


def _bucketed(
    codes: np.ndarray, bits: int,
    side: int) -> Iterator[tuple[int, tuple[int, np.ndarray, np.ndarray]]]:
  """(bucket, (side, sorted codes, counts)) of one batch's codes."""
  if codes.size == 0:
    return
  unique, counts = np.unique(codes, return_counts=True)
  counts = counts.astype(np.int64)
  if bits == 0:
    yield 0, (side, unique, counts)
    return
  bucket = (unique >> np.uint64(64 - bits)).astype(np.int64)
  edges = np.searchsorted(bucket, np.arange(2**bits + 1))
  for b in np.nonzero(np.diff(edges))[0].tolist():
    lo, hi = edges[b], edges[b + 1]
    yield b, (side, unique[lo:hi], counts[lo:hi])


@dataclass(frozen=True)
class KeyStats:  # pylint: disable=too-many-instance-attributes  # one field per keyed-count total
  """One table's keyed-count totals (sums over sides and buckets); the
  frequency of frequencies of each side's content and of both sides
  pooled (R73)."""
  n_src: int = 0
  n_syn: int = 0
  null_src: int = 0  # rows with no record content (R60)
  null_syn: int = 0
  pk_null: int = 0  # synthetic rows whose PK has a NULL part
  id_null: int = 0
  nk_dup_src: int = 0  # rows in a non-key hash counted twice or more
  nk_dup_syn: int = 0
  nk_match_syn: int = 0  # synthetic rows whose non-key hash the source holds
  pk_dup: int = 0
  pk_distinct: int = 0
  id_dup: int = 0
  id_distinct: int = 0
  row_match_syn: int = 0  # keyed mode: synthetic rows a source row matches
  freqs_src: FreqOfFreqs = ()
  freqs_syn: FreqOfFreqs = ()
  freqs_pool: FreqOfFreqs = ()  # both sides' rows together (the R73 null)

  def plus(self, other: KeyStats) -> KeyStats:
    values: dict[str, Any] = {}
    for f in fields(self):
      mine, theirs = getattr(self, f.name), getattr(other, f.name)
      values[f.name] = (
          _freqs_sum(mine, theirs) if isinstance(mine, tuple) else mine +
          theirs)
    return KeyStats(**values)


def key_counts(
    spec: MembershipSpec, batch: EncodedBatch
) -> tuple[list[tuple[tuple[str, str, int], tuple[int, np.ndarray,
                                                  np.ndarray]]], KeyStats]:
  """One batch's raw keyed-count parts `((table, kind, bucket), (side,
  codes, counts))` and its row totals (module docstring);
  REFERENCE/HOLDOUT batches count nothing."""
  if batch.side not in _COUNTED_SIDES:
    return [], KeyStats()
  source = batch.side is Side.SOURCE
  side = _SRC if source else _SYN
  table, bits = spec.table, spec.bucket_bits
  out: list[tuple[tuple[str, str, int], tuple[int, np.ndarray,
                                              np.ndarray]]] = []
  nulls = 0
  if spec.nonkey_columns:
    nk = batch.nonkey_hash
    content = nk != np.uint64(spec.null_content)
    nulls = int((~content).sum())
    out += [((table, "nk", b), p) for b, p in _bucketed(nk[content], bits, side)
           ]
  pk_null = id_null = 0
  if not source:
    for kind, cols, codes in (("pk", spec.pk, batch.pk_hash),
                              ("id", spec.identity, batch.identity_hash)):
      if not cols:
        continue
      present = codes != np.uint64(NULL_CODE)
      if kind == "pk":
        pk_null = int((~present).sum())
      else:
        id_null = int((~present).sum())
      out += [((table, kind, b), p)
              for b, p in _bucketed(codes[present], bits, side)]
  if spec.source_mode == KEYED_COUNT and spec.keyed:
    rh = batch.row_hash
    out += [
        ((table, "row", b), p)
        for b, p in _bucketed(rh[rh != np.uint64(spec.null_row)], bits, side)
    ]
  n = batch.n
  totals = KeyStats(
      n_src=n if source else 0,
      n_syn=0 if source else n,
      null_src=nulls if source else 0,
      null_syn=0 if source else nulls,
      pk_null=pk_null,
      id_null=id_null)
  return out, totals


@dataclass(frozen=True, eq=False)
class SideCounts:
  """A bucket's exact counts per side: sorted codes and their counts."""
  src_codes: np.ndarray
  src_counts: np.ndarray
  syn_codes: np.ndarray
  syn_counts: np.ndarray


def _held(accumulator: Sequence[Packed]) -> int:
  return sum(len(part[1]) // 8 for part in accumulator)


class KeyCountsCombineFn(beam.CombineFn):
  """Exact per-code counts of one bucket from packed sparse parts; the
  accumulator is a list of packed parts, and `compact` (Beam calls it on
  a lifted accumulator before the shuffle) merges it to one part per
  side, so a code crosses the shuffle at most once per side and bundle."""

  def create_accumulator(self) -> list[Packed]:
    return []

  def add_input(self, mutable_accumulator: list[Packed],
                element: Packed) -> list[Packed]:
    mutable_accumulator.append(element)
    if _held(mutable_accumulator) > _COMPACT_CODES:
      return self.compact(mutable_accumulator)
    return mutable_accumulator

  def merge_accumulators(self,
                         accumulators: Iterable[list[Packed]]) -> list[Packed]:
    return self.compact([part for acc in accumulators for part in acc])

  def compact(self, accumulator: list[Packed]) -> list[Packed]:
    if len(accumulator) <= 1:
      return accumulator
    return _merge_parts(_unpack(part) for part in accumulator)

  def extract_output(self, accumulator: list[Packed]) -> SideCounts:
    sides = {_SRC: (_EMPTY_CODES, _EMPTY_POS), _SYN: (_EMPTY_CODES, _EMPTY_POS)}
    for part in self.compact(accumulator):
      side, codes, counts = _unpack(part)
      sides[side] = (codes, counts)
    return SideCounts(*sides[_SRC], *sides[_SYN])  # pylint: disable=no-value-for-parameter  # four positional fields


def _stats_of(kind: str, counts: SideCounts) -> KeyStats:
  """One bucket's exact counts as `KeyStats` terms."""
  src, syn = counts.src_counts, counts.syn_counts
  if kind == "nk":
    pooled = _merge_side([(counts.src_codes, src), (counts.syn_codes, syn)])[1]
    return KeyStats(
        nk_dup_src=int(src[src >= _DUPLICATED].sum()),
        nk_dup_syn=int(syn[syn >= _DUPLICATED].sum()),
        nk_match_syn=int(syn[np.isin(counts.syn_codes,
                                     counts.src_codes)].sum()),
        freqs_src=_freqs(src),
        freqs_syn=_freqs(syn),
        freqs_pool=_freqs(pooled))
  if kind == "pk":
    return KeyStats(
        pk_dup=int(syn[syn >= _DUPLICATED].sum()),
        pk_distinct=int((syn > 0).sum()))
  if kind == "id":
    return KeyStats(
        id_dup=int(syn[syn >= _DUPLICATED].sum()),
        id_distinct=int((syn > 0).sum()))
  return KeyStats(
      row_match_syn=int(syn[np.isin(counts.syn_codes, counts.src_codes)].sum()))


def _stats_entry(
    item: tuple[tuple[str, str, int], SideCounts]) -> tuple[str, KeyStats]:
  (table, kind, _), counts = item
  return table, _stats_of(kind, counts)


class _SumStatsCombineFn(beam.CombineFn):
  """Sums `KeyStats` (one table's totals)."""

  def create_accumulator(self) -> KeyStats:
    return KeyStats()

  def add_input(self, mutable_accumulator: KeyStats,
                element: KeyStats) -> KeyStats:
    return mutable_accumulator.plus(element)

  def merge_accumulators(self, accumulators: Iterable[KeyStats]) -> KeyStats:
    return functools.reduce(KeyStats.plus, accumulators, KeyStats())

  def extract_output(self, accumulator: KeyStats) -> KeyStats:
    return accumulator


class RowKeysFn(beam.DoFn):
  """`EncodedBatch` → packed keyed-count parts `((table, kind, bucket),
  part)` (main), row totals (tagged `stats`) and per-table failures
  (tagged `failed`), for every table in `specs` (a table in `skip`
  failed on the driver: its batches are ignored).

  The batches of a bundle are merged per key before anything is emitted
  (flushed once `flush_codes` codes are held, and in `finish_bundle`), so
  an element carries many codes, not a batch's handful per bucket."""

  def __init__(self,
               specs: Mapping[str, MembershipSpec],
               *,
               skip: Iterable[str] = (),
               flush_codes: int = FLUSH_CODES):
    super().__init__()
    self._specs = dict(specs)
    self._skip = frozenset(skip)
    bits = max((s.bucket_bits for s in self._specs.values()), default=0)
    self._flush_at = max(flush_codes, _FLUSH_PER_BUCKET << bits)
    self._pending: dict[tuple[str, str, int], list[tuple[int, np.ndarray,
                                                         np.ndarray]]] = {}
    self._held = 0

  def start_bundle(self) -> None:
    self._pending, self._held = {}, 0

  def process(self, element: EncodedBatch) -> Iterator[Any]:
    if element.table in self._skip:
      return
    spec = self._specs.get(element.table)
    if spec is None:
      raise ValueError(f"no membership spec for table {element.table!r}")
    try:
      parts, totals = key_counts(spec, element)
    except WORKER_ERRORS as exc:
      yield beam.pvalue.TaggedOutput(_FAILED, (spec.table, _failure(exc)))
      return
    if element.side in _COUNTED_SIDES:
      yield beam.pvalue.TaggedOutput(_STATS, (spec.table, totals))
    for key, part in parts:
      self._pending.setdefault(key, []).append(part)
      self._held += len(part[1])
    if self._held >= self._flush_at:
      yield from self._flush(windowed=False)

  def finish_bundle(self) -> Iterator[Any]:
    yield from self._flush(windowed=True)

  def _flush(self, *, windowed: bool) -> Iterator[Any]:
    pending, self._pending, self._held = self._pending, {}, 0
    for key, parts in pending.items():
      for packed in _merge_parts(parts):
        value = (key, packed)
        yield GlobalWindows.windowed_value(value) if windowed else value


# --------------------------------------------------------------------------
# duplicates at matched n (R73)
# --------------------------------------------------------------------------
def _classes_of(
    freqs: Iterable[tuple[int, int]]) -> tuple[np.ndarray, np.ndarray, int]:
  """(counts c, their frequencies f_c, rows N = Σ c·f_c) as arrays."""
  pairs = [(c, f) for c, f in freqs if c > 0 and f > 0]
  c = np.array([p[0] for p in pairs], dtype=np.float64)
  f = np.array([p[1] for p in pairs], dtype=np.float64)
  return c, f, int((c * f).sum())


def rarefied_duplicates(freqs: Iterable[tuple[int, int]], m: int) -> float:
  """E[D_m] of a side whose content rows have the frequency of
  frequencies `freqs` ((c, f_c): f_c records held c times, N = Σ c·f_c
  rows) subsampled without replacement to `m` rows: the rows of the
  subsample whose record it holds twice or more, Σ_c f_c · (c·m/N -
  P(X = 1)) with X ~ Hypergeometric(N, c, m) (Hurlbert, 1971; Heck, van
  Belle & Simberloff, 1975). At m ≥ N, the observed rows."""
  c, f, total = _classes_of(freqs)
  if m <= 0 or total == 0:
    return 0.0
  if m >= total:
    return float((c * f)[c >= _DUPLICATED].sum())
  p1 = _p_one(c, total, m)
  return max(0.0, float((f * (m * c / total - p1)).sum()))


def _log_terms(q: int, offset: int, total: int, start: int,
               stop: int) -> np.ndarray:
  """log1p(-q / (total - offset - u)) for u in [start, stop): the exact
  per-step log-ratios of the hypergeometric products below (-inf once
  fewer than q rows would remain)."""
  u = np.arange(start, stop, dtype=np.float64)
  denom = total - offset - u
  with np.errstate(divide="ignore", invalid="ignore"):
    ratio = np.where(denom > 0, q / np.where(denom > 0, denom, 1.0), np.inf)
    steps: np.ndarray = np.where(ratio < 1.0, np.log1p(-np.minimum(ratio, 1.0)),
                                 -np.inf)
  return steps


def _log_steps(q: int, offset: int, count: int, total: int) -> np.ndarray:
  """[0, Σ_{u<k} log1p(-q / (total - offset - u)) for k = 1..count]: the
  running sum as a dense table (`count` ≤ PAIR_TABLE_CAP), never a
  difference of log-gamma values of size N·ln N."""
  out = np.zeros(count + 1)
  np.cumsum(_log_terms(q, offset, total, 0, count), out=out[1:])
  return out


def _log_steps_at(q: int, offset: int, total: int,
                  ks: np.ndarray) -> np.ndarray:
  """The same running sum at each k of `ks` (any order), walked in chunks
  of _WALK_CHUNK terms up to max(ks): exact to the rounding of each term,
  with memory bounded whatever the largest k — a heavy record's count
  or a pair sum past the dense table."""
  ks = np.asarray(ks, dtype=np.int64)
  out = np.zeros(len(ks))
  if q == 0 or not ks.size:
    return out  # every factor is 1
  order = np.argsort(ks, kind="stable")
  wanted = ks[order]
  values = np.zeros(len(ks))
  lo = int(np.searchsorted(wanted, 1))  # k = 0: the empty sum
  acc, start, last = 0.0, 0, int(wanted[-1])
  while start < last:
    if acc == -np.inf:  # a zero factor: every later product is 0
      values[lo:] = -np.inf
      break
    stop = min(start + _WALK_CHUNK, last)
    running = acc + np.cumsum(_log_terms(q, offset, total, start, stop))
    hi = int(np.searchsorted(wanted, stop, side="right"))
    values[lo:hi] = running[wanted[lo:hi] - start - 1]
    lo, acc, start = hi, float(running[-1]), stop
  out[order] = values
  return out


def _one_steps(c: np.ndarray, total: int, m: int) -> np.ndarray:
  """A(c - 1) = log(P(X = 1) / (c m / N)) per count c, X ~
  Hypergeometric(N = total, c, m): the running log sum of Π_{u < c - 1}
  (1 - (m - 1) / (N - 1 - u)). Each factor is at most exp(-(m - 1) / N),
  so a count whose bound (c m / N) · exp(-(c - 1)(m - 1) / N) is below
  1e-15 is -inf without walking its product."""
  out = np.full(len(c), -np.inf)
  if not c.size:
    return out
  live = np.log(c * m / total) - (c - 1.0) * (m - 1) / total > _LOG_SKIP
  if live.any():
    out[live] = _log_steps_at(m - 1, 1, total, c[live].astype(np.int64) - 1)
  return out


def _p_one(c: np.ndarray, total: int, m: int) -> np.ndarray:
  """P(X = 1) = c · C(N - c, m - 1) / C(N, m) per count c (`_one_steps`)."""
  out: np.ndarray = np.exp(np.log(c * m / total) + _one_steps(c, total, m))
  return out


def duplicate_null_variance(freqs_pool: Iterable[tuple[int, int]],
                            m: int) -> float:
  """Var(D_m) of an m-row subsample, without replacement, of the POOLED
  rows of both sides (frequency of frequencies `freqs_pool`): the null of
  `row.internal_duplicate_excess`, under which source and synthetic rows
  are exchangeable and each side is a random m-subset of the pool.

  D_m = m - F1 (F1 = the rows of records held once), so Var(D_m) =
  Var(F1) = Σ_x a_x (1 - a_x) + Σ_{x≠y} a_x a_y (rho_xy - 1), with a_x =
  P(X_x = 1) (hypergeometric), b_xy = P(X_x = 1, X_y = 1) =
  c_x c_y C(N - c_x - c_y, m - 2) / C(N, m) (multivariate
  hypergeometric) and rho_xy = b_xy / (a_x a_y), summed over the count
  classes. A heavy record (always duplicated) adds nothing; a class with
  P(X = 1) below 1e-12 is dropped.

  Exactness: with A(k) and B(k) the running sums of the exact per-step
  log-ratios of the two hypergeometric products (`_log_steps`),
  log rho = log1p(-(N - m) / (m (N - 1))) + B(c_x + c_y - 2) - A(c_x - 1)
  - A(c_y - 1) and rho - 1 = expm1(log rho): never a difference of log-gamma
  values of size N·ln N, nor of b and a a (both of size m²), whose
  rounding swamps the variance at millions of rows. B is a dense table
  for sums up to PAIR_TABLE_CAP; a pair sum past it (a record held more than
  about a million times) is walked on from the same running sum, at the
  distinct sums needed only — the whole pair sum never switches formula
  (Ruling R79). Held to Fraction/comb references (1e-9 relative) at
  N ≈ 1e9 with records held 2-3 million times, to a simulation, and to
  the exact 0 of an all-distinct pool.

  Memory: the pair sum runs in row blocks of _PAIR_BLOCK class pairs,
  and the table and the walks hold at most PAIR_TABLE_CAP / _WALK_CHUNK
  float64s (8 MB an array): a call peaks at about 60 MiB whatever the
  number of classes or the heaviest record (Ruling R76). Before the walk
  was chunked, a record held about 2^21 times spiked 78-200 MiB."""
  c, f, total = _classes_of(freqs_pool)
  if m <= 1 or total <= m:
    return 0.0
  steps_a = _one_steps(c, total, m)
  a = np.exp(np.log(c * m / total) + steps_a)
  kept = a > _NEGLIGIBLE
  c, f, a, steps_a = c[kept], f[kept], a[kept], steps_a[kept]
  if not c.size:
    return 0.0
  counts = c.astype(np.int64)
  fa = f * a
  s_max = 2 * int(counts.max())
  top = min(s_max, PAIR_TABLE_CAP)
  table = _log_steps(m - 2, _PAIR_OFFSET, max(0, top - _PAIR_OFFSET), total)
  step = max(1, _PAIR_BLOCK // len(c))  # rows per block: bounded memory
  blocks = [slice(lo, lo + step) for lo in range(0, len(c), step)]
  large: np.ndarray | None = None
  large_steps = np.zeros(0)
  if s_max > top:  # sums past the table: walked on, at the ones needed
    found = [2 * counts[2 * counts > top]]
    for rows in blocks:
      sums = counts[rows, None] + counts[None, :]
      found.append(np.unique(sums[sums > top]))
    large = np.unique(np.concatenate(found))
    large_steps = _log_steps_at(m - 2, _PAIR_OFFSET, total,
                                large - _PAIR_OFFSET)

  def steps_b(sums: np.ndarray) -> np.ndarray:
    out: np.ndarray = table[np.minimum(sums, top) - _PAIR_OFFSET]
    if large is not None:
      past = sums > top
      if past.any():
        out = out.copy()
        out[past] = large_steps[np.searchsorted(large, sums[past])]
    return out

  const = math.log1p(-(total - m) / (m * (total - 1.0)))

  def rho_minus_one(sums: np.ndarray, a_i: np.ndarray,
                    a_j: np.ndarray) -> np.ndarray:
    out: np.ndarray = np.expm1(const + steps_b(sums) - a_i - a_j)
    return out

  pair = 0.0
  for rows in blocks:
    pair += float((fa[rows, None] * fa[None, :] *
                   rho_minus_one(counts[rows, None] + counts[None, :],
                                 steps_a[rows, None], steps_a[None, :])).sum())
  # the x = y terms: a class's pairs with itself are f (f - 1), not f²
  diagonal = float((fa * a * rho_minus_one(2 * counts, steps_a, steps_a)).sum())
  variance = float((fa * (1.0 - a)).sum()) + pair - diagonal
  return max(0.0, variance)


def _pooled_effective_n(pooled_share: float, var_d: float, m: int) -> float:
  """ONE effective sample size for both sides (Ruling R76): Korn &
  Graubard's n* = p̄ (1 - p̄) / var_d at the POOLED rarefied share p̄,
  capped at m (a design effect never below 1) and never below 1. A
  per-side n* would read a near-unique source's handful of pairs as a
  tiny sample and swallow a real excess."""
  n_eff = float(m)
  if 0.0 < pooled_share < 1.0 and var_d > 0.0:
    n_eff = min(float(m), pooled_share * (1.0 - pooled_share) / var_d)
  return max(1.0, n_eff)


class DuplicateExcess(NamedTuple):
  """`duplicate_excess`'s result: the excess and its interval, the matched
  n, each side's rarefied duplicate rows, the pooled share, the null
  variance of a side's share and the one effective n (R73, R76)."""
  value: float
  ci_low: float
  ci_high: float
  m: int
  d_src: float
  d_syn: float
  pooled_share: float
  var_share: float
  n_eff: float


def duplicate_excess(freqs_src: FreqOfFreqs, freqs_syn: FreqOfFreqs,
                     freqs_pool: FreqOfFreqs) -> DuplicateExcess:
  """`row.internal_duplicate_excess` from each side's content frequency
  of frequencies and the pooled one (module docstring): both sides
  rarefied to m = min(n_src, n_syn), Newcombe's interval on ONE effective
  n from the pooled share and the pooled null variance (the pool's
  finite-population factor removed), with unrounded effective counts.

  Raises:
    ValueError: a side has no content row (m would be 0).
  """
  n_src = sum(c * f for c, f in freqs_src)
  n_syn = sum(c * f for c, f in freqs_syn)
  m = min(n_src, n_syn)
  if m <= 0:
    raise ValueError("duplicate_excess needs content rows on both sides")
  d_src = rarefied_duplicates(freqs_src, m)
  d_syn = rarefied_duplicates(freqs_syn, m)
  # each side varies like an m-row sample of the population, not of the
  # pool: the pool's finite-population factor is removed (Cochran, 1977)
  var_share = duplicate_null_variance(freqs_pool, m) / (
      (1.0 - m / (n_src + n_syn)) * m * m)
  pooled_share = rarefied_duplicates(freqs_pool, m) / m
  n_eff = _pooled_effective_n(pooled_share, var_share, m)
  lo, hi = noise.newcombe_diff_interval(d_syn / m * n_eff, n_eff,
                                        d_src / m * n_eff, n_eff)
  return DuplicateExcess((d_syn - d_src) / m, lo, hi, m, d_src, d_syn,
                         pooled_share, var_share, n_eff)


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------
class _Emitter:
  """Collects one table's `MetricValue`s."""

  def __init__(self, spec: MembershipSpec):
    self.spec = spec
    self.rows: list[MetricValue] = []

  def value(self, metric_id: str, value: float | None, **fields_: Any) -> None:
    fields_.setdefault("method", Method.EXACT)
    self.rows.append(
        MetricValue(
            metric_id=metric_id,
            table=self.spec.table,
            value=value,
            encoding_plan_digest=self.spec.encoding_plan_digest,
            **fields_))

  def skip(self, metric_id: str, reason: str, **detail: Any) -> None:
    self.rows.append(
        _skipped(self.spec.table, self.spec.encoding_plan_digest, metric_id,
                 reason, detail))


def _skipped(table: str, digest: str, metric_id: str, reason: str,
             detail: Mapping[str, Any]) -> MetricValue:
  return MetricValue(
      metric_id=metric_id,
      table=table,
      value=None,
      method=Method.EXACT,
      encoding_plan_digest=digest,
      detail={
          "reason": reason,
          **detail
      })


def _failed_metrics(table: str, digest: str, reason: str) -> list[MetricValue]:
  """Every owned id `not_evaluated` with a failed table's reason."""
  return [_skipped(table, digest, i, reason, {}) for i in OWNED_METRIC_IDS]


def _sampling(spec: MembershipSpec) -> dict[str, Any]:
  """A synthetic row sample's `method`/`sample_rate` fields (R72)."""
  if spec.rate_synthetic >= 1.0:
    return {}
  return {"method": Method.SAMPLE, "sample_rate": spec.rate_synthetic}


def _share(e: _Emitter, metric_id: str, k: int, n: int, *, n_source: int,
           detail: Mapping[str, Any]) -> None:
  lo, hi = noise.wilson_interval(k, n)
  sampled = _sampling(e.spec)
  notes = dict(detail)
  if sampled:
    notes["sample_rate"] = sampled["sample_rate"]
  e.value(
      metric_id,
      k / n,
      ci_low=lo,
      ci_high=hi,
      n_source=n_source,
      n_synthetic=n,
      detail=notes,
      **sampled)


def _exact_rates(e: _Emitter, acc: MembershipAcc, keys: KeyStats) -> None:
  spec = e.spec
  side_input = spec.source_mode == SIDE_INPUT
  for metric_id in ("row.exact_match_rate", "row.exact_match_rate_nonkey"):
    nonkey = metric_id.endswith("_nonkey")
    if nonkey and not spec.nonkey_columns:
      e.skip(metric_id, _NO_CONTENT)
      continue
    if keys.n_syn <= 0:
      e.skip(metric_id, _NO_SYNTHETIC)
      continue
    if keys.n_src <= 0:
      e.skip(metric_id, _NO_SOURCE)
      continue
    if nonkey:
      k = acc.src_nonkey if side_input else keys.nk_match_syn
    elif side_input:
      k = acc.src_row
    else:
      k = keys.row_match_syn if spec.keyed else keys.nk_match_syn
    if spec.rate_source < 1.0:
      e.skip(
          metric_id,
          _SAMPLED_SOURCE.format(rate=spec.rate_source),
          matches_lower_bound=k,
          sample_rate_source=spec.rate_source)
      continue
    detail: dict[str, Any] = {
        "matches": k,
        "path": spec.source_mode,
        "path_note": spec.source_note,
    }
    if nonkey:
      detail["null_content_rows"] = keys.null_syn
    _share(e, metric_id, k, keys.n_syn, n_source=keys.n_src, detail=detail)


def _rate_ratio_detail(m1: int, t1: int, m2: int, t2: int,
                       names: tuple[str, str]) -> dict[str, Any]:
  """A secondary lift for `detail`: no rate ratio exists when either
  exclusive set is empty, so it then says why instead."""
  out: dict[str, Any] = {
      f"events_{names[0]}": m1,
      f"events_{names[1]}": m2,
      f"exposed_{names[0]}": t1,
      f"exposed_{names[1]}": t2,
  }
  if t1 <= 0 or t2 <= 0:
    return {
        "lift": None,
        "ci_low": None,
        "ci_high": None,
        "reason": ("an exclusive set outside the exposed prefix is empty "
                   "(every exclusive record of that half is exposed): no "
                   "rate ratio"),
        **out
    }
  ratio, lo, hi = noise.rate_ratio(m1, t1, m2, t2, alpha=LIFT_ALPHA)
  return {
      "lift": ratio,
      "ci_low": lo,
      "ci_high": None if math.isinf(hi) else hi,
      **out
  }


def _lift(e: _Emitter, metric_id: str, counts: tuple[int, int, int, int],
          names: tuple[str, str], n_syn: int, detail: Mapping[str,
                                                              Any]) -> None:
  m1, t1, m2, t2 = counts
  if t1 <= 0 or t2 <= 0:
    set_a, set_b = ("R", "H") if names[0] == "r" else ("E", "H_E")
    e.skip(metric_id, _NO_EXPOSURE.format(a=set_a, b=set_b), **{
        f"exposed_{names[0]}": t1,
        f"exposed_{names[1]}": t2
    })
    return
  ratio, lo, hi = noise.rate_ratio(m1, t1, m2, t2, alpha=LIFT_ALPHA)
  sampled = _sampling(e.spec)
  notes: dict[str, Any] = {
      f"events_{names[0]}": m1,
      f"events_{names[1]}": m2,
      f"exposed_{names[0]}": t1,
      f"exposed_{names[1]}": t2,
      "unit": "distinct exclusive reference records",
      "alpha": LIFT_ALPHA,
      **detail,
  }
  if sampled:
    notes["sample_rate"] = sampled["sample_rate"]
  e.value(
      metric_id,
      ratio,
      ci_low=lo,
      ci_high=None if math.isinf(hi) else hi,
      n_synthetic=n_syn,
      detail=notes,
      **sampled)


def _panel_reason(spec: MembershipSpec, stats: PanelStats, keys: KeyStats,
                  near: bool) -> str | None:
  if not spec.nonkey_columns:
    return _NO_CONTENT
  if near and len(spec.nonkey_columns) < _MIN_NEAR_COLUMNS:
    return _FEW_COLUMNS
  reason = stats.near_reason if near else stats.lift_reason
  if reason is not None:
    return reason
  if keys.n_syn <= 0:
    return _NO_SYNTHETIC
  return None


def _lifts(e: _Emitter, stats: PanelStats, acc: MembershipAcc,
           keys: KeyStats) -> None:
  reason = _panel_reason(e.spec, stats, keys, near=False)
  if reason is not None:
    for metric_id in ("row.memorization_lift", "row.exposure_lift"):
      e.skip(metric_id, reason)
    return
  e_hits, rest_r = len(acc.hits_e), len(acc.hits_r_rest)
  he_hits, rest_h = len(acc.hits_he), len(acc.hits_h_rest)
  _lift(
      e, "row.memorization_lift",
      (e_hits + rest_r, stats.t_e + stats.t_r_rest, he_hits + rest_h,
       stats.t_he + stats.t_h_rest), ("r", "h"), keys.n_syn, {
           "rows_r": acc.rows_r_only,
           "rows_h": acc.rows_h_only,
           "rows_matching_r": acc.rows_r,
           "rows_matching_h": acc.rows_h,
           "full_row_matches_r": acc.rows_full_r,
       })
  _lift(
      e, "row.exposure_lift", (e_hits, stats.t_e, he_hits, stats.t_he),
      ("e", "he"), keys.n_syn, {
          "e_n":
              stats.e_n,
          "he_n":
              stats.he_n,
          "unexposed":
              _rate_ratio_detail(rest_r, stats.t_r_rest, rest_h, stats.t_h_rest,
                                 ("r", "h")),
      })


def _near_metrics(e: _Emitter, stats: PanelStats, acc: MembershipAcc,
                  keys: KeyStats) -> None:
  reason = _panel_reason(e.spec, stats, keys, near=True)
  if reason is not None:
    for metric_id in ("row.near_match_rate", "row.near_match_lift"):
      e.skip(metric_id, reason)
    return
  _share(
      e,
      "row.near_match_rate",
      acc.near_rows_r,
      keys.n_syn,
      n_source=stats.n_r,
      detail={
          "near_rows_r": acc.near_rows_r,
          "near_rows_h": acc.near_rows_h
      })
  _lift(
      e, "row.near_match_lift",
      (len(acc.near_hits_r), stats.t_near_r, len(
          acc.near_hits_h), stats.t_near_h), ("r", "h"), keys.n_syn, {
              "unit": "distinct exclusive leave-one-out keys",
              "rows_r": acc.near_rows_r,
              "rows_h": acc.near_rows_h,
          })


def _duplicates(e: _Emitter, stats: PanelStats, keys: KeyStats) -> None:
  metric_id = "row.internal_duplicate_excess"
  spec = e.spec
  if not spec.nonkey_columns:
    e.skip(metric_id, _NO_CONTENT)
    return
  n_syn, n_src = keys.n_syn - keys.null_syn, keys.n_src - keys.null_src
  observed = {
      "duplicate_rows_synthetic": keys.nk_dup_syn,
      "duplicate_rows_source": keys.nk_dup_src,
  }
  for side, rate in (("source", spec.rate_source), ("synthetic",
                                                    spec.rate_synthetic)):
    if rate < 1.0:
      e.skip(metric_id, _SAMPLED_DUPLICATES.format(side=side, rate=rate),
             **observed)
      return
  if n_syn <= 0 or n_src <= 0:
    side = "synthetic" if n_syn <= 0 else "source"
    e.skip(metric_id, f"no {side} row with non-key content was read")
    return
  x = duplicate_excess(keys.freqs_src, keys.freqs_syn, keys.freqs_pool)
  m = x.m
  detail: dict[str, Any] = {
      **observed,
      "null_content_rows_synthetic": keys.null_syn,
      "null_content_rows_source": keys.null_src,
      "matched_n": m,
      "rarefied_duplicate_rows_synthetic": x.d_syn,
      "rarefied_duplicate_rows_source": x.d_src,
      "null_sd_share": math.sqrt(x.var_share),
      "pooled_share": x.pooled_share,
      "effective_n": x.n_eff,
      "counted_on": "exact rarefaction to matched n (Ruling R73)",
  }
  baseline = None
  if stats.r_freqs:
    n_r = sum(c * f for c, f in stats.r_freqs)
    m_r = min(n_r, n_src)
    baseline = (rarefied_duplicates(stats.r_freqs, m_r) -
                rarefied_duplicates(keys.freqs_src, m_r)) / m_r
  else:
    detail["baseline_reason"] = ("no reference sample (the R panel)"
                                 if stats.r_freqs is None else
                                 "no reference row with non-key content")
  e.value(
      metric_id,
      x.value,
      source_value=x.d_src / m,
      synthetic_value=x.d_syn / m,
      baseline_value=baseline,
      ci_low=x.ci_low,
      ci_high=x.ci_high,
      n_source=n_src,
      n_synthetic=n_syn,
      detail=detail)


def _key_duplicates(e: _Emitter, keys: KeyStats) -> None:
  spec = e.spec
  for metric_id, what, cols, dup, distinct, nulls, missing in (
      ("table.pk_duplicate_rate", "primary-key", spec.pk, keys.pk_dup,
       keys.pk_distinct, keys.pk_null, _NO_PK),
      ("table.identity_duplicate_rate", "identity", spec.identity, keys.id_dup,
       keys.id_distinct, keys.id_null, _NO_IDENTITY),
  ):
    if not cols:
      e.skip(metric_id, missing)
      continue
    if keys.n_syn <= 0:
      e.skip(metric_id, _NO_SYNTHETIC)
      continue
    if spec.rate_synthetic < 1.0:
      e.skip(
          metric_id,
          _SAMPLED_KEYS.format(what=what, rate=spec.rate_synthetic),
          duplicate_rows_lower_bound=dup,
          null_key_rows=nulls)
      continue
    e.value(
        metric_id,
        dup / keys.n_syn,
        n_synthetic=keys.n_syn,
        detail={
            "duplicate_rows": dup,
            "distinct_keys": distinct,
            "null_key_rows": nulls,
            "key_columns": list(cols),
            "null_key_note": _NULL_KEY_NOTE,
        })


def table_outputs(spec: MembershipSpec, stats: PanelStats,
                  acc: MembershipAcc | None,
                  keys: KeyStats | None) -> list[MetricValue]:
  """One table's membership metrics: a row per owned id, a value or
  `not_evaluated` with a reason, in `OWNED_METRIC_IDS` order."""
  acc = acc or MembershipAcc()
  keys = keys or KeyStats()
  e = _Emitter(spec)
  _exact_rates(e, acc, keys)
  _lifts(e, stats, acc, keys)
  _near_metrics(e, stats, acc, keys)
  _duplicates(e, stats, keys)
  _key_duplicates(e, keys)
  order = {metric_id: i for i, metric_id in enumerate(OWNED_METRIC_IDS)}
  return sorted(e.rows, key=lambda mv: order[mv.metric_id])


# --------------------------------------------------------------------------
# flags
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RowFlag:  # pylint: disable=too-many-instance-attributes  # the flag table's fields
  """One `evaluation_row_flags` row before the run's ids are attached
  (`flag_row`). Keys only: the synthetic handle (PK, else identity — the
  synthetic table's own key, which for a full-row copy is the copied
  source row's key; D6 hashing does not cover it) and a keyed label of
  the matched source record's code; never an attribute value. A check
  with no matched source record (Task 24's `detectable`) has no
  `source_set` and no label."""
  table: str
  check: str
  rank: int
  synthetic_key: Mapping[str, Any] | None
  source_key_hash: str | None
  source_set: str | None
  distance: float | None
  score: float | None
  detail: Mapping[str, Any] = field(default_factory=dict)


def _flags(
    item: tuple[tuple[str, str], Iterable[_Candidate]],
    label_key: bytes,
    failed: Iterable[tuple[str, str]] = ()) -> list[RowFlag]:
  """One (table, check)'s ranked flags, labelled with the worker's key; a
  failed table writes none."""
  (table, check), candidates = item
  if not isinstance(label_key, bytes) or not label_key:
    raise ValueError("the label key side input must be non-empty bytes (R64)")
  if table in {name for name, _ in failed}:
    return []
  return [
      RowFlag(
          table=table,
          check=check,
          rank=rank,
          synthetic_key=c.synthetic_key,
          source_key_hash=(None if c.source_code is None else hashed_label(
              c.source_code, key=label_key)),
          source_set=c.source_set,
          distance=c.distance,
          score=1.0 - c.distance,
          detail=c.detail)
      for rank, c in enumerate(sorted(candidates, key=_flag_order), start=1)
  ]


def _utc(evaluated_at: datetime | str) -> str:
  """`evaluated_at` as UTC ISO-8601 with microseconds (naive = UTC), as
  `scoring.to_metric_row` writes it."""
  if isinstance(evaluated_at, str):
    evaluated_at = datetime.fromisoformat(evaluated_at)
  return str(canonical_value(evaluated_at))


def flag_row(flag: RowFlag, *, evaluation_id: str,
             evaluated_at: datetime | str) -> dict[str, Any]:
  """One `evaluation_row_flags` row: exactly its fields, in schema order,
  JSON-safe. `source_key` is always NULL (keyed source-key hashes only,
  D6/R64; `row_flags_source_keys=raw` is refused by the plan's knobs).

  Raises:
    ValueError: `evaluated_at` is a string that is not ISO-8601.
  """
  row = {
      "evaluation_id": evaluation_id,
      "evaluated_at": _utc(evaluated_at),
      "table_name": flag.table,
      "check": flag.check,
      "rank": flag.rank,
      "synthetic_key":
          (None if flag.synthetic_key is None else dict(flag.synthetic_key)),
      "source_key_hash": flag.source_key_hash,
      "source_key": None,
      "source_set": flag.source_set,
      "distance": flag.distance,
      "score": flag.score,
      "detail": dict(flag.detail) or None,
  }
  safe: dict[str, Any] = json_safe(row)
  return safe


class _TopFlagsCombineFn(beam.CombineFn):
  """The `k` best distinct candidates of one (table, check) (`_top`)."""

  def __init__(self, k: int):
    super().__init__()
    self._k = k

  def create_accumulator(self) -> list[_Candidate]:
    return []

  def add_input(self, mutable_accumulator: list[_Candidate],
                element: _Candidate) -> list[_Candidate]:
    mutable_accumulator.append(element)
    if len(mutable_accumulator) > 2 * self._k:
      return _top(mutable_accumulator, self._k)
    return mutable_accumulator

  def merge_accumulators(
      self, accumulators: Iterable[list[_Candidate]]) -> list[_Candidate]:
    return _top([c for acc in accumulators for c in acc], self._k)

  def compact(self, accumulator: list[_Candidate]) -> list[_Candidate]:
    return _top(accumulator, self._k)

  def extract_output(self, accumulator: list[_Candidate]) -> list[_Candidate]:
    return _top(accumulator, self._k)


# --------------------------------------------------------------------------
# in process (the reference implementation the Beam path is held to)
# --------------------------------------------------------------------------
class MembershipResult(NamedTuple):
  metrics: list[MetricValue]
  flags: list[RowFlag]


def membership_outputs(
    table: TablePlan,
    batches: Iterable[EncodedBatch],
    *,
    salt: str,
    label_key: bytes,
    row_flags_top_k: int = 100,
    panel_max_bytes: int = PANEL_SET_MAX_BYTES,
    source_set_max_bytes: int = SOURCE_SET_MAX_BYTES) -> MembershipResult:
  """The whole membership pass of one table in one process. As in Beam,
  an error while the spec and the panel index are built (the driver's
  part, `TABLE_ERRORS`) or a data error in the rest (a worker's part,
  `WORKER_ERRORS`) makes the table not_evaluated; a `MemoryError` in the
  worker's part is raised."""

  def failed(exc: Exception) -> MembershipResult:
    return MembershipResult(
        _failed_metrics(table.name, table.encoding_plan_digest, _failure(exc)),
        [])

  try:
    spec = MembershipSpec.from_table(
        table,
        salt=salt,
        panel_max_bytes=panel_max_bytes,
        source_set_max_bytes=source_set_max_bytes)
    index = PanelIndex.build(spec, PanelRefs.from_table(table, spec))
  except TABLE_ERRORS as exc:
    return failed(exc)
  try:
    return _membership_outputs(
        table, spec, index, batches, label_key=label_key, top_k=row_flags_top_k)
  except WORKER_ERRORS as exc:
    return failed(exc)


def _membership_outputs(table: TablePlan, spec: MembershipSpec,
                        index: PanelIndex, batches: Iterable[EncodedBatch], *,
                        label_key: bytes, top_k: int) -> MembershipResult:
  batches = [b for b in batches if b.table == table.name]
  sources = None
  if spec.source_mode == SIDE_INPUT:
    combine = SourceSetsCombineFn()
    parts = combine.create_accumulator()
    for batch in batches:
      if batch.side is Side.SOURCE:
        parts = combine.add_input(parts, source_hashes(spec, batch))
    sources = combine.extract_output(parts)
  keys = KeyStats()
  counts = KeyCountsCombineFn()
  buckets: dict[tuple[str, str, int], list[Packed]] = {}
  for batch in batches:
    elements, totals = key_counts(spec, batch)
    keys = keys.plus(totals)
    for key, part in elements:
      for packed in _merge_parts([part]):
        buckets[key] = counts.add_input(buckets.get(key, []), packed)
  for (_, kind, _), acc_ in sorted(buckets.items(), key=lambda kv: kv[0]):
    keys = keys.plus(_stats_of(kind, counts.extract_output(acc_)))
  acc = MembershipAcc()
  best: dict[str, list[_Candidate]] = {}
  for batch in batches:
    if batch.side is not Side.SYNTHETIC:
      continue
    batch_acc, candidates = batch_membership(spec, index, sources, batch, top_k)
    acc = acc.merge(batch_acc)
    for check, items in candidates.items():
      best[check] = _top([*best.get(check, []), *items], top_k)
  flags = [
      flag for check in sorted(best)
      for flag in _flags(((table.name, check), best[check]), label_key)
  ]
  return MembershipResult(table_outputs(spec, index.stats, acc, keys), flags)


# --------------------------------------------------------------------------
# Beam
# --------------------------------------------------------------------------
class MembershipFn(beam.DoFn):
  """Synthetic `EncodedBatch` → `(table, MembershipAcc)`, tagged flag
  candidates `((table, check), candidate)` and tagged failures `(table,
  reason)`. The panel index is built once per worker from the panel side
  input (`Shared`, tagged by its content token) and kept alive by the
  DoFn; other sides pass through untouched."""

  def __init__(self, spec: MembershipSpec, top_k: int):
    super().__init__()
    self._spec = spec
    self._top_k = top_k
    self._shared = Shared()
    self._index: PanelIndex | None = None

  def process(self,
              element: EncodedBatch,
              refs: PanelRefs,
              sources: SourceSets | None = None) -> Iterator[Any]:
    if element.side is not Side.SYNTHETIC:
      return
    spec = self._spec
    try:
      self._index = self._shared.acquire(
          functools.partial(PanelIndex.build, spec, refs), tag=refs.token)
      acc, candidates = batch_membership(spec, self._index, sources, element,
                                         self._top_k)
    except WORKER_ERRORS as exc:
      yield beam.pvalue.TaggedOutput(_FAILED, (spec.table, _failure(exc)))
      return
    yield spec.table, acc
    for check, items in candidates.items():
      for candidate in items:
        yield beam.pvalue.TaggedOutput(_FLAGS, ((spec.table, check), candidate))


class _SourceHashesFn(beam.DoFn):
  """Source `EncodedBatch` → its distinct `SourceSets`; tagged failures."""

  def __init__(self, spec: MembershipSpec):
    super().__init__()
    self._spec = spec

  def process(self, element: EncodedBatch) -> Iterator[Any]:
    if element.side is not Side.SOURCE:
      return
    try:
      yield source_hashes(self._spec, element)
    except WORKER_ERRORS as exc:
      yield beam.pvalue.TaggedOutput(_FAILED, (self._spec.table, _failure(exc)))


class MembershipCombineFn(beam.CombineFn):
  """Merges `MembershipAcc`s (sums and position unions)."""

  def create_accumulator(self) -> MembershipAcc:
    return MembershipAcc()

  def add_input(self, mutable_accumulator: MembershipAcc,
                element: MembershipAcc) -> MembershipAcc:
    return mutable_accumulator.merge(element)

  def merge_accumulators(
      self, accumulators: Iterable[MembershipAcc]) -> MembershipAcc:
    return functools.reduce(MembershipAcc.merge, accumulators, MembershipAcc())

  def extract_output(self, accumulator: MembershipAcc) -> MembershipAcc:
    return accumulator


class _ByTable(beam.PartitionFn):
  """The partition of a batch: its table's index in `names`; a table that
  failed on the driver goes to the last partition, which nothing reads."""

  def __init__(self, names: Sequence[str], failed: Iterable[str]):
    super().__init__()
    self._names = tuple(names)
    self._failed = frozenset(failed)

  def partition_for(self, element: Any, num_partitions: int, *args: Any,
                    **kwargs: Any) -> int:
    del args, kwargs  # no extra partition arguments
    if element.table in self._failed:
      return num_partitions - 1
    if element.table not in self._names:
      raise ValueError(f"no membership spec for table {element.table!r}")
    return self._names.index(element.table)


def _tagged(item: tuple[str, Any], tag: str) -> tuple[str, tuple[str, Any]]:
  table, value = item
  return table, (tag, value)


def _emit_table(item: tuple[str, Iterable[tuple[str, Any]]],
                specs: Mapping[str, MembershipSpec], stats: Mapping[str,
                                                                    PanelStats],
                failed: Mapping[str, str], digests: Mapping[str,
                                                            str]) -> list[Any]:
  """One table's metrics — or, when the table failed anywhere (the
  driver, a worker, or here), every owned id not_evaluated with the
  reason; a failure HERE is also tagged `failed`, so the flags step drops
  the table's flags as it does for an earlier failure."""
  table, entries = item
  acc, keys, reasons = None, None, []
  for tag, value in entries:
    if tag == _ACC:
      acc = value
    elif tag == _KEYS:
      keys = value
    elif tag == _FAILED:
      reasons.append(value)
  if table in failed:
    reasons.append(failed[table])
  spec = specs.get(table)
  if spec is None or reasons:
    return _failed_metrics(table, digests[table], sorted(reasons)[0])
  try:
    return list(table_outputs(spec, stats[table], acc, keys))
  except WORKER_ERRORS as exc:
    reason = _failure(exc)
    out: list[Any] = _failed_metrics(table, spec.encoding_plan_digest, reason)
    return [*out, beam.pvalue.TaggedOutput(_FAILED, (table, reason))]


class Membership(beam.PTransform):
  """`PCollection[EncodedBatch]` (every table, every side) → `{"metrics":
  PCollection[MetricValue], "flags": PCollection[RowFlag]}` (module
  docstring).

  `label_key` is the one-element key PCollection `beam.label_key.LabelKey`
  makes on a worker, read as a side input when the flags are labelled, so
  the key never enters the job graph (Rulings R64, R68). `salt` must be
  the salt the batches were encoded with (the plan's). The panel hashes
  are built here, on the driver, and shipped as one side input per table
  — embedded in the job graph, or, given a `staging`, as one object per
  table the workers read (`beam.staged`, Ruling R154);
  only each table's slim `MembershipSpec` is pickled into the DoFns. A
  table whose spec or panel cannot be built (`TABLE_ERRORS`) is not
  evaluated, with the reason; the others run.

  Raises:
    TypeError: `label_key` is not a PCollection (bytes here would be
      pickled into the graph).
  """

  def __init__(self,
               tables: Sequence[TablePlan],
               *,
               salt: str,
               label_key: beam.PCollection,
               row_flags_top_k: int = 100,
               panel_max_bytes: int = PANEL_SET_MAX_BYTES,
               source_set_max_bytes: int = SOURCE_SET_MAX_BYTES,
               staging: staged.Staging | None = None):
    super().__init__()
    self._staging = staging
    if not isinstance(label_key, beam.PCollection):
      raise TypeError("label_key must be the LabelKey PCollection, never "
                      "bytes: a constructor argument is pickled into the job "
                      "graph (Ruling R68)")
    self._label_key = label_key
    self._top_k = row_flags_top_k
    self._all = tuple(table.name for table in tables)
    self._digests = {t.name: t.encoding_plan_digest for t in tables}
    self._specs: dict[str, MembershipSpec] = {}
    self._refs: dict[str, PanelRefs] = {}
    self._stats: dict[str, PanelStats] = {}
    self._failed: dict[str, str] = {}
    for table in tables:
      try:
        spec = MembershipSpec.from_table(
            table,
            salt=salt,
            panel_max_bytes=panel_max_bytes,
            source_set_max_bytes=source_set_max_bytes)
        refs = PanelRefs.from_table(table, spec)
        stats = PanelIndex.build(spec, refs).stats
      except TABLE_ERRORS as exc:
        self._failed[table.name] = _failure(exc)
        continue
      self._specs[table.name] = spec
      self._refs[table.name] = refs
      self._stats[table.name] = stats

  def expand(self, input_or_inputs: beam.PCollection) -> dict[str, Any]:
    batches = input_or_inputs
    p = batches.pipeline
    specs = self._specs
    names = tuple(name for name in self._all if name in specs)
    keyed = batches | "RowKeys" >> beam.ParDo(
        RowKeysFn(specs, skip=self._failed)).with_outputs(
            _STATS, _FAILED, main=_COUNTS)
    key_stats = ((
        keyed[_COUNTS]
        | "SumKeys" >> beam.CombinePerKey(KeyCountsCombineFn())
        | "KeyStats" >> beam.Map(_stats_entry),
        keyed[_STATS],
    )
                 | "FlattenKeyStats" >> beam.Flatten()
                 | "SumKeyStats" >> beam.CombinePerKey(_SumStatsCombineFn()))
    parts = batches | "ByTable" >> beam.Partition(
        _ByTable(names, self._failed),
        len(names) + 1)
    accs, candidates, failures = [], [], [keyed[_FAILED]]
    for i, name in enumerate(names):
      spec = specs[name]
      refs = beam.pvalue.AsSingleton(
          staged.create(p, f"Panel[{name}]", [self._refs[name]], self._staging))
      side_inputs: list[Any] = [refs]
      if spec.source_mode == SIDE_INPUT:
        hashed = parts[i] | f"SourceHashes[{name}]" >> beam.ParDo(
            _SourceHashesFn(spec)).with_outputs(
                _FAILED, main=_COUNTS)
        failures.append(hashed[_FAILED])
        sets = hashed[_COUNTS] | f"SourceSets[{name}]" >> beam.CombineGlobally(
            SourceSetsCombineFn())
        side_inputs.append(beam.pvalue.AsSingleton(sets))
      out = parts[i] | f"Membership[{name}]" >> beam.ParDo(
          MembershipFn(spec, self._top_k), *side_inputs).with_outputs(
              _FLAGS, _FAILED, main=_ACCS)
      accs.append(out[_ACCS])
      candidates.append(out[_FLAGS])
      failures.append(out[_FAILED])
    failed = failures | "FlattenFailures" >> beam.Flatten()
    combined = ((accs or [p | "NoAccs" >> beam.Create([])])
                | "FlattenAccs" >> beam.Flatten()
                | "SumAccs" >> beam.CombinePerKey(MembershipCombineFn()))
    seeds = p | "Seeds" >> beam.Create([(name, (_SEED, None))
                                        for name in self._all])
    emitted = ((
        combined | "TagAccs" >> beam.Map(_tagged, _ACC),
        key_stats | "TagKeys" >> beam.Map(_tagged, _KEYS),
        failed | "TagFailures" >> beam.Map(_tagged, _FAILED),
        seeds,
    )
               | "Parts" >> beam.Flatten()
               | "ByTableParts" >> beam.GroupByKey()
               | "Emit" >> beam.FlatMap(
                   _emit_table, specs, self._stats, self._failed,
                   self._digests).with_outputs(_FAILED, main="metrics"))
    # every failure — driver, worker or emit — drops that table's flags
    dropped = (failed, emitted[_FAILED]) | "FlattenDropped" >> beam.Flatten()
    flags = (
        (candidates or [p | "NoCandidates" >> beam.Create([])])
        | "FlattenCandidates" >> beam.Flatten()
        | "TopFlags" >> beam.CombinePerKey(_TopFlagsCombineFn(self._top_k))
        |
        "Flags" >> beam.FlatMap(_flags, beam.pvalue.AsSingleton(
            self._label_key), beam.pvalue.AsList(dropped)))
    return {"metrics": emitted["metrics"], "flags": flags}
