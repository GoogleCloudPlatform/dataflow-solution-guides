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
"""Where the Beam pass reads its rows and writes its output rows.

    Sources.read(p, table, side) ─► PCollection[dict]
      source, synthetic   BigQuerySources  ReadFromBigQuery(table=read table,
                                           method=DIRECT_READ, selected_fields=
                                           plan columns, use_native_datetime=
                                           True) → normalize_direct_read
                          PagedBigQuerySources
                                           the BigQuery client's `list_rows`
                                           (REST tabledata.list) by row
                                           ranges, plan columns only; no
                                           normalisation ("The paged read")
                          RoutedBigQuerySources
                                           DIRECT_READ, except the read
                                           tables of the projects that
                                           refused a read session: paged
                          InMemorySources  beam.Create(rows_by[(table, side)])
      reference, holdout  every one        table.panel.r_rows / h_rows,
                                           projected to the plan columns —
                                           never re-read (D3): in the job
                                           graph (beam.Create), or, given a
                                           staging root, one object a worker
                                           reads (`beam.staged`, R154)

    Sinks.write(rows, table) ─► the PCollections complete once the rows are
                                committed (the FINAL registry row waits on
                                them as side inputs, D7)
      BigQuerySinks    WriteToBigQuery(FILE_LOADS, WRITE_APPEND, CREATE_NEVER,
                       the package schema) → (load job ids, copy job ids);
                       never STREAMING_INSERTS
      LocalJsonSinks   NDJSON shards <out_dir>/<table>/<NNNN>-<label>-*.jsonl,
                       one prefix per write → (files,); `read_rows` reads
                       only this instance's prefixes, and a table directory
                       holding another run's files is refused; `write_rows`
                       is the driver's own write (a registry event, written
                       at once as one shard of its own prefix)
      ClientLoadSinks  LocalJsonSinks, then — after the pipeline, driver-side
                       — `load(bq)`: one `Bq.load_json` job per table, the
                       registry (evaluation_data_history) last

The read table of each side is materialised before the pipeline runs (the
CLI executes `EvaluationPlan.prepare_sql`, Ruling R5): the pinned source
clone and the scope or sample table, both plain tables DIRECT_READ can
read.

DIRECT_READ types. The encoder hashes canonical values, so a cell must
reach it with the Python type the BigQuery client gives the same cell —
the client is what reads the R/H panel (`context.reference.fetch_panel`)
and what the generator read its reference sample with
(`sdfb_beam.io.bq_sources.load_reference_rows`). Beam 2.76's
`_CustomBigQueryStorageSource` reads Avro through fastavro by default and
Arrow (`read_arrow`, each cell `.as_py()`) with `use_native_datetime=True`:

    BigQuery type    client (panel)       Arrow DIRECT_READ     Avro DIRECT_READ    paged read
    ───────────────  ───────────────────  ────────────────────  ──────────────────  ──────────
    TIMESTAMP        aware UTC datetime   aware UTC datetime    aware UTC datetime  = client
    DATETIME         naive datetime       naive datetime        text  ◄ fixed here  = client
    DATE, TIME       date, time           date, time            date, time          = client
    NUMERIC          Decimal              Decimal, scale 9      Decimal, scale 9    = client
    BIGNUMERIC       Decimal              Decimal, scale 38     Decimal, scale 38   = client
    BYTES            bytes                bytes                 bytes               = client
    JSON             parsed value         JSON text ◄ fixed     JSON text ◄ fixed   = client
    GEOGRAPHY        WKT text             WKT text              WKT text            = client
    RECORD, ARRAY    dict, list           dict, list            dict, list          = client

Decimal scale needs no fix (`canonical_value` strips trailing zeros). Arrow
is chosen because it types DATETIME natively at every depth, RECORD
sub-fields included; `normalize_direct_read` fixes the top-level JSON (and,
should an Avro read ever feed it, DATETIME text) by the plan's `bq_type`.
Known limits, all on types the generator does not write or nests: a JSON
sub-field inside a RECORD stays text, and INTERVAL (client `relativedelta`,
Arrow `MonthDayNano`) and RANGE are not normalised — rows holding them hash
differently on the panel and DIRECT_READ sides. The paged read has none of
these limits: its cells are decoded by the code that decodes the panel's.
A table with one side paged and the other read by DIRECT_READ therefore
differs between its sides only on those same limits.

The paged read. DIRECT_READ needs `bigquery.readsessions.create` on the
project of every table it reads, and a project may refuse it (a
deployment that does not grant the role; a public project, where the
caller is expected to hold no role — expected, not observed). The driver
asks once per such project before the graph is built
(`probe_direct_read`, the call a worker makes when it splits the read),
and one rule follows (`RoutedBigQuerySources.paged`): A READ TABLE IS
PAGED IF AND ONLY IF ITS PROJECT REFUSED. Every other read table keeps
DIRECT_READ. The paged read needs `bigquery.tables.get` (the table's
metadata) and `bigquery.tables.getData` (its rows) on the read table,
and no read session:

    Create([read table])
      │ Ranges   NOW (the read table is built by the prepare statements,
      │          after planning): tables.get → numRows, numBytes,
      │          lastModifiedTime, and ONE tabledata.list request for
      │          one row → totalRows, the count the row positions
      │          address → row ranges (start, count) holding about
      │          RANGE_BYTES each
      ▼
    Reshuffle    the ranges spread over the workers
      ▼
    Page         per range: tabledata.list(startIndex = position,
      │          maxResults = rows left, selectedFields = plan columns),
      │          ONE response per request — the API cuts a response at
      │          its byte limit, so the next request starts at
      │          position + rows returned, never at a page token
      ▼
    rows ──► the same PCollection[dict] DIRECT_READ gives, in the
             client's types (the table above)

A range is only the rows it claims on a table that does not change, so
the read fails the job (`TableChangedError`, naming the table and both
readings) instead of reading a changing table wrong:

    the cut            the metadata's `numRows` is `tabledata.list`'s
                       `totalRows`. A table is empty only when the list
                       says so: metadata counting no row is still asked
    every response     it carries a `totalRows` (one that does not
                       fails: it cannot be checked), and that is the row
                       count of the cut
    every range        returns exactly its row count; then tables.get
                       again: the row count and the modification time of
                       the cut. The check runs inside the bundle that
                       emitted the range's rows, so a range that fails
                       it commits none of them
    after the last     the rows the ranges emitted (each range counts
      range            what it yields) add up to the cut, and the
                       table's metadata is still the cut's

Metadata that says nothing never passes for an answer: without a
`numRows` the count is the list's own and one WARNING names the table;
without a `lastModifiedTime` the check for a rewrite with as many rows
is off for that table, and one WARNING says so.

What the checks cannot see: a table whose rows changed ORDER while its
row count and modification time did not. The source pin and the sample
tables are immutable; the landing table of a `table` / `manual` scope
and an unpinned source are live tables.

Throttling. `tabledata.list` has a byte quota per project (3.7 GB of row
data per minute, 7.5 GB in the US and EU multi-regions), charged to the
project that CONTAINS THE TABLE and shared with every other reader of
that project and with `jobs.getQueryResults`. A page
BigQuery throttles waits and is asked for again (`page_retry_waits`: by
the error's REASON — `rateLimitExceeded`, `quotaExceeded`, a backend
error — or a transient transport error; never on a 403 alone, so a
missing permission fails at once), with exponential back-off up to
PAGE_RETRY_MAXIMUM_SECONDS between attempts, for PAGE_RETRY_SECONDS in
all: long enough to ride out a per-minute limit the job's own readers
keep hitting, short enough that a quota waiting does not bring back (a
daily or custom one) fails the range, naming the reason, in minutes.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import abc
import glob
import json
import logging
import os
import re
import time
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

import apache_beam as beam
from apache_beam.io.gcp.bigquery import (
    BigQueryDisposition,
    ReadFromBigQuery,
    WriteToBigQuery,
)

from sdfb_evaluation.beam import staged
from sdfb_evaluation.canonical import json_safe
from sdfb_evaluation.context.bq import normalize_fqn
from sdfb_evaluation.schemas import TABLES, load_schema
from sdfb_evaluation.types import Side

if TYPE_CHECKING:
  from sdfb_evaluation.context.plan import ColumnPlan, TablePlan

__all__ = [
    "DEFAULT_RANGE_ROWS",
    "LOAD_ORDER",
    "MAX_RANGE_ROWS",
    "MIN_RANGE_ROWS",
    "PAGE_RETRY_SECONDS",
    "PROBE_SECONDS",
    "RANGE_BYTES",
    "BigQuerySinks",
    "BigQuerySources",
    "ClientLoadSinks",
    "InMemorySources",
    "JsonLoader",
    "LocalJsonSinks",
    "PagedBigQuerySources",
    "RoutedBigQuerySources",
    "Sinks",
    "Sources",
    "TableChangedError",
    "json_line",
    "normalize_direct_read",
    "page_retry_waits",
    "probe_direct_read",
    "read_table_of",
    "row_ranges",
]

# The metric tables first, the registry last: its FINAL row must never be
# visible before the rows it summarises (D7).
LOAD_ORDER = (
    "evaluation_metrics",
    "evaluation_profiles",
    "evaluation_row_flags",
    "evaluation_data_history",
)
_PANEL_SIDES = frozenset({Side.REFERENCE, Side.HOLDOUT})
# Beam's default shard template (-SSSSS-of-NNNNN) after a write's prefix.
_SHARD_SUFFIX = "-[0-9]*-of-[0-9]*.jsonl"
_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
# Beam's file sink writes under <dir>/beam-temp-<prefix name>-<uuid1 hex>
# and removes it when it finalises; a pipeline that fails leaves it.
_BEAM_TEMP = "beam-temp-"
_BEAM_TEMP_ID = re.compile(r"[0-9a-f]{32}")
_GLOB_CHARS = re.compile(r"[*?\[\]]")
_SHOWN_FILES = 3
_LOGGER = logging.getLogger(__name__)

# The paged read (module docstring). A range holds about RANGE_BYTES of
# the table's logical bytes: a few tabledata.list responses (the API cuts
# one at 10 MB), so a bundle that is retried re-reads little and the
# ranges of a large table spread over every worker.
RANGE_BYTES = 64 << 20
MIN_RANGE_ROWS = 1_000
MAX_RANGE_ROWS = 1_000_000
DEFAULT_RANGE_ROWS = 50_000  # a table whose metadata gives no size
# The probe of the Storage Read API, retries included (`probe_direct_read`).
PROBE_SECONDS = 45.0
# One page request: how long it may take, and how it waits when BigQuery
# throttles it (module docstring, Throttling).
PAGE_TIMEOUT_SECONDS = 300.0
PAGE_RETRY_SECONDS = 900.0
PAGE_RETRY_INITIAL_SECONDS = 1.0
PAGE_RETRY_MAXIMUM_SECONDS = 60.0
_PAGE_RETRY_MULTIPLIER = 2.0
_WAIT_REASONS = frozenset({
    "rateLimitExceeded",
    "quotaExceeded",
    "backendError",
    "internalError",
    "badGateway",
})
# `project` → a `google.cloud.bigquery.Client`, called on a worker.
ClientFactory = Callable[[str], Any]
_RANGES = "ranges"
_CUT = "cut"
_ROWS = "rows"
_READ = "read"
_ERROR_CHARS = 300


# --------------------------------------------------------------------------
# DIRECT_READ types
# --------------------------------------------------------------------------
def _type_fixes(
    columns: Sequence[ColumnPlan]) -> tuple[tuple[str, str, bool], ...]:
  """(name, bq_type, repeated) of the columns DIRECT_READ types unlike the
  client: JSON (text) and DATETIME (text on the Avro path)."""
  return tuple((c.name, c.bq_type, c.mode == "REPEATED")
               for c in columns
               if c.bq_type in ("JSON", "DATETIME"))


def _fixed(name: str, bq_type: str, value: Any) -> Any:
  if not isinstance(value, str):
    return value
  try:
    if bq_type == "JSON":
      return json.loads(value)
    return datetime.fromisoformat(value)
  except ValueError as exc:
    # The message names the column and the type, never the value.
    raise ValueError(f"{bq_type} column {name!r}: DIRECT_READ returned text "
                     f"that is not {bq_type} ({type(exc).__name__})") from exc


def _apply_fixes(row: Mapping[str, Any],
                 fixes: Sequence[tuple[str, str, bool]]) -> dict[str, Any]:
  out = dict(row)
  for name, bq_type, repeated in fixes:
    value = out.get(name)
    if value is None:
      continue
    if repeated:
      out[name] = [_fixed(name, bq_type, item) for item in value]
    else:
      out[name] = _fixed(name, bq_type, value)
  return out


def normalize_direct_read(row: Mapping[str, Any],
                          columns: Sequence[ColumnPlan]) -> dict[str, Any]:
  """A DIRECT_READ row with the BigQuery client's Python types (module
  docstring): top-level JSON text parsed, DATETIME text made a naive
  `datetime`. Returns a new dict; `row` is not modified.

  Raises:
    ValueError: a JSON/DATETIME cell's text does not parse (the message
      names the column, never the value).
  """
  return _apply_fixes(row, _type_fixes(columns))


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------
def _panel_rows(table: TablePlan, side: Side) -> list[dict]:
  """The panel side's rows restricted to the plan's columns: the panel is
  `SELECT ref.*` of the source, and the rows travel to the workers in
  the job graph or as a staged object, so source-only columns would only
  make either bigger. A plan column a row lacks stays absent (the
  encoder names it)."""
  panel = table.panel
  if panel is None:
    raise ValueError(
        f"{table.landing_table}: no R/E/H panel was planned, so the {side} "
        "side has no rows; the reference-based metrics are not evaluated "
        "for this table (check TablePlan.panel before reading it)")
  names = [c.name for c in table.columns]
  rows = panel.r_rows if side is Side.REFERENCE else panel.h_rows
  return [{name: row[name] for name in names if name in row} for row in rows]


class Sources(abc.ABC):
  """Reads one (table, side) into a `PCollection[dict]`.

  The reference and holdout sides always come from the plan's panel,
  whatever the implementation — the exact rows the planner read and
  verified: embedded in the job graph (`beam.Create`), or, given a
  `staging`, written by the driver as one object the workers read
  (`beam.staged`).
  """

  def read(self,
           p: beam.Pipeline,
           table: TablePlan,
           side: Side | str,
           *,
           staging: staged.Staging | None = None) -> beam.PCollection:
    """The rows of `table`'s `side`, labelled by landing table and side.
    `staging` is where a panel side's rows are staged instead of
    embedded (None: embedded); the source and synthetic sides never use
    it.

    Raises:
      ValueError: a panel side with no panel, or (per implementation) a
        side with nothing to read.
      StagedObjectError: the panel rows could not be staged.
    """
    side = Side(side)
    label = f"Read[{table.landing_table}/{side}]"
    if side in _PANEL_SIDES:
      return staged.create(
          p, label, _panel_rows(table, side), staging, spread=True)
    return self._read(p, table, side, label)

  @abc.abstractmethod
  def _read(self, p: beam.Pipeline, table: TablePlan, side: Side,
            label: str) -> beam.PCollection:
    """The source or synthetic side's rows."""


