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
"""Tests for the paged read of `sdfb_evaluation.beam.io` (Task 44): the
source and synthetic sides read through the BigQuery client's
`list_rows` when the Storage Read API refuses a read session.

The client is the real one; only its HTTP connection is a fake that
answers as the REST API does (`bq_api`), so the row ranges, the page
requests and every cell conversion are exercised, not imitated. No
network.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import logging
import re
from collections.abc import Iterator
from typing import Any, ClassVar

import apache_beam as beam
import numpy as np
import pytest
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.testing.test_pipeline import TestPipeline as BeamTestPipeline
from apache_beam.testing.util import assert_that, equal_to
from google.api_core import exceptions as api_exceptions
from google.cloud import bigquery, bigquery_storage_v1

from sdfb_evaluation.beam import io as eval_io
from sdfb_evaluation.beam.encode import BatchEncoder
from sdfb_evaluation.beam.io import (
    InMemorySources,
    LocalJsonSinks,
    PagedBigQuerySources,
    RoutedBigQuerySources,
    TableChangedError,
    page_retry_waits,
    probe_direct_read,
    read_table_of,
    row_ranges,
)
from sdfb_evaluation.beam.pipeline import (
    build_evaluation_pipeline,
    pipeline_options_defaults,
)
from sdfb_evaluation.canonical import canonical_json, row_digest
from sdfb_evaluation.context.bq import Bq
from sdfb_evaluation.context.plan import ColumnPlan, TablePlan
from sdfb_evaluation.types import ColumnKind, Side

from . import bq_api
from .acceptance_data import (
    ITEMS_FIELDS,
    ORDER_EDGE,
    ORDERS_FIELDS,
    USER_EDGE,
    USERS_FIELDS,
    evaluation_plan,
    launch_rows,
)
from .acceptance_data import table_plan as acceptance_table
from .bq_api import (
    ApiTable,
    FakeApi,
    api_error,
    full_schema,
    typed_rows,
    wire_rows,
)
from .tables import (
    PROJECT,
    all_types,
    all_types_plan,
    client_rows,
    make_panel,
    table_plan,
)

SALT = "5a17" * 8


@pytest.fixture(name="fast_retries", autouse=True)
def fixture_fast_retries(monkeypatch) -> None:
  """A throttled page waits milliseconds here, not seconds."""
  monkeypatch.setattr(eval_io, "PAGE_RETRY_INITIAL_SECONDS", 0.001)
  monkeypatch.setattr(eval_io, "PAGE_RETRY_MAXIMUM_SECONDS", 0.002)


@pytest.fixture(name="installed", autouse=True)
def fixture_installed() -> Iterator[None]:
  """No test leaves its fake API behind for the next one."""
  yield
  bq_api.uninstall()


def _all_types_table(**kwargs: Any) -> ApiTable:
  fixture = all_types()
  fields = full_schema(fixture["schema"], fixture["rows"])
  return ApiTable(fields, wire_rows(fields, fixture["rows"]), **kwargs)


def _all_types_api(**kwargs: Any) -> tuple[TablePlan, FakeApi]:
  """The all-types table behind both read tables of its plan."""
  plan = all_types_plan()
  api = FakeApi(
      {
          plan.source_read_table: _all_types_table(kind="SNAPSHOT"),
          plan.synthetic_read_table: _all_types_table(),
      }, **kwargs)
  return plan, bq_api.install(api)


def _sources() -> PagedBigQuerySources:
  return PagedBigQuerySources(PROJECT, make_client=bq_api.make_client)


def _comparable(row: dict[str, Any]) -> tuple:
  return tuple(sorted((k, repr(v)) for k, v in row.items()))


# Rows a running pipeline hands back (the in-process runner shares the
# module; `_collect` is pickled by reference, a bound `list.append` would
# carry a copy of the list).
_SINK: list[dict[str, Any]] = []


def _collect(row: dict[str, Any]) -> None:
  _SINK.append(row)


def _read(plan: TablePlan, side: Side) -> list[dict[str, Any]]:
  """Run a pipeline that reads `side` of `plan` by pages; its rows."""
  _SINK.clear()
  with BeamTestPipeline() as p:
    _ = _sources().read(p, plan, side) | beam.Map(_collect)
  return list(_SINK)


_ID = ColumnPlan(
    name="id",
    bq_type="INT64",
    mode="REQUIRED",
    kind=ColumnKind.IDENTIFIER,
    is_key=True,
    day_granularity=False)
_NOTE = ColumnPlan(
    name="note",
    bq_type="STRING",
    mode="NULLABLE",
    kind=ColumnKind.TEXT,
    is_key=False,
    day_granularity=False)
_NARROW = [{
    "name": "id",
    "type": "INT64",
    "mode": "REQUIRED"
}, {
    "name": "unplanned",
    "type": "STRING",
    "mode": "NULLABLE"
}, {
    "name": "note",
    "type": "STRING",
    "mode": "NULLABLE"
}]


def _narrow(rows: int, **kwargs: Any) -> tuple[TablePlan, FakeApi]:
  """A table of `rows` rows (`id` 0…, a column the plan does not read,
  `note`) behind the synthetic read table of a two-column plan."""
  plan = table_plan("events", [_ID, _NOTE], pk=["id"])
  raw = [{"id": i, "unplanned": "x" * 40, "note": f"n{i}"} for i in range(rows)]
  table = ApiTable(list(_NARROW), wire_rows(_NARROW, raw))
  api = FakeApi({plan.synthetic_read_table: table}, **kwargs)
  return plan, bq_api.install(api)


# ---------------------------------------------------------------------------
# row ranges
# ---------------------------------------------------------------------------


def test_row_ranges_cover_a_table_exactly_once():
  for rows, size in ((1, 10), (999, 1 << 20), (1_000_000, 5 << 30),
                     (12_345_678, 40 << 30), (5_000, 0), (5_000, None)):
    ranges = row_ranges(rows, size)
    assert ranges[0][0] == 0
    assert all(count > 0 for _, count in ranges)
    # contiguous: each range starts where the one before it ends
    for (start, count), (following, _) in itertools.pairwise(ranges):
      assert following == start + count
    assert sum(count for _, count in ranges) == rows
  assert not row_ranges(0, 0)


def test_row_ranges_are_sized_by_the_tables_bytes_within_bounds():
  per_range = eval_io.RANGE_BYTES
  # 1 KB rows: a range holds RANGE_BYTES of them
  ranges = row_ranges(10_000_000, 10_000_000 * 1024)
  assert ranges[0][1] == per_range // 1024
  # tiny rows: never more than MAX_RANGE_ROWS in one range
  assert row_ranges(50_000_000, 50_000_000)[0][1] == eval_io.MAX_RANGE_ROWS
  # huge rows: never fewer than MIN_RANGE_ROWS
  assert row_ranges(10_000, 10_000 * (64 << 20))[0][1] == eval_io.MIN_RANGE_ROWS
  # no size known: a fixed row count
  assert row_ranges(1_000_000, None)[0][1] == eval_io.DEFAULT_RANGE_ROWS


# ---------------------------------------------------------------------------
# the read
# ---------------------------------------------------------------------------


def test_paged_sources_read_each_side_through_tabledata_list():
  plan, api = _all_types_api()
  want = [_comparable(row) for row in client_rows()]
  for side in (Side.SOURCE, Side.SYNTHETIC):
    assert sorted(_comparable(row) for row in _read(plan, side)) == sorted(want)
  names = ",".join(c.name for c in plan.columns)
  lists = api.pages()
  assert len(lists) == 2  # one page a side: four rows
  for params in lists:
    # the plan's columns only, and never a page token
    assert params["selectedFields"] == names
    assert params["startIndex"] == 0 and params["maxResults"] == 4
    assert "pageToken" not in params


def test_only_the_plan_columns_are_asked_for_in_the_tables_own_order():
  plan, api = _narrow(5)
  # the plan lists its columns in another order than the table's schema
  shuffled = dataclasses.replace(plan, columns=(_NOTE, _ID))
  rows = _read(shuffled, Side.SYNTHETIC)
  assert sorted(
      rows, key=lambda r: r["id"]) == [{
          "id": i,
          "note": f"n{i}"
      } for i in range(5)]
  assert api.pages()[0]["selectedFields"] == "id,note"


def test_a_table_is_read_in_ranges_and_short_pages_exactly_once(monkeypatch):
  """The response's byte cap returns fewer rows than asked: the next
  request starts where the rows returned ended, inside each range."""
  monkeypatch.setattr(eval_io, "MIN_RANGE_ROWS", 100)
  monkeypatch.setattr(eval_io, "RANGE_BYTES", 1)  # MIN_RANGE_ROWS a range
  plan, api = _narrow(1_000, page_rows=37)
  rows = _read(plan, Side.SYNTHETIC)
  assert sorted(row["id"] for row in rows) == list(range(1_000))
  assert sorted(row["note"] for row in rows) == sorted(
      f"n{i}" for i in range(1_000))
  assert all(row["note"] == "n" + str(row["id"]) for row in rows)
  lists = api.pages()
  starts = sorted(p["startIndex"] for p in lists)
  # ten ranges of 100 rows, each read as pages of 37, 37 and 26 rows
  assert starts == sorted(
      base + offset for base in range(0, 1_000, 100) for offset in (0, 37, 74))
  for params in lists:
    left = 100 - params["startIndex"] % 100
    assert params["maxResults"] == left
    assert "pageToken" not in params


def test_an_empty_table_is_asked_once_and_yields_no_row():
  """An empty table is empty because `tabledata.list` says so, never
  because its metadata does: one request, which counts no row."""
  plan, api = _narrow(0)
  assert not _read(plan, Side.SYNTHETIC)
  assert len(api.lists()) == 1
  assert api.lists()[0]["maxResults"] == 1
  assert not api.pages()


def test_paged_sources_refuse_a_side_with_nothing_to_read():
  empty = dataclasses.replace(all_types_plan(), source_read_table="")
  with pytest.raises(ValueError, match="nothing to read"):
    _sources().read(beam.Pipeline(), empty, Side.SOURCE)


def test_panel_sides_still_come_from_the_plan():
  rows = client_rows()
  plan = all_types_plan(panel=make_panel(rows[:2], rows[2:]))
  api = bq_api.install(FakeApi({}))
  with BeamTestPipeline() as p:
    assert_that(
        _sources().read(p, plan, Side.REFERENCE) | beam.Map(_comparable),
        equal_to([_comparable(row) for row in rows[:2]]))
  assert not api.requests


def test_each_side_of_each_table_has_its_own_labels():
  plan = all_types_plan()
  p = beam.Pipeline()
  for side in (Side.SOURCE, Side.SYNTHETIC):
    _sources().read(p, plan, side)
  labels = set(p.applied_labels)
  for side in ("source", "synthetic"):
    assert f"Read[{plan.landing_table}/{side}]/Page" in labels


def test_the_paged_read_serialises_into_a_job_graph_without_a_client():
  """What Dataflow receives: the DoFns pickle with the production client
  factory, and no client (so no credentials) is made before a worker's
  `setup()`. The network guard fails the test if one is."""
  plan = all_types_plan()
  p = beam.Pipeline()
  sources = PagedBigQuerySources(PROJECT)  # the real client factory
  for side in (Side.SOURCE, Side.SYNTHETIC):
    sources.read(p, plan, side)
  proto = p.to_runner_api()
  names = {t.unique_name for t in proto.components.transforms.values()}
  for side in ("source", "synthetic"):
    for step in ("Ranges", "Reshuffle", "Page", "Count", "Unchanged"):
      assert any(
          name.startswith(f"Read[{plan.landing_table}/{side}]/{step}")
          for name in names), (side, step)


def test_a_plan_column_the_table_lacks_fails_naming_it():
  plan, _ = _narrow(3)
  ghost = dataclasses.replace(_NOTE, name="ghost")
  with pytest.raises(Exception, match="ghost"):
    _read(dataclasses.replace(plan, columns=(_ID, ghost)), Side.SYNTHETIC)


# ---------------------------------------------------------------------------
# the probe
# ---------------------------------------------------------------------------


class _ReadClient:
  """`BigQueryReadClient`, recording the session it is asked for."""
  asked: ClassVar[list[dict[str, Any]]] = []
  error: ClassVar[BaseException | None] = None

  def create_read_session(self, **kwargs: Any) -> Any:
    type(self).asked.append(kwargs)
    error = type(self).error
    if error is not None:
      raise error
    return object()


@pytest.fixture(name="read_client")
def fixture_read_client(monkeypatch) -> type[_ReadClient]:
  monkeypatch.setattr(bigquery_storage_v1, "BigQueryReadClient", _ReadClient)
  monkeypatch.setattr(_ReadClient, "asked", [])
  monkeypatch.setattr(_ReadClient, "error", None)
  return _ReadClient


def test_the_probe_asks_what_a_direct_read_worker_asks(read_client):
  """Beam 2.76's `_CustomBigQueryStorageSource.split`: the session's
  parent is the READ TABLE's project, the table by its resource name,
  Arrow, the plan's columns. One stream, and no row is read."""
  probe_direct_read("demo-other-project.thelook_source.order_items",
                    ["id", "status"])
  [asked] = read_client.asked
  assert asked["parent"] == "projects/demo-other-project"
  assert asked["max_stream_count"] == 1
  session = asked["read_session"]
  assert session.table == ("projects/demo-other-project/datasets/"
                           "thelook_source/tables/order_items")
  assert session.data_format == bigquery_storage_v1.types.DataFormat.ARROW
  assert list(session.read_options.selected_fields) == ["id", "status"]
  # bounded, retries included: a launcher has twelve minutes in all
  assert asked["timeout"] == eval_io.PROBE_SECONDS <= 60
  assert asked["retry"].timeout == eval_io.PROBE_SECONDS


