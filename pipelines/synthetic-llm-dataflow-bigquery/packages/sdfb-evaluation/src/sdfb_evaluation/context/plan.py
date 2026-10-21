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
"""The evaluation plan: everything the Beam job needs to know before it
reads a row, decided from BigQuery metadata and ONE aggregate scan per
table side.

    launch + models ──► tables: launch tables parents first, their
          │             already-landed parents read-only ("external");
          │             a table the loaded model does not declare → PlanError
          │             (R50), unless the launch is that one table → standalone
          │             (R55), or relationships are off (R53)
          ▼
    per launch table: bq.table → resolve_scope (+ foreign writers)
          │           → pin_source → provisional ColumnPlans
          │           → as_of_diff only: its start snapshot (R57)
          ▼
    dry runs (planning SELECTs, panel, prepare DDL and, sampled, the
          │  worst-case sample reads) ─► Budget.check_bytes; a table whose
          │  dry run fails is skipped as scope `unknown`, not the plan
          ▼             nothing billed before this point
    ONE planning SELECT per side → scope.verify(rows) → R/E/H panel
          ▼             the tables' scans run at once ("Concurrent scans")
    [sampled] a salted sample CTAS per side above sample_rows ─► check again
          ▼
    census shares (fixed shuffle first, the rest max-min fair across
    tables, then columns) → kinds, grids, dictionaries → pairs → digest

Planning SQL, per column (`t` is the read; a key column stops after the
distinct count, a nested one after the null count):

    all          COUNTIF(NULL)            (REPEATED: ARRAY_LENGTH = 0)
    STRING/BYTES COUNTIF(TRIM(x) = '')    (BYTES: LENGTH(x) = 0)
    scalar       APPROX_COUNT_DISTINCT(x)
    numeric,     APPROX_QUANTILES(v, 1000), AVG, STDDEV_POP, MIN, MAX,
    temporal     APPROX_TOP_COUNT(v, 11) (atoms); TIMESTAMP/DATETIME also
                 COUNTIF(TIME(x) = 00:00:00) (day granularity)
    STRING/BYTES AVG(LENGTH(x)), APPROX_TOP_COUNT(x', 255) (dictionaries),
                 x' = x when BYTE_LENGTH(x) <= 1024, else NULL
    BOOL         APPROX_TOP_COUNT(x, 255)

A top list counts NULL as a value, so each asks for one slot more than it
keeps (10 atoms, 254 dictionary values). A value over 1 KiB is never a
category and never enters one: that bounds a top list at 255 * (1 KiB +
16 B) whatever the column holds.

`v` is x on one numeric scale: FLOAT64 with NaN/±Inf as NULL, the other
numerics CAST AS FLOAT64, and temporal values as UNIX_MICROS epochs (DATE
and DATETIME read as UTC; TIME as microseconds since midnight). Every
temporal grid, atom, mean and std in a `ColumnPlan` is on that scale, so
the Beam encoder must encode temporal cells in epoch microseconds too.
BigQuery allows 10 000 output columns per SELECT — counted as leaves, so
a top list's ARRAY<STRUCT<value, count>> is three — about 1 M characters
of query text and a 100 MB result row. `planning_queries` keeps every
SELECT under all three (the row under 90 MB, estimating each top list at
its bound and each quantile grid at 1001 * 8 B): a table past any of them
is planned in several SELECTs over the same read, column groups that never
split a column, each with its own `COUNT(*)`, which must agree. Columnar
billing reads each column once, so the chunks cost what one SELECT would.

Concurrent scans (Ruling R153). A scan is one table's billed reads: the
planning SELECTs of both sides, the row-count check and the panel. Their
time grows with the tables' rows, and a launcher has a fixed number of
minutes, so the scans of the tables run at once, on `min(tables,
SCAN_THREADS)` threads sharing the one BigQuery client:

    dry runs (one table after another)
          ▼
    scan(users) ─┐
    scan(orders) ├─ each its own statements, in order, on its own table's
    scan(items) ─┘  scratch record; the statement count under a lock
          ▼         every scan has ended, whatever the others did
    the first failing table IN TABLE ORDER raises its own error
          ▼
    sampling, census, pairs, digests: one table after another, in table
    order — they append to shared lists, so the plan never depends on
    which scan ended first

The plan is the one a table-after-table planning makes. What differs is
a failure: the statements of the OTHER tables have run (and were billed)
by the time the error is raised — a cost, never a result. When more
than one table fails, the first in table order is raised and each other
one is logged as a WARNING, so no paid-for failure goes unsaid.

An interrupt is not a failure. Ctrl-C (or a `SystemExit`) reaches the
thread that waits for the scans, not the scans. It leaves planning at
once and stops them: the tables whose scan has not started are never
scanned, and a scan in flight sends no further statement (it checks
before each one; the statement BigQuery is already running cannot be
called back, and the process ends when it returns).

Determinism: BigQuery documents APPROX_QUANTILES, APPROX_COUNT_DISTINCT
and APPROX_TOP_COUNT as approximate but says nothing about repeat runs
over identical data. The plan (grids, atoms, dictionaries, kinds, pairs,
census methods — and so `encoding_plan_digest`) may therefore differ
slightly between two plannings of the same data. `evaluation_key` does
not depend on them; metrics are deterministic for a given plan.

STRING routing (source statistics, non-NULL n):

    key (PK / FK column) or identity column   → identifier
    distinct ≤ 1000                            → categorical
    distinct / n ≥ 0.9, AVG(LENGTH) ≥ 20      → text
    distinct / n ≥ 0.9                         → identifier
    otherwise, AVG(LENGTH) ≥ 20               → text
    otherwise                                  → categorical (high cardinality)

Literals (D6): `literal_ok` holds when the source top list covers every
non-NULL row with at most 50 values, each counted at least 10 times (the
list itself is the evidence, not the HLL distinct estimate). A value is
stored literally iff `literal_ok` AND `hash64(name, value)` is in
`detection_dictionary`, which is built from SOURCE values only — a value
only the synthetic side holds is always a hashed label. `dictionary` is
the `hash64` codes of the top-9 source values (the pair grid's 10th cell
is "other"), `detection_dictionary` the top-254 (Ruling R32), most
frequent first, NULL never a value.

Census head (Ruling R67): `census_head` holds the `hash64` codes of the
source's top-254 values followed by the synthetic side's top-254 (each
side's own `APPROX_TOP_COUNT(x', 255)`; duplicates once), for every
non-key STRING, BYTES and BOOL column whatever its routed kind. A
value-sampled census includes these values with certainty (weight 1)
and hash-samples only the tail (weight 1 / rate), a stratified
Horvitz-Thompson design, so a heavy hitter is never left to the hash.
Numeric and temporal columns have no head (their top list is 10 atoms on
the planning scale, not value codes).

Pairs: the `pair_max_columns` pairable columns (numeric, temporal,
categorical, boolean; never a key; ≥ 2 source-distinct values) ranked by
APPROX_COUNT_DISTINCT as an entropy proxy on the 10-cell pair grid —
first by log2(min(distinct, 10)) descending, then by |ln(distinct / 10)|
ascending (a numeric or temporal column fills the grid by deciles, so its
distance is 0), then schema order — and every pair among them.

Design: docs/designs/2026-07-07-evaluation-framework-design.md

Design: docs/DESIGN.md §5 Fidelity
(ADR 0022).
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import math
import re
import threading
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from sdfb_evaluation.canonical import canonical_value, hash64, json_safe
from sdfb_evaluation.catalogue import load_catalogue
from sdfb_evaluation.context.bq import BqApiError, normalize_fqn, quote_fqn
from sdfb_evaluation.context.budget import (
    CENSUS_KEY_BYTES,
    GB,
    Budget,
    BudgetExceededError,
    census_demand,
    fixed_shuffle_bytes,
    mask_bytes,
    predict_shuffle_gb,
    source_sets_fit,
    table_key_shape,
    value_census_bytes,
    value_sample_rate,
    water_fill,
)
from sdfb_evaluation.context.jobs import (
    JobWrite,
    foreign_writes,
    parse_timestamp,
)
from sdfb_evaluation.context.launch import LaunchContext, RunRecord
from sdfb_evaluation.context.reference import Panel, fetch_panel, panel_sql
from sdfb_evaluation.context.relationships import (
    Edge,
    RelModel,
    component,
    enforced_edges,
    generation_order,
    model_for,
    sha12,
)
from sdfb_evaluation.context.scope import (
    MODES as SCOPE_MODES,
    ScopePlan,
    SourcePin,
    from_item,
    pin_source,
    read_params,
    resolve_scope,
    sampled_read,
)
from sdfb_evaluation.schemas import load_schema
from sdfb_evaluation.types import ColumnKind
from sdfb_evaluation.version import EVALUATOR_VERSION

__all__ = [
    "ColumnPlan",
    "EvaluationPlan",
    "Knobs",
    "PlanError",
    "PlanningOutput",
    "PrepareStatement",
    "TablePlan",
    "apply_planning",
    "build_plan",
    "encoding_plan_digest",
    "evaluation_key",
    "kinds_from_schema",
    "parent_landing",
    "parse_planning",
    "planning_outputs",
    "planning_queries",
    "planning_sql",
    "select_pairs",
    "string_kind",
    "table_roles",
]

GRID_POINTS = 1001  # APPROX_QUANTILES(x, 1000)
_QUANTILE_STEPS = GRID_POINTS - 1
_ATOM_CANDIDATES = 10
ATOM_TOP_K = _ATOM_CANDIDATES + 1  # NULL may take a slot
_ATOM_TOP_K = ATOM_TOP_K
_ATOM_MIN_COUNT = 2  # a value seen once is no point mass
PAIR_GRID_CELLS = 10  # top-9 + other, or deciles
_MIN_PAIR_DISTINCT = 2  # a constant column has no dependence
_FQN_PARTS = 3  # project.dataset.table
DICTIONARY_SIZE = PAIR_GRID_CELLS - 1
DETECTION_DICTIONARY_SIZE = 254  # Ruling R32
DICTIONARY_TOP_K = DETECTION_DICTIONARY_SIZE + 1  # NULL may take a slot
_TOP_ENTRY_OVERHEAD = 16  # count + struct/length overhead per top value
_TOP_LEAVES = 3  # ARRAY<STRUCT<value, count>>
_GRID_BYTES = GRID_POINTS * 8
_SCALAR_BYTES = 8
CENSUS_EXACT_FLOOR = 1000  # keys: a census this small is always exact
LITERAL_MAX_DISTINCT = 50  # ADR 0022
LITERAL_MIN_COUNT = 10  # the repo's k-anonymity floor
CATEGORICAL_MAX_DISTINCT = 1000
UNIQUE_RATIO = 0.9
TEXT_MIN_AVG_LENGTH = 20
MAX_OUTPUT_COLUMNS = 10_000  # BigQuery's cap on a SELECT's columns
MAX_ROW_BYTES = 90_000_000  # BigQuery's 100 MB result row, 10 % headroom
TOP_VALUE_MAX_BYTES = 1024  # a longer value is never a category
_MAX_QUERY_CHARS = 900_000  # under BigQuery's 1,024 K-character query text
SAMPLE_MODULUS = 1_000_000
SCAN_THREADS = 8  # the tables scanned at once (module docstring)
_GRANT_TOLERANCE = 1e-9  # float round trips never downgrade an exact census

MODES = ("exact", "sampled")
TRIGGERS = ("cli", "composer", "chained", "agent")
ROLES = ("root", "driven", "side_input", "isolated", "standalone", "external")
CENSUS_METHODS = ("none", "exact", "value_sampled")
_ROWS = "n_rows"
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_DATASET_RE = re.compile(r"[A-Za-z0-9_]{1,1024}")
_BAD_NAME_CHARS = re.compile(r"[`\\\x00-\x1f]")

_INT_TYPES = frozenset({"INT64", "INTEGER"})
_FLOAT_TYPES = frozenset({"FLOAT64", "FLOAT"})
_DECIMAL_TYPES = frozenset({"NUMERIC", "BIGNUMERIC", "DECIMAL", "BIGDECIMAL"})
_NUMERIC_TYPES = _INT_TYPES | _FLOAT_TYPES | _DECIMAL_TYPES
_TEMPORAL_TYPES = frozenset({"TIMESTAMP", "DATETIME", "DATE", "TIME"})
_BOOL_TYPES = frozenset({"BOOL", "BOOLEAN"})
_STRING_TYPES = frozenset({"STRING", "BYTES"})
_NESTED_TYPES = frozenset(
    {"RECORD", "STRUCT", "JSON", "GEOGRAPHY", "INTERVAL", "RANGE"})
KNOWN_TYPES = (
    _NUMERIC_TYPES | _TEMPORAL_TYPES | _BOOL_TYPES | _STRING_TYPES
    | _NESTED_TYPES)
# The same sets by their public names, for `context.offline`, which plans
# from rows in memory and must read a type name as this module does.
NUMERIC_TYPES = _NUMERIC_TYPES
TEMPORAL_TYPES = _TEMPORAL_TYPES
BOOL_TYPES = _BOOL_TYPES
STRING_TYPES = _STRING_TYPES
NESTED_TYPES = _NESTED_TYPES
_PAIR_KINDS = frozenset({
    ColumnKind.NUMERIC, ColumnKind.TEMPORAL, ColumnKind.CATEGORICAL,
    ColumnKind.BOOLEAN
})
_GRID_KINDS = frozenset({ColumnKind.NUMERIC, ColumnKind.TEMPORAL})
_CODED_KINDS = frozenset({ColumnKind.CATEGORICAL, ColumnKind.BOOLEAN})
_SCOPE_REQUESTS = ("auto", *SCOPE_MODES)  # whatever modes scope.py knows
_SOURCE_KEYS = ("hashed",)  # "raw" is refused with its own reason
# Knobs that change no metric value: they stay out of evaluation_key.
_OPERATIONAL_KNOBS = frozenset(
    {"max_bytes_billed", "output_dataset", "temp_dataset", "evaluation_id"})
_BQ_ERRORS = (PermissionError, LookupError, BqApiError)
# Malformed table metadata (R61): not a BqApiError, but no less a failure.
_METADATA_ERRORS = (KeyError, TypeError, ValueError)
_PAD = timedelta(seconds=1)
_HTTP_CONFLICT = 409  # BigQuery uses 409 for more than "Already Exists" (R61)


def _is_already_exists(exc: BaseException) -> bool:
  """A BigQuery 409 Conflict whose message says "Already Exists" (e.g. a
  retried CREATE SNAPSHOT TABLE under the same name). `status` is
  `BqApiError`'s own structured HTTP status (R61: set from the
  already-computed status in context/bq.py's `_translated`), checked
  first; BigQuery uses 409 for other conflicts too (a concurrent job
  clashing on id, a concurrent DDL on the same table), so the "already
  exists" text is the discriminator between those, not the only
  signal — an exception whose `status` is not 409 (including one that
  carries no `status` attribute at all) is never treated as this."""
  return (getattr(exc, "status", None) == _HTTP_CONFLICT and
          "already exists" in str(exc).lower())


class PlanError(ValueError):
  """The launch cannot be planned as resolved (the message says why and
  what to pass). `planning_ddl` lists any as_of_diff start snapshot phase
  A already created before the raise (R58) — empty unless a table's DDL
  already ran, since every raise this class carries happens before phase A
  starts."""

  def __init__(
      self, message: str, planning_ddl: tuple[PrepareStatement,
                                              ...] = ()) -> None:
    super().__init__(message)
    self.planning_ddl = planning_ddl


class _BudgetExceededWithDdlError(BudgetExceededError):
  """`BudgetExceededError` (defined in `context.budget`, so it cannot
  carry `planning_ddl` itself) wrapped with the as_of_diff start
  snapshot(s) phase A already created before a later budget check refused
  the plan (R58): the CLI can report or clean them up."""

  def __init__(self, message: str, planning_ddl: tuple[PrepareStatement,
                                                       ...]) -> None:
    super().__init__(message)
    self.planning_ddl = planning_ddl


# --------------------------------------------------------------------------
# columns
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ColumnPlan:  # pylint: disable=too-many-instance-attributes  # the plan's per-column contract is wide by design
  """How one column is encoded, profiled and compared.

  `quantiles_src`/`quantiles_syn` are the 1,001-point `APPROX_QUANTILES`
  grids of each side and `atoms` the source's point masses (a top-10
  value holding at least one grid step, 1/1000, of the non-NULL mass);
  with `mean_src`/`std_src` they are on the planning scale — numbers as
  FLOAT64, temporal values as epoch MICROseconds. `census` is how the
  keyed value census runs (`none` | `exact` | `value_sampled`, the last at
  `value_sample_rate` of the value-hash space). `dictionary` (top-9) and
  `detection_dictionary` (top-254) hold `hash64(name, value)` codes of
  SOURCE values. D6: a value is stored literally iff `literal_ok` AND
  `hash64(name, value) in detection_dictionary`; every other value —
  including any value only the synthetic side holds — is a hashed label.
  `census_head` (R67) holds the codes of the source's then the synthetic
  side's top-254 values: the certainty stratum of a value-sampled
  census (module docstring); never a literal gate. `avg_len` is the
  larger side's AVG(LENGTH(x)) of a STRING/BYTES column: the census
  budget sizes the mask pass's keys by it (R70); it changes no encoding.
  """
  name: str
  bq_type: str
  mode: str
  kind: ColumnKind
  is_key: bool
  day_granularity: bool
  census: str = "none"
  value_sample_rate: float | None = None
  quantiles_src: tuple[float, ...] | None = None
  quantiles_syn: tuple[float, ...] | None = None
  atoms: tuple[float, ...] = ()
  mean_src: float | None = None
  std_src: float | None = None
  dictionary: tuple[int, ...] | None = None
  literal_ok: bool = False
  source_distinct: int | None = None
  detection_dictionary: tuple[int, ...] | None = None
  synthetic_distinct: int | None = None
  census_head: tuple[int, ...] | None = None
  avg_len: float | None = None

  def __post_init__(self) -> None:
    object.__setattr__(self, "kind", ColumnKind(self.kind))
    if self.census not in CENSUS_METHODS:
      raise ValueError(f"{self.name}: census {self.census!r}, expected one "
                       f"of {list(CENSUS_METHODS)}")
    sampled = self.census == "value_sampled"
    if sampled != (self.value_sample_rate is not None):
      raise ValueError(f"{self.name}: value_sample_rate is set exactly when "
                       "the census is value_sampled")


def _kind_of(bq_type: str, mode: str) -> ColumnKind:
  if mode == "REPEATED" or bq_type not in KNOWN_TYPES - _NESTED_TYPES:
    return ColumnKind.NESTED
  if bq_type in _NUMERIC_TYPES:
    return ColumnKind.NUMERIC
  if bq_type in _TEMPORAL_TYPES:
    return ColumnKind.TEMPORAL
  if bq_type in _BOOL_TYPES:
    return ColumnKind.BOOLEAN
  return ColumnKind.CATEGORICAL  # STRING / BYTES until routed


def kinds_from_schema(
    fields: Iterable[Mapping[str, Any]],
    *,
    keys: Iterable[str],
    identity: Iterable[str] = ()) -> list[ColumnPlan]:
  """Provisional `ColumnPlan`s from a BigQuery JSON schema, in order.

  The kind follows the type (legacy and standard names alike; REPEATED,
  RECORD/STRUCT, JSON, GEOGRAPHY, INTERVAL, RANGE and any type this
  module does not know are `nested`, never an error). A `keys` column
  (PK/FK) is an identifier with `is_key`; an `identity` column
  (unique-but-not-a-key) is an identifier WITHOUT `is_key`, so its values
  still enter the census. STRING/BYTES stay `categorical` until
  `apply_planning` routes them; only a DATE is day-granular before the
  planning scan has seen values.
  """
  key_set, identity_set = set(keys), set(identity)
  columns = []
  for spec in fields:
    name = str(spec["name"])
    bq_type = str(spec.get("type") or "").upper()
    mode = str(spec.get("mode") or "NULLABLE").upper()
    kind = _kind_of(bq_type, mode)
    nested = kind is ColumnKind.NESTED
    is_key = name in key_set and not nested
    if not nested and (is_key or name in identity_set):
      kind = ColumnKind.IDENTIFIER
    columns.append(
        ColumnPlan(
            name=name,
            bq_type=bq_type,
            mode=mode,
            kind=kind,
            is_key=is_key,
            day_granularity=kind is ColumnKind.TEMPORAL and bq_type == "DATE"))
  return columns


# --------------------------------------------------------------------------
# planning SQL
# --------------------------------------------------------------------------
class PlanningOutput(NamedTuple):
  """One output column of the planning SELECT: `sql AS alias`."""
  alias: str
  column: str
  stat: str
  sql: str


def _ref(name: str) -> str:
  if not name or _BAD_NAME_CHARS.search(name):
    raise ValueError(f"column name {name!r} cannot be quoted for SQL")
  return f"t.`{name}`"


def _numeric_expr(ref: str, bq_type: str) -> str:
  """The column on the planning scale (see the module docstring)."""
  if bq_type in _FLOAT_TYPES:
    return f"IF(IS_NAN({ref}) OR IS_INF({ref}), NULL, {ref})"
  if bq_type == "TIMESTAMP":
    return f"UNIX_MICROS({ref})"
  if bq_type in ("DATE", "DATETIME"):
    return f"UNIX_MICROS(TIMESTAMP({ref}))"
  if bq_type == "TIME":
    return f"TIME_DIFF({ref}, TIME '00:00:00', MICROSECOND)"
  return f"CAST({ref} AS FLOAT64)"


def _column_outputs(index: int, column: ColumnPlan) -> list[PlanningOutput]:
  ref = _ref(column.name)
  bq_type = column.bq_type
  items: list[tuple[str, str]] = []
  if column.kind is ColumnKind.NESTED:
    empty = (f"ARRAY_LENGTH({ref}) = 0"
             if column.mode == "REPEATED" else f"{ref} IS NULL")
    items.append(("null", f"COUNTIF({empty})"))
  else:
    items.append(("null", f"COUNTIF({ref} IS NULL)"))
    if bq_type == "STRING":
      items.append(("empty", f"COUNTIF(TRIM({ref}) = '')"))
    elif bq_type == "BYTES":
      items.append(("empty", f"COUNTIF(LENGTH({ref}) = 0)"))
    items.append(("distinct", f"APPROX_COUNT_DISTINCT({ref})"))
    if not column.is_key:
      items.extend(_value_outputs(ref, bq_type))
  return [
      PlanningOutput(f"c{index}_{stat}", column.name, stat, sql)
      for stat, sql in items
  ]


def _value_outputs(ref: str, bq_type: str) -> list[tuple[str, str]]:
  if bq_type in _NUMERIC_TYPES | _TEMPORAL_TYPES:
    value = _numeric_expr(ref, bq_type)
    items = [
        ("quantiles", f"APPROX_QUANTILES({value}, {_QUANTILE_STEPS})"),
        ("mean", f"AVG({value})"),
        ("std", f"STDDEV_POP({value})"),
        ("min", f"MIN({value})"),
        ("max", f"MAX({value})"),
        ("top", f"APPROX_TOP_COUNT({value}, {_ATOM_TOP_K})"),
    ]
    if bq_type in ("TIMESTAMP", "DATETIME"):
      items.append(("midnight", f"COUNTIF(TIME({ref}) = TIME '00:00:00')"))
    return items
  if bq_type in _BOOL_TYPES:
    return [("top", f"APPROX_TOP_COUNT({ref}, {DICTIONARY_TOP_K})")]
  if bq_type in _STRING_TYPES:
    bounded = f"IF(BYTE_LENGTH({ref}) <= {TOP_VALUE_MAX_BYTES}, {ref}, NULL)"
    return [("avg_len", f"AVG(LENGTH({ref}))"),
            ("top", f"APPROX_TOP_COUNT({bounded}, {DICTIONARY_TOP_K})")]
  return []


def _output_cost(output: PlanningOutput, bq_type: str) -> tuple[int, int]:
  """(leaves, estimated result bytes) of one planning output."""
  if output.stat == "top":
    if bq_type in _STRING_TYPES:
      value = TOP_VALUE_MAX_BYTES
      k = DICTIONARY_TOP_K
    else:
      value = _SCALAR_BYTES
      k = DICTIONARY_TOP_K if bq_type in _BOOL_TYPES else _ATOM_TOP_K
    return _TOP_LEAVES, k * (value + _TOP_ENTRY_OVERHEAD)
  if output.stat == "quantiles":
    return 1, _GRID_BYTES
  return 1, _SCALAR_BYTES


def _column_cost(index: int,
                 column: ColumnPlan) -> tuple[list[PlanningOutput], int, int]:
  """A column's outputs with their leaves and row bytes."""
  outputs = _column_outputs(index, column)
  costs = [_output_cost(o, column.bq_type) for o in outputs]
  return outputs, sum(c[0] for c in costs), sum(c[1] for c in costs)