def read_table_of(table: TablePlan, side: Side | str) -> str:
  """The BigQuery table the pipeline reads for one source/synthetic side
  of `table` (`project.dataset.table`).

  Raises:
    ValueError: a panel side, or a side with nothing to read.
  """
  side = Side(side)
  if side in _PANEL_SIDES:
    raise ValueError(f"the {side} side comes from the plan's panel, not "
                     "from BigQuery")
  if side is Side.SOURCE:
    read_table = table.source_read_table
  else:
    read_table = table.synthetic_read_table or table.scope.read_table
  if not read_table:
    why = table.skip_reason or "see the plan warnings"
    raise ValueError(f"{table.landing_table}: nothing to read for the "
                     f"{side} side (no read table was planned: {why})")
  return read_table


class BigQuerySources(Sources):
  """DIRECT_READ (BigQuery Storage Read API, Arrow) of each side's read
  table, restricted to the plan's columns, then `normalize_direct_read`."""

  def transform(self, table: TablePlan, side: Side | str) -> ReadFromBigQuery:
    """The `ReadFromBigQuery` of one source/synthetic side.

    Raises:
      ValueError: a panel side, or a side with nothing to read.
    """
    return ReadFromBigQuery(
        table=read_table_of(table, side),
        method=ReadFromBigQuery.Method.DIRECT_READ,
        selected_fields=[c.name for c in table.columns],
        use_native_datetime=True)

  def _read(self, p: beam.Pipeline, table: TablePlan, side: Side,
            label: str) -> beam.PCollection:
    fixes = _type_fixes(table.columns)
    rows: beam.PCollection = p | label >> self.transform(table, side)
    if fixes:
      rows = rows | f"{label}/Types" >> beam.Map(_apply_fixes, fixes)
    return rows