def test_the_probe_raises_what_the_api_raised(read_client):
  denied = api_exceptions.PermissionDenied("no bigquery.readsessions.create")
  read_client.error = denied
  with pytest.raises(api_exceptions.PermissionDenied) as caught:
    probe_direct_read(f"{PROJECT}.thelook_source.order_items", ["id"])
  assert caught.value is denied
  assert caught.value.code == 403  # what the driver reads as a refusal
  with pytest.raises(ValueError, match=r"project\.dataset\.table"):
    probe_direct_read("order_items", ["id"])


# ---------------------------------------------------------------------------
# one rule: a read table is paged if and only if its project refused
# ---------------------------------------------------------------------------

OTHER = "demo-other-project"


def _two_projects() -> TablePlan:
  """The all-types plan with its source read from another project."""
  plan = all_types_plan()
  moved = plan.source_read_table.replace(PROJECT, OTHER, 1)
  return dataclasses.replace(plan, source_read_table=moved)


def _routed(*refused: str) -> RoutedBigQuerySources:
  return RoutedBigQuerySources(refused, PROJECT, make_client=bq_api.make_client)


def test_a_read_table_is_paged_iff_its_project_refused():
  plan = _two_projects()
  sources = _routed(OTHER)
  assert sources.paged(plan.source_read_table)
  assert not sources.paged(plan.synthetic_read_table)
  assert not _routed().paged(plan.source_read_table)
  both = _routed(OTHER, PROJECT)
  assert both.paged(plan.source_read_table)
  assert both.paged(plan.synthetic_read_table)
  with pytest.raises(ValueError, match=r"project\.dataset\.table"):
    sources.paged("not a table")


