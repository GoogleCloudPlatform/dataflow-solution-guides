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
"""Which landing rows one generation job wrote, and the source as the job
saw it — planned as SQL, never run here.

Landing rows carry no run id, so one job's rows are recovered from the
launch's write disposition and the job's own commit window (the labelled
`JobWrite`s of `context.jobs`):

    disposition  the job's writes / later writers   mode        what is read
    ───────────  ─────────────────────────────────  ──────────  ─────────────────────
    overwrite    no foreign write after the job     table       the landing table
    overwrite    a foreign write after the job      as_of       snapshot clone AS OF
                                                                the window end
    append       LOAD / DML only                    appends     CTAS over APPENDS(t,
                                                                window start, end)
    append       any COPY job (Beam FILE_LOADS'     as_of_diff  CTAS: t AS OF end
                 multi-partition path)                          minus the start snapshot
    --scope manual                                  manual      the table as it is now

`as_of_diff` exists because BigQuery's documented change-history
operations (CREATE TABLE, INSERT, MERGE, load, streaming) do not list
copy jobs, and Beam lands large writes through temp tables plus COPY
jobs. It is the exact multiset difference of the two states: rows are
numbered within identical `TO_JSON_STRING` groups on both sides
(`ROW_NUMBER() OVER (PARTITION BY TO_JSON_STRING(t))`) and the end-side
`(json, rn)` pairs absent from the start side are kept — so a duplicate
before the window, a duplicate after it and a row re-appended identical
to an existing one all count exactly. It is exact for an append-only
window; a delete or update inside the window shrinks it (the count check
says so).

BigQuery's time-travel rule shapes it: "A single query statement can't
reference a single table at more than one point in time, including the
current time." So it takes two statements, each reading every table at
one point:

    planning_sql  CREATE SNAPSHOT TABLE <start> CLONE t FOR SYSTEM_TIME
    (planner,     AS OF <ws> OPTIONS(24 h)       zero bytes, auto-expiring
     phase A)
    read_expr /   t FOR SYSTEM_TIME AS OF <we>   minus   <start> (read now)
    prepare_sql     one table at we                   another table, now

The start snapshot is the ONE table planning may create (R5 amended by
R57): the scope's `read_expr` reads it, and the planner's dry runs and
planning SELECTs run over that `read_expr`. It is named
`{temp_dataset}.sdfb_eval_{evaluation_id}_start_{short}` and recorded
(`ScopePlan.planning_sql`/`start_table`, the plan's `planning_ddl`). The
difference reads the table and the snapshot once each — about 2x the
table's bytes, which the planner's dry run of the CTAS counts into the
budget. A table created inside the window has no start state to
subtract: it is read AS OF the window end (`as_of`), and the reason says
so.

The window pads the job's own writes by a second on each side, and every
other writer is placed against it:

      now - time_travel_hours + 1 h     ws = first start - 1 s   we = last end + 1 s
    ──────────┬──────────────────────────┬──────── job writes ────────┬──────────►
              │ earlier writers: ignored │ overlapping writers:       │ later writers:
              │ (truncated / outside the │ contaminated — their rows  │ as_of (overwrite),
              │ window)                  │ look like the job's        │ ignored (append)
              └── ws (appends, as_of_diff) or we (as_of) older than this floor → expired

The floor keeps a 1 h safety margin inside the table's time-travel
window: the prepare DDL runs later than planning, and a point that ages
out in between would fail the whole run. A window end after `now` (the
job still writing, or skewed clocks) is `unknown`. A writer overlaps
when its own [start, end] meets [ws, we]: its commit lies somewhere in
that span, so these are exactly the writers whose commit MAY fall in the
window (conservative for an overwrite, where a writer committed before
the truncating first commit is flagged although its rows were removed).

`expired` (time travel can no longer recover the window), `empty` (the
job wrote nothing), a rejected `contaminated` and an `unknown` that
cannot be placed leave nothing to read (`read_table == ""`): the current
table is never read in their place. `--scope manual` is the explicit way
to evaluate the table as it is now.

SQL values. The APPENDS window is bound as `@start_<short>`/`@end_<short>`,
namespaced per table so two tables' read expressions can share one
statement (`ScopePlan.params` carries the values for `prepare_sql` AND
`read_expr`). Every `FOR SYSTEM_TIME AS OF` — the snapshot clone, the
as-of read, both sides of `as_of_diff`, the source pin — carries an RFC
3339 `TIMESTAMP '…'` literal re-rendered from a parsed `datetime`, so
only digits and fixed separators reach the text. Query parameters are
constant expressions and would do there too; the literal is kept for
safety, so that one rendering serves the DDL, the planning reads and the
dry runs, identically to the microsecond.

Temp tables are `{temp_dataset}.sdfb_eval_{evaluation_id}_{side}_{short}`
(`short` = the table id plus an 8-hex digest of the table and the
table's use, so two landing tables named alike, or a scope and a sample
of the same table, never collide) and expire 24 h after creation. The
DDL is a plain `CREATE TABLE`: a retry of the same `evaluation_id`
within those 24 h collides loudly, so every attempt must mint a fresh
`evaluation_id`. The temp dataset must live in the landing/source
table's location. The permissions this needs: on the base (landing or
source) table, `bigquery.tables.get`, `tables.getData`,
`tables.createSnapshot`, `datasets.get` and `jobs.create`; on the temp
dataset, `tables.create` and `tables.updateData`; for the 24 h expiry,
`tables.deleteSnapshot`. Only the predefined `dataOwner`, `admin` or
`studioAdmin` roles can create a snapshot with an expiration time.

The row count read is always checked (`ScopePlan.verify`): against Σ the
job's own committed `output_rows` for the table and, when present, Σ
`validation_runs.valid_count`. A writer with no BigQuery job (a
streaming insert) or run from another project is invisible to
`foreign_writes`; that count check is the only guard there.

M4 live checks (not provable on the laptop):

- `APPENDS` over a LOAD-only window returns exactly the job's rows (the
  count check stays ok), and whether it also returns WRITE_APPEND copy-job
  rows, which are not in its documented operation list;
- `as_of_diff`'s CTAS runs within budget on a large landing table (about
  2x its bytes) and its count matches Σ output_rows;
- the snapshot clone with `OPTIONS(expiration_timestamp=…)` and the
  as-of literal succeed with the evaluator's roles;
- a landing table created by the job (`--create_if_not_exists`) takes
  the `as_of` path via `Bq.table`'s `created`;
- `creationTime` after a CREATE OR REPLACE TABLE (a new table: later than
  the window, so `unknown`) and after a WRITE_TRUNCATE load or copy
  (assumed unchanged — if it moves, as_of_diff windows fall back to
  `as_of` and overwrite windows may read as `unknown`).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, NamedTuple

from sdfb_evaluation.context.bq import normalize_fqn, quote_fqn
from sdfb_evaluation.context.jobs import JobWrite, parse_timestamp

__all__ = [
    "MODES",
    "STATUSES",
    "ScopePlan",
    "SourcePin",
    "as_of_expr",
    "from_item",
    "pin_source",
    "read_params",
    "resolve_scope",
    "sampled_read",
]

MODES = ("table", "as_of", "appends", "as_of_diff", "manual")
STATUSES = ("ok", "count_mismatch", "contaminated", "expired", "empty",
            "unknown")
_REQUESTS = ("auto", *MODES)
_DISPOSITIONS = ("append", "overwrite")

_PAD = timedelta(seconds=1)
_MARGIN = timedelta(hours=1)
_SKEW = timedelta(minutes=5)  # tolerated Dataflow-vs-evaluator clock skew
_EXPIRY = ("OPTIONS(expiration_timestamp="
           "TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR))")
# Row counts agree within 0.5 % — exactly below 1000 rows.
_COUNT_TOLERANCE = 0.005
_EXACT_BELOW = 1000
_COUNT_RULE = "equal below 1000 rows, within 0.5 % above"
# Why a scope's count can miss, and what to do about it.
_LIKELY_CAUSE = {
    "appends": ("rows landed by an operation outside APPENDS' documented "
                "list (CREATE TABLE, INSERT, MERGE, load, streaming), e.g. a "
                "copy job"),
    "as_of_diff": ("rows deleted or updated inside the window (a DML "
                   "DELETE/UPDATE or a truncate): the AS OF difference only "
                   "sees net additions"),
    "as_of": ("a writer the JOBS view cannot see (a streaming insert, "
              "another project)"),
    "table": ("a writer the JOBS view cannot see (a streaming insert, "
              "another project) or a later delete"),
    "manual": "rows other launches wrote to the same table",
}
_REMEDY = ("if the table holds only this job's rows, --scope manual reads it "
           "whole")

_EVALUATION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SIDE_RE = re.compile(r"[a-z][a-z0-9_]{0,15}")
_SHORT_STEM = 48
# as_of_diff: number identical rows on each side, keep the unmatched end ones.
_NUMBERED = ("TO_JSON_STRING(t) AS __sdfb_json, ROW_NUMBER() OVER "
             "(PARTITION BY TO_JSON_STRING(t)) AS __sdfb_rn")


def _rfc3339(moment: datetime) -> str:
  return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _moment(value: str | datetime, what: str) -> datetime:
  try:
    return parse_timestamp(value)
  except (TypeError, ValueError) as exc:
    raise ValueError(f"{what}: {value!r} is not an RFC 3339 timestamp") from exc


def _timestamp_literal(moment: datetime) -> str:
  return f"TIMESTAMP '{_rfc3339(moment)}'"


def _count(value: Any, what: str) -> int:
  if isinstance(value, bool) or not isinstance(value, int) or value < 0:
    raise ValueError(f"{what}: expected a non-negative int, got {value!r}")
  return value


def _hours(value: Any) -> int:
  if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
    raise ValueError(
        f"time_travel_hours: expected a positive int, got {value!r}")
  return value


def _floor(now: datetime, hours: int) -> datetime:
  """The oldest point planned against: the time-travel window less the
  1 h safety margin."""
  return now - timedelta(hours=hours) + _MARGIN


def _short(fqn: str, use: str) -> str:
  """The table id, sanitized, plus an 8-hex digest of (use, table)."""
  stem = re.sub(r"[^A-Za-z0-9_]", "_", fqn.rsplit(".", 1)[1])[:_SHORT_STEM]
  digest = hashlib.blake2b(f"{use}|{fqn}".encode(), digest_size=4).hexdigest()
  return f"{stem}_{digest}"


def as_of_expr(table: str, moment: str | datetime) -> str:
  """`(SELECT * FROM `t` FOR SYSTEM_TIME AS OF TIMESTAMP '…')`: the table
  as it was at `moment`, parenthesized so `FROM {expr} AS alias` stays
  valid (BigQuery's grammar puts a table's alias BEFORE its `FOR
  SYSTEM_TIME AS OF`, so a bare clause could not take one after it)."""
  literal = _timestamp_literal(_moment(moment, "as-of time"))
  return f"(SELECT * FROM {quote_fqn(table)} FOR SYSTEM_TIME AS OF {literal})"


def _temp_table(temp_dataset: str, evaluation_id: str, side: str, table: str,
                use: str) -> str:
  if _EVALUATION_ID_RE.fullmatch(evaluation_id or "") is None:
    raise ValueError(f"evaluation_id {evaluation_id!r}: expected 1-128 of "
                     "[A-Za-z0-9_-]")
  if _SIDE_RE.fullmatch(side or "") is None:
    raise ValueError(f"side {side!r}: expected a short lowercase word")
  try:
    dataset = normalize_fqn(f"{temp_dataset}.probe").rsplit(".", 1)[0]
  except ValueError as exc:
    raise ValueError(f"temp_dataset {temp_dataset!r}: expected "
                     "project.dataset") from exc
  fqn = normalize_fqn(table)
  return normalize_fqn(
      f"{dataset}.sdfb_eval_{evaluation_id}_{side}_{_short(fqn, use)}")


def _joined(reason: str | None, note: str) -> str:
  return f"{reason}; {note}" if reason else note


def _agree(observed: int, count: int) -> bool:
  if count < _EXACT_BELOW:
    return observed == count
  return abs(observed - count) <= _COUNT_TOLERANCE * count


@dataclass(frozen=True)
class ScopePlan:
  """The rows of one landing table that one generation job wrote.

  `read_table` is what the pipeline DIRECT_READs once `prepare_sql` (DDL,
  run driver-side right before the pipeline) has built it; `read_expr` is
  the same rows as a FROM item for planning queries and dry runs, which
  must not create tables — except `planning_sql`: an `as_of_diff`
  scope's zero-byte, expiring start snapshot (`start_table`), which the
  planner creates before its dry runs because `read_expr` reads it (R57).
  All of them reference exactly the parameters in `params`.
  `read_table == ""` means nothing can be read (see `readable`).
  `written_rows` is Σ the job's own committed `output_rows` for the table
  (None when any is unknown); `expected_rows` is Σ `valid_count`.
  """
  landing_table: str
  mode: str  # table | as_of | appends | as_of_diff | manual
  status: str  # ok | count_mismatch | contaminated | expired | empty | unknown
  reason: str | None
  read_table: str
  prepare_sql: tuple[str, ...]
  window: tuple[str | None, str | None]  # RFC 3339 UTC, padded by 1 s
  expected_rows: int | None
  read_expr: str = ""
  params: dict[str, Any] = field(default_factory=dict, hash=False)
  observed_rows: int | None = None
  written_rows: int | None = None
  planning_sql: tuple[str, ...] = ()
  start_table: str = ""

  @property
  def ok(self) -> bool:
    """The scope check passed (the registry's `scope_ok`)."""
    return self.status == "ok"

  @property
  def readable(self) -> bool:
    """There are rows to read — possibly with a non-ok status (an allowed
    contamination, an `unknown` whole-table read, a count mismatch)."""
    return bool(self.read_table)

  def verify(self, observed_rows: int) -> ScopePlan:
    """This plan with the row count actually read checked against every
    count known: Σ the job's committed `output_rows` (`written_rows`) and
    Σ `validation_runs.valid_count` (`expected_rows`) — equal below 1000
    rows, within 0.5 % above.

    A mismatch turns an `ok` scope into `count_mismatch`, and the reason
    carries the numbers, the likely cause for this mode and the remedy. No
    count at all turns `ok` into `unknown`: an unchecked scope is never
    ok. A scope that is already worse keeps its status (the note is
    appended). `observed_rows` is set either way.

    Raises:
      ValueError: `observed_rows` is not a non-negative int, or the plan
        has nothing to read.
    """
    observed = _count(observed_rows, "observed_rows")
    if not self.readable:
      raise ValueError(f"nothing was read for {self.landing_table} (scope "
                       f"{self.status}): there is no row count to verify")
    counts = [(n, label)
              for n, label in ((self.written_rows,
                                "committed by the job (output_rows)"),
                               (self.expected_rows,
                                "validated (validation_runs valid_count)"))
              if n is not None]
    worse = self.status != "ok"
    if not counts:
      return replace(
          self,
          status=self.status if worse else "unknown",
          observed_rows=observed,
          reason=_joined(
              self.reason, f"row count not verified: {observed} rows read, "
              "and neither the job's committed output_rows nor "
              "validation_runs' valid_count is known"))
    misses = [f"{n} {label}" for n, label in counts if not _agree(observed, n)]
    if not misses:
      return replace(self, observed_rows=observed)
    cause = _LIKELY_CAUSE.get(self.mode, "rows outside the planned scope")
    remedy = "" if self.mode == "manual" else f"; {_REMEDY}"
    against = " and ".join(misses)
    note = (f"row count mismatch: {observed} rows read vs {against} "
            f"({_COUNT_RULE}) — likely {cause}{remedy}")
    return replace(
        self,
        status=self.status if worse else "count_mismatch",
        observed_rows=observed,
        reason=_joined(self.reason, note))


class SourcePin(NamedTuple):
  """Where the source is read from: `(read_table, prepare_sql, pinned)`
  first, then the planning FROM item, the pin time, a reason (why it was
  not pinned, or a note on the pin such as clamped clock skew; None
  otherwise) and the parameters `read_expr`/`prepare_sql` bind (none: the
  pin's AS OF is a literal)."""
  read_table: str
  prepare_sql: tuple[str, ...]
  pinned: bool
  read_expr: str
  as_of: str | None
  reason: str | None
  params: Mapping[str, Any] = MappingProxyType({})


def from_item(source: str | SourcePin | ScopePlan) -> str:
  """A FROM item for `source`, from its structured pieces.

  A `SourcePin` or `ScopePlan` contributes the `read_expr` this module
  built for it (a table reference, an as-of read, an APPENDS or as_of_diff
  subquery; bind `read_params(source)` with it). A string must be a
  strict `project.dataset.table`, bare or backtick-quoted; text is never
  re-parsed into anything else.

  Raises:
    ValueError: a plan with nothing to read, or a string that is not a
      table name.
  """
  if isinstance(source, (SourcePin, ScopePlan)):
    if not source.read_expr:
      kind = type(source).__name__
      raise ValueError(f"nothing to read from this {kind}")
    return source.read_expr
  text = (source or "").strip()
  if len(text) > 1 and text[0] == "`" and text[-1] == "`":
    text = text[1:-1]
  return quote_fqn(text)


def read_params(source: str | SourcePin | ScopePlan) -> dict[str, Any]:
  """The query parameters `from_item(source)` binds (`{}` for a table)."""
  if isinstance(source, (SourcePin, ScopePlan)):
    return dict(source.params)
  return {}


def _start(write: JobWrite) -> datetime:
  return _moment(write.start or write.end, f"start of job {write.job_id}")


def _end(write: JobWrite) -> datetime:
  return _moment(write.end or write.start, f"end of job {write.job_id}")


def _for_table(writes: Sequence[JobWrite], table: str) -> list[JobWrite]:
  return [w for w in writes if normalize_fqn(w.table) == table]


def _written(own: Sequence[JobWrite]) -> int | None:
  if not own or any(w.output_rows is None for w in own):
    return None
  return sum(int(w.output_rows or 0) for w in own)


def _is_empty(own: Sequence[JobWrite], expected: int | None) -> bool:
  """Nothing to evaluate: `validation_runs` counted zero valid rows and no
  write committed any, or (count unknown) every write committed zero."""
  if expected == 0:
    return not any((w.output_rows or 0) > 0 for w in own)
  if expected is None:
    return bool(own) and all(w.output_rows == 0 for w in own)
  return False


def _diff_select(table: str, start_table: str, we: datetime) -> str:
  """The multiset difference `table AS OF we` minus `start_table` (the
  table's snapshot AS OF the window start, read now): each table at one
  point in time, as one BigQuery statement must."""
  return (f"SELECT e.* EXCEPT(__sdfb_json, __sdfb_rn) FROM (SELECT t.*, "
          f"{_NUMBERED} FROM {as_of_expr(table, we)} AS t) AS e LEFT JOIN "
          f"(SELECT {_NUMBERED} FROM {quote_fqn(start_table)} AS t) AS s ON "
          "e.__sdfb_json = s.__sdfb_json AND e.__sdfb_rn = s.__sdfb_rn "
          "WHERE s.__sdfb_rn IS NULL")


def _snapshot_sql(snapshot: str, table: str, moment: datetime) -> str:
  return (f"CREATE SNAPSHOT TABLE `{snapshot}` CLONE {quote_fqn(table)} "
          f"FOR SYSTEM_TIME AS OF {_timestamp_literal(moment)} {_EXPIRY}")


@dataclass(frozen=True)
class _Scope:
  """What every plan of one `resolve_scope` call shares."""
  table: str
  temp: str
  start: str  # the as_of_diff start snapshot's name
  expected: int | None
  written: int | None
  window: tuple[str | None, str | None]
  hours: int
  floor: datetime  # the oldest point planned against (1 h margin kept)

  def plan(self, mode: str, status: str, reason: str | None,
           **read: Any) -> ScopePlan:
    return ScopePlan(
        landing_table=self.table,
        mode=mode,
        status=status,
        reason=reason,
        read_table=read.get("read_table", ""),
        prepare_sql=read.get("prepare_sql", ()),
        window=self.window,
        expected_rows=self.expected,
        read_expr=read.get("read_expr", ""),
        params=read.get("params", {}),
        written_rows=self.written,
        planning_sql=read.get("planning_sql", ()),
        start_table=read.get("start_table", ""))

  def reads(self,
            mode: str,
            bounds: tuple[datetime, datetime] | None = None) -> dict[str, Any]:
    """`read_table`/`prepare_sql`/`read_expr`/`params` (+ as_of_diff's
    `planning_sql`/`start_table`) of one mode; `bounds` = the padded
    window, which every mode but table/manual needs.

    Raises:
      ValueError: a mode with no read plan (never a silent fallthrough),
        or a windowed mode without its window.
    """
    table = quote_fqn(self.table)
    if mode in ("table", "manual"):
      return {"read_table": self.table, "read_expr": table}
    if mode not in ("as_of", "appends", "as_of_diff"):
      raise ValueError(f"scope mode {mode!r} has no read plan")
    if bounds is None:
      raise ValueError(f"scope mode {mode!r} needs the job's write window")
    ws, we = bounds
    if mode == "as_of":
      return {
          "read_table": self.temp,
          "prepare_sql": (_snapshot_sql(self.temp, self.table, we),),
          "read_expr": as_of_expr(self.table, we),
      }
    extra: dict[str, Any] = {}
    if mode == "appends":
      suffix = _short(self.table, "params")
      select = ("SELECT * EXCEPT(_CHANGE_TYPE, _CHANGE_TIMESTAMP) FROM "
                f"APPENDS(TABLE {table}, @start_{suffix}, @end_{suffix})")
      params = {f"start_{suffix}": ws, f"end_{suffix}": we}
    else:
      select, params = _diff_select(self.table, self.start, we), {}
      extra = {
          "planning_sql": (_snapshot_sql(self.start, self.table, ws),),
          "start_table": self.start,
      }
    return {
        "read_table": self.temp,
        "prepare_sql": (f"CREATE TABLE `{self.temp}` {_EXPIRY} AS {select}",),
        "read_expr": f"({select})",
        "params": params,
        **extra,
    }


def _manual(scope: _Scope, own: Sequence[JobWrite],
            others: Sequence[JobWrite]) -> ScopePlan:
  writers = (["this job"] if own else []) + sorted({w.job_id for w in others})
  reason = None
  if len(writers) > 1:
    # Hoisted: a quote nested in an f-string trips the py3.14 W1405 gate.
    named = ", ".join(writers)
    reason = (f"manual scope: {scope.table} is read as it is now; "
              f"{len(writers)} writers touched it ({named}), so rows this "
              "job did not write are evaluated too")
  return scope.plan("manual", "ok", reason, **scope.reads("manual"))


def _without_window(scope: _Scope, mode: str, requested: str) -> ScopePlan:
  missing = (f"no labelled BigQuery write by this job into {scope.table} "
             "was found (JOBS denied or past retention, or nothing landed)")
  if mode == "table" and requested == "table":
    return scope.plan(
        "table", "ok",
        f"{missing}; the whole table is attributed to the job as requested",
        **scope.reads("table"))
  if mode == "table":
    return scope.plan(
        "table", "unknown",
        f"{missing}: the whole table is read, but writes after the job "
        "cannot be ruled out — verify() compares its row count with the "
        "expected count", **scope.reads("table"))
  return scope.plan(
      mode, "unknown", f"{missing}, so the {mode} window cannot be placed; "
      "pass --scope manual to evaluate the table as it is now, or --scope "
      "table if the job overwrote it")


def _auto_mode(write_disposition: str | None,
               own: Sequence[JobWrite]) -> str | None:
  if write_disposition == "overwrite":
    return "table"
  if write_disposition == "append":
    copies = any(w.job_type == "COPY" for w in own)
    return "as_of_diff" if copies else "appends"
  return None


def resolve_scope(*,
                  landing_table: str,
                  write_disposition: str | None,
                  writes: Sequence[JobWrite],
                  foreign: Sequence[JobWrite],
                  expected_rows: int | None,
                  requested: str = "auto",
                  now: str | datetime,
                  time_travel_hours: int,
                  temp_dataset: str,
                  evaluation_id: str,
                  allow_contaminated: bool = False,
                  table_created: str | datetime | None = None) -> ScopePlan:
  """The landing rows one generation job wrote, as a read plan.

  Args:
    landing_table: the landing FQN (`project.dataset.table` or
      `project:dataset.table`).
    write_disposition: the launch's `--write_disposition` (`append` |
      `overwrite`), None when unresolved.
    writes: the job's labelled writes (`writes_by_beam_job`); writes into
      other tables are ignored.
    foreign: other writers into the table (`foreign_writes` from the
      window start, open-ended); earlier ones are ignored.
    expected_rows: Σ `validation_runs.valid_count` for the table, or None.
    requested: `auto` (derive from the disposition and the writes) or a
      mode to force.
    now: the evaluation's clock (RFC 3339 text or a datetime).
    time_travel_hours: the table's own time-travel window (its dataset's
      `max_time_travel_hours`; BigQuery's default is 168).
    temp_dataset: `project.dataset` for the materialized scope.
    evaluation_id: names the temp table (`[A-Za-z0-9_-]`); fresh per
      attempt.
    allow_contaminated: evaluate a contaminated scope anyway; the status
      stays `contaminated` and the reason says so.
    table_created: the landing table's creation time (`Bq.table`'s
      `created`), when known.

  Returns:
    A `ScopePlan`; nothing is raised for a scope that cannot be read —
    its status and reason say why and `read_table` is empty.

  Raises:
    ValueError: an input that would reach SQL is malformed, `requested`
      or `write_disposition` is unknown, or a write's times do not parse.
  """
  table = normalize_fqn(landing_table)
  if requested not in _REQUESTS:
    choices = ", ".join(_REQUESTS)
    raise ValueError(f"requested scope {requested!r}: expected one of "
                     f"{choices}")
  if write_disposition is not None and write_disposition not in _DISPOSITIONS:
    raise ValueError(f"write_disposition {write_disposition!r}: expected "
                     "append or overwrite (or None when unknown)")
  expected = (None if expected_rows is None else _count(expected_rows,
                                                        "expected_rows"))
  hours = _hours(time_travel_hours)
  clock = _moment(now, "now")
  created = (None if table_created in (None, "") else _moment(
      table_created, "table_created"))
  temp = _temp_table(temp_dataset, evaluation_id, "syn", table, "scope")
  start = _temp_table(temp_dataset, evaluation_id, "start", table, "start")

  own = _for_table(writes, table)
  own_ids = {w.job_id for w in own}
  others = [w for w in _for_table(foreign, table) if w.job_id not in own_ids]
  bounds = ((min(_start(w) for w in own) - _PAD,
             max(_end(w) for w in own) + _PAD) if own else None)
  scope = _Scope(
      table=table,
      temp=temp,
      start=start,
      expected=expected,
      written=_written(own),
      window=((_rfc3339(bounds[0]), _rfc3339(bounds[1])) if bounds else
              (None, None)),
      hours=hours,
      floor=_floor(clock, hours))

  if requested == "manual":
    return _manual(scope, own, others)
  mode = (
      requested if requested != "auto" else _auto_mode(write_disposition, own))
  if mode is None:
    return scope.plan(
        "manual", "unknown",
        "write disposition unknown (no launch parameters resolved): the "
        "job's rows cannot be isolated; pass --scope table|as_of|appends|"
        "as_of_diff|manual")
  if _is_empty(own, expected):
    return scope.plan(mode, "empty", f"the job committed no rows into {table}")
  if bounds is None:
    return _without_window(scope, mode, requested)
  return _windowed(
      scope,
      mode,
      requested,
      others=others,
      own=own,
      bounds=bounds,
      clock=clock,
      created=created,
      allow_contaminated=allow_contaminated)


def _windowed(scope: _Scope, mode: str, requested: str, *,
              others: Sequence[JobWrite], own: Sequence[JobWrite],
              bounds: tuple[datetime, datetime], clock: datetime,
              created: datetime | None, allow_contaminated: bool) -> ScopePlan:
  """The plan once the job's window is known: unknown, expired,
  contaminated (rejected or allowed) or ok."""
  table = scope.table
  ws, we = bounds
  if we > clock:
    return scope.plan(
        mode, "unknown",
        f"the job's write window ends at {_rfc3339(we)}, after now "
        f"({_rfc3339(clock)}): the job may still be writing, or the clocks "
        "disagree; evaluate once it has finished")
  if created is not None and created > we:
    return scope.plan(
        mode, "unknown",
        f"{table} was (re)created at {_rfc3339(created)}, after the job's "
        "write window: the rows the job wrote are no longer in it")
  overlapping = [w for w in others if _start(w) <= we and _end(w) >= ws]
  later = [w for w in others if _start(w) > we]
  if mode == "table" and later and requested == "auto":
    mode = "as_of"
  note = None
  if mode == "as_of_diff" and created is not None and created >= ws:
    mode = "as_of"
    note = (f"{table} was created at {_rfc3339(created)}, inside the job's "
            "write window: there is no earlier state to subtract, so "
            "as_of_diff reads it as_of the window end")
  point = {"appends": ws, "as_of_diff": ws, "as_of": we}.get(mode)
  if point is not None and point < scope.floor:
    return scope.plan(
        mode, "expired",
        f"the {mode} point {_rfc3339(point)} is older than {table}'s "
        f"{scope.hours} h time-travel window less a 1 h safety margin "
        f"(earliest planned {_rfc3339(scope.floor)}): the job's rows can no "
        "longer be separated; pass --scope manual to evaluate the table as "
        "it is now")

  copies = ", ".join(sorted(w.job_id for w in own if w.job_type == "COPY"))
  if mode == "appends" and copies:
    note = (f"appends requested although COPY job(s) {copies} landed rows "
            "in the window; copy jobs are not in APPENDS' documented "
            "operation list, so verify() may find it short")
  contaminating = overlapping + (later if mode == "table" else [])
  if not contaminating:
    return scope.plan(mode, "ok", note, **scope.reads(mode, bounds))
  ids = ", ".join(sorted({w.job_id for w in contaminating}))
  where = ("inside or after the job's write window"
           if mode == "table" else "inside the job's write window")
  found = (f"{len(contaminating)} other write(s) into {table} {where} "
           f"[{scope.window[0]}, {scope.window[1]}] ({ids}): their rows "
           "cannot be told apart from the job's")
  if not allow_contaminated:
    return scope.plan(
        mode, "contaminated",
        _joined(
            note, f"{found}; rejected — pass allow_contaminated=True to "
            "evaluate the scope anyway"))
  return scope.plan(
      mode, "contaminated",
      _joined(
          note, f"{found}; evaluated anyway (allow_contaminated=True): its "
          "metrics include rows the job did not write"),
      **scope.reads(mode, bounds))


def pin_source(*, source_table: str, job_create_time: str | datetime | None,
               now: str | datetime, time_travel_hours: int, temp_dataset: str,
               evaluation_id: str) -> SourcePin:
  """The source table as the generation job saw it (D4).

  Within the source's time-travel window (less the 1 h safety margin), a
  snapshot clone AS OF the job's create time (`read_table` = the clone,
  built by `prepare_sql`; `read_expr` = the same state for planning
  reads). Otherwise, or when the create time is unknown, the source as it
  is now with `pinned=False` and the reason — the reference digest check
  then tells whether it still yields the generator's sample.

  A create time at most 5 min after `now` is clock skew between Dataflow
  and the evaluator: it is pinned at `now`, and the reason says so.

  Raises:
    ValueError: an input that would reach SQL is malformed, or the create
      time is more than 5 min after `now`.
  """
  source = normalize_fqn(source_table)
  current = quote_fqn(source)
  clock = _moment(now, "now")
  hours = _hours(time_travel_hours)
  floor = _floor(clock, hours)
  temp = _temp_table(temp_dataset, evaluation_id, "src", source, "pin")
  if job_create_time is None or job_create_time == "":
    return SourcePin(
        source, (), False, current, None,
        "generation job create time unknown: the source is read as it is "
        "now, not as the job saw it")
  created = _moment(job_create_time, "job_create_time")
  note = None
  if created > clock + _SKEW:
    raise ValueError(f"job create time {_rfc3339(created)} is after now "
                     f"({_rfc3339(clock)}) by more than the tolerated "
                     "5 min clock skew")
  if created > clock:
    note = (f"job create time {_rfc3339(created)} is after now "
            f"({_rfc3339(clock)}): clock skew of at most 5 min, pinned at now")
    created = clock
  if created < floor:
    return SourcePin(
        source, (), False, current, None,
        f"job create time {_rfc3339(created)} is older than {source}'s "
        f"{hours} h time-travel window less a 1 h safety margin (earliest "
        f"planned {_rfc3339(floor)}): the source is read as it is now, not "
        "as the job saw it")
  prepare = (f"CREATE SNAPSHOT TABLE `{temp}` CLONE {current} FOR SYSTEM_TIME "
             f"AS OF {_timestamp_literal(created)} {_EXPIRY}",)
  return SourcePin(temp, prepare, True, as_of_expr(source, created),
                   _rfc3339(created), note)


def sampled_read(read_table: str, *, keep: int, modulo: int, salt: str,
                 temp_dataset: str, evaluation_id: str,
                 side: str) -> tuple[str, str, dict[str, Any]]:
  """`(CTAS sql, temp table, params)`: a salted hash sample of
  `read_table` keeping the rows whose `ABS(MOD(FARM_FINGERPRINT(
  CONCAT(@salt, TO_JSON_STRING(t))), modulo)) < keep` — about keep/modulo
  of them, the same rows for the same salt and contents.

  `params` is `{"salt": salt}`: the salt is bound by construction and
  never enters the text. `keep` and `modulo` are validated ints.
  `ABS(MOD(x, m))` keeps exactly the rows `MOD(ABS(x), m)` would
  (BigQuery's MOD takes the sign of `x`) but cannot fail: `ABS` of the
  one INT64 without a positive counterpart is an overflow error. Sample
  from a scope's `read_table` after its `prepare_sql` ran.

  Raises:
    ValueError: a malformed table, count, salt or id.
  """
  table = normalize_fqn(read_table)
  modulo = _count(modulo, "modulo")
  keep = _count(keep, "keep")
  if not 0 < keep <= modulo:
    raise ValueError(f"keep {keep} / modulo {modulo}: expected "
                     "0 < keep <= modulo")
  if not isinstance(salt, str) or not salt:
    raise ValueError("salt: expected a non-empty string")
  temp = _temp_table(temp_dataset, evaluation_id, side, table, "sample")
  sql = (f"CREATE TABLE `{temp}` {_EXPIRY} AS SELECT * FROM `{table}` AS t "
         "WHERE ABS(MOD(FARM_FINGERPRINT(CONCAT(@salt, TO_JSON_STRING(t))), "
         f"{modulo})) < {keep}")
  return sql, temp, {"salt": salt}