def planning_outputs(
    columns: Sequence[ColumnPlan]) -> tuple[PlanningOutput, ...]:
  """Every output of the planning scan over `columns`, aliased
  `c<index>_<stat>` by position in `columns`."""
  return tuple(o for i, c in enumerate(columns) for o in _column_outputs(i, c))


def _select(expr: str, outputs: Sequence[PlanningOutput]) -> str:
  lines = [f"  COUNT(*) AS {_ROWS}"]
  lines += [f"  {o.sql} AS {o.alias}" for o in outputs]
  body = ",\n".join(lines)
  return f"SELECT\n{body}\nFROM {expr} AS t"


def planning_sql(read_table: str | SourcePin | ScopePlan,
                 columns: Sequence[ColumnPlan]) -> str:
  """ONE aggregate SELECT over `read_table` computing every planning
  statistic of `columns` (see the module docstring).

  `read_table` is a strict table name or the `SourcePin`/`ScopePlan` to
  read (`scope.from_item`: its own `read_expr`; bind
  `scope.read_params(read_table)` when running the SQL). Text is never
  parsed into a subquery.

  Raises:
    ValueError: an unsafe table or column name, or more outputs than one
      SELECT may have (use `planning_queries`).
  """
  costed = [_column_cost(i, c) for i, c in enumerate(columns)]
  leaves = 1 + sum(c[1] for c in costed)
  row_bytes = _SCALAR_BYTES + sum(c[2] for c in costed)
  if leaves > MAX_OUTPUT_COLUMNS or row_bytes > MAX_ROW_BYTES:
    raise ValueError(
        f"{leaves} output columns (limit {MAX_OUTPUT_COLUMNS}) and an "
        f"estimated {row_bytes} B result row (limit {MAX_ROW_BYTES}) do not "
        "fit one SELECT; plan the table with planning_queries, which splits "
        "it")
  return _select(from_item(read_table), [o for c in costed for o in c[0]])