@pytest.mark.parametrize("refused, paged", [
    ((), set()),
    ((OTHER,), {"source"}),
    ((PROJECT,), {"synthetic"}),
    ((OTHER, PROJECT), {"source", "synthetic"}),
])
def test_each_side_gets_the_read_of_its_own_project(refused, paged):
  """Composed, never run (DIRECT_READ cannot run here): a paged side
  has the paged read's steps, a DIRECT_READ side the type fix that
  follows `ReadFromBigQuery`."""
  plan = _two_projects()
  p = beam.Pipeline()
  sources = _routed(*refused)
  for side in (Side.SOURCE, Side.SYNTHETIC):
    sources.read(p, plan, side)
  labels = set(p.applied_labels)
  for side in ("source", "synthetic"):
    label = f"Read[{plan.landing_table}/{side}]"
    assert (f"{label}/Page" in labels) == (side in paged), side
    assert (f"{label}/Types" in labels) == (side not in paged), side


def test_the_paged_side_of_a_routed_read_reads_its_rows():
  plan = _two_projects()
  api = bq_api.install(
      FakeApi({plan.source_read_table: _all_types_table(kind="SNAPSHOT")}))
  _SINK.clear()
  with BeamTestPipeline() as p:
    _ = _routed(OTHER).read(p, plan, Side.SOURCE) | beam.Map(_collect)
  assert sorted(_comparable(row) for row in _SINK) == sorted(
      _comparable(row) for row in client_rows())
  assert len(api.pages()) == 1