def probe_direct_read(read_table: str, columns: Sequence[str]) -> None:
  """Ask the BigQuery Storage Read API for ONE read session on
  `read_table`, as a DIRECT_READ worker does when it splits the read
  (Beam 2.76, `_CustomBigQueryStorageSource.split`): the same call
  (`BigQueryReadClient.create_read_session`), the same parent — the READ
  TABLE's own project, which is where `bigquery.readsessions.create` is
  checked — Arrow, the plan's columns, and one stream instead of many. No
  row is read; a session nobody reads expires by itself.

  The answer is the workers' answer when the caller runs as the account
  they run as: a Flex Template launcher and its job's workers share one
  service account, and a local runner reads as the caller. A Dataflow job
  submitted from an operator's own machine runs as another account than
  the one that probed.

  The call is bounded: `PROBE_SECONDS` in all, the retries of an
  unavailable service included, so an endpoint that does not answer costs
  a launcher a known part of its twelve minutes and then fails the
  launch.

  Returns when the API accepts the session.

  Raises:
    ValueError: `read_table` is not a `project.dataset.table`.
    Exception: whatever the API raised (`google.api_core.exceptions`): a
      `Forbidden` (`PermissionDenied`) is a refusal; anything else may
      pass on a retry.
  """
  from google.api_core import exceptions  # pylint: disable=import-outside-toplevel  # with the client below
  from google.api_core import retry as retries  # pylint: disable=import-outside-toplevel  # same
  from google.cloud import bigquery_storage_v1 as bq_storage  # pylint: disable=import-outside-toplevel  # credentials and gRPC only when a real read is probed

  project, dataset, name = normalize_fqn(read_table).split(".")
  session = bq_storage.types.ReadSession(
      table=f"projects/{project}/datasets/{dataset}/tables/{name}",
      data_format=bq_storage.types.DataFormat.ARROW,
      read_options=bq_storage.types.ReadSession.TableReadOptions(
          selected_fields=list(columns)))
  bq_storage.BigQueryReadClient().create_read_session(
      parent=f"projects/{project}",
      read_session=session,
      max_stream_count=1,
      retry=retries.Retry(
          predicate=retries.if_exception_type(exceptions.ServiceUnavailable,
                                              exceptions.DeadlineExceeded),
          initial=1.0,
          maximum=8.0,
          multiplier=2.0,
          timeout=PROBE_SECONDS),
      timeout=PROBE_SECONDS)


