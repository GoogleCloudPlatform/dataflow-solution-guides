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
"""The composed evaluation pipeline: one Beam graph per evaluation.

    per evaluated table, per side (source, synthetic; reference and
    holdout from the plan's panel when it has one — in the graph, or,
    given a `staging`, as objects the workers read: `beam.staged`):
      Sources.read (once per landing table and side: the relational
      │             pass reuses the same read for its parent keys)
      │  BatchElements ─► EncodeBatchFn ──► failed ─┐ (table, reason)
      ▼                                             │
    batches (every table, every side)               │
      ├─► DenseMetrics ─── accumulators ─┬─► CensusMetrics
      ├─► Membership                     └─► source_stats_drift (R62)
      ├─► Privacy                                   │
      └─► Relational (+ Sources for parents)        │
    + table.row_count_ratio, a driver-failed table's rows
      ▼                                             ▼
    MetricValue ─ Guard (a failed table's rows → not_evaluated: the
      │           driver's failures as a constant, the workers' as
      │           AsDict(failures)) ─ metric_row (rounded to what is
      │           persisted, a reversed CI dropped, THEN scored) ─┐
      │                                                         │
      └─► CombineGlobally(summary) ─► roll-up rows ─────────────┤
                    │                                           ▼
                    │     sinks: evaluation_metrics, evaluation_profiles,
                    │            evaluation_row_flags ─► load / copy job
                    │                                    PCollections
                    ▼                                           │
    FINAL registry row ◄── AsIter(every signal) ────────────────┘
      └─► sinks: evaluation_data_history

The label key is made ONCE on a worker (`LabelKey`, R64/R68) and the
same one-element PCollection feeds Dense, Census, Membership and
Privacy (their flags and labels) as `AsSingleton`; only its URI enters
the graph, and `--experiments=enable_data_sampling` is never set (it
would sample the key into the monitoring UI).

The FINAL row waits for every write to commit (D7). `BigQuerySinks`
returns `WriteToBigQuery`'s `destination_load_jobid_pairs` and
`destination_copy_jobid_pairs`, `LocalJsonSinks` its finalised files,
and the FINAL step reads every one of them as an `AsIter` side input.
What the two job-id PCollections are in Beam 2.76 (read in the installed
`apache_beam/io/gcp/bigquery_file_loads.py`):

    PCollection   produced by                       relative to the wait
    ────────────  ────────────────────────────────  ────────────────────
    load pairs    `TriggerLoadJobs.process()`, its  yielded when the job
                  ONGOING_JOBS tagged output        is SUBMITTED; the
                                                    wait on it comes
                                                    later, in
                                                    `finish_bundle`
    copy pairs    `TriggerCopyJobs.finish_bundle`,  yielded after the
                  its main output (temp-table       wait on each job
                  loads only: empty when a table
                  loads in one partition)

So a load pair is not, by itself, a finished load job. The ordering
rests on two runner guarantees instead: a bundle's outputs are committed
only when the bundle completes, `finish_bundle` included — the wait
belongs to the bundle that yielded the pair, and a failed job raises
there and fails that bundle — and a batch side input can be read only
once its whole PCollection is computed (the side-input barrier). The
FINAL step therefore cannot start before the bundles that submitted AND
awaited every load and copy job are done.

Failures stay per table (the run continues, nothing is dropped):

    where                             the table's rows
    ────────────────────────────────  ─────────────────────────────────────
    driver: a side cannot be read,    every owned metric not_evaluated
      the layout/spec/panel refs      with the reason (`failed_table_
      raise                           metrics`); no transform sees it
    worker: a batch fails to encode   every metric row it produced
                                      rewritten not_evaluated (Guard); its
                                      profiles and flags dropped
    inside a transform                the transform's own per-table
                                      handling (membership, privacy,
                                      relational)

The first two make the run PARTIAL with the reason — FAILED when they
leave no launch table evaluated (`assemble`'s status table); a
transform's own not_evaluated rows only count. A worker's `MemoryError`
is not a data error: it fails the bundle, which the runner retries.

What the driver saw fail is a CONSTANT of the graph: the Guard and the
FINAL step receive it as a plain argument, so no graded row of such a
table can exist whatever a runner does with a side input. Only a
worker's failure, unknown until the job runs, travels in the `failures`
side input.

Runners. That side input, the profile and flag filters, the FINAL row
written after every sink signal and every side input inside the
transforms (the label key, the panel sets, the parent key sets) rest on
one contract of batch execution: a side input is COMPLETE before the
step that reads it runs.

    runner                              the contract
    ──────────────────────────────────  ─────────────────────────────────
    Dataflow (batch)                    holds
    FnApiRunner (Beam's in-process      holds: a stage runs to completion
      Python runner)                    before the stages reading it
    Prism — what Beam's `DirectRunner`  does NOT hold (2.74, unchanged in
      hands a batch pipeline to         2.76): a step is "ready" once its
                                        side input's watermark has caught
                                        up with its main input's
                                        (`engine/elementmanager.go`,
                                        `stageState.bundleReady`), and
                                        both sit at the end-of-global-
                                        window timestamp while a
                                        GroupByKey's output is in flight
                                        on each branch — the step then
                                        runs with the side input as far as
                                        it has got, possibly empty

On Prism a failed table's `table.row_count_ratio` was published PASS
and its roll-ups scored, in the runs where the failure map was still in
its `CombinePerKey` while the relational rows left their `GroupByKey`.
So a local run is in process: `pipeline_options_defaults` turns
`DirectRunner` into `FnApiRunner`, and `build_evaluation_pipeline`
refuses a pipeline whose runner is Beam's switching `DirectRunner` or
`PrismRunner` rather than run with rows that may be wrong.

Runner defaults (`pipeline_options_defaults`): the runner as above;
`save_main_session` False (every DoFn is importable);
`max_cache_memory_usage_mb` sized to the side inputs a worker holds at
once (Beam 2.76's default is 0: no side-input cache, so every bundle
would re-read them) and at least 512 (it matters on Dataflow and has
no effect on a local run, whose in-process runner's embedded worker has
a fixed cache); on Dataflow `--experiments=upload_graph` (a graph this wide exceeds the job-creation
request limit).

`prepare_evaluation` runs the plan's DDL before the pipeline, with two
degradations instead of a failed job: a source pin (snapshot clone)
BigQuery refuses — a source in another organisation or region — reads
the source unpinned with a warning; and a read-only parent whose table
cannot be read (a `LIMIT 0` dry run: `tables.get` succeeding does not
mean `tables.getData` does) loses that side, so its edges say why
instead of failing at DIRECT_READ. Both degrade only on a REFUSAL — an
HTTP 400, 403 or 404, which `context.bq` raises as `BqApiError(status=
400)`, `PermissionError` and `LookupError`. Anything else (a 5xx, a 429,
a conflict, an error with no status) may succeed on a retry, so it
raises and the driver writes the FAILED row: a transient error never
silently changes what the evaluation reads.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

import apache_beam as beam

from sdfb_evaluation.beam.assemble import (
    SUMMARY_FIELDS,
    DriftSpec,
    RegistryContext,
    RowContext,
    RowSummary,
    StatsQuery,
    TableStats,
    aggregate_metrics,
    drift_metrics,
    failed_table_metrics,
    finish_time,
    guarded,
    metric_row,
    read_source_stats,
    row_count_metric,
    skipped_row,
    stable_profile,
    summarize_rows,
)
from sdfb_evaluation.beam.census import (
    CensusMetrics,
    CensusSpec,
    FreeTextPool,
    census_refs,
)
from sdfb_evaluation.beam.dense import DenseMetrics, DenseProfile, DenseSpec
from sdfb_evaluation.beam.encode import (
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    BatchLayout,
    EncodeBatchFn,
)
from sdfb_evaluation.beam.io import Sinks, Sources
from sdfb_evaluation.beam.label_key import LabelKey, Reader
from sdfb_evaluation.beam.membership import (
    TABLE_ERRORS,
    Membership,
    RowFlag,
    flag_row,
)
from sdfb_evaluation.beam.privacy import Privacy
from sdfb_evaluation.beam.relational import SIDE_INPUT, Relational, plan_edges
from sdfb_evaluation.beam.staged import Staging
from sdfb_evaluation.context.bq import BqApiError, normalize_fqn, quote_fqn
from sdfb_evaluation.context.bq import is_refusal
from sdfb_evaluation.context.budget import (
    SOURCE_SET_MAX_BYTES,
    source_set_arrays,
    source_sets_fit,
    table_key_shape,
)
from sdfb_evaluation.context.plan import (
    SAMPLE_MODULUS,
    EvaluationPlan,
    PrepareStatement,
    TablePlan,
)
from sdfb_evaluation.context.scope import SourcePin, sampled_read
from sdfb_evaluation.scoring import Thresholds, to_profile_row
from sdfb_evaluation.types import MetricValue, ProfileValue, Side

__all__ = [
    "MIN_CACHE_MB",
    "build_evaluation_pipeline",
    "pipeline_options_defaults",
    "prepare_evaluation",
    "side_input_bytes",
]

MIN_CACHE_MB = 512
_MB = 1 << 20
_CACHE_HEADROOM = 1.25
_HASH_BYTES = 8  # a uint64 in a sorted array
_PY_ENTRY_BYTES = 64  # a Python dict/set entry or panel cell, boxed
_LABEL_KEY_BYTES = 64
_BATCHES, _FAILED = "batches", "failed"
_PANEL_SIDES = (Side.REFERENCE, Side.HOLDOUT)
_REASON_CHARS = 300
_BQ_ERRORS = (BqApiError, PermissionError, LookupError)
# The data errors that make a table not_evaluated on a worker:
# `TABLE_ERRORS` without MemoryError, which is the worker's state, not
# the table's — it fails the bundle and the runner retries it.
_ENCODE_ERRORS: tuple[type[Exception], ...] = (ArithmeticError, IndexError,
                                               KeyError, TypeError, ValueError)
_DATAFLOW = "dataflow"
# Runners (module docstring). The in-process runner a local run uses, the
# names Beam resolves to its switching DirectRunner, and the runner
# classes that execute a batch pipeline on Prism.
_LOCAL_RUNNER = "FnApiRunner"
_PRISM = "prism"
_PRISM_ROUTED = frozenset({"direct", "switchingdirect", "testdirect"})
_PRISM_BACKED = frozenset({"SwitchingDirectRunner", "PrismRunner"})
_METRICS_TABLE = "evaluation_metrics"
_PROFILES_TABLE = "evaluation_profiles"
_FLAGS_TABLE = "evaluation_row_flags"
_REGISTRY_TABLE = "evaluation_data_history"


# --------------------------------------------------------------------------
# runner options
# --------------------------------------------------------------------------
def _panel_sizes(table: TablePlan) -> tuple[int, int, int, int]:
  panel = table.panel
  if panel is None:
    return 0, 0, 0, 0
  return len(panel.r_rows), len(panel.h_rows), panel.e_n, panel.he_n


def _relational_sets(tables: Sequence[TablePlan]) -> int:
  """8 B per parent key of every side-input join the relational pass
  plans, each distinct (parent, side, referenced columns) set once. Read
  from that pass's own edge plan (`relational.plan_edges`: its parent
  resolution, its join switch, the sides it does not evaluate), so the
  estimate holds a set exactly when the pass broadcasts one."""
  seen: dict[tuple[str, Side, tuple[str, ...]], float] = {}
  for spec, _ in plan_edges(tables):
    if spec.reason is not None:
      continue
    for side in (Side.SOURCE, Side.SYNTHETIC):
      planned = spec.side(side)
      if (planned.reason is None and planned.path == SIDE_INPUT and
          planned.parent is not None and planned.planned_keys is not None):
        seen[(planned.parent, side, spec.ref_cols)] = planned.planned_keys
  return int(sum(seen.values()) * _HASH_BYTES)


def side_input_bytes(plan: EvaluationPlan) -> int:
  """What the side inputs a worker may hold at once add up to (an
  estimate: every broadcast set of the graph summed, each Python value
  counted at `_PY_ENTRY_BYTES`):

      membership   the panel's hashes and non-key cell codes, and the
                   full-source sorted sets when they fit (8 B a row and
                   array, `context.budget.source_sets_fit`)
      privacy      the R/H panel rows (Python dicts, the plan's columns)
                   and the density sample (min(|R|, n_syn) rows)
      census       per census column, R's value counts and the H, E, H_E
                   value sets
      relational   the side-input parent key sets (8 B a key)
      label key    one value
  """
  total = _LABEL_KEY_BYTES + _relational_sets(plan.tables)
  for table in plan.tables:
    if not table.evaluated:
      continue
    n_r, n_h, e_n, he_n = _panel_sizes(table)
    width = len(table.columns)
    nonkey, keyed = table_key_shape(table.columns, table.pk, table.identity,
                                    table.edges)
    try:
      content = len(BatchLayout.from_table(table).nonkey_columns)
    except TABLE_ERRORS:  # a malformed plan: the pipeline fails it later
      content = width
    rows_source, rows_synthetic = table.rows_read
    total += (n_r + n_h) * _HASH_BYTES * (6 + content)
    if table.rows_source is not None and source_sets_fit(
        rows_source, nonkey=nonkey, keyed=keyed,
        max_bytes=SOURCE_SET_MAX_BYTES):
      arrays = source_set_arrays(nonkey=nonkey, keyed=keyed)
      total += int(rows_source * _HASH_BYTES * arrays)
    total += (n_r + n_h) * width * _PY_ENTRY_BYTES
    total += int(min(n_r, rows_synthetic) * width * _PY_ENTRY_BYTES)
    censused = sum(1 for column in table.columns if column.census != "none")
    total += censused * (n_r + n_h + e_n + he_n) * _PY_ENTRY_BYTES
  return int(total)


def _runner_key(name: str) -> str:
  """A runner name as Beam's `create_runner` matches it: the class name
  without its module, lower case, without a trailing `runner`."""
  return name.strip().rsplit(".", 1)[-1].lower().removesuffix("runner")


def _checked_runner(p: beam.Pipeline) -> None:
  """Refuse a pipeline that would run on Prism (module docstring,
  Runners): its runner is Beam's switching `DirectRunner` — what the
  name `DirectRunner` creates — or `PrismRunner`, or a subclass.

  The check reads the class of `p.runner`, so it cannot detect a `p` that
  is not a `Pipeline` and has no `runner` attribute (it is let through),
  nor a `PortableRunner` whose job endpoint is a Prism server (the class
  is not Prism's; the endpoint is not read).

  Raises:
    ValueError: the pipeline's runner is backed by Prism.
  """
  runner = getattr(p, "runner", None)
  if _PRISM_BACKED.isdisjoint(cls.__name__ for cls in type(runner).__mro__):
    return
  raise ValueError(
      f"the evaluation pipeline cannot run on {type(runner).__name__}: Beam "
      "runs it on Prism, which starts a step before its batch side input is "
      "complete, so a failed table's rows could be published as evaluated. "
      "Build the pipeline's options from pipeline_options_defaults(runner, "
      f"plan), which runs a local evaluation on {_LOCAL_RUNNER}, or pass "
      f"runner={_LOCAL_RUNNER!r}")


def pipeline_options_defaults(runner: str,
                              plan: EvaluationPlan | None = None
                             ) -> dict[str, Any]:
  """The evaluator's `PipelineOptions` keyword defaults for `runner`
  (module docstring); with a plan the side-input cache is sized from it
  (`side_input_bytes`, 25 % headroom, at least `MIN_CACHE_MB`) — a size
  that matters on Dataflow and has no effect on a local run (the
  in-process runner's embedded worker has a fixed cache).
  `enable_data_sampling` is never among the experiments (R71).

  The `runner` returned is the one to run on, not always the one asked
  for: `DirectRunner` (any spelling Beam resolves to it) becomes
  `FnApiRunner`, Beam's in-process Python runner, because Beam hands a
  `DirectRunner` batch pipeline to Prism (module docstring, Runners).
  Pass the dict as keywords to `PipelineOptions` together with an
  explicit `flags` list — `PipelineOptions([], **defaults)` — since
  `PipelineOptions(**defaults)` alone also parses `sys.argv`.

  Raises:
    ValueError: an empty runner name, or the Prism runner.
  """
  if not isinstance(runner, str) or not runner.strip():
    raise ValueError(f"runner: expected a runner name, got {runner!r}")
  key = _runner_key(runner)
  if key == _PRISM:
    raise ValueError(
        f"runner {runner!r}: the evaluator does not run on Prism, which "
        "starts a step before its batch side input is complete; use "
        f"DirectRunner (run in process, on {_LOCAL_RUNNER}) or "
        "DataflowRunner")
  if key in _PRISM_ROUTED:
    runner = _LOCAL_RUNNER
  cache = MIN_CACHE_MB
  if plan is not None:
    wanted = math.ceil(side_input_bytes(plan) * _CACHE_HEADROOM / _MB)
    cache = max(MIN_CACHE_MB, wanted)
  options: dict[str, Any] = {
      "runner": runner,
      "save_main_session": False,
      "max_cache_memory_usage_mb": cache,
  }
  if _DATAFLOW in runner.lower():
    options["experiments"] = ["upload_graph"]
  return options


# --------------------------------------------------------------------------
# prepare (driver, before the pipeline)
# --------------------------------------------------------------------------
def _unpinned(
    plan: EvaluationPlan, table: TablePlan, exc: BaseException
) -> tuple[TablePlan, str | None, PrepareStatement | None]:
  """`table` read from its live source instead of the pin BigQuery
  refused, and — in sampled mode — the sample to build over the live
  source instead (with the planned sample's table, now stale)."""
  source = normalize_fqn(str(table.source_table))
  pin = table.source_pin
  assert pin is not None  # only a pin's own DDL gets here
  why = (f"{table.landing_table}: the source pin (a snapshot clone of "
         f"{source}) could not be created ({type(exc).__name__}: {exc}); the "
         "source is read unpinned, as it is now, so it may differ from what "
         "the generation job read")[:_REASON_CHARS * 2]
  stale: str | None = None
  replacement: PrepareStatement | None = None
  read = source
  if table.source_read_table and table.source_read_table != pin.read_table:
    rate = table.sample_rate_source or 1.0
    keep = min(SAMPLE_MODULUS, max(1, round(rate * SAMPLE_MODULUS)))
    sql, read, params = sampled_read(
        source,
        keep=keep,
        modulo=SAMPLE_MODULUS,
        salt=plan.salt,
        temp_dataset=plan.temp_dataset,
        evaluation_id=plan.evaluation_id,
        side="src")
    stale = table.source_read_table
    replacement = PrepareStatement(sql, params)
  degraded = dataclasses.replace(
      table,
      source_read_table=read,
      source_pinned=False,
      source_pin=SourcePin(source, (), False, quote_fqn(source), None, why),
      warnings=(*table.warnings, why))
  return degraded, stale, replacement


def _creates(statement: PrepareStatement, table: str) -> bool:
  return statement.sql.startswith(f"CREATE TABLE `{table}`")


def _refusal(exc: BaseException) -> bool:
  """Whether BigQuery REFUSED the call (module docstring): the one
  rule of `context.bq.is_refusal` — a 400, 403 or 404. A 5xx, a 429, a
  conflict or an error with no status is not a refusal: it may pass on
  a retry."""
  return is_refusal(exc)


def _readable(bq: Any, table: str) -> str | None:
  """Why `table` cannot be read (a `LIMIT 0` dry run BigQuery refuses, or
  a name that is not a table), or None.

  Raises:
    BqApiError: the dry run failed for another reason (`_refusal`).
  """
  try:
    bq.dry_run_bytes(f"SELECT 1 FROM {quote_fqn(normalize_fqn(table))} "
                     "LIMIT 0")
  except ValueError as exc:
    return f"{type(exc).__name__}: {exc}"[:_REASON_CHARS]
  except _BQ_ERRORS as exc:
    if not _refusal(exc):
      raise
    return f"{type(exc).__name__}: {exc}"[:_REASON_CHARS]
  return None


def _preflight(table: TablePlan, bq: Any, notes: list[str]) -> TablePlan:
  """A read-only parent without the sides BigQuery will not read."""
  if table.source_read_table:
    why = _readable(bq, table.source_read_table)
    if why is not None:
      notes.append(f"{table.landing_table}: the read-only parent's source "
                   f"twin {table.source_read_table} cannot be read ({why}); "
                   "its edges' source-side metrics are not evaluated")
      table = dataclasses.replace(table, source_read_table="")
  landing = table.synthetic_read_table or table.scope.read_table
  if landing:
    why = _readable(bq, landing)
    if why is not None:
      reason = f"the read-only parent cannot be read ({why})"
      notes.append(f"{table.landing_table}: {reason}; its edges' synthetic "
                   "metrics are not evaluated")
      table = dataclasses.replace(
          table,
          synthetic_read_table="",
          scope=dataclasses.replace(table.scope, read_table="", reason=reason))
  return table


def prepare_evaluation(plan: EvaluationPlan, bq: Any) -> EvaluationPlan:
  """Run `plan`'s prepare DDL (module docstring) and return the plan the
  pipeline must read: a refused source pin degraded to the live source,
  an unreadable read-only parent's side dropped, each with a warning.

  Raises:
    BqApiError, PermissionError, LookupError: any other statement failed,
      or a pin's DDL or a parent's readability check failed without
      being refused (`_refusal`: a transient error); the driver then
      writes the FAILED row.
  """
  notes: list[str] = []
  tables = [
      _preflight(t, bq, notes) if t.role == "external" else t
      for t in plan.tables
  ]
  owners = {
      sql: i for i, t in enumerate(tables) if t.evaluated and t.source_pin
      for sql in t.source_pin.prepare_sql
  }
  pending = list(plan.prepare_sql)
  ran: list[PrepareStatement] = []
  stale: set[str] = set()
  while pending:
    statement = pending.pop(0)
    if any(_creates(statement, name) for name in stale):
      continue
    try:
      bq.execute(
          statement.sql,
          dict(statement.params),
          max_bytes=plan.budget.max_bytes_billed)
    except _BQ_ERRORS as exc:
      owner = owners.get(statement.sql)
      if owner is None or not _refusal(exc):
        raise
      tables[owner], old, replacement = _unpinned(plan, tables[owner], exc)
      notes.append(tables[owner].warnings[-1])
      if old is not None and replacement is not None:
        stale.add(old)
        pending.insert(0, replacement)
      continue
    ran.append(statement)
  return dataclasses.replace(
      plan,
      tables=tuple(tables),
      prepare_sql=tuple(ran),
      warnings=tuple(dict.fromkeys([*plan.warnings, *notes])))


# --------------------------------------------------------------------------
# the graph
# --------------------------------------------------------------------------
class _ReadOnce(Sources):
  """`Sources` that read each (landing table, side) once: a repeated read
  (the relational pass's parent rows) gets the same PCollection, and a
  side that failed to read fails again with the same reason. The panel
  sides are read with the run's `staging` (None: embedded)."""

  def __init__(self, inner: Sources, staging: Staging | None = None):
    self._inner = inner
    self._staging = staging
    self._reads: dict[tuple[str, Side], beam.PCollection] = {}
    self._errors: dict[tuple[str, Side], str] = {}

  def read(self,
           p: beam.Pipeline,
           table: TablePlan,
           side: Side | str,
           *,
           staging: Staging | None = None) -> beam.PCollection:
    del staging  # the run's own, given once
    key = (table.landing_table, Side(side))
    if key in self._errors:
      raise ValueError(self._errors[key])
    if key not in self._reads:
      try:
        self._reads[key] = self._inner.read(
            p, table, side, staging=self._staging)
      except TABLE_ERRORS as exc:
        self._errors[key] = (f"the {key[1]} side of {table.landing_table} "
                             f"cannot be read ({type(exc).__name__}: "
                             f"{exc})")[:_REASON_CHARS]
        raise ValueError(self._errors[key]) from exc
    return self._reads[key]

  def _read(self, p: beam.Pipeline, table: TablePlan, side: Side,
            label: str) -> beam.PCollection:
    del label  # the inner source labels its own reads
    return self._inner.read(p, table, side)


class _SafeEncodeFn(EncodeBatchFn):
  """`EncodeBatchFn`, a data error (`_ENCODE_ERRORS`) tagged `failed` as
  (table, reason) instead of failing the job (the table becomes
  not_evaluated). A `MemoryError` is raised: the bundle fails and is
  retried, never a permanent not_evaluated."""

  def __init__(self, table: TablePlan, side: Side, *, salt: str):
    super().__init__(table, side, salt=salt)
    self._table_name = table.name
    self._side_name = str(side)

  def process(self, rows: Sequence[Mapping[str, Any]]) -> Iterator[Any]:
    try:
      batches = list(super().process(rows))
    except _ENCODE_ERRORS as exc:
      reason = (f"the {self._side_name} rows could not be encoded "
                f"({type(exc).__name__}: {exc})")[:_REASON_CHARS]
      yield beam.pvalue.TaggedOutput(_FAILED, (self._table_name, reason))
      return
    yield from batches


def _sides(table: TablePlan) -> tuple[Side, ...]:
  base = (Side.SOURCE, Side.SYNTHETIC)
  return base + _PANEL_SIDES if table.panel is not None else base


def _checked(table: TablePlan) -> None:
  """Everything the transforms build from `table` on the driver (a
  malformed plan raises here, for this table only)."""
  BatchLayout.from_table(table)
  DenseSpec.from_table(table)
  CensusSpec.from_table(table)
  census_refs(table)
  DriftSpec.from_table(table)


def _flatten(p: beam.Pipeline, pcolls: Sequence[beam.PCollection],
             label: str) -> beam.PCollection:
  if not pcolls:
    empty: beam.PCollection = p | f"{label}/None" >> beam.Create([])
    return empty
  flat: beam.PCollection = pcolls | label >> beam.Flatten()
  return flat


def _source_profile(
    item: tuple[tuple[str, str], DenseProfile]) -> Iterator[Any]:
  (table, side), profile = item
  if side == Side.SOURCE.value:
    yield table, profile


def _drift_rows(item: tuple[str, Iterable[DenseProfile | None]],
                specs: Mapping[str, DriftSpec],
                stats: Mapping[str, TableStats]) -> list[MetricValue]:
  table, entries = item
  profiles = [e for e in entries if e is not None]
  profile = None
  for entry in profiles:
    profile = entry if profile is None else profile.merge(entry)
  missing = TableStats(tier=None, reason="source_table_stats not read")
  return drift_metrics(specs[table], profile, stats.get(table, missing))


class _SummaryCombineFn(beam.CombineFn):
  """The scored rows' roll-up inputs (`SUMMARY_FIELDS` only) →
  `assemble.RowSummary`. Order-free: every mean is an exact fsum."""

  def create_accumulator(self) -> list[tuple[Any, ...]]:
    return []

  def add_input(self, mutable_accumulator: list[tuple[Any, ...]],
                element: Mapping[str, Any]) -> list[tuple[Any, ...]]:
    mutable_accumulator.append(tuple(element[k] for k in SUMMARY_FIELDS))
    return mutable_accumulator

  def merge_accumulators(
      self, accumulators: Iterable[list[tuple[Any,
                                              ...]]]) -> list[tuple[Any, ...]]:
    merged: list[tuple[Any, ...]] = []
    for accumulator in accumulators:
      merged.extend(accumulator)
    return merged

  def extract_output(self, accumulator: list[tuple[Any, ...]]) -> RowSummary:
    return summarize_rows(
        dict(zip(SUMMARY_FIELDS, row, strict=True)) for row in accumulator)


def _rollup_rows(summary: RowSummary,
                 context: RowContext) -> list[dict[str, Any]]:
  return [
      metric_row(mv, context)
      for mv in aggregate_metrics(summary, context.digests)
  ]


def _unfailed(item: ProfileValue | RowFlag, failures: Mapping[str,
                                                              str]) -> bool:
  return item.table not in failures


def _profile_row(pv: ProfileValue, context: RowContext) -> dict[str, Any]:
  """The profile's row: only its moment-derived values rounded
  (`assemble.stable_profile`); edges, quantiles and counts as computed."""
  return to_profile_row(
      stable_profile(pv),
      evaluation_id=context.evaluation_id,
      evaluated_at=context.evaluated_at)


def _flag_row(flag: RowFlag, context: RowContext) -> dict[str, Any]:
  return flag_row(
      flag,
      evaluation_id=context.evaluation_id,
      evaluated_at=context.evaluated_at)


def _guard(mv: MetricValue, known: Mapping[str, str],
           failures: Mapping[str, str]) -> MetricValue:
  """`assemble.guarded` against both failure sets: `known`, the tables
  the driver had already seen fail when it built the graph — a constant
  of this step, so nothing can deliver it late or incomplete — and then
  the `failures` side input, which adds the tables a worker failed."""
  return guarded(guarded(mv, known), failures)


def _final_row(summary: RowSummary, registry: RegistryContext,
               known: Mapping[str, str], failures: Mapping[str, str], *signals:
               Iterable[Any]) -> dict[str, Any]:
  """The FINAL event, once every signal (the sinks' committed load and
  copy jobs) is complete — a batch side input is, before the step
  reading it runs, on every runner `_checked_runner` admits; each one is
  read to its end all the same. The failed tables are the ones the
  driver knew (`known`, a constant of the step) and the ones a worker
  tagged (`failures`)."""
  for signal in signals:
    for _ in signal:
      pass
  evaluated_at = str(registry.seed["evaluated_at"])
  failed = {**failures, **known}
  return registry.final(summary, failed, finished_at=finish_time(evaluated_at))


def _stamped(seed: Mapping[str, Any]) -> dict[str, Any]:
  """The SKIPPED row stamped when the pipeline runs (after the RUNNING
  row the driver wrote)."""
  finished = finish_time(str(seed["evaluated_at"]))
  return {**seed, "recorded_at": finished, "finished_at": finished}


def _skipped(p: beam.Pipeline, plan: EvaluationPlan,
             sinks: Sinks) -> dict[str, beam.PCollection]:
  seed = skipped_row(plan, finished_at=plan.evaluated_at)
  final = (
      p
      | "Registry/Skipped" >> beam.Create([seed])
      | "Registry/Final" >> beam.Map(_stamped))
  sinks.write(final, _REGISTRY_TABLE)
  return {"registry": final}


def _encode(rows: beam.PCollection, table: TablePlan, side: Side,
            salt: str) -> tuple[beam.PCollection, beam.PCollection]:
  label = f"Encode[{table.landing_table}/{side}]"
  out = (
      rows
      | f"{label}/Batch" >> beam.BatchElements(
          min_batch_size=MIN_BATCH_SIZE, max_batch_size=MAX_BATCH_SIZE)
      | f"{label}/Encode" >> beam.ParDo(_SafeEncodeFn(
          table, side, salt=salt)).with_outputs(_FAILED, main=_BATCHES))
  return out[_BATCHES], out[_FAILED]


def build_evaluation_pipeline(  # pylint: disable=too-many-locals  # the composition is one flat recipe
    p: beam.Pipeline,
    plan: EvaluationPlan,
    *,
    sources: Sources,
    sinks: Sinks,
    stats_query: StatsQuery | None = None,
    label_key_reader: Reader | None = None,
    pools: Mapping[str, Mapping[str, FreeTextPool]] | None = None,
    thresholds: Thresholds | None = None,
    staging: Staging | None = None) -> dict[str, beam.PCollection]:
  """Compose the whole evaluation of `plan` on `p` (module docstring).

  `stats_query` reads the generator's source_table_stats on the driver
  (`Bq.query`'s shape; None: `column.source_stats_drift` is
  not_evaluated with that reason); `label_key_reader` replaces the
  worker's key reader (tests); `pools` are the free-text pools
  (`census.read_pools`, per table); `thresholds` are the run's overrides
  of the catalogue's warn and fail (`{metric id: (warn, fail)}`, Ruling
  R93-6): every metric row of a named metric is graded, scored and
  stored under them, roll-ups included; `staging` is where the panel
  data the driver built (the R and H rows, the census's column views,
  each table's panel hashes and privacy inputs) is written as objects
  for the workers to read (`beam.staged`, Ruling R154) instead of being
  embedded in the graph — None, the default, embeds, and the driver
  passes one for a Dataflow run only. The RUNNING row is the driver's
  (`assemble.running_row`); a plan with nothing to evaluate writes only
  its FINAL SKIPPED row.

  Returns the written PCollections: `metrics`, `profiles`, `flags`,
  `registry` and the per-table `failures` (table, reason); only
  `registry` for a skipped plan.

  Raises:
    ValueError: `p` would run on Prism (module docstring, Runners).
    StagedObjectError: a panel object could not be written under
      `staging` (nothing is submitted: the driver's FAILED row).
  """
  _checked_runner(p)
  if plan.skip_reason is not None:
    return _skipped(p, plan, sinks)
  reads = _ReadOnce(sources, staging)
  failed: dict[str, str] = {}
  ready: list[TablePlan] = []
  side_rows: list[tuple[TablePlan, Side, beam.PCollection]] = []
  for table in plan.tables:
    if not table.evaluated:
      continue
    try:
      _checked(table)
      rows = [
          (table, side, reads.read(p, table, side)) for side in _sides(table)
      ]
    except TABLE_ERRORS as exc:
      failed[table.name] = (f"the table could not be set up for the "
                            f"pipeline ({type(exc).__name__}: "
                            f"{exc})")[:_REASON_CHARS]
      continue
    ready.append(table)
    side_rows.extend(rows)
  key = p | "LabelKey" >> LabelKey(plan.label_key_uri, reader=label_key_reader)
  encoded = [_encode(rows, t, side, plan.salt) for t, side, rows in side_rows]
  batches = _flatten(p, [b for b, _ in encoded], "Batches")
  driver_failures = p | "DriverFailures" >> beam.Create(sorted(failed.items()))
  failures = (
      _flatten(p, [*(f for _, f in encoded), driver_failures], "Failures")
      | "FirstFailure" >> beam.CombinePerKey(min))
  failure_map = beam.pvalue.AsDict(failures)
  knobs = plan.knobs
  dense_out = batches | "Dense" >> DenseMetrics(ready, label_key=key)
  census_out = {
      "batches": batches,
      "accumulators": dense_out["accumulators"]
  } | "Census" >> CensusMetrics(
      ready, label_key=key, pools=pools, staging=staging)
  member_out = batches | "Membership" >> Membership(
      ready,
      salt=plan.salt,
      label_key=key,
      row_flags_top_k=knobs.row_flags_top_k,
      staging=staging)
  privacy_out = batches | "Privacy" >> Privacy(
      ready,
      salt=plan.salt,
      label_key=key,
      privacy_sample_rows=knobs.privacy_sample_rows,
      detection_sample_rows=knobs.detection_sample_rows,
      row_flags_top_k=knobs.row_flags_top_k,
      staging=staging)
  relational_out = batches | "Relational" >> Relational(
      list(plan.tables), sources=reads)
  stats = read_source_stats(plan, stats_query)
  drift_specs = {t.name: DriftSpec.from_table(t) for t in ready}
  drift = ((
      dense_out["accumulators"]
      | "Drift/SourceProfiles" >> beam.FlatMap(_source_profile),
      p | "Drift/Seeds" >> beam.Create([(t.name, None) for t in ready]),
  )
           | "Drift/Entries" >> beam.Flatten()
           | "Drift/ByTable" >> beam.GroupByKey()
           | "Drift/Rows" >> beam.FlatMap(_drift_rows, drift_specs, stats))
  by_name = {t.name: t for t in plan.tables}
  planned = [row_count_metric(t) for t in plan.tables if t.evaluated]
  planned += [
      mv for name, reason in sorted(failed.items())
      for mv in failed_table_metrics(by_name[name], reason)
  ]
  context = RowContext.from_plan(plan, thresholds=thresholds)
  measured = ((
      dense_out["metrics"],
      census_out["metrics"],
      member_out["metrics"],
      privacy_out["metrics"],
      relational_out["metrics"],
      drift,
      p | "PlanMetrics" >> beam.Create(planned),
  )
              | "Metrics" >> beam.Flatten()
              | "Guard" >> beam.Map(_guard, dict(failed), failure_map)
              | "MetricRows" >> beam.Map(metric_row, context))
  summary = measured | "Summary" >> beam.CombineGlobally(_SummaryCombineFn())
  metric_rows = (
      measured,
      summary | "Rollups" >> beam.FlatMap(_rollup_rows, context),
  ) | "AllMetricRows" >> beam.Flatten()
  profile_rows = ((
      dense_out["profiles"],
      census_out["profiles"],
      privacy_out["profiles"],
  )
                  | "Profiles" >> beam.Flatten()
                  | "GuardProfiles" >> beam.Filter(_unfailed, failure_map)
                  | "ProfileRows" >> beam.Map(_profile_row, context))
  flag_rows = ((member_out["flags"], privacy_out["flags"])
               | "Flags" >> beam.Flatten()
               | "GuardFlags" >> beam.Filter(_unfailed, failure_map)
               | "FlagRows" >> beam.Map(_flag_row, context))
  signals = [
      *sinks.write(metric_rows, _METRICS_TABLE),
      *sinks.write(profile_rows, _PROFILES_TABLE),
      *sinks.write(flag_rows, _FLAGS_TABLE),
  ]
  final = summary | "Final" >> beam.Map(
      _final_row, RegistryContext.from_plan(plan), dict(failed), failure_map,
      *(beam.pvalue.AsIter(signal) for signal in signals))
  sinks.write(final, _REGISTRY_TABLE)
  return {
      "metrics": metric_rows,
      "profiles": profile_rows,
      "flags": flag_rows,
      "registry": final,
      "failures": failures,
  }