# ---------------------------------------------------------------------------
# a table that changes while it is read
# ---------------------------------------------------------------------------

# The request during which a table changes: the third `tabledata.list`
# request, which is the second range's page (the first one counts the
# table, the second is the first range's page).
_MID_READ = 3


def _grow(api: FakeApi, request: int) -> None:
  if request == _MID_READ:
    table = next(iter(api.tables.values()))
    table.rows.append(table.rows[0])


def _touch(api: FakeApi, request: int) -> None:
  if request == _MID_READ:
    next(iter(api.tables.values())).modified_ms += 60_000


def test_rows_added_while_a_table_is_read_fail_the_job(monkeypatch):
  monkeypatch.setattr(eval_io, "MIN_RANGE_ROWS", 10)
  monkeypatch.setattr(eval_io, "RANGE_BYTES", 1)
  plan, api = _narrow(40)
  api.on_list = _grow
  with pytest.raises(Exception) as caught:
    _read(plan, Side.SYNTHETIC)
  message = str(caught.value)
  assert TableChangedError.__name__ in message or isinstance(
      caught.value, TableChangedError)
  assert plan.synthetic_read_table in message
  assert "40 rows" in message and "41 rows" in message
  assert "on the page at row" in message  # a page's own count caught it


def test_a_table_rewritten_with_as_many_rows_fails_the_job(monkeypatch):
  """Same row count, another modification time: only the table's
  metadata, read again after a range's last page, can tell."""
  monkeypatch.setattr(eval_io, "MIN_RANGE_ROWS", 10)
  monkeypatch.setattr(eval_io, "RANGE_BYTES", 1)
  plan, api = _narrow(40)
  api.on_list = _touch
  with pytest.raises(Exception) as caught:
    _read(plan, Side.SYNTHETIC)
  message = str(caught.value)
  assert plan.synthetic_read_table in message
  assert "modified" in message
  cut = bq_api.MODIFIED_MS
  assert str(cut) in message and str(cut + 60_000) in message
  assert re.search(r"after rows \[\d+, \d+\)", message)  # a range's re-read