def planning_queries(read_table: str | SourcePin | ScopePlan,
                     columns: Sequence[ColumnPlan]) -> tuple[str, ...]:
  """The planning scan of `columns` as few SELECTs as BigQuery's limits
  allow: one for any ordinary table; for a wide one, consecutive column
  groups (a column's outputs never split), each under 10 000 output
  leaves, ~1 M characters and a 90 MB estimated result row, all over the
  same read (module docstring)."""
  expr = from_item(read_table)
  chunks: list[list[PlanningOutput]] = [[]]
  used = [1, 0, _SCALAR_BYTES]  # leaves (COUNT(*) included), chars, bytes
  for index, column in enumerate(columns):
    outputs, leaves, row_bytes = _column_cost(index, column)
    chars = sum(len(o.sql) + len(o.alias) + 8 for o in outputs)
    if chunks[-1] and (used[0] + leaves > MAX_OUTPUT_COLUMNS or
                       used[1] + chars > _MAX_QUERY_CHARS or
                       used[2] + row_bytes > MAX_ROW_BYTES):
      chunks.append([])
      used = [1, 0, _SCALAR_BYTES]
    chunks[-1].extend(outputs)
    used = [used[0] + leaves, used[1] + chars, used[2] + row_bytes]
  return tuple(_select(expr, chunk) for chunk in chunks)


def _count(value: Any) -> int | None:
  return None if value is None else int(value)


def _finite(value: Any) -> float | None:
  if value is None:
    return None
  number = float(value)
  return number if math.isfinite(number) else None


def _top_pairs(value: Any) -> list[tuple[Any, int]]:
  """`APPROX_TOP_COUNT`'s ARRAY<STRUCT<value, count>> as (value, count),
  whatever row shape the client returned."""
  pairs = []
  for item in value or ():
    if isinstance(item, Mapping):
      pairs.append((item.get("value"), int(item["count"])))
    else:
      pairs.append((item[0], int(item[1])))
  return pairs


def _grid(value: Any) -> tuple[float, ...] | None:
  """A quantile grid as finite floats; None when there is none."""
  grid = [_finite(v) for v in value or ()]
  return tuple(v for v in grid if v is not None) or None


def _parse_stat(stat: str, value: Any) -> Any:
  if stat in ("null", "empty", "distinct", "midnight"):
    return _count(value)
  if stat == "quantiles":
    return _grid(value)
  if stat == "top":
    return _top_pairs(value)
  return _finite(value)


def parse_planning(rows: Sequence[Mapping[str, Any]],
                   columns: Sequence[ColumnPlan]) -> dict[str, Any]:
  """`{"rows": n, "columns": {name: {stat: value}}}` from the result row
  of each planning SELECT (one per `planning_queries` chunk).

  Raises:
    ValueError: no row, or the chunks disagree on the row count (the
      table changed while it was planned).
  """
  if not rows:
    raise ValueError("no planning result row to parse")
  merged: dict[str, Any] = {}
  counts = set()
  for row in rows:
    counts.add(_count(row.get(_ROWS)))
    merged.update(row)
  if len(counts) != 1 or None in counts:
    raise ValueError(f"planning chunks disagree on row counts "
                     f"{sorted(counts, key=str)}: the read changed while it "
                     "was planned")
  stats: dict[str, dict[str, Any]] = {c.name: {} for c in columns}
  for output in planning_outputs(columns):
    stats[output.column][output.stat] = _parse_stat(output.stat,
                                                    merged.get(output.alias))
  return {"rows": counts.pop(), "columns": stats}


# --------------------------------------------------------------------------
# routing, dictionaries, census
# --------------------------------------------------------------------------
def string_kind(distinct: int | None, non_null: int,
                avg_length: float | None) -> ColumnKind:
  """The kind of a non-key STRING/BYTES column (module docstring table)."""
  if not non_null or distinct is None or distinct <= CATEGORICAL_MAX_DISTINCT:
    return ColumnKind.CATEGORICAL
  long_values = (avg_length or 0.0) >= TEXT_MIN_AVG_LENGTH
  if distinct / non_null >= UNIQUE_RATIO:
    return ColumnKind.TEXT if long_values else ColumnKind.IDENTIFIER
  return ColumnKind.TEXT if long_values else ColumnKind.CATEGORICAL


def _ranked_values(top: Sequence[tuple[Any, int]]) -> list[tuple[Any, int]]:
  """Non-NULL top values, most frequent first, ties by canonical value."""
  present = [(v, c) for v, c in top if v is not None]
  return sorted(
      present,
      key=lambda vc: (-vc[1], json.dumps(canonical_value(vc[0]), default=str)))


def _literal_ok(values: Sequence[tuple[Any, int]], non_null: int) -> bool:
  """D6's column gate: the top list is exhaustive (it covers every
  non-NULL row) with at most 50 values, each counted at least 10 times.
  The list is the evidence; the HLL distinct estimate is not consulted."""
  return (0 < len(values) <= LITERAL_MAX_DISTINCT and non_null > 0 and
          sum(c for _, c in values) == non_null and
          min(c for _, c in values) >= LITERAL_MIN_COUNT)


def _atoms(top: Sequence[tuple[Any, int]], non_null: int) -> tuple[float, ...]:
  floor = max(_ATOM_MIN_COUNT, math.ceil(non_null / _QUANTILE_STEPS))
  points = {
      float(v)
      for v, c in top
      if v is not None and c >= floor and math.isfinite(float(v))
  }
  return tuple(sorted(points))


def _day_granular(column: ColumnPlan, kind: ColumnKind, midnight: int | None,
                  non_null: int) -> bool:
  if kind is not ColumnKind.TEMPORAL:
    return False
  if column.bq_type == "DATE":
    return True
  return non_null > 0 and midnight == non_null


def _avg_len(src: Mapping[str, Any], syn: Mapping[str, Any]) -> float | None:
  """The larger side's planned AVG(LENGTH(x)), or None when neither has
  one (all NULL)."""
  lengths = [
      value for value in (_finite(src.get("avg_len")),
                          _finite(syn.get("avg_len"))) if value is not None
  ]
  return max(lengths) if lengths else None


def _census_head(name: str, src_top: Sequence[tuple[Any, int]],
                 syn_top: Sequence[tuple[Any, int]]) -> tuple[int, ...]:
  """The census's certainty stratum (R67): each side's ranked top values
  (source first), as `hash64` codes, each once."""
  head: dict[int, None] = {}
  for top in (src_top, syn_top):
    for value, _ in _ranked_values(top)[:DETECTION_DICTIONARY_SIZE]:
      head.setdefault(hash64(name, value), None)
  return tuple(head)


def _planned(column: ColumnPlan, src: Mapping[str, Any], syn: Mapping[str, Any],
             rows: int) -> ColumnPlan:
  non_null = max(rows - (src.get("null") or 0), 0)
  distinct = src.get("distinct")
  kind = column.kind
  if kind is ColumnKind.CATEGORICAL and not column.is_key:
    kind = string_kind(distinct, non_null, src.get("avg_len"))
  top = _top_pairs(src.get("top"))
  grid = kind in _GRID_KINDS and not column.is_key
  coded = kind in _CODED_KINDS and not column.is_key
  values = _ranked_values(top) if coded else []
  codes = tuple(hash64(column.name, v) for v, _ in values)
  headed = (not column.is_key and kind is not ColumnKind.NESTED and
            column.bq_type in _STRING_TYPES | _BOOL_TYPES)
  return dataclasses.replace(
      column,
      kind=kind,
      day_granularity=_day_granular(column, kind, src.get("midnight"),
                                    non_null),
      census="none",
      value_sample_rate=None,
      quantiles_src=_grid(src.get("quantiles")) if grid else None,
      quantiles_syn=_grid(syn.get("quantiles")) if grid else None,
      atoms=_atoms(top, non_null) if grid else (),
      mean_src=_finite(src.get("mean")) if grid else None,
      std_src=_finite(src.get("std")) if grid else None,
      dictionary=codes[:DICTIONARY_SIZE] if coded else None,
      detection_dictionary=codes[:DETECTION_DICTIONARY_SIZE] if coded else None,
      literal_ok=coded and _literal_ok(values, non_null),
      source_distinct=distinct,
      synthetic_distinct=syn.get("distinct"),
      census_head=(_census_head(column.name, top, _top_pairs(syn.get("top")))
                   if headed else None),
      avg_len=_avg_len(src, syn) if column.bq_type in _STRING_TYPES else None,
  )


def _rows_read(stats: Mapping[str, Any]) -> float:
  return float(stats.get("rows_read", stats.get("rows") or 0))


def apply_planning(cols: Sequence[ColumnPlan], src_stats: Mapping[str, Any],
                   syn_stats: Mapping[str, Any], *,
                   budget: Budget) -> list[ColumnPlan]:
  """The provisional `cols` completed from the planning scans.

  `src_stats`/`syn_stats` are `parse_planning` results (optionally with
  `rows_read`, the rows the pipeline will read of that side when it
  samples). STRING/BYTES columns are routed (module docstring), grids,
  atoms, moments and dictionaries filled from the source (the synthetic
  grid too), and every non-key, non-nested column gets its census method:
  `budget.max_shuffle_gb` is the census shuffle THIS table may use, shared
  max-min fairly across its columns; a column granted less than its
  distinct keys is value-sampled (`budget.value_sample_rate`). A column
  expecting at most `CENSUS_EXACT_FLOOR` keys is always exact, whatever
  the share (a boolean or a small category never needs sampling).
  """
  src_columns = src_stats.get("columns") or {}
  syn_columns = syn_stats.get("columns") or {}
  rows = int(src_stats.get("rows") or 0)
  planned = [
      _planned(c,
               src_columns.get(c.name) or {},
               syn_columns.get(c.name) or {}, rows) for c in cols
  ]
  rows_src, rows_syn = _rows_read(src_stats), _rows_read(syn_stats)
  eligible = [
      i for i, c in enumerate(planned)
      if not c.is_key and c.kind is not ColumnKind.NESTED
  ]
  demands = [census_demand(planned[i], rows_src, rows_syn) for i in eligible]
  tiny = [d <= CENSUS_EXACT_FLOOR for d in demands]
  capacity = (
      budget.max_shuffle_gb * GB / CENSUS_KEY_BYTES -
      sum(d for d, small in zip(demands, tiny, strict=True) if small))
  shared = iter(
      water_fill(
          [d for d, small in zip(demands, tiny, strict=True) if not small],
          capacity))
  grants = [
      d if small else next(shared)
      for d, small in zip(demands, tiny, strict=True)
  ]
  for index, demand, grant in zip(eligible, demands, grants, strict=True):
    rate = value_sample_rate(grant * (1 + _GRANT_TOLERANCE), demand)
    planned[index] = dataclasses.replace(
        planned[index],
        census="exact" if rate is None else "value_sampled",
        value_sample_rate=rate)
  return planned


# --------------------------------------------------------------------------
# pairs and the encoding digest
# --------------------------------------------------------------------------
def _pairable(column: ColumnPlan) -> bool:
  return (not column.is_key and column.kind in _PAIR_KINDS and
          column.source_distinct is not None and
          column.source_distinct >= _MIN_PAIR_DISTINCT)


def _pair_rank(column: ColumnPlan) -> tuple[float, float]:
  distinct = int(column.source_distinct or 0)
  fills_grid = column.kind in _GRID_KINDS and distinct >= PAIR_GRID_CELLS
  distance = 0.0 if fills_grid else abs(math.log(distinct / PAIR_GRID_CELLS))
  return -math.log2(min(distinct, PAIR_GRID_CELLS)), distance