# --------------------------------------------------------------------------
# the paged read
# --------------------------------------------------------------------------
class TableChangedError(RuntimeError):
  """A table read by row ranges is not the table its ranges were cut
  from (module docstring, The paged read)."""


class _TableState(NamedTuple):
  """What `tables.get` says of a read table: its rows, its logical bytes
  and `lastModifiedTime` (ms). Each is None when the resource does not
  carry it — an absent row count is never read as zero."""
  rows: int | None
  size: int | None
  modified: int | None

  def seen(self) -> str:
    """This reading in words, for an error message."""
    rows = "no row count" if self.rows is None else f"{self.rows} rows"
    return f"{rows}, last modified {self.modified}"


def row_ranges(num_rows: int, num_bytes: int | None) -> list[tuple[int, int]]:
  """`(start, count)` row ranges that cover rows `0 … num_rows - 1` once
  each, in order: about `RANGE_BYTES` of the table's logical bytes a
  range, between `MIN_RANGE_ROWS` and `MAX_RANGE_ROWS` rows, or
  `DEFAULT_RANGE_ROWS` when the size is unknown. `[]` for an empty
  table."""
  if num_rows <= 0:
    return []
  if num_bytes:
    size = RANGE_BYTES * num_rows // num_bytes
    size = min(MAX_RANGE_ROWS, max(MIN_RANGE_ROWS, size))
  else:
    size = DEFAULT_RANGE_ROWS
  return [(start, min(size, num_rows - start))
          for start in range(0, num_rows, size)]


def _reason(exc: BaseException) -> str | None:
  """The `reason` of the first error an API exception carries, if any."""
  try:
    reason = exc.errors[0]["reason"]  # type: ignore[attr-defined]
  except (AttributeError, IndexError, KeyError, TypeError):
    return None
  return str(reason) if reason else None


def _transient(exc: BaseException) -> bool:
  """An error with no reason that a retry may remove: the transport and
  server failures the BigQuery client's own default retry waits for."""
  import requests.exceptions  # pylint: disable=import-outside-toplevel  # the client's transport, loaded with it
  from google.api_core import exceptions  # pylint: disable=import-outside-toplevel  # same
  from google.auth import exceptions as auth_exceptions  # pylint: disable=import-outside-toplevel  # same

  if isinstance(exc, requests.exceptions.SSLError):
    return False  # a configuration or security problem: it does not pass
  return isinstance(
      exc, (ConnectionError, exceptions.TooManyRequests,
            exceptions.InternalServerError, exceptions.BadGateway,
            exceptions.ServiceUnavailable, exceptions.GatewayTimeout,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ConnectionError, requests.exceptions.Timeout,
            auth_exceptions.TransportError))


def page_retry_waits(exc: BaseException) -> bool:
  """Whether a failed page request waits and is sent again (module
  docstring, Throttling). The error's REASON decides when it has one: a
  rate limit, a quota or a backend error waits; `accessDenied`,
  `notFound`, `invalid` and every other reason fail at once. An error
  without a reason waits only when it is a transport or server failure —
  never because its status is 403."""
  reason = _reason(exc)
  if reason is not None:
    return reason in _WAIT_REASONS
  return _transient(exc)


def _bigquery_client(project: str) -> Any:
  from google.cloud import bigquery  # pylint: disable=import-outside-toplevel  # credentials are resolved on the worker that reads

  return bigquery.Client(project=project)


def _table_state(client: Any, read_table: str) -> tuple[_TableState, Any]:
  """(`_TableState`, the `Table`) of `read_table`, from `tables.get`."""
  table = client.get_table(read_table)
  resource = table.to_api_repr()
  size, modified = resource.get("numBytes"), resource.get("lastModifiedTime")
  rows = resource.get("numRows")
  state = _TableState(
      rows=int(rows) if rows is not None else None,
      size=int(size) if size is not None else None,
      modified=int(modified) if modified is not None else None)
  return state, table


def _first_line(exc: BaseException) -> str:
  lines = str(exc).strip().splitlines()
  return (lines[0] if lines else type(exc).__name__)[:_ERROR_CHARS]