def _warnings(caplog) -> list[str]:
  """The WARNING lines the paged read logged."""
  return [
      r.getMessage()
      for r in caplog.records
      if r.levelno == logging.WARNING and r.name == "sdfb_evaluation.beam.io"
  ]


@pytest.mark.parametrize("said, held", [(6, 4), (0, 4), (3, 0)])
def test_a_table_whose_metadata_disagrees_with_its_rows_fails(said, held):
  """`tables.get` counts `said` rows where `tabledata.list` has `held`
  (a streaming buffer, a change between the two calls): never a short
  read, and never an empty one — metadata that counts NO row over a
  table that holds four is the case no page would ever have checked."""
  plan, api = _narrow(held)
  next(iter(api.tables.values())).num_rows = said
  with pytest.raises(Exception) as caught:
    _read(plan, Side.SYNTHETIC)
  message = str(caught.value)
  assert plan.synthetic_read_table in message
  assert f"tables.get counts {said} rows" in message
  assert f"tabledata.list counts {held} rows" in message
  assert not api.pages()  # it failed when the ranges were cut


def test_metadata_without_a_row_count_reads_the_rows_and_says_so(caplog):
  """An absent `numRows` is not zero and not a disagreement: the count
  is `tabledata.list`'s own, which is the one row positions address."""
  plan, api = _narrow(4)
  next(iter(api.tables.values())).omit = ("numRows",)
  with caplog.at_level("INFO", logger="sdfb_evaluation.beam.io"):
    rows = _read(plan, Side.SYNTHETIC)
  assert sorted(row["id"] for row in rows) == [0, 1, 2, 3]
  warned = _warnings(caplog)
  assert len(warned) == 1
  assert plan.synthetic_read_table in warned[0]
  assert "carries no row count" in warned[0]
  assert "the count is tabledata.list's own" in warned[0]
  assert "4 rows" in warned[0]


def test_metadata_without_a_modification_time_reads_and_warns_once(
    caplog, monkeypatch):
  """Without `lastModifiedTime` a rewrite with as many rows cannot be
  seen: the read goes on and says, once for the table, that the check
  is off."""
  monkeypatch.setattr(eval_io, "MIN_RANGE_ROWS", 10)
  monkeypatch.setattr(eval_io, "RANGE_BYTES", 1)
  plan, api = _narrow(40)  # four ranges
  next(iter(api.tables.values())).omit = ("lastModifiedTime",)
  with caplog.at_level("INFO", logger="sdfb_evaluation.beam.io"):
    rows = _read(plan, Side.SYNTHETIC)
  assert sorted(row["id"] for row in rows) == list(range(40))
  warned = _warnings(caplog)
  assert len(warned) == 1
  assert plan.synthetic_read_table in warned[0]
  assert "carries no modification time" in warned[0]
  assert "rewritten with as many rows" in warned[0]


@pytest.mark.parametrize("first_without", [1, 2])
def test_a_response_without_total_rows_fails(first_without):
  """Every response is checked against the row count of the cut; one
  that carries no `totalRows` (the count request, then a page) cannot
  be, so it fails instead of being read unchecked."""
  plan, api = _narrow(4)
  api.no_total_rows_from = first_without
  with pytest.raises(Exception, match="without totalRows") as caught:
    _read(plan, Side.SYNTHETIC)
  assert plan.synthetic_read_table in str(caught.value)
  assert len(api.lists()) == first_without


def test_a_table_without_a_column_fails_when_its_ranges_are_cut():
  """The count request names one column, so that the client asks the
  API and nothing else for the count."""
  plan = table_plan("events", [_ID, _NOTE], pk=["id"])
  bq_api.install(FakeApi({plan.synthetic_read_table: ApiTable([], [])}))
  with pytest.raises(Exception, match="has no column"):
    _read(plan, Side.SYNTHETIC)