def select_pairs(columns: Sequence[ColumnPlan],
                 max_columns: int) -> tuple[tuple[int, int], ...]:
  """Every pair among the `max_columns` best-ranked pairable columns, as
  `(i, j)` indices into `columns` with i < j (ranking: module
  docstring)."""
  ranked = sorted((i for i, c in enumerate(columns) if _pairable(c)),
                  key=lambda i: (*_pair_rank(columns[i]), i))
  chosen = sorted(ranked[:max(max_columns, 0)])
  return tuple((a, b) for x, a in enumerate(chosen) for b in chosen[x + 1:])


def _edge_payload(edge: Edge) -> str:
  return json.dumps(
      {
          "cols": list(edge.cols),
          "ref": edge.ref,
          "ref_cols": list(edge.ref_cols),
          "enforced": edge.enforced,
      },
      sort_keys=True)


def _column_payload(column: ColumnPlan) -> dict[str, Any]:

  def listed(values: Sequence[Any] | None) -> list[Any] | None:
    return None if values is None else list(values)

  return {
      "kind": str(column.kind),
      "is_key": column.is_key,
      "day_granularity": column.day_granularity,
      "census": column.census,
      "value_sample_rate": column.value_sample_rate,
      "quantiles_src": listed(column.quantiles_src),
      "quantiles_syn": listed(column.quantiles_syn),
      "atoms": list(column.atoms),
      "dictionary": listed(column.dictionary),
      "detection_dictionary": listed(column.detection_dictionary),
      "census_head": listed(column.census_head),
  }


def encoding_plan_digest(columns: Sequence[ColumnPlan],
                         pairs: Sequence[tuple[int, int]],
                         edges: Sequence[Edge]) -> str:
  """blake2b-128 over the canonical JSON of what the encoding depends on:
  per column (keyed by name) its kind, key flag, census method, quantile
  grids, atoms, dictionaries and census head; the pairs by column name;
  the edges.
  Column, pair and edge ORDER does not move it; any grid, dictionary,
  census or pair change does."""
  names = [c.name for c in columns]
  payload = {
      "columns": {
          c.name: _column_payload(c) for c in columns
      },
      "pairs": sorted(sorted((names[i], names[j])) for i, j in pairs),
      "edges": sorted(_edge_payload(e) for e in edges),
  }
  text = json.dumps(
      payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
  return hashlib.blake2b(text.encode(), digest_size=16).hexdigest()


# --------------------------------------------------------------------------
# knobs, table and evaluation plans
# --------------------------------------------------------------------------
def _positive_int(name: str, value: Any, *, minimum: int = 1) -> None:
  if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
    raise ValueError(f"{name}: expected an int >= {minimum}, got {value!r}")


@dataclass(frozen=True)
class Knobs:  # pylint: disable=too-many-instance-attributes  # one field per CLI knob
  """The evaluation's knobs (the `sdfb-eval run` flags that shape the plan).

  Every knob except `max_bytes_billed`, `output_dataset`, `temp_dataset`
  and `evaluation_id` (which change no metric value) enters
  `evaluation_key`, and so the salt and every sample: a knob must change
  a result to be one (the top-k profile size is the constant
  `census.TOPK_ITEMS`, Ruling R113). `temp_dataset` (`project.dataset`)
  defaults to `<bq project>.<output_dataset>`.
  """
  sample_rows: int = 200_000
  privacy_sample_rows: int = 50_000
  detection_sample_rows: int = 50_000
  pair_max_columns: int = 20
  row_flags_top_k: int = 100
  row_flags_source_keys: str = "hashed"
  max_bytes_billed: int = 1 << 40
  max_shuffle_gb: float = 500.0
  scope: str = "auto"
  allow_contaminated: bool = False
  output_dataset: str = "synthetic_data_quality"
  temp_dataset: str | None = None
  evaluation_id: str | None = None

  def __post_init__(self) -> None:
    for name in ("sample_rows", "privacy_sample_rows", "detection_sample_rows",
                 "row_flags_top_k"):
      _positive_int(name, getattr(self, name))
    _positive_int("pair_max_columns", self.pair_max_columns, minimum=0)
    Budget(  # validates both caps
        max_shuffle_gb=self.max_shuffle_gb,
        max_bytes_billed=self.max_bytes_billed)
    if self.scope not in _SCOPE_REQUESTS:
      raise ValueError(f"scope {self.scope!r}: expected one of "
                       f"{list(_SCOPE_REQUESTS)}")
    if self.row_flags_source_keys == "raw":
      raise ValueError(
          "row_flags_source_keys 'raw' is not supported: row flags carry the "
          "matched source key only as a keyed hash (the label key, Rulings "
          "R64/R68), never its value; use 'hashed'")
    if self.row_flags_source_keys not in _SOURCE_KEYS:
      raise ValueError(f"row_flags_source_keys {self.row_flags_source_keys!r}"
                       f": expected one of {list(_SOURCE_KEYS)}")
    if not isinstance(self.allow_contaminated, bool):
      raise ValueError("allow_contaminated: expected true or false")
    if _DATASET_RE.fullmatch(self.output_dataset or "") is None:
      raise ValueError(f"output_dataset {self.output_dataset!r}: expected a "
                       "dataset id")
    if self.temp_dataset is not None:
      normalize_fqn(f"{self.temp_dataset}.probe")
    if (self.evaluation_id is not None and
        _ID_RE.fullmatch(self.evaluation_id) is None):
      raise ValueError(f"evaluation_id {self.evaluation_id!r}: expected 1-128 "
                       "of [A-Za-z0-9_-]")

  @classmethod
  def from_mapping(cls, values: Mapping[str, Any]) -> Knobs:
    """Knobs from a mapping; an unknown key is a `ValueError` naming it."""
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(values) - known)
    if unknown:
      raise ValueError(f"unknown knob(s) {unknown}; expected any of "
                       f"{sorted(known)}")
    return cls(**dict(values))

  @property
  def budget(self) -> Budget:
    return Budget(
        max_shuffle_gb=self.max_shuffle_gb,
        max_bytes_billed=self.max_bytes_billed)

  def to_dict(self) -> dict[str, Any]:
    return dataclasses.asdict(self)

  def key_dict(self) -> dict[str, Any]:
    """The knobs that can change a metric value (they key the run)."""
    return {
        k: v for k, v in self.to_dict().items() if k not in _OPERATIONAL_KNOBS
    }


class PrepareStatement(NamedTuple):
  """One DDL statement to run before the pipeline, with the parameters it
  binds (`bq.execute(sql, params)`): a scope's or a source pin's own
  `params` (`{}` for a pin: its AS OF is a literal), `sampled_read`'s
  `{"salt": …}` for a sample."""
  sql: str
  params: Mapping[str, Any]


@dataclass(frozen=True)
class TablePlan:  # pylint: disable=too-many-instance-attributes  # the registry's per-table record is wide
  """One table of the evaluation.

  Launch tables are evaluated; a role-`external` table is a read-only
  parent (an FK parent this launch did not write) read only for integrity
  joins. `source_read_table`/`synthetic_read_table` are what the pipeline
  reads of each side (the pinned clone / scope, or their samples);
  `skip_reason` is set when the table cannot be evaluated.
  """
  name: str
  landing_table: str
  source_table: str | None
  run_id: str | None
  role: str
  pk: tuple[str, ...]
  identity: tuple[str, ...]
  edges: tuple[Edge, ...]
  columns: tuple[ColumnPlan, ...]
  scope: ScopePlan
  source_read_table: str
  source_pinned: bool
  panel: Panel | None
  pairs: tuple[tuple[int, int], ...]
  encoding_plan_digest: str
  model: str | None = None
  source_pin: SourcePin | None = None
  synthetic_read_table: str = ""
  rows_source: int | None = None
  rows_synthetic: int | None = None
  sample_rate_source: float | None = None
  sample_rate_synthetic: float | None = None
  source_drifted: bool | None = None
  reference_digest: str | None = None
  skip_reason: str | None = None
  warnings: tuple[str, ...] = ()

  def __post_init__(self) -> None:
    if self.role not in ROLES:
      raise ValueError(f"{self.name}: role {self.role!r}, expected one of "
                       f"{list(ROLES)}")

  @property
  def evaluated(self) -> bool:
    """A launch table with something to read on both sides."""
    return self.role != "external" and self.skip_reason is None

  @property
  def rows_read(self) -> tuple[float, float]:
    """(source, synthetic) rows the pipeline reads, sampling applied."""
    return ((self.rows_source or 0) * (self.sample_rate_source or 1.0),
            (self.rows_synthetic or 0) * (self.sample_rate_synthetic or 1.0))

  def registry_entry(self) -> dict[str, Any]:
    """This table's `tables` record of `evaluation_data_history`."""
    scope, panel, pin = self.scope, self.panel, self.source_pin
    evaluated = self.evaluated
    rates = (self.sample_rate_source, self.sample_rate_synthetic)
    return {
        "name":
            self.name,
        "landing_table":
            self.landing_table,
        "source_table":
            self.source_table,
        "run_id":
            self.run_id,
        "role":
            self.role,
        "scope_mode":
            scope.mode,
        "scope_status":
            scope.status,
        "scope_ok":
            scope.ok,
        "scope_reason":
            scope.reason,
        "window_start":
            scope.window[0],
        "window_end":
            scope.window[1],
        "source_snapshot_ts":
            pin.as_of if pin is not None else None,
        "source_drifted":
            self.source_drifted,
        "reference_digest":
            self.reference_digest,
        "reference_verified":
            panel.verified if panel is not None else
            (False if evaluated else None),
        "reference_n":
            len(panel.r_rows) if panel is not None else None,
        "exposure_n":
            panel.e_n if panel is not None else None,
        "holdout_n":
            len(panel.h_rows) if panel is not None else None,
        "rows_source":
            self.rows_source,
        "rows_synthetic":
            self.rows_synthetic,
        "rows_expected":
            scope.expected_rows,
        "sampled":
            any(r is not None and r < 1 for r in rates) if evaluated else None,
        "sample_rate_source":
            self.sample_rate_source,
        "sample_rate_synthetic":
            self.sample_rate_synthetic,
        "encoding_plan_digest":
            self.encoding_plan_digest,
        "table_score":
            None,
    }