class _PagedFn(beam.DoFn):
  """A DoFn that talks to one read table through a BigQuery client made
  in `setup()` (on the worker; never pickled into the graph)."""

  def __init__(self, read_table: str, project: str, make_client: ClientFactory):
    super().__init__()
    self._read_table = read_table
    self._project = project
    self._make_client = make_client
    self._client: Any = None
    self._waits = beam.metrics.Metrics.counter("sdfb_evaluation.paged_read",
                                               "waits")

  def setup(self) -> None:
    self._client = self._make_client(self._project)

  def teardown(self) -> None:
    if self._client is not None:
      self._client.close()
    self._client = None

  def _changed(self, cut: tuple[int, int | None],
               now: str) -> TableChangedError:
    """The error of a table that is not the table of the cut; `now` is
    the second reading, in words."""
    return TableChangedError(
        f"{self._read_table} changed while it was read by row ranges: "
        f"{cut[0]} rows, last modified {cut[1]} (ms since the epoch) when "
        f"its ranges were cut; {now}. A paged read is only correct on a "
        "table that does not change (one being written to, or with rows in "
        "a streaming buffer, is not), so the job fails rather than evaluate "
        "rows read twice or not at all; evaluate it again once nothing "
        "writes to the table")

  @staticmethod
  def _moved(now: _TableState, cut: Sequence[Any]) -> bool:
    """Whether the table's metadata is no longer the cut's `(rows,
    modified)`: another modification time, or another row count where
    the metadata carries one (the cut's count is `tabledata.list`'s
    own, which every response is checked against)."""
    return now.modified != cut[1] or (now.rows is not None and
                                      now.rows != cut[0])

  def _waited(self, exc: BaseException) -> None:
    self._waits.inc()
    _LOGGER.info("paged read of %s: a request waits (%s: %s)", self._read_table,
                 _reason(exc) or type(exc).__name__, _first_line(exc))

  def _list(self, reference: Any, fields: Sequence[Any], start: int | None,
            limit: int, doing: str) -> tuple[Any, int]:
    """ONE `tabledata.list` request for up to `limit` rows of `fields`
    from row `start` (None: no row position is sent): (the response's
    rows as a page, its `totalRows`). `doing` says what the request is
    for, in an error message.

    Raises:
      RuntimeError: the request was still throttled after
        `PAGE_RETRY_SECONDS` (the message names the reason), or the
        response carries no `totalRows`, so it cannot be checked against
        the table's row count.
      Exception: any error that does not wait (`page_retry_waits`).
    """
    from google.api_core import exceptions  # pylint: disable=import-outside-toplevel  # with the client, on a worker
    from google.api_core import retry as retries  # pylint: disable=import-outside-toplevel  # same

    retry = retries.Retry(
        predicate=page_retry_waits,
        initial=PAGE_RETRY_INITIAL_SECONDS,
        maximum=PAGE_RETRY_MAXIMUM_SECONDS,
        multiplier=_PAGE_RETRY_MULTIPLIER,
        timeout=PAGE_RETRY_SECONDS,
        on_error=self._waited)
    rows = self._client.list_rows(
        reference,
        selected_fields=fields,
        start_index=start,
        max_results=limit,
        retry=retry,
        timeout=PAGE_TIMEOUT_SECONDS)
    try:
      page = next(rows.pages, None)
    except exceptions.RetryError as exc:
      cause = exc.cause or exc
      raise RuntimeError(
          f"{self._read_table}: tabledata.list still answered "
          f"{_reason(cause) or type(cause).__name__} after "
          f"{PAGE_RETRY_SECONDS:.0f} s of waiting ({_first_line(cause)}) "
          f"while {doing}. A quota that waiting does not bring back has to "
          "be raised, or the table read with --mode sampled or through the "
          "Storage Read API (roles/bigquery.readSessionUser)") from exc
    total = rows.total_rows
    if total is None:
      raise RuntimeError(
          f"{self._read_table}: tabledata.list answered without totalRows "
          f"while {doing}, so the response cannot be checked against the "
          "table's row count; the job fails rather than evaluate rows it "
          "could not check")
    return page, int(total)


class _CutRangesFn(_PagedFn):
  """The read table → its row ranges `(start, count, rows, modified)`,
  cut at read time, and — tagged `cut` — the `(rows, modified)` every
  range is checked against.

  The row count is `tabledata.list`'s own (`totalRows` of one request
  for one row of one column): row positions address that count, and it
  is there to be asked even when the table's metadata says nothing. The
  metadata's `numRows` is compared with it where the metadata carries
  one, and is never what makes a table empty:

      numRows   totalRows   then
      ───────   ─────────   ──────────────────────────────────────────
      equal     n           n rows in ranges (none, and no row, for 0)
      differs   n           TableChangedError naming both readings
      absent    n           n rows in ranges, and one WARNING

  An absent `lastModifiedTime` switches off the one check that sees a
  rewrite with as many rows: one WARNING says so, and the read goes on.
  """

  def process(self, element: Any) -> Iterator[Any]:
    del element  # the table is this DoFn's own
    state, table = _table_state(self._client, self._read_table)
    first = list(table.schema[:1])
    if not first:
      raise ValueError(f"{self._read_table} has no column, so it has no "
                       "row to page")
    page, total = self._list(table.reference, first, None, 1,
                             "counting its rows")
    if state.rows is None:
      _LOGGER.warning(
          "paged read of %s: the table's metadata carries no row count; "
          "the count is tabledata.list's own (%d rows)", self._read_table,
          total)
    elif state.rows != total:
      raise TableChangedError(
          f"{self._read_table}: tables.get counts {state.rows} rows and "
          f"tabledata.list counts {total} rows (a table being written to, "
          "or with rows in a streaming buffer, is counted differently by "
          "the two). A paged read is only correct on a table that does not "
          "change, so the job fails rather than evaluate rows read twice "
          "or not at all; evaluate it again once nothing writes to the "
          "table")
    if total == 0 and page is not None and page.num_items:
      raise TableChangedError(
          f"{self._read_table}: tabledata.list counts no row and returned "
          "a row; the job fails rather than evaluate a table it cannot "
          "count")
    if state.modified is None:
      _LOGGER.warning(
          "paged read of %s: the table's metadata carries no modification "
          "time, so a table rewritten with as many rows while it is read "
          "would not be noticed; its row count is still checked on every "
          "page", self._read_table)
    yield beam.pvalue.TaggedOutput(_CUT, (total, state.modified))
    ranges = row_ranges(total, state.size)
    _LOGGER.info(
        "paged read of %s: %d rows, %s logical bytes, last modified %s (ms), "
        "%d row ranges", self._read_table, total, state.size, state.modified,
        len(ranges))
    for start, count in ranges:
      yield (start, count, total, state.modified)