def test_a_count_of_no_row_that_comes_with_a_row_fails():
  plan, api = _narrow(4)
  next(iter(api.tables.values())).num_rows = 0
  api.total_rows = 0  # the list agrees with the metadata, and returns a row
  with pytest.raises(Exception, match="returned a row"):
    _read(plan, Side.SYNTHETIC)


def _last_check(plan: TablePlan) -> Any:
  """The DoFn that runs after a table's last range, set up over the
  installed fake API."""
  check = eval_io._UnchangedFn(  # pylint: disable=protected-access  # the DoFn itself: no pipeline reaches its two failures
      plan.synthetic_read_table, PROJECT, bq_api.make_client)
  check.setup()
  return check


def test_the_last_check_fails_a_table_that_changed_after_its_last_range():
  plan, api = _narrow(4)
  table = next(iter(api.tables.values()))
  cut = (4, bq_api.MODIFIED_MS)
  check = _last_check(plan)
  check.process(4, cut)  # the table of the cut: nothing is raised
  table.modified_ms += 60_000
  with pytest.raises(TableChangedError, match="after its last range") as caught:
    check.process(4, cut)
  assert plan.synthetic_read_table in str(caught.value)
  assert str(bq_api.MODIFIED_MS + 60_000) in str(caught.value)
  table.modified_ms = bq_api.MODIFIED_MS
  table.rows.append(table.rows[0])
  with pytest.raises(TableChangedError, match="after its last range") as caught:
    check.process(4, cut)
  assert "4 rows" in str(caught.value) and "5 rows" in str(caught.value)
  check.teardown()


def test_the_last_check_fails_when_the_ranges_returned_too_few_rows():
  plan, _ = _narrow(4)
  check = _last_check(plan)
  with pytest.raises(TableChangedError) as caught:
    check.process(3, (4, bq_api.MODIFIED_MS))
  message = str(caught.value)
  assert plan.synthetic_read_table in message
  assert "its row ranges returned 3 rows" in message
  assert "4 rows when they were cut" in message
  check.teardown()


class _OneRowShort:
  """A page that yields one row fewer than it says it holds."""

  def __init__(self, page: Any):
    self._page = page
    self.num_items = page.num_items

  def __iter__(self) -> Iterator[Any]:
    return itertools.islice(iter(self._page), self.num_items - 1)


def test_a_range_reports_the_rows_it_emitted_not_the_rows_it_was_asked_for(
    monkeypatch):
  """The last check adds up what the ranges emitted. A range that
  reported its own size instead would make that sum true by
  construction; here a page loses a row on the way out and only the sum
  can tell."""
  plan, _ = _narrow(4)
  real_page = eval_io._ReadRangeFn._page  # pylint: disable=protected-access  # the one request of a page, wrapped to lose a row

  def page(self, position, left, span):
    found, total = real_page(self, position, left, span)
    return _OneRowShort(found), total

  monkeypatch.setattr(eval_io._ReadRangeFn, "_page", page)  # pylint: disable=protected-access  # as above
  with pytest.raises(Exception, match="its row ranges returned 3 rows"):
    _read(plan, Side.SYNTHETIC)


def test_a_page_with_no_row_inside_a_range_fails_instead_of_looping():
  plan, _ = _narrow(4, page_rows=0)
  with pytest.raises(Exception, match="returned no row"):
    _read(plan, Side.SYNTHETIC)


# ---------------------------------------------------------------------------
# throttling and refusals
# ---------------------------------------------------------------------------

_THROTTLED = (
    api_error(
        403, "rateLimitExceeded", "Exceeded rate limits: too many "
        "tabledata.list bytes per second per project"),
    api_error(403, "quotaExceeded", "Quota exceeded: tabledata.list bytes "
              "per minute"),
    api_error(500, "backendError", "Backend error"),
    api_error(503, "backendError", "Service unavailable"),
    api_exceptions.TooManyRequests("slow down"),
    api_exceptions.ServiceUnavailable("try again"),
    ConnectionError("connection reset"),
)
_REFUSED = (
    api_error(
        403, "accessDenied", "Access Denied: Table demo: User does not "
        "have bigquery.tables.getData permission"),
    api_exceptions.Forbidden("no reason given"),
    api_error(404, "notFound", "Not found: Table demo"),
    api_error(400, "invalid", "Invalid field name"),
    ValueError("not an API error"),
)


def test_only_rate_quota_and_transient_errors_wait():
  for exc in _THROTTLED:
    assert page_retry_waits(exc), exc
  for exc in _REFUSED:
    assert not page_retry_waits(exc), exc