def _rfc3339(value: str | datetime) -> str:
  return parse_timestamp(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ordered(row: Mapping[str, Any], fields: Sequence[Mapping[str,
                                                              Any]]) -> dict:
  """`row` in schema order; a missing or extra key is a programming error."""
  names = [str(f["name"]) for f in fields]
  if set(row) != set(names):
    raise AssertionError(f"registry row keys differ from the schema: missing "
                         f"{sorted(set(names) - set(row))}, extra "
                         f"{sorted(set(row) - set(names))}")
  return {name: row[name] for name in names}


@dataclass(frozen=True)
class EvaluationPlan:  # pylint: disable=too-many-instance-attributes  # one field per registry concern
  """The whole evaluation, decided before the pipeline runs.

  `prepare_sql` is a tuple of `PrepareStatement` (sql, params) pairs, in
  execution order; `prepare(bq)` runs them. `planning_ddl` lists what
  planning ALREADY ran (R57: the as_of_diff start snapshots, zero bytes,
  expiring in 24 h) — for the report and for cleanup. `bq_bytes_estimate`
  sums the dry runs of every query the evaluation issues (planning,
  panel, prepare, samples); `predicted_shuffle_gb` is
  `budget.predict_shuffle_gb`. `label_key_uri` is the operator's label
  key (Rulings R64, R68: a Secret Manager version, `gs://` object or
  local path — never the key itself), None for an ephemeral key; the
  pipeline resolves it on a worker and the registry records only the
  mode. `thresholds_uri` and `thresholds_digest` record a run graded
  under its own thresholds (Ruling R93-6: the file's URI and the digest
  of the overrides it held), None for the catalogue's; the registry
  carries both in `evaluation_params`.
  """
  evaluation_id: str
  evaluation_key: str
  evaluated_at: str
  salt: str
  mode: str
  trigger: str
  runner: str
  launch: LaunchContext
  models: tuple[RelModel, ...]
  model_sha: str | None
  tables: tuple[TablePlan, ...]
  knobs: Knobs
  prepare_sql: tuple[PrepareStatement, ...]
  bq_bytes_estimate: int
  predicted_shuffle_gb: float
  warnings: tuple[str, ...] = ()
  planning_ddl: tuple[PrepareStatement, ...] = ()
  catalogue_version: str = ""
  evaluator_version: str = EVALUATOR_VERSION
  temp_dataset: str = ""
  label_key_uri: str | None = None
  thresholds_uri: str | None = None
  thresholds_digest: str | None = None

  @property
  def budget(self) -> Budget:
    return self.knobs.budget

  @property
  def skip_reason(self) -> str | None:
    """Why nothing can be evaluated (every launch table skipped), or None."""
    launch_tables = [t for t in self.tables if t.role != "external"]
    if any(t.evaluated for t in launch_tables):
      return None
    reasons = "; ".join(f"{t.name}: {t.skip_reason}" for t in launch_tables)
    return f"no table can be evaluated — {reasons}"

  def prepare(self, bq: Any) -> None:
    """Run every prepare statement, in order, with its own parameters and
    the budget's byte cap (a CTAS over APPENDS or an AS OF difference can
    scan a lot)."""
    for statement in self.prepare_sql:
      bq.execute(
          statement.sql,
          dict(statement.params),
          max_bytes=self.budget.max_bytes_billed)

  def registry_seed(self) -> dict[str, Any]:
    """Every `evaluation_data_history` field known before the pipeline
    runs, in schema order: a RUNNING event, or — when no table can be
    evaluated — the FINAL SKIPPED event (reason and warnings included,
    metric counts 0). Scores stay NULL; `bq_bytes_processed` is the
    dry-run estimate of the queries issued so far and to come."""
    launch = self.launch
    reason = self.skip_reason
    skipped = reason is not None
    counts = 0 if skipped else None
    fields = load_schema("evaluation_data_history")
    record = next(f for f in fields if f["name"] == "tables")
    row: dict[str, Any] = {
        "evaluation_id":
            self.evaluation_id,
        "recorded_at":
            self.evaluated_at,
        "evaluated_at":
            self.evaluated_at,
        "finished_at":
            self.evaluated_at if skipped else None,
        "event":
            "FINAL" if skipped else "RUNNING",
        "status":
            "SKIPPED" if skipped else "RUNNING",
        "status_reason":
            reason,
        "evaluation_key":
            self.evaluation_key,
        "trigger":
            self.trigger,
        "runner":
            self.runner,
        "mode":
            self.mode,
        "evaluator_version":
            self.evaluator_version,
        "catalogue_version":
            self.catalogue_version,
        "evaluation_job_id":
            None,
        "image":
            None,
        **self._generation_fields(),
        "tables": [
            _ordered(t.registry_entry(), record["fields"]) for t in self.tables
        ],
        **launch.typed_filters(),
        "generation_params":
            json.loads(json.dumps(json_safe(dict(launch.params)), default=str)),
        "evaluation_params": {
            **self.knobs.to_dict(),
            "temp_dataset": self.temp_dataset,
            "mode": self.mode,
            "trigger": self.trigger,
            "runner": self.runner,
            "thresholds_uri": self.thresholds_uri,
            "thresholds_digest": self.thresholds_digest,
        },
        "overall_score":
            None,
        "fidelity_score":
            None,
        "privacy_score":
            None,
        "integrity_score":
            None,
        "diversity_score":
            None,
        "metrics_total":
            counts,
        "metrics_pass":
            counts,
        "metrics_warn":
            counts,
        "metrics_fail":
            counts,
        "metrics_not_evaluated":
            counts,
        "metrics_info":
            counts,
        "bq_bytes_processed":
            self.bq_bytes_estimate,
        "predicted_shuffle_gb":
            self.predicted_shuffle_gb,
        "warnings":
            list(self.warnings),
        "artifacts_uri":
            None,
    }
    return _ordered(row, fields)

  def _generation_fields(self) -> dict[str, Any]:
    launch = self.launch
    names = sorted({t.model for t in self.tables if t.model})
    adjusted = launch.adjusted_model_uri if launch.model_adjusted else None
    return {
        "generation_job_id":
            launch.generation_job_id,
        "generation_job_name":
            launch.job_name,
        "generation_region":
            launch.region,
        "generation_started_at":
            _rfc3339(launch.started_at) if launch.started_at else None,
        "generation_finished_at":
            _rfc3339(launch.finished_at) if launch.finished_at else None,
        "base_run_id":
            launch.base_run_id,
        "run_ids":
            list(launch.run_ids),
        "params_source":
            launch.params_source,
        "relationship_model":
            launch.model_name or ",".join(names) or None,
        "relationship_model_sha":
            launch.model_sha or self.model_sha,
        "relationship_model_uri":
            adjusted or launch.relationships_uri,
        "model_adjusted":
            launch.model_adjusted,
    }


# --------------------------------------------------------------------------
# build_plan
# --------------------------------------------------------------------------
def table_roles(edges_by_table: Mapping[str, Sequence[Edge]]) -> dict[str, str]:
  """The role of each launch table among `edges_by_table` (table name →
  its edges), from the ENFORCED edges only:

      driven      an enforced edge into another table of the launch
      side_input  enforced edges, all into tables outside it
      root        no enforced edge, and a launch table references it
      isolated    neither

  (`standalone` and `external` are not derived from edges: the planner
  sets them for a table without a model entry and a read-only parent.)
  """
  referenced = {
      e.ref
      for edges in edges_by_table.values()
      for e in edges
      if e.enforced and not e.external and e.ref in edges_by_table
  }
  roles = {}
  for name, edges in edges_by_table.items():
    enforced = [e for e in edges if e.enforced]
    if any(not e.external and e.ref in edges_by_table for e in enforced):
      roles[name] = "driven"
    elif enforced:
      roles[name] = "side_input"
    elif name in referenced:
      roles[name] = "root"
    else:
      roles[name] = "isolated"
  return roles


def _flag(value: Any, default: bool) -> bool:
  if value is None or value == "":
    return default
  if isinstance(value, bool):
    return value
  return str(value).strip().lower() not in ("false", "0", "no", "off")


def _names(value: Any) -> tuple[str, ...]:
  return tuple(p.strip() for p in str(value or "").split(",") if p.strip())


def parent_landing(child: str, edge: Edge) -> str:
  """The landing table of `edge`'s parent when the launch that wrote
  `child` (its landing table, `project.dataset.table`) did not write the
  parent: an external `ref` (`dataset.table`, or fully qualified) resolves
  in the child's project; an in-model parent lives in the child's dataset
  (the generator's parent landing). The planner reads its read-only
  parents from here and `beam.relational` finds them by it."""
  project, dataset, _ = child.split(".")
  if edge.external:
    parts = edge.ref.split(".")
    return normalize_fqn(
        edge.ref if len(parts) == _FQN_PARTS else f"{project}.{edge.ref}")
  return normalize_fqn(f"{project}.{dataset}.{edge.ref}")


@dataclass
class _Work:  # pylint: disable=too-many-instance-attributes  # the planner's scratch record
  """One table while it is being planned (mutable scratch)."""
  name: str
  landing: str
  role: str
  model: str | None = None
  pk: tuple[str, ...] = ()
  identity: tuple[str, ...] = ()
  edges: tuple[Edge, ...] = ()
  key_cols: set[str] = field(default_factory=set)
  children: list[_Work] = field(default_factory=list)
  run: RunRecord | None = None
  source: str | None = None
  scope: ScopePlan | None = None
  pin: SourcePin | None = None
  cols_syn: list[ColumnPlan] = field(default_factory=list)
  cols_src: list[ColumnPlan] = field(default_factory=list)
  src_queries: tuple[str, ...] = ()
  syn_queries: tuple[str, ...] = ()
  src_stats: dict[str, Any] | None = None
  syn_stats: dict[str, Any] | None = None
  panel: Panel | None = None
  drifted: bool | None = None
  skip: str | None = None
  source_read: str = ""
  synthetic_read: str = ""
  rate_src: float | None = None
  rate_syn: float | None = None
  columns: list[ColumnPlan] = field(default_factory=list)
  notes: list[str] = field(default_factory=list)
  table_rows: dict[str, int] = field(default_factory=dict)  # side → numRows
  sample_bytes: dict[str, int] = field(default_factory=dict)  # side → dry run

  @property
  def active(self) -> bool:
    return self.role != "external" and self.skip is None

  def skip_with(self, reason: str) -> None:
    self.skip = reason
    self.notes.append(f"{self.landing}: not evaluated — {reason}")


def _incomparable(landing: ColumnPlan, source: ColumnPlan) -> bool:
  """The two sides do not hold the same kind of quantity: different
  kinds, or temporal families that differ on the micros scale — a TIME
  (time of day) against any date/instant, or a DATE against a TIMESTAMP
  (a civil day against an instant). DATE/DATETIME and DATETIME/TIMESTAMP
  compare (both read as UTC)."""
  if landing.kind != source.kind:
    return True
  if landing.kind is not ColumnKind.TEMPORAL:
    return False
  types = {landing.bq_type, source.bq_type}
  return ("TIME" in types and len(types) > 1) or types == {"DATE", "TIMESTAMP"}


def _unreadable(landing: str, reason: str) -> ScopePlan:
  return ScopePlan(
      landing_table=landing,
      mode="table",
      status="unknown",
      reason=reason,
      read_table="",
      prepare_sql=(),
      window=(None, None),
      expected_rows=None)


_LOGGER = logging.getLogger(__name__)
_ALL_TABLES = "(all tables)"
# The `Bq` calls planning counts, by what they are (the summary line).
_STATEMENT_KINDS = {
    "table": "metadata reads",
    "query": "queries",
    "dry_run_bytes": "dry runs",
    "execute": "DDL",
}

_ERROR_CHARS = 300  # of another table's error, in the planner's log


def _first_line(exc: BaseException) -> str:
  lines = str(exc).strip().splitlines()
  return (lines[0] if lines else type(exc).__name__)[:_ERROR_CHARS]


class _ScansInterruptedError(RuntimeError):
  """Planning was interrupted while the tables were scanned: the scan
  that meets this sends no further statement. No caller sees it: the
  interrupt itself is what `scan_all` raises."""


class _CountedBq:
  """The planner's `Bq`, counting the statements it sends by kind
  (`_STATEMENT_KINDS`); everything else passes through. The scans of
  several tables send theirs from several threads at once, so a count is
  taken under a lock (`counts[kind] += 1` is a read, then a write)."""

  def __init__(self, bq: Any):
    self._bq = bq
    self._lock = threading.Lock()
    self.counts: Counter[str] = Counter()

  def __getattr__(self, name: str) -> Any:
    attribute = getattr(self._bq, name)
    kind = _STATEMENT_KINDS.get(name)
    if kind is None or not callable(attribute):
      return attribute

    def counted(*args: Any, **kwargs: Any) -> Any:
      with self._lock:
        self.counts[kind] += 1
      return attribute(*args, **kwargs)

    return counted

  def summary(self) -> str:
    """`N BigQuery statements (a metadata reads, b queries, …)`."""
    kinds = ", ".join(
        f"{self.counts[kind]} {kind}" for kind in _STATEMENT_KINDS.values())
    return f"{sum(self.counts.values())} BigQuery statements ({kinds})"


class _Planner:  # pylint: disable=too-many-instance-attributes  # holds one build_plan call's state
  """One `build_plan` call (see its docstring)."""

  def __init__(self, *, launch: LaunchContext, models: Sequence[RelModel],
               bq: Any, knobs: Knobs, mode: str, now: str | datetime,
               evaluation_id: str, salt: str, temp_dataset: str):
    self.launch = launch
    self.models = tuple(models)
    self.bq = _CountedBq(bq)
    self.started = time.monotonic()
    self.knobs = knobs
    self.budget = knobs.budget
    self.mode = mode
    self.now = now
    self.evaluation_id = evaluation_id
    self.salt = salt
    self.temp_dataset = temp_dataset
    self.notes: list[str] = []
    self.bytes: dict[str, int] = {"planning": 0, "panel": 0, "prepare": 0}
    self.samples: list[PrepareStatement] = []
    self.planning_ddl: list[PrepareStatement] = []  # run in phase A (R57)
    self.multi = len(launch.tables_in_order) > 1
    self._interrupted = threading.Event()  # set once: the scans stop

  @contextlib.contextmanager
  def timed(self, table: str, step: str) -> Iterator[None]:
    """Log one finished planning step and the seconds it took (table
    names only: no SQL, no value)."""
    began = time.monotonic()
    try:
      yield
    finally:
      _LOGGER.info("planning %s %s: %s %.2fs", self.evaluation_id, table, step,
                   time.monotonic() - began)

  # --- tables ----------------------------------------------------------------
  def targets(self) -> list[_Work]:
    """Launch tables (parents first) and their read-only parents; raises
    `PlanError` for a table the model does not declare (Ruling R50)."""
    listed = [normalize_fqn(t) for t in self.launch.tables_in_order]
    tables = list(dict.fromkeys(listed))
    if len(tables) != len(listed):
      self.notes.append("the launch listed a table more than once; each is "
                        "evaluated once")
    if not tables:
      raise PlanError("the launch resolved no landing table to evaluate")
    self._check_models(tables)
    generate = _flag(
        self.launch.params.get("generate_fk_relationships"), default=True)
    works = [self._launch_work(t, generate) for t in tables]
    works = self._parents_first(works)
    by_name = {w.name: w for w in works}
    self._assign_roles(works, by_name)
    parents = self._read_only_parents(works, by_name)
    if generate:
      self._check_components(works, {p.name for p in parents})
    return parents + works

  def _check_components(self, works: Sequence[_Work],
                        read_only: set[str]) -> None:
    """Warn about model tables connected to the launch that it neither
    wrote nor reads as a parent: they are not evaluated."""
    launched = {w.name for w in works}
    connected = {
        t for w in works if w.model is not None
        for t in component(self.models, w.name)
    }
    left = sorted(connected - launched - read_only)
    if left:
      self.notes.append(
          f"model tables {left} are connected to this launch's tables but "
          "were not generated by it; they are not evaluated")

  def _check_models(self, tables: Sequence[str]) -> None:
    missing = [t for t in tables if model_for(self.models, t) is None]
    if not missing:
      return
    relationships_off = (not self.models and
                         not (self.launch.relationships_uri or "").strip())
    if len(tables) == 1 or relationships_off:
      if self.models:
        self.notes.append(
            f"{missing[0]} is not declared in the relationship model(s) "
            f"{self._model_names()}; like the generator, which generates an "
            "undeclared table in isolation, it is evaluated standalone (keys "
            "from --pk_cols / --identity_cols) — R55")
      elif len(tables) > 1:
        self.notes.append(
            f"relationships are off for this launch (no relationships_uri "
            f"and no model): each of its {len(tables)} tables is evaluated "
            "standalone, with no FK edges")
      return
    uri = self.launch.relationships_uri or "(no relationships_uri recorded)"
    adjusted = self.launch.adjusted_model_uri
    also = f" (adjusted model: {adjusted})" if adjusted else ""
    missing_text = ", ".join(missing)
    raise PlanError(
        f"{missing_text}: no entry in the relationship model(s) loaded from "
        f"{uri}{also} — models {self._model_names()}. A relational launch is "
        "evaluated against the model it applied: a table the model does not "
        "declare has no keys, parents or edges to evaluate, and is never "
        "skipped silently. Load the model the launch applied, or evaluate "
        "the table alone (a single-table launch).")

  def _model_names(self) -> list[str]:
    return sorted({m.model for m in self.models})

  def _launch_work(self, landing: str, generate: bool) -> _Work:
    name = landing.rsplit(".", 1)[1]
    model = model_for(self.models, name)
    if model is None:
      return _Work(
          name=name,
          landing=landing,
          role="standalone",
          pk=_names(self.launch.params.get("pk_cols")),
          identity=_names(self.launch.params.get("identity_cols")))
    relations = model.tables[name]
    edges = self._edges(name, relations.enabled)
    if edges and not generate:
      self.notes.append(
          f"{landing}: generate_fk_relationships=false — its FK edges were "
          "not generated, so they are not evaluated")
      edges = ()
    return _Work(
        name=name,
        landing=landing,
        role="",
        model=model.model,
        pk=relations.pk,
        identity=relations.identity,
        edges=edges)

  def _edges(self, name: str, enabled: bool) -> tuple[Edge, ...]:
    """Enforced edges as drawn (widened), then documented edges whose
    parent is external or enabled."""
    if not enabled:
      return ()
    model = model_for(self.models, name)
    assert model is not None
    documented = tuple(
        e for e in model.tables[name].fk
        if not e.enforced and (e.external or self._enabled(e.ref)))
    return enforced_edges(self.models, name) + documented

  def _enabled(self, table: str) -> bool:
    model = model_for(self.models, table)
    return model is None or model.tables[table.rsplit(".", 1)[-1]].enabled

  def _parents_first(self, works: list[_Work]) -> list[_Work]:
    position = {w.name: i for i, w in enumerate(works)}
    late = [
        w.name
        for w in works
        for e in w.edges
        if e.enforced and position.get(e.ref, -1) > position[w.name]
    ]
    if not late:
      return works
    order = generation_order(self.models, tuple(position))
    self.notes.append(
        f"the launch's table order put {sorted(set(late))} before their "
        f"parents; evaluated parents first as {list(order)}")
    by_name = {w.name: w for w in works}
    return [by_name[n] for n in order]

  @staticmethod
  def _assign_roles(works: list[_Work], by_name: Mapping[str, _Work]) -> None:
    roles = table_roles({name: work.edges for name, work in by_name.items()})
    for work in works:
      work.key_cols = set(work.pk) | {c for e in work.edges for c in e.cols}
      if work.role != "standalone":
        work.role = roles[work.name]

  def _read_only_parents(self, works: Sequence[_Work],
                         by_name: Mapping[str, _Work]) -> list[_Work]:
    parents: dict[str, _Work] = {}
    for work in works:
      for edge in work.edges:
        if not edge.external and edge.ref in by_name:
          continue
        landing = parent_landing(work.landing, edge)
        parent = parents.setdefault(
            landing,
            _Work(name=edge.ref_name, landing=landing, role="external"))
        parent.key_cols.update(edge.ref_cols)
        parent.children.append(work)
    return list(parents.values())

  # --- per launch table ------------------------------------------------------
  def locate(self, work: _Work) -> None:
    """Scope, source pin and provisional columns of one launch table."""
    with self.timed(work.name, "locate"):
      land_meta = self._locate_landing(work)
    if land_meta is not None:
      with self.timed(work.name, "source pin"):
        self._pin_and_columns(work, land_meta)

  def _locate_landing(self, work: _Work) -> Mapping[str, Any] | None:
    """The landing table's metadata and the scope of one launch table, or
    None when the table is skipped (its reason is on `work`)."""
    land_meta: Mapping[str, Any]
    try:
      land_meta = self.bq.table(work.landing)
    except _BQ_ERRORS as exc:
      work.scope = _unreadable(work.landing,
                               f"the landing table could not be read ({exc})")
      work.skip_with(work.scope.reason or "")
      return None
    work.run = self.launch.run_for(work.landing)
    work.table_rows["syn"] = int(land_meta.get("numRows") or 0)
    work.scope = resolve_scope(
        landing_table=work.landing,
        write_disposition=self.launch.write_disposition,
        writes=self.launch.writes,
        foreign=self._foreign(work, land_meta),
        expected_rows=work.run.valid_count if work.run else None,
        requested=self.knobs.scope,
        now=self.now,
        time_travel_hours=int(land_meta["timeTravelHours"]),
        temp_dataset=self.temp_dataset,
        evaluation_id=self.evaluation_id,
        allow_contaminated=self.knobs.allow_contaminated,
        table_created=land_meta.get("created"))
    if not work.scope.readable:
      work.skip_with(f"scope {work.scope.status}: {work.scope.reason}")
      return None
    if not work.scope.ok:
      work.notes.append(f"{work.landing}: scope {work.scope.status} — "
                        f"{work.scope.reason}")
    work.source = self._source_of(work)
    if work.source is None:
      work.skip_with("no source table is known (validation_runs has no "
                     "reference_table for it and the launch named no "
                     "--reference_table); pass manual reference_table")
      return None
    return land_meta

  def locate_table(self, work: _Work) -> None:
    """One phase-A launch table: `locate`, then (still active)
    `create_planning_tables` — guarded against the table's OWN metadata
    being malformed or missing (e.g. a non-numeric or absent
    `timeTravelHours`): a `KeyError`/`TypeError`/`ValueError` here marks
    just this table unreadable, the same as a read failure, rather than
    escaping this per-table loop in `build_plan`. R61 picks "skip this
    table, keep planning" over raising — a later table's bad metadata
    says nothing about an earlier table's already-created snapshot
    (`planning_ddl`, R57/R58), so there is nothing to unwind. `PlanError`
    (itself a `ValueError`, e.g. `create_planning_tables`' own 409
    Already Exists) is excluded from that and always still escapes."""
    try:
      self.locate(work)
      if work.active:
        with self.timed(work.name, "planning tables"):
          self.create_planning_tables(work)
    except PlanError:
      # PlanError IS a ValueError (create_planning_tables' own 409
      # Already Exists, R58): that one must still escape, planning_ddl
      # and all — never mistaken for this table's own malformed metadata.
      raise
    except _METADATA_ERRORS as exc:
      reason = f"{work.landing}: table metadata is malformed ({exc})"
      if work.scope is None:
        work.scope = _unreadable(work.landing, reason)
        work.skip_with(reason)
      else:
        self._unreadable_scope(work, reason)

  def _foreign(self, work: _Work, meta: Mapping[str,
                                                Any]) -> tuple[JobWrite, ...]:
    own = [
        w for w in self.launch.writes if normalize_fqn(w.table) == work.landing
    ]
    job = self.launch.generation_job_id
    location = self.bq.location or meta.get("location")
    if not own or not job or not location:
      return ()
    try:
      start = min(parse_timestamp(w.start or w.end) for w in own) - _PAD
      return tuple(
          foreign_writes(
              self.bq,
              location=str(location),
              table=work.landing,
              window=(_rfc3339(start), None),
              exclude_job=job,
              max_bytes=self.budget.max_bytes_billed))
    except (*_BQ_ERRORS, ValueError) as exc:
      work.notes.append(
          f"{work.landing}: the contamination check (other writers in the "
          f"job's window) could not run ({exc}); the scope rests on the "
          "row-count check alone")
      return ()

  def _source_of(self, work: _Work) -> str | None:
    if work.run is not None and work.run.reference_table:
      return normalize_fqn(work.run.reference_table)
    reference = (
        self.launch.reference_table or
        str(self.launch.params.get("reference_table") or "").strip())
    if not reference:
      return None
    reference = normalize_fqn(reference)
    if not self.multi:
      return reference
    dataset = reference.rsplit(".", 1)[0]
    return f"{dataset}.{work.name}"

  def _pin_and_columns(self, work: _Work, land_meta: Mapping[str, Any]) -> None:
    assert work.source is not None and work.scope is not None
    try:
      src_meta = self.bq.table(work.source)
    except _BQ_ERRORS as exc:
      # R61: generalises _unreadable_scope's principle to this bail too —
      # a skipped table carries no temp-table names.
      self._unreadable_scope(
          work, f"the source table {work.source} could not be read ({exc})")
      return
    work.table_rows["src"] = int(src_meta.get("numRows") or 0)
    created = self.launch.started_at
    work.pin = pin_source(
        source_table=work.source,
        job_create_time=created,
        now=self.now,
        time_travel_hours=int(src_meta["timeTravelHours"]),
        temp_dataset=self.temp_dataset,
        evaluation_id=self.evaluation_id)
    if work.pin.reason:
      work.notes.append(f"{work.source}: {work.pin.reason}")
    modified = src_meta.get("lastModified")
    if modified and created:
      work.drifted = parse_timestamp(modified) > parse_timestamp(created)
    self._columns(work, land_meta["schema"], src_meta["schema"])
    work.src_queries = planning_queries(work.pin, work.cols_src)
    work.syn_queries = planning_queries(work.scope, work.cols_syn)
    work.source_read = work.pin.read_table
    work.synthetic_read = work.scope.read_table

  def _columns(self, work: _Work, landing_fields: Sequence[Mapping],
               source_fields: Sequence[Mapping]) -> None:
    source_by = {str(f["name"]): f for f in source_fields}
    landing_names = {str(f["name"]) for f in landing_fields}
    common = [f for f in landing_fields if str(f["name"]) in source_by]
    only = sorted(landing_names ^ set(source_by))
    if only:
      work.notes.append(f"{work.landing}: columns {only} exist on one side "
                        "only and are not compared")
    identity = set(work.identity)
    syn = kinds_from_schema(common, keys=work.key_cols, identity=identity)
    src = kinds_from_schema([source_by[str(f["name"])] for f in common],
                            keys=work.key_cols,
                            identity=identity)
    pairs = list(zip(syn, src, strict=True))
    clash = [
        f"{a.name} ({a.bq_type} vs {b.bq_type})" for a, b in pairs
        if _incomparable(a, b)
    ]
    if clash:
      work.notes.append(f"{work.landing}: columns {clash} (landing vs source "
                        "type) are not the same kind of quantity and are not "
                        "compared")
    unknown = sorted({c.bq_type for c in syn if c.bq_type not in KNOWN_TYPES})
    if unknown:
      work.notes.append(f"{work.landing}: BigQuery types {unknown} are not "
                        "known to the planner; those columns are nested "
                        "(null counts only)")
    kept = [i for i, (a, b) in enumerate(pairs) if not _incomparable(a, b)]
    work.cols_syn = [syn[i] for i in kept]
    work.cols_src = [src[i] for i in kept]

  # --- bytes -----------------------------------------------------------------
  def panel_n(self) -> int | None:
    n = self.launch.typed_filters().get("reference_rows_limit")
    return n if isinstance(n, int) and n > 0 else None

  def dry_run(self, works: Sequence[_Work]) -> None:
    """Dry-run every query planning issues and every prepare statement —
    and, sampled, the full read of every side whose table holds more than
    `sample_rows` (the worst case: the planning counts that decide it are
    not known yet) — then refuse the plan over `max_bytes_billed`, before
    anything is billed. A table whose dry run BigQuery rejects (a 400, a
    denied or missing table) is skipped as scope `unknown` with the error
    as its reason — naming the source when the source side's own reads are
    what failed (R58) — and its bytes are not counted; a `_dry_prepare`
    fallback note gathered for it is dropped rather than kept stale. The
    other tables go on."""
    worst = 0
    for work in works:
      before = dict(self.bytes)
      pending: list[str] = []
      try:
        self._dry_run_source(work, pending)
      except _BQ_ERRORS as exc:
        self.bytes = before
        self._unreadable_scope(work,
                               f"source dry run failed: {work.source} ({exc})")
        continue
      try:
        worst += self._dry_run_synthetic(work, pending)
      except _BQ_ERRORS as exc:
        self.bytes = before
        self._unreadable_scope(
            work, f"scope unknown: a dry run of its reads failed ({exc})")
        continue
      work.notes.extend(pending)
    parts = dict(self.bytes)
    if self.mode == "sampled":
      parts["sample (worst case)"] = worst
    self.budget.check_bytes(parts)

  def _dry_run_source(self, work: _Work, notes: list[str]) -> None:
    """The source side's phase-A dry runs (into `self.bytes`): its
    planning queries, the R/E/H panel and its prepare DDL. A
    `_dry_prepare` fallback note is appended to `notes`, not committed
    yet (R58: dropped if the table's dry run fails after all)."""
    assert work.pin is not None
    n = self.panel_n()
    for sql in work.src_queries:
      self.bytes["planning"] += self.bq.dry_run_bytes(sql,
                                                      read_params(work.pin))
    if n is not None:
      self.bytes["panel"] += self.bq.dry_run_bytes(
          panel_sql(work.pin, n), read_params(work.pin))
    for sql in work.pin.prepare_sql:
      self.bytes["prepare"] += self._dry_prepare(sql, work.pin, notes)

  def _dry_run_synthetic(self, work: _Work, notes: list[str]) -> int:
    """The synthetic side's phase-A dry runs (into `self.bytes`) and,
    sampled mode, the worst-case full-read bytes of both sides over
    `sample_rows`. Returns the worst-case sample bytes; `notes` collects
    `_dry_prepare` fallbacks the same way as the source side."""
    # The sampled path (_worst_samples) reads work.pin too, for the
    # source side's own worst-case bytes: asserted here, not only inside
    # it, since this is where the real precondition is (R61).
    assert work.scope is not None and work.pin is not None
    for sql in work.syn_queries:
      self.bytes["planning"] += self.bq.dry_run_bytes(sql, work.scope.params)
    for sql in work.scope.prepare_sql:
      self.bytes["prepare"] += self._dry_prepare(sql, work.scope, notes)
    return self._worst_samples(work) if self.mode == "sampled" else 0

  def _unreadable_scope(self, work: _Work, why: str) -> None:
    """Skip one table whose scope cannot be read after all: `why` becomes
    both the scope's reason (status `unknown`) and, verbatim, the table's
    skip reason — the caller names the side that failed. Every read table
    already resolved for it — the scope's own, and the source pin's — is
    cleared too (R58): a skipped table reports no temp table, snapshot or
    pin, pinned or not. The rest of the plan goes on."""
    assert work.scope is not None
    work.scope = dataclasses.replace(
        work.scope,
        status="unknown",
        reason=why,
        read_table="",
        read_expr="",
        prepare_sql=(),
        params={})
    work.synthetic_read = ""
    work.source_read = ""
    work.pin = None
    work.skip_with(why)

  def create_planning_tables(self, work: _Work) -> None:
    """R57 (R5 amended): an as_of_diff scope's start snapshot — zero bytes
    billed, expiring in 24 h — is the one table planning creates, before
    the dry runs, because the scope's `read_expr` reads it. A failure
    (e.g. the table did not exist yet at the window start) skips only
    this table — except a 409 Already Exists, which means the same
    `evaluation_id` already created it under a previous, still-live
    attempt: that RAISES `PlanError`, since every planning attempt must
    mint a fresh `evaluation_id` (R58)."""
    assert work.scope is not None
    for sql in work.scope.planning_sql:
      try:
        self.bq.execute(sql, {}, max_bytes=self.budget.max_bytes_billed)
      except _BQ_ERRORS as exc:
        if _is_already_exists(exc):
          raise PlanError(
              f"{work.landing}: the as_of_diff start snapshot "
              f"{work.scope.start_table} already exists ({exc}) — each "
              "planning attempt needs a fresh evaluation_id",
              tuple(self.planning_ddl)) from exc
        self._unreadable_scope(
            work, f"scope unknown: its as_of_diff start snapshot could not "
            f"be created ({exc})")
        return
      self.planning_ddl.append(PrepareStatement(sql, {}))
      work.notes.append(
          f"{work.landing}: planning created the as_of_diff start snapshot "
          f"{work.scope.start_table} (zero bytes billed, expires in 24 h)")

  def _worst_samples(self, work: _Work) -> int:
    assert work.scope is not None and work.pin is not None
    total = 0
    for side, source in (("src", work.pin), ("syn", work.scope)):
      if work.table_rows.get(side, 0) > self.knobs.sample_rows:
        work.sample_bytes[side] = self._full_read_bytes(source)
        total += work.sample_bytes[side]
    return total

  def _full_read_bytes(self, source: SourcePin | ScopePlan) -> int:
    """The dry-run bytes of reading every column of `source`'s rows."""
    return int(
        self.bq.dry_run_bytes(f"SELECT * FROM {from_item(source)} AS t",
                              read_params(source)))

  def _dry_prepare(self, sql: str, source: SourcePin | ScopePlan,
                   notes: list[str]) -> int:
    """A DDL statement's dry run, with `source`'s parameters; when
    BigQuery refuses to dry-run it, the full read it materializes stands
    in (an upper bound), noted in `notes` — committed to the table's
    warnings only once the rest of its phase-A dry run also succeeds
    (R58: a table that fails afterward drops the note as stale)."""
    try:
      return int(self.bq.dry_run_bytes(sql, read_params(source)))
    except _BQ_ERRORS as exc:
      notes.append(f"a prepare statement could not be dry-run ({exc}); its "
                   "full read is counted instead")
      return self._full_read_bytes(source)

  # --- running the scans -----------------------------------------------------
  def scan_all(self, works: Sequence[_Work]) -> None:
    """`scan` of every table of `works`, at once (module docstring,
    Concurrent scans): `min(len(works), SCAN_THREADS)` threads, each
    table's own statements in order on its own thread. Returns when
    every scan has ended.

    A scan writes only its own table's `_Work`; what the threads share
    is the BigQuery client, the statement count (`_CountedBq`, locked),
    the stop signal and the log.

    Raises:
      Exception: the error of the first table IN TABLE ORDER whose scan
        failed — the object that scan raised, as a table-after-table
        planning would have raised it. Every other scan has run to its
        end by then, so their statements were billed: a cost, never a
        result. Any other table's failure is logged as a WARNING.
      BaseException: whatever interrupted this thread while it waited
        (`KeyboardInterrupt`, `SystemExit`), at once: the scans not yet
        started are cancelled and a scan in flight sends no further
        statement.
    """
    if not works:
      return
    pool = ThreadPoolExecutor(
        max_workers=min(len(works), SCAN_THREADS),
        thread_name_prefix="sdfb-eval-scan")
    try:
      with self.timed(_ALL_TABLES, "scans"):
        futures = [pool.submit(self.scan, work) for work in works]
        pool.shutdown(wait=True)
    except BaseException:
      # Not a scan's error (those stay in the futures): this thread was
      # interrupted while it waited. Stop everything it can still stop.
      self._interrupted.set()
      pool.shutdown(wait=False, cancel_futures=True)
      raise
    failed = [(work, future.exception())
              for work, future in zip(works, futures, strict=True)]
    errors = [(work, error) for work, error in failed if error is not None]
    if not errors:
      return
    (first, raised), *others = errors
    for work, error in others:
      _LOGGER.warning(
          "planning %s %s: its scan failed too (%s: %s); the error raised "
          "is the one of %s, the first failing table in table order",
          self.evaluation_id, work.name,
          type(error).__name__, _first_line(error), first.name)
    raise raised

  def _proceed(self) -> None:
    """Called by a scan before each statement it sends.

    Raises:
      _ScansInterruptedError: planning was interrupted (`scan_all`).
    """
    if self._interrupted.is_set():
      raise _ScansInterruptedError(
          "planning was interrupted: no further statement is sent")

  def scan(self, work: _Work) -> None:
    """The planning SELECTs of both sides, the row-count check and the
    R/E/H panel of one table. Run on a thread of `scan_all`: it may
    write `work` and nothing else of the planner, and it asks
    `_proceed` before every statement."""
    assert work.scope is not None and work.pin is not None
    cap = self.budget.max_bytes_billed
    with self.timed(work.name, "planning statistics"):
      src_rows = [
          self._one(sql, read_params(work.pin), cap) for sql in work.src_queries
      ]
      syn_rows = [
          self._one(sql, work.scope.params, cap) for sql in work.syn_queries
      ]
      work.src_stats = parse_planning(src_rows, work.cols_src)
      work.syn_stats = parse_planning(syn_rows, work.cols_syn)
    with self.timed(work.name, "scope verification"):
      verified = work.scope.verify(int(work.syn_stats["rows"]))
      if verified.status != work.scope.status:
        work.notes.append(f"{work.landing}: scope {verified.status} — "
                          f"{verified.reason}")
      work.scope = verified
    with self.timed(work.name, "reference panel"):
      self._read_panel(work, cap)

  def _read_panel(self, work: _Work, cap: int) -> None:
    assert work.pin is not None
    n = self.panel_n()
    if n is None:
      work.notes.append(f"{work.landing}: reference_rows_limit unknown — no "
                        "R/E/H panel, so the reference-based privacy metrics "
                        "are not evaluated")
      return
    self._proceed()
    work.panel = fetch_panel(
        self.bq,
        source_read_table=work.pin,
        n=n,
        expected_digest=work.run.reference_digest if work.run else None,
        max_bytes=cap)
    if not work.panel.verified:
      work.notes.append(f"{work.landing}: {work.panel.reason}")

  def _one(self, sql: str, params: Mapping[str, Any], cap: int) -> dict:
    self._proceed()
    rows = self.bq.query(sql, dict(params), max_bytes=cap)
    if len(rows) != 1:
      raise ValueError(f"a planning SELECT returned {len(rows)} rows, "
                       "expected exactly 1")
    return dict(rows[0])

  # --- sampling and census ---------------------------------------------------
  def sample(self, work: _Work) -> None:
    """In sampled mode, a salted sample CTAS per side over `sample_rows`."""
    assert work.pin is not None and work.scope is not None
    assert work.src_stats is not None and work.syn_stats is not None
    work.rate_src, work.source_read = self._side_sample(
        work, work.pin, int(work.src_stats["rows"]), "src")
    work.rate_syn, work.synthetic_read = self._side_sample(
        work, work.scope, int(work.syn_stats["rows"]), "syn")
    work.src_stats["rows_read"] = work.src_stats["rows"] * work.rate_src
    work.syn_stats["rows_read"] = work.syn_stats["rows"] * work.rate_syn

  def _side_sample(self, work: _Work, source: SourcePin | ScopePlan, rows: int,
                   side: str) -> tuple[float, str]:
    """(rate, what the pipeline reads) of one side; a sample's full read
    reuses its phase-A dry run."""
    read_table = source.read_table
    if self.mode != "sampled" or rows <= self.knobs.sample_rows:
      return 1.0, read_table
    keep = min(
        math.ceil(self.knobs.sample_rows * SAMPLE_MODULUS / rows),
        SAMPLE_MODULUS)
    sql, temp, bound = sampled_read(
        read_table,
        keep=keep,
        modulo=SAMPLE_MODULUS,
        salt=self.salt,
        temp_dataset=self.temp_dataset,
        evaluation_id=self.evaluation_id,
        side=side)
    self.samples.append(PrepareStatement(sql, bound))
    cost = work.sample_bytes.get(side)
    if cost is None:  # planned rows exceed what the table metadata said
      cost = self._full_read_bytes(source)
    self.bytes["sample"] = self.bytes.get("sample", 0) + cost
    return keep / SAMPLE_MODULUS, temp

  def census(self, works: Sequence[_Work]) -> None:
    """Max-min fair census shares: fixed shuffle first, then tables, then
    (inside `apply_planning`) columns."""
    unlimited = Budget(
        max_shuffle_gb=math.inf, max_bytes_billed=self.budget.max_bytes_billed)
    demands, fixed = [], 0.0
    for work in works:
      assert work.src_stats is not None and work.syn_stats is not None
      exact = apply_planning(
          work.cols_syn, work.src_stats, work.syn_stats, budget=unlimited)
      rows = (_rows_read(work.src_stats), _rows_read(work.syn_stats))
      demands.append(value_census_bytes(exact, *rows))
      # the mask pass is never value-sampled: a fixed cost (R67)
      fixed += mask_bytes(exact, *rows)
      nonkey, keyed = table_key_shape(exact, work.pk, work.identity, work.edges)
      fixed += fixed_shuffle_bytes(
          rows_source=rows[0],
          rows_synthetic=rows[1],
          edges=len(work.edges),
          keyed_counts=int(bool(work.pk)) + int(bool(work.identity)),
          nonkey=nonkey,
          keyed=keyed,
          side_input=source_sets_fit(rows[0], nonkey=nonkey, keyed=keyed),
          table=work.name)
    capacity = self.budget.max_shuffle_gb * GB - fixed
    if capacity < 0:
      self.notes.append(
          f"the fixed shuffle alone (non-census parts and the mask pass, "
          f"{fixed / GB:.3f} GB) exceeds "
          f"max_shuffle_gb = {self.budget.max_shuffle_gb}: every census "
          f"column above {CENSUS_EXACT_FLOOR} keys is value-sampled at the "
          "minimum rate")
    for work, grant in zip(works, water_fill(demands, capacity), strict=True):
      share = Budget(
          max_shuffle_gb=grant / GB,
          max_bytes_billed=self.budget.max_bytes_billed)
      assert work.src_stats is not None and work.syn_stats is not None
      work.columns = apply_planning(
          work.cols_syn, work.src_stats, work.syn_stats, budget=share)
      sampled = [c.name for c in work.columns if c.census == "value_sampled"]
      if sampled:
        work.notes.append(f"{work.landing}: value-sampled census for "
                          f"{sampled} (max_shuffle_gb)")

  # --- read-only parents -----------------------------------------------------
  def locate_parent(self, work: _Work) -> None:
    """A read-only parent: its key columns, read as they are now."""
    try:
      meta = self.bq.table(work.landing)
    except _BQ_ERRORS as exc:
      work.scope = _unreadable(work.landing,
                               f"read-only parent could not be read ({exc})")
      work.notes.append(f"{work.landing}: {work.scope.reason}; the edges "
                        "into it are not evaluated")
      return
    fields = [f for f in meta["schema"] if str(f["name"]) in work.key_cols]
    work.columns = kinds_from_schema(fields, keys=work.key_cols)
    missing = sorted(work.key_cols - {c.name for c in work.columns})
    if missing:
      work.notes.append(f"{work.landing}: referenced key column(s) {missing} "
                        "are not in the table")
    work.scope = ScopePlan(
        landing_table=work.landing,
        mode="table",
        status="ok",
        reason="read-only parent: this launch did not write it; read as it "
        "is now, for integrity joins only",
        read_table=work.landing,
        prepare_sql=(),
        window=(None, None),
        expected_rows=None,
        read_expr=quote_fqn(work.landing))
    work.synthetic_read = work.landing
    work.source = self._parent_source(work)
    work.source_read = work.source or ""

  def _parent_source(self, work: _Work) -> str | None:
    sources = [c.source for c in work.children if c.source]
    if not sources:
      return None
    dataset = sources[0].rsplit(".", 1)[0]
    candidate = normalize_fqn(f"{dataset}.{work.name}")
    try:
      self.bq.table(candidate)
    except _BQ_ERRORS as exc:
      work.notes.append(f"{work.landing}: its source-side twin {candidate} "
                        f"could not be read ({exc}); source-side integrity "
                        "on the edges into it is not evaluated")
      return None
    return candidate

  # --- assembly --------------------------------------------------------------
  def table_plan(self, work: _Work) -> TablePlan:
    assert work.scope is not None
    columns = tuple(work.columns or work.cols_syn)
    active = work.active and work.src_stats is not None
    pairs = select_pairs(columns, self.knobs.pair_max_columns) if active else ()
    return TablePlan(
        name=work.name,
        landing_table=work.landing,
        source_table=work.source,
        run_id=work.run.run_id if work.run else None,
        role=work.role,
        pk=work.pk,
        identity=work.identity,
        edges=work.edges,
        columns=columns,
        scope=work.scope,
        source_read_table=work.source_read,
        source_pinned=bool(work.pin and work.pin.pinned),
        panel=work.panel,
        pairs=pairs,
        encoding_plan_digest=encoding_plan_digest(columns, pairs, work.edges),
        model=work.model,
        source_pin=work.pin,
        synthetic_read_table=work.synthetic_read,
        rows_source=int(work.src_stats["rows"])
        if active and work.src_stats else None,
        rows_synthetic=int(work.syn_stats["rows"])
        if active and work.syn_stats else None,
        sample_rate_source=work.rate_src if active else None,
        sample_rate_synthetic=work.rate_syn if active else None,
        source_drifted=work.drifted,
        reference_digest=work.run.reference_digest if work.run else None,
        skip_reason=work.skip,
        warnings=tuple(work.notes))


def _evaluation_key(launch: LaunchContext, catalogue_version: str, mode: str,
                    knobs: Knobs) -> str:
  payload = {
      "generation_job_id": launch.generation_job_id,
      "run_ids": list(launch.run_ids),
      "tables": [normalize_fqn(t) for t in launch.tables_in_order],
      "catalogue_version": catalogue_version,
      "evaluator_version": EVALUATOR_VERSION,
      "mode": mode,
      "knobs": knobs.key_dict(),
  }
  text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
  return hashlib.blake2b(text.encode(), digest_size=16).hexdigest()


def evaluation_key(launch: LaunchContext, mode: str,
                   knobs: Knobs | Mapping[str, Any]) -> str:
  """The `evaluation_key` `build_plan` gives the evaluation of `launch`
  under `mode` and `knobs`, with the packaged catalogue: for a caller
  that has a resolved launch and no plan (a planning failure's registry
  row carries the key the plan would have had).

  Raises:
    ValueError: a landing table that is not `project.dataset.table`, or
      a bad knob.
  """
  knobs = knobs if isinstance(knobs, Knobs) else Knobs.from_mapping(knobs)
  return _evaluation_key(launch, load_catalogue().version, mode, knobs)


def _check_call(mode: str, trigger: str, runner: str) -> None:
  if mode not in MODES:
    raise ValueError(f"mode {mode!r}: expected one of {list(MODES)}")
  if trigger not in TRIGGERS:
    raise ValueError(f"trigger {trigger!r}: expected one of {list(TRIGGERS)}")
  if not isinstance(runner, str) or not runner.strip():
    raise ValueError(f"runner: expected a runner name, got {runner!r}")


def build_plan(*, launch: LaunchContext, models: Sequence[RelModel], bq: Any,
               knobs: Knobs | Mapping[str, Any], mode: str, trigger: str,
               runner: str, now: str | datetime) -> EvaluationPlan:
  """Plan the evaluation of one generation launch (module docstring).

  Identity first: `evaluation_key = blake2b(generation job id, run ids,
  tables, catalogue_version, evaluator_version, mode, value knobs)`,
  `salt = blake2b(evaluation_key)`, and `evaluation_id` (the knob, else
  `ev_<now>_<key[:8]>`) names every temp table. Then the tables (R50: a
  launch table with no model entry raises `PlanError` naming it and the
  model URI, unless the launch is a single table or relationships are
  off — then it is `standalone`), their scopes, source pins, dry runs
  (refused over `max_bytes_billed`), one planning SELECT per side, the
  panel, samples (`mode="sampled"`), census shares, pairs and digests.
  The scans (planning SELECTs, row-count check, panel) of the tables run
  at once, on up to `SCAN_THREADS` threads; everything after them runs
  one table after another in table order, so the plan is the one a
  table-after-table planning makes whichever scan ends first (R153).
  Only the planning SELECTs, the panel and the metadata reads run here;
  every DDL waits in `prepare_sql` (R5) — with ONE exception (R57): an
  `as_of_diff` scope's start snapshot (zero bytes billed, expiring in
  24 h) is created in phase A, before the dry runs, because the scope's
  `read_expr` reads it. Each one is recorded in `planning_ddl` and noted
  in the table's warnings. A table whose snapshot or dry run fails is
  skipped with scope status `unknown` and the error as its reason; the
  other tables are still planned. The same holds for a table whose OWN
  metadata is malformed or missing (e.g. a non-numeric `timeTravelHours`):
  `_Planner.locate_table` marks it unreadable and moves on rather than
  letting the `KeyError`/`TypeError`/`ValueError` escape this loop, so a
  later table's bad metadata can never lose an earlier table's
  already-created snapshot (R61).

  Raises:
    PlanError: a launch table the model does not declare (R50), no table
      at all, or (R58) a 409 Already Exists on an as_of_diff start
      snapshot — `planning_ddl` lists what phase A already created.
    BudgetExceededError: the dry runs exceed `max_bytes_billed`;
      `planning_ddl` lists what phase A already created (R58).
    ValueError: a bad mode/trigger/runner/knob, or malformed input that
      would reach SQL.
    Exception: a table's scan failed (a BigQuery error, a planning
      SELECT that did not return one row): the error of the first such
      table in table order, raised once every scan has ended — the
      other tables' statements have run by then (a cost, never a
      result); another table's failure is logged as a WARNING.
    KeyboardInterrupt, SystemExit: an interrupt during the scans, at
      once; no further statement is sent (module docstring).
  """
  knobs = knobs if isinstance(knobs, Knobs) else Knobs.from_mapping(knobs)
  _check_call(mode, trigger, runner)
  catalogue_version = load_catalogue().version
  key = _evaluation_key(launch, catalogue_version, mode, knobs)
  evaluated_at = _rfc3339(now)
  stamp = parse_timestamp(now).strftime("%Y%m%dT%H%M%S")
  planner = _Planner(
      launch=launch,
      models=models,
      bq=bq,
      knobs=knobs,
      mode=mode,
      now=now,
      evaluation_id=knobs.evaluation_id or f"ev_{stamp}_{key[:8]}",
      salt=hashlib.blake2b(key.encode(), digest_size=16).hexdigest(),
      temp_dataset=knobs.temp_dataset or f"{bq.project}.{knobs.output_dataset}")
  works = planner.targets()
  for work in works:
    if work.role != "external":
      planner.locate_table(work)
  active = [w for w in works if w.active]
  try:
    with planner.timed(_ALL_TABLES, "dry runs"):
      planner.dry_run(active)
    active = [w for w in active if w.active]  # a failed dry run skips a table
    planner.scan_all(active)
    for work in active:  # in table order: sampling appends to shared lists
      with planner.timed(work.name, "sampling"):
        planner.sample(work)
    planner.budget.check_bytes(planner.bytes)
  except BudgetExceededError as exc:
    # R58: phase A (create_planning_tables, above) already ran in full —
    # attach what it created so the CLI can report or clean it up.
    raise _BudgetExceededWithDdlError(str(exc),
                                      tuple(planner.planning_ddl)) from exc
  with planner.timed(_ALL_TABLES, "census"):
    planner.census(active)
  for work in works:
    if work.role == "external":
      with planner.timed(work.name, "read-only parent"):
        planner.locate_parent(work)
  tables = tuple(planner.table_plan(w) for w in works)
  plan = _assemble(planner, tables, key, evaluated_at, catalogue_version,
                   trigger, runner)
  _LOGGER.info("planning %s: done in %.2fs: %d tables, %s",
               planner.evaluation_id,
               time.monotonic() - planner.started,
               sum(1 for t in tables if t.role != "external"),
               planner.bq.summary())
  return plan


def _assemble(planner: _Planner, tables: tuple[TablePlan, ...], key: str,
              evaluated_at: str, catalogue_version: str, trigger: str,
              runner: str) -> EvaluationPlan:
  launch, knobs = planner.launch, planner.knobs
  notes = list(planner.notes)
  model_sha = sha12(planner.models) if planner.models else None
  if launch.model_sha and model_sha and launch.model_sha != model_sha:
    notes.append(f"the relationship model loaded here (sha {model_sha}) is "
                 f"not the one the launch applied (sha {launch.model_sha})")
  prepare = [
      PrepareStatement(sql, t.scope.params) for t in tables if t.evaluated
      for sql in t.scope.prepare_sql
  ]
  prepare += [
      PrepareStatement(sql, read_params(t.source_pin)) for t in tables
      if t.evaluated and t.source_pin is not None
      for sql in t.source_pin.prepare_sql
  ]
  predicted = predict_shuffle_gb(tables)
  if predicted > knobs.max_shuffle_gb:
    notes.append(f"predicted shuffle {predicted:.3f} GB exceeds "
                 f"max_shuffle_gb = {knobs.max_shuffle_gb} even with value "
                 "sampling (row-level and relational keys are not sampled)")
  table_notes = [n for t in tables for n in t.warnings]
  warnings = tuple(dict.fromkeys([*launch.warnings, *notes, *table_notes]))
  return EvaluationPlan(
      evaluation_id=planner.evaluation_id,
      evaluation_key=key,
      evaluated_at=evaluated_at,
      salt=planner.salt,
      mode=planner.mode,
      trigger=trigger,
      runner=runner,
      launch=launch,
      models=planner.models,
      model_sha=model_sha,
      tables=tables,
      knobs=knobs,
      prepare_sql=tuple(prepare) + tuple(planner.samples),
      planning_ddl=tuple(planner.planning_ddl),
      bq_bytes_estimate=sum(planner.bytes.values()),
      predicted_shuffle_gb=predicted,
      warnings=warnings,
      catalogue_version=catalogue_version,
      temp_dataset=planner.temp_dataset)