class _ReadRangeFn(_PagedFn):
  """One row range → its rows, page by page (module docstring, The
  paged read), and — tagged `read` — the number of rows it emitted."""

  def __init__(self, read_table: str, columns: Sequence[str], project: str,
               make_client: ClientFactory):
    super().__init__(read_table, project, make_client)
    self._columns = tuple(columns)
    self._fields: list[Any] | None = None
    self._reference: Any = None
    self._pages = beam.metrics.Metrics.counter("sdfb_evaluation.paged_read",
                                               "pages")

  def setup(self) -> None:
    super().setup()
    self._fields = None
    self._reference = None

  def _selected(self) -> list[Any]:
    """The plan's columns as the table's own schema fields, in the
    TABLE's order: the client decodes a row's cells by position.

    Raises:
      ValueError: the table lacks a column the plan reads.
    """
    if self._fields is None:
      table = self._client.get_table(self._read_table)
      names = {field.name for field in table.schema}
      missing = [name for name in self._columns if name not in names]
      if missing:
        raise ValueError(f"{self._read_table} has no column {missing}, which "
                         "the plan reads")
      wanted = set(self._columns)
      self._fields = [f for f in table.schema if f.name in wanted]
      self._reference = table.reference
    return self._fields

  def _page(self, position: int, left: int, span: str) -> tuple[Any, int]:
    """(the response's rows as a page, its `totalRows`) of ONE request
    for up to `left` rows from row `position` (`_PagedFn._list`)."""
    fields = self._selected()  # also sets the table's reference
    found = self._list(self._reference, fields, position, left,
                       f"reading {span} at row {position}")
    self._pages.inc()
    return found

  def process(self, element: tuple[int, int, int, int | None]) -> Iterator[Any]:
    start, count, cut_rows, cut_modified = element
    cut = (cut_rows, cut_modified)
    end = start + count
    span = f"rows [{start}, {end})"
    self._selected()
    began = time.monotonic()
    position, pages, emitted = start, 0, 0
    while position < end:
      page, total = self._page(position, end - position, span)
      got = page.num_items if page is not None else 0
      if total != cut_rows:
        raise self._changed(
            cut, f"{total} rows counted by tabledata.list on the page at row "
            f"{position} of {span}")
      if got == 0 or got > end - position:
        returned = "no row" if got == 0 else f"{got} rows"
        raise TableChangedError(
            f"{self._read_table}: tabledata.list returned {returned} at row "
            f"{position} of {span}, where {end - position} were left of the "
            f"{cut_rows} rows its ranges were cut from; the job fails rather "
            "than evaluate rows read twice or not at all")
      for row in page:
        emitted += 1
        yield dict(row.items())
      position += got
      pages += 1
    now, _ = _table_state(self._client, self._read_table)
    if self._moved(now, cut):
      raise self._changed(cut, f"{now.seen()} after {span}")
    _LOGGER.info("paged read of %s: %s in %d pages, %.1f s", self._read_table,
                 span, pages,
                 time.monotonic() - began)
    yield beam.pvalue.TaggedOutput(_READ, emitted)


class _UnchangedFn(_PagedFn):
  """After the last range: the table is still the table of the cut, and
  the rows the ranges emitted are the rows of the cut."""

  def process(self, rows_read: int, cut: tuple[int, int | None]) -> None:
    now, _ = _table_state(self._client, self._read_table)
    if self._moved(now, cut):
      raise self._changed(cut, f"{now.seen()} after its last range")
    if rows_read != cut[0]:
      raise TableChangedError(
          f"{self._read_table}: its row ranges returned {rows_read} rows "
          f"where the table had {cut[0]} rows when they were cut; the job "
          "fails rather than evaluate rows read twice or not at all")
    _LOGGER.info("paged read of %s: complete, %d rows, unchanged",
                 self._read_table, rows_read)


class PagedBigQuerySources(Sources):
  """Each side's read table through the BigQuery client's `list_rows`
  (REST `tabledata.list`), by row ranges, restricted to the plan's
  columns (module docstring, The paged read). It needs
  `bigquery.tables.get` and `bigquery.tables.getData` on the read tables
  and no read session; the rows have the client's own Python types, the
  panel's, so nothing normalises them.

  Args:
    project: the project the workers' BigQuery client is made for (the
      job's).
    make_client: `project` → a `google.cloud.bigquery.Client` (tests
      give one over a fake connection); called on the worker.
  """

  def __init__(self,
               project: str,
               *,
               make_client: ClientFactory = _bigquery_client):
    self.project = project
    self._make_client = make_client

  def _read(self, p: beam.Pipeline, table: TablePlan, side: Side,
            label: str) -> beam.PCollection:
    read_table = normalize_fqn(read_table_of(table, side))
    columns = [c.name for c in table.columns]
    about = (read_table, self.project, self._make_client)
    cut = (
        p
        | f"{label}/Table" >> beam.Create([read_table])
        | f"{label}/Ranges" >> beam.ParDo(_CutRangesFn(*about)).with_outputs(
            _CUT, main=_RANGES))
    read = (
        cut[_RANGES]
        | f"{label}/Reshuffle" >> beam.Reshuffle()
        | f"{label}/Page" >> beam.ParDo(
            _ReadRangeFn(read_table, columns, self.project,
                         self._make_client)).with_outputs(_READ, main=_ROWS))
    _ = (
        read[_READ]
        | f"{label}/Count" >> beam.CombineGlobally(sum)
        | f"{label}/Unchanged" >> beam.ParDo(
            _UnchangedFn(*about), beam.pvalue.AsSingleton(cut[_CUT])))
    rows: beam.PCollection = read[_ROWS]
    return rows