def test_everything_the_clients_default_retry_waits_for_waits_here_too():
  predicate = bigquery.DEFAULT_RETRY._predicate  # pylint: disable=protected-access  # the client's own rule, read to compare with ours
  for exc in (*_THROTTLED, *_REFUSED):
    if predicate(exc):
      assert page_retry_waits(exc), exc


def test_a_throttled_page_waits_and_is_asked_for_again():
  plan, api = _narrow(6, page_rows=3)
  api.page_failures = [_THROTTLED[0], _THROTTLED[1], _THROTTLED[3]]
  rows = _read(plan, Side.SYNTHETIC)
  assert sorted(row["id"] for row in rows) == list(range(6))
  # the first page was asked for four times, the second once
  assert [p["startIndex"] for p in api.pages()] == [0, 0, 0, 0, 3]


def test_the_request_that_counts_a_table_waits_like_a_page():
  plan, api = _narrow(6)
  api.failures = [_THROTTLED[1], _THROTTLED[0]]  # the first requests made
  rows = _read(plan, Side.SYNTHETIC)
  assert sorted(row["id"] for row in rows) == list(range(6))
  assert len(api.counts()) == 3 and len(api.pages()) == 1


def test_a_missing_permission_fails_at_once():
  plan, api = _narrow(6)
  api.failures = [_REFUSED[0]]
  with pytest.raises(Exception, match=r"bigquery\.tables\.getData"):
    _read(plan, Side.SYNTHETIC)
  assert len(api.lists()) == 1


def test_a_range_that_gives_up_says_on_which_reason(monkeypatch):
  monkeypatch.setattr(eval_io, "PAGE_RETRY_SECONDS", 0.05)
  plan, api = _narrow(6)
  api.page_failures = [_THROTTLED[1]] * 1_000
  with pytest.raises(Exception) as caught:
    _read(plan, Side.SYNTHETIC)
  message = str(caught.value)
  assert "quotaExceeded" in message
  assert plan.synthetic_read_table in message
  assert "rows [0, 6)" in message
  assert len(api.pages()) > 1


# ---------------------------------------------------------------------------
# types: the paged rows are the panel's rows
# ---------------------------------------------------------------------------


def _panel_rows(api: FakeApi) -> list[dict[str, Any]]:
  """The all-types rows as the PANEL reads them: `Bq.query` over the
  same real client, the rows travelling as `jobs.getQueryResults` sends
  them."""
  table = _all_types_table()
  api.query_result = (table.fields, table.rows)
  return Bq(PROJECT, client=bq_api.client_over(api)).query("SELECT 1")


def test_both_read_paths_ask_the_api_for_the_same_timestamp_format():
  plan, api = _all_types_api()
  _panel_rows(api)
  _read(plan, Side.SOURCE)
  results = [
      r["query_params"]
      for r in api.requests
      if "/queries/" in r["path"] and (r["query_params"].get("maxResults") != 0)
  ]
  assert results and all(
      p["formatOptions.useInt64Timestamp"] is True for p in results)
  assert all(p["formatOptions.useInt64Timestamp"] is True for p in api.lists())


@pytest.mark.parametrize("bq_type", [
    "INT64", "FLOAT64", "BOOL", "STRING", "NUMERIC", "BIGNUMERIC", "BYTES",
    "TIMESTAMP", "DATETIME", "DATE", "TIME", "JSON", "GEOGRAPHY", "RECORD",
    "ARRAY"
])
def test_a_paged_cell_is_the_panels_cell(bq_type):
  """Per BigQuery type: the cell `tabledata.list` sends, decoded by the
  client inside the paged read, has the panel's Python type, the
  panel's canonical value and the panel's digest. Nothing normalises
  it in between."""
  plan, api = _all_types_api()
  panel = sorted(_panel_rows(api), key=lambda row: row["id"])
  paged = sorted(_read(plan, Side.SOURCE), key=lambda row: row["id"])
  schema = full_schema(all_types()["schema"], all_types()["rows"])
  if bq_type == "ARRAY":
    names = [f["name"] for f in schema if f.get("mode") == "REPEATED"]
  else:
    names = [
        f["name"]
        for f in schema
        if f["type"] == bq_type and f.get("mode") != "REPEATED"
    ]
  assert names, f"the all-types fixture has no {bq_type} column"
  for got, want in zip(paged, panel, strict=True):
    for name in names:
      assert type(got[name]) is type(want[name]), name
      assert canonical_json(got, [name]) == canonical_json(want, [name])
      assert row_digest(got, [name]) == row_digest(want, [name])
      if isinstance(want[name], dict):  # a RECORD's sub-fields, typed alike
        assert {
            k: type(v) for k, v in got[name].items()
        } == {
            k: type(v) for k, v in want[name].items()
        }


