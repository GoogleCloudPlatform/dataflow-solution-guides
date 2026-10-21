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
"""Which landing rows one generation job wrote (`resolve_scope`), the
source as it was when the job started (`pin_source`), and the salted
hash sample (`sampled_read`) — all pure SQL planning, no BigQuery.

One invented timeline (thelook `orders`, `demo-project`) throughout:

    13:10:16.5 job created ─ 13:49:21 ┬ write #1 ┬ 13:49:30.5
                                      13:49:31 ┴ write #2 ┴ 13:49:40.25
    scope window = [13:49:20, 13:49:41.25]   (first start - 1 s, last end + 1 s)
    now = 2026-09-14 12:00; the time-travel floor is now - 168 h + 1 h margin

The writes are LOAD jobs (an APPENDS window) unless a test swaps in the
COPY jobs Beam's multi-partition FILE_LOADS path commits (as_of_diff).
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from sdfb_evaluation.context import scope as scope_module
from sdfb_evaluation.context.bq import BqApiError
from sdfb_evaluation.context.jobs import JobWrite
from sdfb_evaluation.context.scope import (
    ScopePlan,
    from_item,
    pin_source,
    resolve_scope,
    sampled_read,
)

from .bq_rules import check_one_point_in_time_per_table

DS = "demo-project.thelook_synthetic"
ORDERS = f"{DS}.orders"
SOURCE = "demo-project.thelook_ecommerce.orders"
TEMP = "demo-project.sdfb_eval_tmp"
EVAL_ID = "ev_20260914_0001"
NOW = "2026-09-14T12:00:00.000000Z"
EXPIRY = ("OPTIONS(expiration_timestamp="
          "TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR))")
WINDOW = ("2026-09-13T13:49:20.000000Z", "2026-09-13T13:49:41.250000Z")
WINDOW_DT = (datetime(2026, 9, 13, 13, 49, 20, tzinfo=UTC),
             datetime(2026, 9, 13, 13, 49, 41, 250000, tzinfo=UTC))
START_LIT = "TIMESTAMP '2026-09-13T13:49:20.000000Z'"
END_LIT = "TIMESTAMP '2026-09-13T13:49:41.250000Z'"


def _write(start: str,
           end: str,
           *,
           table: str = ORDERS,
           rows: int | None = 9000,
           job_id: str = "beam_bq_job_LOAD_sdfbthelook0913_0",
           job_type: str = "LOAD") -> JobWrite:
  return JobWrite(
      table=table,
      job_type=job_type,
      start=start,
      end=end,
      output_rows=rows,
      job_id=job_id)


WRITES = (
    _write(
        "2026-09-13T13:49:21.000000Z",
        "2026-09-13T13:49:30.500000Z",
        rows=9000,
        job_id="beam_bq_job_LOAD_sdfbthelook0913_0"),
    _write(
        "2026-09-13T13:49:31.000000Z",
        "2026-09-13T13:49:40.250000Z",
        rows=9250,
        job_id="beam_bq_job_LOAD_sdfbthelook0913_1"),
)
COPY_WRITES = (
    WRITES[0],
    _write(
        "2026-09-13T13:49:31.000000Z",
        "2026-09-13T13:49:40.250000Z",
        rows=9250,
        job_id="beam_bq_job_COPY_sdfbthelook0913_1",
        job_type="COPY"),
)
EARLIER_LAUNCH = _write(
    "2026-09-12T09:00:00.000000Z",
    "2026-09-12T09:00:12.000000Z",
    job_id="beam_bq_job_LOAD_sdfbthelook0912_0",
    rows=None)
LATER_DML = _write(
    "2026-09-14T08:00:00.000000Z",
    "2026-09-14T08:00:05.000000Z",
    job_id="bquxjob_5e1f0a2b",
    job_type="QUERY",
    rows=None)
OVERLAPPING_LOAD = _write(
    "2026-09-13T13:49:35.000000Z",
    "2026-09-13T13:49:37.000000Z",
    job_id="manual_load_77",
    rows=None)


def _temp_re(side: str, short: str) -> str:
  return rf"{re.escape(TEMP)}\.sdfb_eval_{EVAL_ID}_{side}_{short}_[0-9a-f]{{8}}"


def _resolve(**overrides) -> ScopePlan:
  kwargs = {
      "landing_table": ORDERS,
      "write_disposition": "append",
      "writes": WRITES,
      "foreign": (),
      "expected_rows": 18250,
      "now": NOW,
      "time_travel_hours": 168,
      "temp_dataset": TEMP,
      "evaluation_id": EVAL_ID,
  }
  kwargs.update(overrides)
  return resolve_scope(**kwargs)


_NUMBERED = ("TO_JSON_STRING(t) AS __sdfb_json, ROW_NUMBER() OVER "
             "(PARTITION BY TO_JSON_STRING(t)) AS __sdfb_rn")


def _diff_select(start: str, table: str = ORDERS) -> str:
  """as_of_diff's SELECT: the table AS OF the window end minus its start
  snapshot (another table, read now), as multisets of TO_JSON_STRING
  rows."""
  return (f"SELECT e.* EXCEPT(__sdfb_json, __sdfb_rn) FROM (SELECT t.*, "
          f"{_NUMBERED} FROM (SELECT * FROM `{table}` FOR SYSTEM_TIME AS OF "
          f"{END_LIT}) AS t) AS e LEFT JOIN (SELECT {_NUMBERED} FROM "
          f"`{start}` AS t) AS s ON e.__sdfb_json = s.__sdfb_json AND "
          "e.__sdfb_rn = s.__sdfb_rn WHERE s.__sdfb_rn IS NULL")


def _r51_select(table: str = ORDERS) -> str:
  """Round 1's shape — the same table at two points in one statement,
  which BigQuery rejects."""
  return (f"SELECT e.* EXCEPT(__sdfb_json, __sdfb_rn) FROM (SELECT t.*, "
          f"{_NUMBERED} FROM (SELECT * FROM `{table}` FOR SYSTEM_TIME AS OF "
          f"{END_LIT}) AS t) AS e LEFT JOIN (SELECT {_NUMBERED} FROM "
          f"(SELECT * FROM `{table}` FOR SYSTEM_TIME AS OF {START_LIT}) AS t) "
          "AS s ON e.__sdfb_json = s.__sdfb_json AND e.__sdfb_rn = "
          "s.__sdfb_rn WHERE s.__sdfb_rn IS NULL")


# ---------------------------------------------------------------------------
# overwrite
# ---------------------------------------------------------------------------


def test_overwrite_uses_whole_table():
  # An earlier launch's append is irrelevant: WRITE_TRUNCATE replaced it.
  plan = _resolve(write_disposition="overwrite", foreign=(EARLIER_LAUNCH,))
  assert plan.mode == "table"
  assert plan.status == "ok" and plan.ok and plan.readable
  assert plan.reason is None
  assert plan.read_table == ORDERS
  assert plan.read_expr == f"`{ORDERS}`"
  assert plan.prepare_sql == ()
  assert plan.params == {}
  assert plan.window == WINDOW
  assert plan.expected_rows == 18250
  assert plan.written_rows == 18250  # Σ the job's own output_rows
  assert plan.planning_sql == () and plan.start_table == ""


def test_overwrite_with_later_write_snapshots_as_of():
  plan = _resolve(write_disposition="overwrite", foreign=(LATER_DML,))
  assert plan.mode == "as_of"
  assert plan.status == "ok"
  assert re.fullmatch(_temp_re("syn", "orders"), plan.read_table)
  assert plan.prepare_sql == (
      f"CREATE SNAPSHOT TABLE `{plan.read_table}` CLONE `{ORDERS}` "
      f"FOR SYSTEM_TIME AS OF {END_LIT} {EXPIRY}",)
  assert plan.read_expr == (f"(SELECT * FROM `{ORDERS}` "
                            f"FOR SYSTEM_TIME AS OF {END_LIT})")
  assert plan.params == {}
  assert plan.window == WINDOW


def test_overwrite_as_of_point_expires_one_hour_before_time_travel_does():
  # 1 h safety margin: the as-of point (13:49:41.25) is refused from the
  # moment it is 167 h old, not 168 h.
  later = (LATER_DML,)
  edge = WINDOW_DT[1] + timedelta(hours=167)
  assert _resolve(write_disposition="overwrite", foreign=later, now=edge).ok
  plan = _resolve(
      write_disposition="overwrite",
      foreign=later,
      now=edge + timedelta(microseconds=1))
  assert plan.mode == "as_of"
  assert plan.status == "expired" and not plan.ok
  assert not plan.readable and plan.read_table == "" and plan.read_expr == ""
  assert plan.prepare_sql == ()
  assert "168 h" in plan.reason and "1 h" in plan.reason
  assert plan.window == WINDOW


def test_overwrite_table_contaminated_by_an_interleaved_writer():
  plan = _resolve(write_disposition="overwrite", foreign=(OVERLAPPING_LOAD,))
  assert plan.mode == "table"
  assert plan.status == "contaminated" and not plan.readable
  assert "manual_load_77" in plan.reason


# ---------------------------------------------------------------------------
# append: APPENDS for LOAD/DML-only windows
# ---------------------------------------------------------------------------


def _suffix(plan: ScopePlan) -> str:
  (start,) = [k for k in plan.params if k.startswith("start_")]
  return start[len("start_"):]


def test_append_within_window_uses_appends_ctas():
  plan = _resolve(foreign=(EARLIER_LAUNCH, LATER_DML))
  assert plan.mode == "appends"
  assert plan.status == "ok" and plan.reason is None
  assert re.fullmatch(_temp_re("syn", "orders"), plan.read_table)
  sfx = _suffix(plan)
  assert re.fullmatch(r"orders_[0-9a-f]{8}", sfx)
  select = ("SELECT * EXCEPT(_CHANGE_TYPE, _CHANGE_TIMESTAMP) FROM APPENDS("
            f"TABLE `{ORDERS}`, @start_{sfx}, @end_{sfx})")
  assert plan.prepare_sql == (
      f"CREATE TABLE `{plan.read_table}` {EXPIRY} AS {select}",)
  assert plan.read_expr == f"({select})"
  # Bound, never formatted — and namespaced per table, so two tables'
  # read_exprs can share one statement.
  assert plan.params == {
      f"start_{sfx}": WINDOW_DT[0],
      f"end_{sfx}": WINDOW_DT[1]
  }
  assert "2026-09-13" not in plan.prepare_sql[0] + plan.read_expr
  assert plan.window == WINDOW
  users = _resolve(
      landing_table=f"{DS}.users",
      writes=tuple(
          JobWrite(f"{DS}.users", w.job_type, w.start, w.end, w.output_rows,
                   w.job_id) for w in WRITES))
  assert not set(users.params) & set(plan.params)


def test_append_beyond_window_is_expired():
  # The floor is now - 167 h: the window start (13:49:20) is one second
  # older than it.
  plan = _resolve(now="2026-09-20T12:49:21.000000Z")
  assert plan.mode == "appends"
  assert plan.status == "expired" and not plan.readable
  assert plan.prepare_sql == () and plan.params == {}
  assert "168 h" in plan.reason and "2026-09-13T13:49:21" in plan.reason
  assert _resolve(now="2026-09-20T12:49:20.000000Z").ok


def test_append_expiry_uses_the_tables_own_time_travel_window():
  plan = _resolve(now="2026-09-15T13:00:00Z", time_travel_hours=48)
  assert plan.status == "expired"
  assert "48 h" in plan.reason
  assert _resolve(now="2026-09-15T12:00:00Z", time_travel_hours=48).ok


def test_a_window_ending_in_the_future_is_unknown():
  # The job may still be writing, or the clocks disagree.
  plan = _resolve(now="2026-09-13T13:49:41.000000Z")
  assert plan.status == "unknown" and not plan.readable
  assert "after now" in plan.reason
  over = _resolve(write_disposition="overwrite", now="2026-09-13T13:49:41Z")
  assert over.status == "unknown" and not over.readable


def test_contaminated_appends_window_rejected():
  plan = _resolve(foreign=(EARLIER_LAUNCH, OVERLAPPING_LOAD, LATER_DML))
  assert plan.mode == "appends"
  assert plan.status == "contaminated" and not plan.ok
  assert not plan.readable and plan.prepare_sql == ()
  assert "manual_load_77" in plan.reason
  assert "allow_contaminated" in plan.reason
  assert "beam_bq_job_LOAD_sdfbthelook0912_0" not in plan.reason
  assert "bquxjob_5e1f0a2b" not in plan.reason


def test_contaminated_appends_window_allowed_keeps_status_and_warns():
  plan = _resolve(foreign=(OVERLAPPING_LOAD,), allow_contaminated=True)
  assert plan.status == "contaminated" and not plan.ok
  assert plan.readable
  assert plan.prepare_sql[0].startswith(f"CREATE TABLE `{plan.read_table}`")
  assert "manual_load_77" in plan.reason
  assert "allow_contaminated" in plan.reason


def test_a_writer_that_started_inside_the_window_contaminates_it():
  # Its commit lies somewhere in its own [start, end], which meets the
  # window, so its rows may carry a change time inside it.
  straddling = _write(
      "2026-09-13T13:49:40.000000Z",
      "2026-09-13T13:49:55.000000Z",
      job_id="bquxjob_straddle",
      job_type="QUERY",
      rows=None)
  assert _resolve(foreign=(straddling,)).status == "contaminated"


# ---------------------------------------------------------------------------
# append with COPY jobs: as_of_diff
# ---------------------------------------------------------------------------


def test_append_with_a_copy_job_uses_the_as_of_difference():
  plan = _resolve(writes=COPY_WRITES, foreign=(EARLIER_LAUNCH, LATER_DML))
  assert plan.mode == "as_of_diff"
  assert plan.status == "ok" and plan.reason is None
  assert re.fullmatch(_temp_re("syn", "orders"), plan.read_table)
  # (1) A zero-byte, expiring snapshot of the table AS OF the window
  # start, created by the PLANNER (the read_expr depends on it) ...
  start = plan.start_table
  assert re.fullmatch(_temp_re("start", "orders"), start)
  assert plan.planning_sql == (f"CREATE SNAPSHOT TABLE `{start}` CLONE "
                               f"`{ORDERS}` FOR SYSTEM_TIME AS OF {START_LIT} "
                               f"{EXPIRY}",)
  # (2) ... then the table AS OF the window end minus that snapshot read
  # now: two tables, each at one point in time.
  assert plan.prepare_sql == (
      f"CREATE TABLE `{plan.read_table}` {EXPIRY} AS {_diff_select(start)}",)
  assert plan.read_expr == f"({_diff_select(start)})"
  assert plan.params == {}  # AS OF carries literals, as the clone does
  assert plan.window == WINDOW
  assert plan.written_rows == 18250
  for sql in (*plan.planning_sql, *plan.prepare_sql, plan.read_expr):
    check_one_point_in_time_per_table(sql)


def test_as_of_difference_expires_on_its_start_point():
  plan = _resolve(writes=COPY_WRITES, now="2026-09-20T12:49:21Z")
  assert plan.mode == "as_of_diff" and plan.status == "expired"
  assert "2026-09-13T13:49:20" in plan.reason  # the start point
  assert plan.planning_sql == () and plan.start_table == ""


def test_as_of_difference_is_contaminated_by_an_overlapping_writer():
  plan = _resolve(writes=COPY_WRITES, foreign=(OVERLAPPING_LOAD,))
  assert plan.mode == "as_of_diff" and plan.status == "contaminated"
  assert not plan.readable and "manual_load_77" in plan.reason
  assert plan.planning_sql == ()  # nothing is created for a rejected scope


@pytest.mark.parametrize("requested", ["auto", "as_of_diff"])
def test_a_table_created_inside_the_window_is_read_as_of_its_end(requested):
  # Nothing existed at the window start, so the table as of the window
  # end IS the difference (and AS OF a pre-creation time would fail).
  plan = _resolve(
      writes=COPY_WRITES,
      requested=requested,
      table_created="2026-09-13T13:49:20.500000Z")
  assert plan.mode == "as_of" and plan.ok
  assert plan.prepare_sql[0].startswith("CREATE SNAPSHOT TABLE")
  assert plan.planning_sql == ()
  # Said, not silent.
  assert "2026-09-13T13:49:20.5" in plan.reason and "as_of" in plan.reason
  before = _resolve(writes=COPY_WRITES, table_created="2026-09-01T00:00:00Z")
  assert before.mode == "as_of_diff" and before.reason is None


def test_a_table_recreated_after_the_window_is_unknown():
  plan = _resolve(writes=COPY_WRITES, table_created="2026-09-14T09:00:00Z")
  assert plan.status == "unknown" and not plan.readable
  assert "re)created" in plan.reason


def test_appends_requested_over_copy_jobs_is_kept_with_a_warning():
  plan = _resolve(writes=COPY_WRITES, requested="appends")
  assert plan.mode == "appends" and plan.ok
  assert "COPY" in plan.reason and "beam_bq_job_COPY" in plan.reason


class _TimeTravelTable:
  """One table's commit history plus a BigQuery reduced to as_of_diff's
  statements over it: the start snapshot clone, the difference SELECT and
  its CTAS. Every statement must read each table at ONE point in time
  (`bq_rules`), as BigQuery requires.

  ROW_NUMBER over identical TO_JSON_STRING rows numbers them in an order
  BigQuery does not promise; `rng` shuffles it on every read.
  """

  _SNAPSHOT_RE = re.compile(
      r"CREATE SNAPSHOT TABLE `(?P<snap>[^`]+)` CLONE `[^`]+` FOR "
      r"SYSTEM_TIME AS OF TIMESTAMP '(?P<at>[^']+)' " + re.escape(EXPIRY))
  _CTAS_RE = re.compile(r"CREATE TABLE `(?P<temp>[^`]+)` " + re.escape(EXPIRY) +
                        r" AS (?P<select>.+)")
  _DIFF_RE = re.compile(
      re.escape(_diff_select("START")).replace(
          re.escape(END_LIT),
          r"TIMESTAMP '(?P<end>[^']+)'").replace("START", "(?P<start>[^`]+)"))

  def __init__(self, seed: int):
    self.commits: list[tuple[datetime, list[dict[str, Any]]]] = []
    self.tables: dict[str, list[dict[str, Any]]] = {}
    self.rng = random.Random(seed)

  def commit(self, at: str, rows: list[dict[str, Any]]) -> None:
    self.commits.append((datetime.fromisoformat(at), [dict(r) for r in rows]))

  def as_of(self, moment: datetime) -> list[dict[str, Any]]:
    rows = [r for at, batch in self.commits if at <= moment for r in batch]
    self.rng.shuffle(rows)
    return rows

  def _numbered(self, rows: list[dict[str, Any]]) -> list[tuple[str, int]]:
    rows = list(rows)
    self.rng.shuffle(rows)
    seen: Counter[str] = Counter()
    out = []
    for row in rows:
      text = json.dumps(row, separators=(",", ":"))
      seen[text] += 1
      out.append((text, seen[text]))
    return out

  def execute(self, sql: str) -> None:
    check_one_point_in_time_per_table(sql)
    snapshot = self._SNAPSHOT_RE.fullmatch(sql)
    if snapshot:
      self.tables[snapshot["snap"]] = self.as_of(
          datetime.fromisoformat(snapshot["at"]))
      return
    ctas = self._CTAS_RE.fullmatch(sql)
    assert ctas, sql
    select = ctas["select"]
    self.tables[ctas["temp"]] = self.query(f"({select})")

  def query(self, read_expr: str) -> list[dict[str, Any]]:
    check_one_point_in_time_per_table(read_expr)
    match = self._DIFF_RE.fullmatch(read_expr[1:-1])
    assert match, read_expr
    snapshot = match["start"]
    if snapshot not in self.tables:
      raise LookupError(f"Not found: Table {snapshot}")
    end = self._numbered(self.as_of(datetime.fromisoformat(match["end"])))
    start = set(self._numbered(self.tables[snapshot]))
    return [json.loads(text) for text, rn in end if (text, rn) not in start]


def _order(oid: int, status: str) -> dict[str, Any]:
  return {"order_id": oid, "user_id": 7, "status": status}


@pytest.mark.parametrize("seed", range(5))
def test_as_of_difference_is_the_exact_multiset_of_appended_rows(seed):
  table = _TimeTravelTable(seed)
  a, b, c = _order(1, "Shipped"), _order(2, "Complete"), _order(3, "Returned")
  # Before the window: a twice (duplicates before), and b.
  table.commit("2026-09-12T09:00:12+00:00", [a, a, b])
  # The job: a again (a duplicate after), b re-appended (an identical row
  # already in the table), c twice.
  table.commit("2026-09-13T13:49:30.500000+00:00", [a, c])
  table.commit("2026-09-13T13:49:40.250000+00:00", [b, c])
  # After the window: never part of the difference.
  table.commit("2026-09-14T08:00:05+00:00", [a, c])
  plan = _resolve(writes=COPY_WRITES, expected_rows=4)
  expected = Counter(json.dumps(r) for r in (a, b, c, c))
  # The read_expr needs the planner's start snapshot first.
  with pytest.raises(LookupError):
    table.query(plan.read_expr)
  for sql in plan.planning_sql:
    table.execute(sql)
  assert Counter(json.dumps(r) for r in table.query(plan.read_expr)) == expected
  for sql in plan.prepare_sql:
    table.execute(sql)
  assert Counter(json.dumps(r) for r in table.tables[plan.read_table]) == (
      expected)


def test_the_fakes_reject_one_table_at_two_points_in_time():
  # Round 1's single-statement difference: BigQuery rejects it, and so
  # must every fake that stands in for BigQuery.
  with pytest.raises(BqApiError, match="more than one point in time"):
    _TimeTravelTable(0).query(f"({_r51_select()})")
  with pytest.raises(BqApiError, match="more than one point in time"):
    _TimeTravelTable(0).execute(f"CREATE TABLE `{TEMP}.x` {EXPIRY} AS "
                                f"{_r51_select()}")
  # AS OF against the current time counts as two points too.
  with pytest.raises(BqApiError):
    check_one_point_in_time_per_table(
        f"SELECT * FROM `{ORDERS}` JOIN (SELECT * FROM `{ORDERS}` FOR "
        f"SYSTEM_TIME AS OF {END_LIT}) USING (order_id)")
  # The same point twice is fine.
  check_one_point_in_time_per_table(
      f"SELECT * FROM `{ORDERS}` JOIN `{ORDERS}` USING (order_id)")


# ---------------------------------------------------------------------------
# verify: Σ output_rows always, Σ valid_count when present
# ---------------------------------------------------------------------------


def test_append_scope_count_mismatch_flags_not_ok():
  plan = _resolve()
  assert plan.ok and plan.observed_rows is None
  exact = plan.verify(18250)
  assert exact.status == "ok" and exact.observed_rows == 18250
  assert exact.reason is None
  # 0.5 % of 18 250 is 91.25 rows.
  assert plan.verify(18250 + 91).status == "ok"
  mismatch = plan.verify(18250 + 92)
  assert mismatch.status == "count_mismatch" and not mismatch.ok
  assert mismatch.observed_rows == 18342
  assert "18342" in mismatch.reason and "18250" in mismatch.reason
  # The likely cause under APPENDS, and the remedy.
  assert "copy job" in mismatch.reason and "--scope manual" in mismatch.reason
  assert plan.status == "ok"  # verify returns a new plan


def test_count_mismatch_under_as_of_diff_names_deletes_and_updates():
  checked = _resolve(writes=COPY_WRITES).verify(17000)
  assert checked.status == "count_mismatch"
  assert "delete" in checked.reason and "--scope manual" in checked.reason


def test_count_check_is_exact_below_one_thousand_rows():
  small = (_write(
      "2026-09-13T13:49:21.000000Z", "2026-09-13T13:49:30.500000Z", rows=999),)
  plan = _resolve(writes=small, expected_rows=999)
  assert plan.verify(999).ok
  assert plan.verify(998).status == "count_mismatch"
  assert plan.verify(1000).status == "count_mismatch"


def test_count_check_uses_the_committed_rows_without_valid_count():
  plan = _resolve(expected_rows=None)
  assert plan.verify(18250).ok
  checked = plan.verify(17000)
  assert checked.status == "count_mismatch"
  assert "18250" in checked.reason and "output_rows" in checked.reason


def test_count_check_compares_against_both_counts():
  # validation_runs and the job's commits disagree: either mismatch shows.
  plan = _resolve(expected_rows=18000)
  checked = plan.verify(18250)
  assert checked.status == "count_mismatch"
  assert "18000" in checked.reason and "valid_count" in checked.reason
  assert "output_rows" not in checked.reason


def test_no_count_at_all_is_unknown_never_ok():
  unread = tuple(
      JobWrite(w.table, w.job_type, w.start, w.end, None, w.job_id)
      for w in WRITES)
  plan = _resolve(writes=unread, expected_rows=None)
  assert plan.ok and plan.written_rows is None
  checked = plan.verify(18250)
  assert checked.status == "unknown" and not checked.ok
  assert checked.observed_rows == 18250 and "not verified" in checked.reason


def test_count_mismatch_keeps_a_worse_status():
  plan = _resolve(foreign=(OVERLAPPING_LOAD,), allow_contaminated=True)
  checked = plan.verify(20000)
  assert checked.status == "contaminated"
  assert "20000" in checked.reason and "manual_load_77" in checked.reason


def test_verify_refuses_a_scope_nothing_was_read_from():
  with pytest.raises(ValueError, match="nothing"):
    _resolve(now="2026-09-30T00:00:00Z").verify(0)
  with pytest.raises(ValueError):
    _resolve().verify(-1)
  with pytest.raises(ValueError):
    _resolve().verify(True)


# ---------------------------------------------------------------------------
# manual, requested modes, missing inputs
# ---------------------------------------------------------------------------


def test_manual_reads_current_table():
  plan = _resolve(requested="manual")
  assert plan.mode == "manual" and plan.ok
  assert plan.read_table == ORDERS and plan.prepare_sql == ()
  assert plan.reason is None


def test_manual_warns_when_more_than_one_writer():
  plan = _resolve(requested="manual", foreign=(OVERLAPPING_LOAD, LATER_DML))
  assert plan.mode == "manual" and plan.status == "ok"
  assert plan.read_table == ORDERS
  assert "3 writers" in plan.reason
  assert "manual_load_77" in plan.reason and "bquxjob_5e1f0a2b" in plan.reason


def test_manual_ignores_the_time_travel_window():
  plan = _resolve(requested="manual", now="2026-12-31T00:00:00Z")
  assert plan.ok and plan.read_table == ORDERS


def test_requested_mode_overrides_the_write_disposition():
  plan = _resolve(requested="appends", write_disposition="overwrite")
  assert plan.mode == "appends" and plan.ok
  assert plan.prepare_sql[0].startswith("CREATE TABLE ")
  diff = _resolve(requested="as_of_diff", write_disposition="overwrite")
  assert diff.mode == "as_of_diff" and diff.ok
  table = _resolve(requested="table", foreign=(LATER_DML,))
  assert table.mode == "table" and table.status == "contaminated"
  assert "bquxjob_5e1f0a2b" in table.reason


def test_unknown_requested_mode_is_rejected():
  with pytest.raises(ValueError, match="snapshot"):
    _resolve(requested="snapshot")


def test_an_unknown_mode_never_falls_through_to_appends():
  scope = scope_module._Scope(  # pylint: disable=protected-access  # the internal mode dispatch has no public door
      table=ORDERS,
      temp=f"{TEMP}.t",
      start=f"{TEMP}.s",
      expected=None,
      written=None,
      window=WINDOW,
      hours=168,
      floor=WINDOW_DT[0])
  with pytest.raises(ValueError, match="bogus"):
    scope.reads("bogus", WINDOW_DT)


def test_unknown_write_disposition_is_unknown_not_guessed():
  plan = _resolve(write_disposition=None)
  assert plan.status == "unknown" and not plan.readable
  assert "--scope" in plan.reason
  with pytest.raises(ValueError, match="truncate"):
    _resolve(write_disposition="truncate")


def test_no_labelled_write_on_an_overwrite_launch_reads_the_table_as_unknown():
  plan = _resolve(write_disposition="overwrite", writes=())
  assert plan.mode == "table" and plan.status == "unknown"
  assert plan.readable and plan.read_table == ORDERS
  assert plan.window == (None, None) and plan.written_rows is None
  assert "no labelled" in plan.reason


def test_no_labelled_write_on_an_append_launch_cannot_be_isolated():
  plan = _resolve(writes=())
  assert plan.mode == "appends" and plan.status == "unknown"
  assert not plan.readable and plan.prepare_sql == ()
  assert "--scope manual" in plan.reason


def test_empty_scope_when_the_job_wrote_no_rows():
  empty = (_write(
      "2026-09-13T13:49:21.000000Z", "2026-09-13T13:49:22.000000Z", rows=0),)
  plan = _resolve(writes=empty, expected_rows=0)
  assert plan.status == "empty" and not plan.readable
  assert _resolve(writes=empty, expected_rows=None).status == "empty"
  # Zero expected but rows committed: not empty, the count check decides.
  assert _resolve(expected_rows=0).status == "ok"


def test_writes_to_other_tables_do_not_widen_the_window():
  users = _write(
      "2026-09-13T13:40:55.100000Z",
      "2026-09-13T13:41:10.200000Z",
      table=f"{DS}.users",
      job_id="beam_bq_job_LOAD_sdfbthelook0913_9")
  plan = _resolve(writes=(users, *WRITES))
  assert plan.window == WINDOW and plan.written_rows == 18250
  # A foreign write into ANOTHER table inside the window is not ours.
  other = _write(
      "2026-09-13T13:49:35.000000Z",
      "2026-09-13T13:49:37.000000Z",
      table=f"{DS}.users",
      job_id="manual_load_78")
  assert _resolve(foreign=(other,)).ok


def test_landing_table_accepts_the_colon_form():
  plan = _resolve(landing_table="demo-project:thelook_synthetic.orders")
  assert plan.landing_table == ORDERS and plan.window == WINDOW


@pytest.mark.parametrize("bad", [{
    "evaluation_id": "ev 1; DROP"
}, {
    "evaluation_id": ""
}, {
    "temp_dataset": "not a dataset"
}, {
    "temp_dataset": "demo-project.tmp.extra"
}, {
    "landing_table": "orders"
}, {
    "time_travel_hours": 0
}, {
    "expected_rows": -1
}, {
    "table_created": "yesterday"
}])
def test_inputs_that_would_reach_sql_are_validated(bad):
  with pytest.raises(ValueError):
    _resolve(write_disposition="overwrite", foreign=(LATER_DML,), **bad)


def test_temp_tables_differ_per_table_even_with_the_same_short_name():
  other = "demo-project.thelook_synthetic_b.orders"
  writes = tuple(
      JobWrite(other, w.job_type, w.start, w.end, w.output_rows, w.job_id)
      for w in WRITES)
  first = _resolve()
  second = _resolve(landing_table=other, writes=writes)
  assert first.read_table != second.read_table
  assert set(first.params).isdisjoint(second.params)
  assert _resolve().read_table == first.read_table  # deterministic


# ---------------------------------------------------------------------------
# pin_source
# ---------------------------------------------------------------------------


def _pin(**overrides):
  kwargs = {
      "source_table": SOURCE,
      "job_create_time": "2026-09-13T13:10:16.512345Z",
      "now": NOW,
      "time_travel_hours": 168,
      "temp_dataset": TEMP,
      "evaluation_id": EVAL_ID,
  }
  kwargs.update(overrides)
  return pin_source(**kwargs)


def test_source_pinned_as_of_job_create_time():
  pin = _pin()
  read_table, prepare_sql, pinned = pin[:3]
  assert pinned is True and pin.pinned
  assert re.fullmatch(_temp_re("src", "orders"), read_table)
  as_of = "TIMESTAMP '2026-09-13T13:10:16.512345Z'"
  assert prepare_sql == (f"CREATE SNAPSHOT TABLE `{read_table}` CLONE "
                         f"`{SOURCE}` FOR SYSTEM_TIME AS OF {as_of} {EXPIRY}",)
  assert pin.read_expr == (f"(SELECT * FROM `{SOURCE}` "
                           f"FOR SYSTEM_TIME AS OF {as_of})")
  assert pin.as_of == "2026-09-13T13:10:16.512345Z"
  assert pin.reason is None
  assert not pin.params and isinstance(pin.params, Mapping)


def test_source_pin_renders_its_own_utc_literal():
  # A datetime with an offset is re-rendered in UTC; text that is not a
  # timestamp never reaches the SQL.
  pin = _pin(
      job_create_time=datetime(
          2026, 9, 13, 15, 10, 16, 512345, tzinfo=timezone(timedelta(hours=2))))
  assert "TIMESTAMP '2026-09-13T13:10:16.512345Z'" in pin.prepare_sql[0]
  with pytest.raises(ValueError):
    _pin(job_create_time="2026-09-13T13:10:16Z' OR TRUE --")


def test_source_pin_keeps_the_one_hour_margin():
  created = datetime(2026, 9, 13, 13, 10, 16, 512345, tzinfo=UTC)
  assert _pin(now=created + timedelta(hours=167)).pinned
  pin = _pin(now=created + timedelta(hours=167, microseconds=1))
  assert pin[:3] == (SOURCE, (), False)
  assert pin.read_expr == f"`{SOURCE}`"
  assert pin.as_of is None
  assert "168 h" in pin.reason and "1 h" in pin.reason


def test_source_with_unknown_create_time_is_unpinned():
  pin = _pin(job_create_time=None)
  assert pin[:3] == (SOURCE, (), False)
  assert "create time" in pin.reason


def test_source_create_time_after_now_is_rejected():
  with pytest.raises(ValueError, match="after"):
    _pin(job_create_time="2026-09-15T00:00:00Z")
  with pytest.raises(ValueError, match="after"):
    _pin(job_create_time="2026-09-14T12:05:00.000001Z")


def test_source_create_time_within_five_minutes_of_now_is_clock_skew():
  pin = _pin(job_create_time="2026-09-14T12:04:59Z")
  assert pin.pinned and pin.as_of == NOW
  assert "TIMESTAMP '2026-09-14T12:00:00.000000Z'" in pin.prepare_sql[0]
  assert "skew" in pin.reason


# ---------------------------------------------------------------------------
# from_item: structured pieces, never re-parsed text
# ---------------------------------------------------------------------------


def test_from_item_takes_every_read_expr_this_module_builds():
  pinned, unpinned = _pin(), _pin(job_create_time=None)
  assert from_item(pinned) == pinned.read_expr
  assert from_item(unpinned) == f"`{SOURCE}`"
  for plan in (_resolve(), _resolve(writes=COPY_WRITES),
               _resolve(write_disposition="overwrite", foreign=(LATER_DML,)),
               _resolve(write_disposition="overwrite")):
    assert from_item(plan) == plan.read_expr
  assert from_item(ORDERS) == from_item(f"`{ORDERS}`") == f"`{ORDERS}`"


@pytest.mark.parametrize("bad", [
    "(SELECT 1)",
    f"(SELECT * FROM `{ORDERS}` FOR SYSTEM_TIME AS OF "
    "TIMESTAMP '2026-09-13T13:10:16Z')",
    "orders",
    f"`{ORDERS}`; DROP TABLE x",
])
def test_from_item_rejects_text_that_is_not_a_table_name(bad):
  with pytest.raises(ValueError):
    from_item(bad)
  with pytest.raises(ValueError, match="nothing"):
    from_item(_resolve(now="2026-09-30T00:00:00Z"))  # expired: no read_expr


# ---------------------------------------------------------------------------
# sampled_read
# ---------------------------------------------------------------------------


def test_sampled_read_deterministic():
  kwargs = {
      "keep": 37,
      "modulo": 1000,
      "salt": "c2FsdA",
      "temp_dataset": TEMP,
      "evaluation_id": EVAL_ID,
      "side": "syn",
  }
  sql, temp, params = sampled_read(ORDERS, **kwargs)
  assert (sql, temp, params) == sampled_read(ORDERS, **kwargs)
  assert re.fullmatch(_temp_re("syn", "orders"), temp)
  assert temp != _resolve().read_table  # never the scope's own temp table
  # ABS(MOD(x, m)): the same rows as MOD(ABS(x), m), without ABS's INT64
  # overflow on the one fingerprint with no positive counterpart.
  assert sql == (f"CREATE TABLE `{temp}` {EXPIRY} AS SELECT * FROM "
                 f"`{ORDERS}` AS t WHERE ABS(MOD(FARM_FINGERPRINT("
                 "CONCAT(@salt, TO_JSON_STRING(t))), 1000)) < 37")
  # The salt is bound by construction: it travels in params, never in
  # the text, so the text does not depend on it.
  assert params == {"salt": "c2FsdA"} and "c2FsdA" not in sql
  other_sql, other_temp, other_params = sampled_read(
      ORDERS, **{
          **kwargs, "salt": "other"
      })
  assert (other_sql, other_temp) == (sql, temp)
  assert other_params == {"salt": "other"}


@pytest.mark.parametrize("bad", [{
    "keep": 1001
}, {
    "keep": -1
}, {
    "modulo": 0
}, {
    "keep": True
}, {
    "modulo": 10.0
}, {
    "salt": ""
}, {
    "side": "syn; DROP"
}, {
    "read_table": "orders"
}])
def test_sampled_read_validates_everything_it_formats(bad):
  kwargs = {
      "read_table": ORDERS,
      "keep": 37,
      "modulo": 1000,
      "salt": "c2FsdA",
      "temp_dataset": TEMP,
      "evaluation_id": EVAL_ID,
      "side": "syn",
  }
  kwargs.update(bad)
  read_table = kwargs.pop("read_table")
  with pytest.raises(ValueError):
    sampled_read(read_table, **kwargs)