class RoutedBigQuerySources(Sources):
  """DIRECT_READ for every read table, except those of the projects
  that refused a read session, which are paged (module docstring, The
  paged read). `paged` is the one rule; nothing else chooses a path.

  Args:
    refused: the projects whose probe was refused.
    project: the project the paged read's client is made for (the job's).
    make_client: see `PagedBigQuerySources`.
  """

  def __init__(self,
               refused: Collection[str],
               project: str,
               *,
               make_client: ClientFactory = _bigquery_client):
    self.refused = frozenset(refused)
    self._direct = BigQuerySources()
    self._paged = PagedBigQuerySources(project, make_client=make_client)

  def paged(self, read_table: str) -> bool:
    """Whether `read_table` (`project.dataset.table`) is read by pages:
    if and only if its project refused a read session."""
    return normalize_fqn(read_table).split(".", 1)[0] in self.refused

  def _read(self, p: beam.Pipeline, table: TablePlan, side: Side,
            label: str) -> beam.PCollection:
    del label  # the chosen sources label the read the same way
    chosen = (
        self._paged if self.paged(read_table_of(table, side)) else self._direct)
    return chosen.read(p, table, side)


class InMemorySources(Sources):
  """`beam.Create` over `rows_by[(table name, side)]` for the source and
  synthetic sides (tests, fixtures, the acceptance run).

  Raises (at construction):
    ValueError: a reference/holdout key — those sides always come from
      the plan's panel.
  """

  def __init__(self, rows_by: Mapping[tuple[str, Side | str],
                                      Sequence[Mapping[str, Any]]]):
    rows: dict[tuple[str, Side], list[Mapping[str, Any]]] = {}
    for (name, side_value), side_rows in rows_by.items():
      side = Side(side_value)
      if side in _PANEL_SIDES:
        raise ValueError(f"({name}, {side}): the reference and holdout sides "
                         "always come from the plan's panel, never from "
                         "in-memory rows")
      rows[(name, side)] = list(side_rows)
    self._rows = rows

  def _read(self, p: beam.Pipeline, table: TablePlan, side: Side,
            label: str) -> beam.PCollection:
    key = (table.name, side)
    if key not in self._rows:
      raise ValueError(f"no in-memory rows for ({table.name}, {side}); "
                       f"known: {sorted(str(k) for k in self._rows)}")
    rows: beam.PCollection = p | label >> beam.Create(self._rows[key])
    return rows


# --------------------------------------------------------------------------
# sinks
# --------------------------------------------------------------------------
def _check_table(table: str) -> None:
  if table not in TABLES:
    raise ValueError(f"{table!r} is not an evaluation table; expected one of "
                     f"{list(TABLES)}")


def json_line(row: Mapping[str, Any], table: str) -> str:
  """`row` as one canonical NDJSON line: JSON-safe (NaN/±Inf → null, as a
  BigQuery load requires), sorted keys, compact separators.

  Raises:
    TypeError: a value JSON cannot hold (sinks take rows as
      `to_metric_row`/`registry_seed` build them: strings, numbers,
      booleans, lists, dicts).
  """
  try:
    return json.dumps(
        json_safe(dict(row)),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False)
  except (TypeError, ValueError) as exc:
    raise TypeError(f"{table}: a row is not JSON-serialisable ({exc})") from exc


class Sinks(abc.ABC):
  """Writes output rows (dicts in an evaluation table's schema)."""

  @abc.abstractmethod
  def write(self,
            rows: beam.PCollection,
            table: str,
            *,
            label: str | None = None) -> tuple[beam.PCollection, ...]:
    """Write `rows` to `table`; return the PCollections that are complete
    once the rows are committed — the side inputs the FINAL registry row
    waits on (D7).

    Raises:
      ValueError: `table` is not one of the evaluation tables.
    """


class BigQuerySinks(Sinks):
  """BigQuery FILE_LOADS appends into the pre-created evaluation tables
  (`schemas.bq_mk_commands`): batch-shaped and cheaper than streaming, and
  their load/copy job ids are the "committed" signal. Rows go through
  `json_safe` first."""

  def __init__(self,
               project: str,
               dataset: str,
               *,
               gcs_temp_location: str | None = None):
    self.project = project
    self.dataset = dataset
    self.gcs_temp_location = gcs_temp_location

  def transform(self, table: str) -> WriteToBigQuery:
    """The `WriteToBigQuery` of one evaluation table."""
    _check_table(table)
    return WriteToBigQuery(
        table=f"{self.project}:{self.dataset}.{table}",
        schema={"fields": load_schema(table)},
        method=WriteToBigQuery.Method.FILE_LOADS,
        write_disposition=BigQueryDisposition.WRITE_APPEND,
        create_disposition=BigQueryDisposition.CREATE_NEVER,
        custom_gcs_temp_location=self.gcs_temp_location)

  def write(self,
            rows: beam.PCollection,
            table: str,
            *,
            label: str | None = None) -> tuple[beam.PCollection, ...]:
    write = self.transform(table)
    label = label or f"Write[{table}]"
    result = (
        rows
        | f"{label}/JsonSafe" >> beam.Map(json_safe)
        | f"{label}/Load" >> write)
    return (result.destination_load_jobid_pairs,
            result.destination_copy_jobid_pairs)