def test_paged_rows_encode_exactly_like_the_panels_rows():
  plan, api = _all_types_api()
  panel = sorted(_panel_rows(api), key=lambda row: row["id"])
  paged = sorted(_read(plan, Side.SOURCE), key=lambda row: row["id"])
  names = [c.name for c in plan.columns]
  assert [row_digest(r, names) for r in paged
         ] == [row_digest(r, names) for r in panel]
  encoder = BatchEncoder.from_table(plan, Side.SOURCE, salt=SALT)
  a, b = encoder.encode(panel), encoder.encode(paged)
  for name in ("row_hash", "nonkey_hash", "h_nonkey", "cat", "pk_hash",
               "identity_hash", "fk_hash", "null_bits", "subsample_m"):
    np.testing.assert_array_equal(getattr(a, name), getattr(b, name), name)
  np.testing.assert_array_equal(a.num, b.num)
  assert a.text == b.text
  # and both are the rows every other test of the package calls "the
  # client's" (`tables.client_rows`)
  assert [_comparable(r) for r in paged
         ] == [_comparable(r) for r in client_rows()]


# ---------------------------------------------------------------------------
# the whole evaluation, paged
# ---------------------------------------------------------------------------


def _three_tables(
) -> tuple[list[TablePlan], dict[tuple[str, str], list[dict]]]:
  """The acceptance generator's users, orders and order_items (120 rows
  a side) as planned launch tables, and their rows by (table, side)."""
  source = launch_rows(seed=5, id_base=100_000)
  good = launch_rows(seed=6, id_base=500_000)
  rows_by = {
      (name, side): rows[:120]
      for side, by_table in (("source", source), ("synthetic", good))
      for name, rows in by_table.items()
  }
  shapes = (
      ("users", USERS_FIELDS, {
          "pk": ("id",),
          "identity": ("email",),
          "role": "root"
      }),
      ("orders", ORDERS_FIELDS, {
          "pk": ("order_id",),
          "edges": (USER_EDGE,),
          "role": "driven"
      }),
      ("order_items", ITEMS_FIELDS, {
          "pk": ("id",),
          "edges": (ORDER_EDGE,),
          "role": "driven"
      }),
  )
  tables = [
      acceptance_table(name, fields, rows_by[(name, "source")],
                       rows_by[(name, "synthetic")], **kwargs)
      for name, fields, kwargs in shapes
  ]
  return tables, rows_by


def _evaluate(plan: Any, sources: Any, out: Any) -> LocalJsonSinks:
  sinks = LocalJsonSinks(str(out))
  options = PipelineOptions([],
                            **pipeline_options_defaults("DirectRunner", plan))
  with beam.Pipeline(options=options) as p:
    build_evaluation_pipeline(p, plan, sources=sources, sinks=sinks)
  return sinks


def test_an_evaluation_read_by_pages_is_the_evaluation_read_in_memory(tmp_path):
  """Three related tables through the whole pipeline, once from rows
  handed over in memory and once paged from the API (short pages,
  several ranges, the parents read again by the relational pass): the
  metric, profile and flag rows are byte-identical."""
  key = tmp_path / "label.key"  # an ephemeral key would differ per run
  key.write_bytes(b"paged-read-test-label-key-0123456789")
  tables, rows_by = _three_tables()
  plan = evaluation_plan(
      tables, evaluation_id="ev_paged", label_key_uri=str(key))
  fields = {
      "users": USERS_FIELDS,
      "orders": ORDERS_FIELDS,
      "order_items": ITEMS_FIELDS
  }
  api = bq_api.install(
      FakeApi(
          {
              read_table_of(table, side):
                  ApiTable([dict(f) for f in fields[table.name]],
                           typed_rows(fields[table.name],
                                      rows_by[(table.name, side)]))
              for table in tables for side in ("source", "synthetic")
          },
          page_rows=50))
  in_memory = _evaluate(plan, InMemorySources(rows_by), tmp_path / "memory")
  paged = _evaluate(plan, _sources(), tmp_path / "paged")
  assert len(api.lists()) >= 6 * 3  # six sides, 120 rows, pages of 50
  for table in ("evaluation_metrics", "evaluation_profiles",
                "evaluation_row_flags"):
    rows = in_memory.read_rows(table)
    assert rows, table
    assert sorted(json.dumps(r, sort_keys=True) for r in rows) == sorted(
        json.dumps(r, sort_keys=True) for r in paged.read_rows(table)), table
  finals = [
      sinks.read_rows("evaluation_data_history") for sinks in (in_memory, paged)
  ]
  assert [len(rows) for rows in finals] == [1, 1]
  assert finals[0][0]["status"] == finals[1][0]["status"]