class LocalJsonSinks(Sinks):
  """NDJSON shards under `<out_dir>/<table>/` (`json_line` per row), for
  the DirectRunner.

  Every `write` gets its own shard prefix, `<NNNN>-<label slug>` (a
  counter per instance), so two writes to one table never finalise onto
  the same file. `read_rows` reads only the prefixes this instance wrote,
  and `write` refuses a table directory that holds anything else — another
  run's shards, or another run's temp files — instead of mixing runs:
  give each evaluation a fresh `out_dir`. The temp directory Beam leaves
  under one of THIS instance's prefixes, when its pipeline failed at run
  time, is its own: the driver can still write its FAILED event
  (`write_rows`) beside it.
  """

  def __init__(self, out_dir: str):
    """Raises:
      ValueError: `out_dir` holds a glob character (`*?[]`): Beam's file
        sink finds its own shards by glob and would fail at finalize.
    """
    if _GLOB_CHARS.search(out_dir):
      raise ValueError(f"output directory {out_dir!r} holds a glob character "
                       "(*?[]): Beam's file sink matches its shards by glob "
                       "and cannot finalise there; choose another path")
    self.out_dir = out_dir
    self._writes = 0
    self._prefixes: dict[str, list[str]] = {}

  def table_dir(self, table: str) -> str:
    _check_table(table)
    return os.path.join(self.out_dir, table)

  def write(self,
            rows: beam.PCollection,
            table: str,
            *,
            label: str | None = None) -> tuple[beam.PCollection, ...]:
    """Raises:
      ValueError: `table` is not an evaluation table, or its directory
        holds files this instance did not write.
    """
    self._refuse_foreign(table)
    label = label or f"Write[{table}]"
    self._writes += 1
    slug = _UNSAFE_PATH_CHARS.sub("_", label).strip("_") or "write"
    prefix = os.path.join(self.table_dir(table), f"{self._writes:04d}-{slug}")
    self._prefixes.setdefault(table, []).append(prefix)
    files = (
        rows
        | f"{label}/Json#{self._writes}" >> beam.Map(json_line, table)
        | f"{label}/Files#{self._writes}" >> beam.io.WriteToText(
            prefix, file_name_suffix=".jsonl"))
    return (files,)

  def write_rows(self, rows: Sequence[Mapping[str, Any]], table: str, *,
                 label: str) -> str:
    """Driver-side: write `rows` to `table` NOW, as one finished shard
    under its own prefix (the registry's RUNNING and FAILED events, which
    the driver writes around the pipeline). The shard is this instance's
    own, so `read_rows` returns it and a later `write` to the same table
    is not refused. Returns the file.

    Raises:
      ValueError: `table` is not an evaluation table, or its directory
        holds files this instance did not write.
      TypeError: a row is not JSON-serialisable.
    """
    self._refuse_foreign(table)
    self._writes += 1
    slug = _UNSAFE_PATH_CHARS.sub("_", label).strip("_") or "write"
    prefix = os.path.join(self.table_dir(table), f"{self._writes:04d}-{slug}")
    lines = [json_line(row, table) for row in rows]
    os.makedirs(self.table_dir(table), exist_ok=True)
    path = f"{prefix}-00000-of-00001.jsonl"
    with open(path, "w", encoding="utf-8") as shard:
      shard.writelines(f"{line}\n" for line in lines)
    self._prefixes.setdefault(table, []).append(prefix)
    return path

  def _own_shards(self, table: str) -> list[str]:
    return sorted(
        path for prefix in self._prefixes.get(table, ())
        for path in glob.glob(glob.escape(prefix) + _SHARD_SUFFIX))

  def _own_leftover(self, table: str, name: str) -> bool:
    """Whether `name` is the temp directory Beam's file sink made for one
    of this instance's own writes: `beam-temp-<own prefix>-<32 hex>`."""
    for prefix in self._prefixes.get(table, ()):
      head = f"{_BEAM_TEMP}{os.path.basename(prefix)}-"
      if name.startswith(head) and _BEAM_TEMP_ID.fullmatch(name[len(head):]):
        return True
    return False

  def _refuse_foreign(self, table: str) -> None:
    directory = self.table_dir(table)
    if not os.path.isdir(directory):
      return
    own = set(self._own_shards(table))
    foreign = sorted(
        name for name in os.listdir(directory)
        if os.path.join(directory, name) not in own and
        not self._own_leftover(table, name))
    if foreign:
      shown = ", ".join(foreign[:_SHOWN_FILES])
      if len(foreign) > _SHOWN_FILES:
        shown += ", …"
      raise ValueError(
          f"{directory} already holds {len(foreign)} file(s) from another run "
          f"({shown}); this sink never mixes runs — write each evaluation to "
          "a fresh output directory")

  def read_rows(self, table: str) -> list[dict]:
    """Every row this instance wrote to `table` (shard order; `[]` when
    none) — never a shard another run left in the directory.

    Raises:
      ValueError: `table` is not an evaluation table.
    """
    _check_table(table)
    rows: list[dict] = []
    for path in self._own_shards(table):
      with open(path, encoding="utf-8") as shard:
        rows.extend(json.loads(line) for line in shard if line.strip())
    return rows


class JsonLoader(Protocol):
  """What `ClientLoadSinks.load` needs of `context.bq.Bq`."""

  def load_json(self, fqn: str, rows: Sequence[dict],
                schema: list[dict]) -> str:
    ...


class ClientLoadSinks(LocalJsonSinks):
  """`LocalJsonSinks` for the DirectRunner, then — after the pipeline
  finishes — `load(bq)` appends each table's rows with one BigQuery load
  job (`Bq.load_json`: WRITE_APPEND, CREATE_NEVER), in `LOAD_ORDER`, the
  registry last."""

  def __init__(self, out_dir: str, *, project: str, dataset: str):
    super().__init__(out_dir)
    self.project = project
    self.dataset = dataset

  def load(self, bq: JsonLoader) -> dict[str, str]:
    """Load every table that has rows, metric tables before the registry;
    returns `{table: load job id}`. A table with no rows gets no job."""
    jobs = {}
    for table in LOAD_ORDER:
      rows = self.read_rows(table)
      if rows:
        jobs[table] = bq.load_json(f"{self.project}.{self.dataset}.{table}",
                                   rows, load_schema(table))
    return jobs
