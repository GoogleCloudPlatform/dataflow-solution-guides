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
"""Planning scans the tables concurrently (`context.plan.build_plan`,
Ruling R153): the plan is the one a table-after-table planning makes,
whichever scan ends first; a failing scan raises as it would have alone;
the statement count stays exact.

    table order      users ─ orders ─ order_items
    a scan           source statistics → landing statistics → panel
    `_GatedBq`       holds statements back so the scans END in a chosen
                     order: every table in flight at once (`last`: only a
                     table's panel waits for the tables before it) or one
                     table at a time (`first`: its first statement waits)

Nothing here is real data: thelook table names, `demo-project`.
"""

from __future__ import annotations

import dataclasses
import itertools
import re
import signal
import threading
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from beam.bq_api import FakeApi, client_over

from sdfb_evaluation.context import plan as planning
from sdfb_evaluation.context.bq import Bq, BqApiError
from sdfb_evaluation.context.budget import BudgetExceededError
from sdfb_evaluation.context.plan import Knobs, build_plan

from .plan_fakes import (
    DS,
    NOW,
    QDS,
    SCHEMAS,
    SRC,
    TABLES,
    PlanBq,
    thelook_launch,
    thelook_models,
    thelook_rows,
)

NAMES = tuple(table.rsplit(".", 1)[1] for table in TABLES)
KNOBS = Knobs(temp_dataset=QDS)
# A scan the planner never starts is waited for this long, then the test
# fails: a planning that scans one table after another never lets a later
# table end first. Long, so a loaded machine cannot fail a correct planner.
PATIENCE = 20.0
SCAN_THREADS = 8


def _standalone(names: Sequence[str]) -> dict[str, Any]:
  """`PlanBq`'s rows and schemas for tables `names`, each a copy of the
  invented `users` table: a launch of any number of unrelated tables."""
  return {
      "source": {
          name: thelook_rows(1)["users"] for name in names
      },
      "landing": {
          name: thelook_rows(2)["users"] for name in names
      },
      "schemas": {
          name: SCHEMAS["users"] for name in names
      },
  }


class _GatedBq(PlanBq):
  """`PlanBq` whose scans end in the order `finish` (module docstring),
  over the thelook rows or, given `standalone`, over those unrelated
  tables. `finished` is the order the tables' last statements returned
  or raised; `failing` maps a table to the error its panel statement
  raises (after its turn has come)."""

  def __init__(self,
               finish: Sequence[str] = (),
               *,
               gate: str = "last",
               failing: Mapping[str, BaseException] | None = None,
               standalone: Sequence[str] = (),
               **kwargs: Any):
    rows = _standalone(standalone) if standalone else {
        "source": thelook_rows(1),
        "landing": thelook_rows(2)
    }
    super().__init__(**rows, **kwargs)
    self._finish = list(finish)
    self._gate = gate
    self._ended = {name: threading.Event() for name in finish}
    self._failing = dict(failing or {})
    self.finished: list[str] = []

  def _wait_for_turn(self, name: str) -> None:
    for earlier in self._finish[:self._finish.index(name)]:
      if not self._ended[earlier].wait(PATIENCE):
        raise AssertionError(
            f"the scan of {name} waited {PATIENCE} s for the scan of "
            f"{earlier} to end: the scans do not run concurrently")

  def query(self,
            sql: str,
            params: Mapping[str, Any] | None = None,
            *,
            max_bytes: int | None = None) -> list[dict]:
    scanning = "n_rows" in sql or "__sdfb_rk" in sql
    if not scanning or not self._finish:
      return super().query(sql, params, max_bytes=max_bytes)
    name = self.reads(sql).rsplit(".", 1)[1]
    last = "__sdfb_rk" in sql
    if last or self._gate == "first":
      self._wait_for_turn(name)
    try:
      if last and name in self._failing:
        raise self._failing[name]
      return super().query(sql, params, max_bytes=max_bytes)
    finally:
      if last:
        self.finished.append(name)
        self._ended[name].set()


def _launch(bq: PlanBq, **fields: Any):
  """The thelook launch with something for every scan to say: the
  recorded reference digest of `orders` is not its panel's, and
  `users` was validated with one row more than it holds."""
  launch = thelook_launch(bq, **fields)
  changed = {
      f"{DS}.orders": {
          "reference_digest": "0" * 64
      },
      f"{DS}.users": {
          "valid_count": len(bq.rows[f"{DS}.users"]) + 1
      },
  }
  runs = [
      dataclasses.replace(run, **changed.get(run.landing_table, {}))
      for run in launch.runs
  ]
  return dataclasses.replace(launch, runs=tuple(runs))


def _plan(bq: PlanBq, *, mode: str = "exact", knobs: Knobs = KNOBS, **fields):
  return build_plan(
      launch=_launch(bq, **fields),
      models=thelook_models(),
      bq=bq,
      knobs=knobs,
      mode=mode,
      trigger="cli",
      runner="DirectRunner",
      now=NOW)


_SAMPLED = dataclasses.replace(KNOBS, sample_rows=200)
_MODES = {"exact": ("exact", KNOBS), "sampled": ("sampled", _SAMPLED)}


@pytest.fixture(scope="module", name="one_at_a_time")
def fixture_one_at_a_time() -> dict[str, Any]:
  """Per mode, the plan of the scans run strictly one table after
  another in table order: what a sequential planning makes."""
  plans = {}
  for label, (mode, knobs) in _MODES.items():
    bq = _GatedBq(NAMES, gate="first")
    plans[label] = _plan(bq, mode=mode, knobs=knobs)
    assert bq.finished == list(NAMES)
  return plans


def test_the_reference_plan_has_something_from_every_scan(one_at_a_time):
  """The fixture is worth comparing against: notes from two scans, a
  panel per table and, sampled, two sample statements in table order."""
  exact, sampled = one_at_a_time["exact"], one_at_a_time["sampled"]
  assert any("reference digest mismatch" in w for w in exact.warnings)
  assert any("scope count_mismatch" in w for w in exact.warnings)
  assert all(t.panel is not None for t in exact.tables if t.evaluated)
  samples = [
      s.sql for s in sampled.prepare_sql if "FARM_FINGERPRINT(CONCAT" in s.sql
  ]
  read = [
      next(
          name
          for name in sorted(NAMES, key=len, reverse=True)
          if name in sql)
      for sql in samples
  ]
  assert read == ["orders", "orders", "order_items", "order_items"]


@pytest.mark.parametrize("mode", sorted(_MODES))
@pytest.mark.parametrize("gate", ["last", "first"])
@pytest.mark.parametrize(
    "finish", [order for order in itertools.permutations(NAMES)], ids="-".join)
def test_any_completion_order_of_the_scans_gives_the_sequential_plan(
    one_at_a_time, finish, gate, mode):
  wanted = one_at_a_time[mode]
  bq = _GatedBq(finish, gate=gate)
  plan = _plan(bq, mode=mode, knobs=_MODES[mode][1])
  assert bq.finished == list(finish)  # the scans did end in this order
  assert [t.name for t in plan.tables] == [t.name for t in wanted.tables]
  assert plan.warnings == wanted.warnings
  assert [t.warnings for t in plan.tables
         ] == [t.warnings for t in wanted.tables]
  assert plan.prepare_sql == wanted.prepare_sql
  assert plan.bq_bytes_estimate == wanted.bq_bytes_estimate
  assert plan.evaluation_key == wanted.evaluation_key
  assert ([t.encoding_plan_digest for t in plan.tables
          ] == [t.encoding_plan_digest for t in wanted.tables])
  assert plan == wanted  # and everything else


def test_an_ungated_planning_gives_the_same_plan(one_at_a_time):
  """No gate at all (the scans end as the threads happen to run)."""
  bq = _GatedBq()
  assert _plan(bq) == one_at_a_time["exact"]


def test_every_scan_ends_then_the_first_failing_table_in_table_order_raises():
  """`orders` and `order_items` both fail, `order_items` first in time:
  the error raised is the one of `orders`, the earlier table — the very
  object — and only once the scan of `users`, which ends last, is over."""
  errors = {
      "orders": BqApiError("query: backend error", status=500),
      "order_items": PermissionError("query: access denied"),
  }
  bq = _GatedBq(("order_items", "orders", "users"), failing=errors)
  with pytest.raises(BqApiError) as info:
    _plan(bq)
  assert info.value is errors["orders"]
  assert bq.finished == ["order_items", "orders", "users"]
  # users was scanned to its end: both statistics and its panel
  users = [
      s for s, _ in bq.queries if f"{SRC}.users" in s or f"{DS}.users" in s
  ]
  assert sum("n_rows" in s for s in users) == 2
  assert sum("__sdfb_rk" in s for s in users) == 1


def test_a_second_failing_scan_is_logged_and_the_first_still_raises(caplog):
  """Two tables fail: the error raised is the first table's in table
  order, and the other one, whose statements were paid for too, is a
  WARNING naming its table instead of being dropped."""
  errors = {
      "orders": BqApiError("query: backend error", status=500),
      "order_items": PermissionError("query: access denied on order_items"),
  }
  bq = _GatedBq(NAMES, failing=errors)
  with caplog.at_level(
      "INFO",
      logger="sdfb_evaluation.context.plan"), pytest.raises(BqApiError) as info:
    _plan(bq)
  assert info.value is errors["orders"]
  warnings = [r for r in caplog.records if r.levelname == "WARNING"]
  assert len(warnings) == 1
  message = warnings[0].getMessage()
  assert re.match(r"planning \S+ order_items: ", message)
  assert "PermissionError" in message and "access denied" in message
  assert "orders" in message.split("order_items: ",
                                   1)[1]  # whose error is raised


def test_one_failing_scan_logs_no_warning(caplog):
  bq = _GatedBq(NAMES, failing={"orders": LookupError("query: Not found")})
  with caplog.at_level(
      "INFO",
      logger="sdfb_evaluation.context.plan"), pytest.raises(LookupError):
    _plan(bq)
  assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_a_scan_that_fails_alone_raises_its_own_error():
  error = LookupError("query: Not found: Table")
  bq = _GatedBq(NAMES, failing={"order_items": error})
  with pytest.raises(LookupError) as info:
    _plan(bq)
  assert info.value is error


def test_a_budget_error_from_a_scan_still_carries_the_planning_ddl():
  """R58 unchanged: the scans run inside the same guard as before."""
  bq = _GatedBq(NAMES, failing={"orders": BudgetExceededError("over")})
  with pytest.raises(BudgetExceededError) as info:
    _plan(bq)
  assert info.value.planning_ddl == ()  # pylint: disable=no-member  # the planner's wrapping subclass


class _InFlightBq(PlanBq):
  """`PlanBq` that counts the tables whose scan is in flight. A scan
  does not end before `hold` of them are in flight (it waits for that as
  long as `PATIENCE`), and then gives one more table `grace` seconds to
  show up. `peak` is the most that were in flight at once."""

  def __init__(self, names: Sequence[str], *, hold: int, grace: float):
    super().__init__(**_standalone(names))
    self._hold, self._grace = hold, grace
    self._lock = threading.Lock()
    self._open: set[str] = set()
    self._held = threading.Event()
    self._over = threading.Event()
    self.peak = 0

  def query(self,
            sql: str,
            params: Mapping[str, Any] | None = None,
            *,
            max_bytes: int | None = None) -> list[dict]:
    rows = super().query(sql, params, max_bytes=max_bytes)
    if "n_rows" not in sql and "__sdfb_rk" not in sql:
      return rows
    name = self.reads(sql).rsplit(".", 1)[1]
    with self._lock:
      self._open.add(name)
      self.peak = max(self.peak, len(self._open))
      if len(self._open) >= self._hold:
        self._held.set()
      if len(self._open) > self._hold:
        self._over.set()
    if "__sdfb_rk" in sql:
      self._held.wait(PATIENCE)
      self._over.wait(self._grace)
      with self._lock:
        self._open.discard(name)
    return rows


def _standalone_plan(bq: PlanBq, names: Sequence[str]):
  tables = [f"{DS}.{name}" for name in names]
  launch = thelook_launch(
      bq,
      tables=tables,
      run_ids=[f"run-{name}" for name in names],
      relationships_uri=None,
      params={"generate_fk_relationships": "false"})
  return build_plan(
      launch=launch,
      models=(),
      bq=bq,
      knobs=KNOBS,
      mode="exact",
      trigger="cli",
      runner="DirectRunner",
      now=NOW)


def test_no_more_than_eight_tables_are_scanned_at_once():
  """Ten tables: eight scans in flight, never a ninth (once eight are,
  each waits a moment for a ninth before it ends)."""
  names = [f"table_{i:02d}" for i in range(SCAN_THREADS + 2)]
  bq = _InFlightBq(names, hold=SCAN_THREADS, grace=0.3)
  plan = _standalone_plan(bq, names)
  assert bq.peak == SCAN_THREADS
  assert [t.name for t in plan.tables] == names
  assert all(t.evaluated for t in plan.tables)
  # and it is the plan of the ten tables scanned one after another
  one_at_a_time = _GatedBq(names, gate="first", standalone=names)
  reference = _standalone_plan(one_at_a_time, names)
  assert one_at_a_time.finished == names
  assert plan.warnings == reference.warnings
  assert plan == reference


def test_fewer_tables_than_threads_are_all_scanned_at_once():
  names = [f"table_{i:02d}" for i in range(3)]
  bq = _InFlightBq(names, hold=3, grace=0.0)
  _standalone_plan(bq, names)
  assert bq.peak == 3


class _InterruptedBq(PlanBq):
  """`PlanBq` over unrelated tables whose scan statements stay in flight
  until `release`. When `hold` of them are in flight the MAIN thread is
  sent a real SIGINT, as Ctrl-C does. `sent` is every scan statement
  that reached BigQuery: (table, whether it was sent after the
  interrupt)."""

  def __init__(self, names: Sequence[str], *, hold: int):
    super().__init__(**_standalone(names))
    self._hold = hold
    self._lock = threading.Lock()
    self.sent: list[tuple[str, bool]] = []
    self.interrupted = threading.Event()
    self.release = threading.Event()

  def query(self,
            sql: str,
            params: Mapping[str, Any] | None = None,
            *,
            max_bytes: int | None = None) -> list[dict]:
    if "n_rows" not in sql and "__sdfb_rk" not in sql:
      return super().query(sql, params, max_bytes=max_bytes)
    name = self.reads(sql).rsplit(".", 1)[1]
    with self._lock:
      self.sent.append((name, self.interrupted.is_set()))
      fire = len(self.sent) == self._hold and not self.interrupted.is_set()
      if fire:
        self.interrupted.set()
    if fire:
      signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)
    self.release.wait(PATIENCE)
    return super().query(sql, params, max_bytes=max_bytes)


def _scan_threads() -> list[threading.Thread]:
  return [
      t for t in threading.enumerate() if t.name.startswith("sdfb-eval-scan")
  ]


@pytest.mark.skipif(
    not hasattr(signal, "pthread_kill"),
    reason="needs a signal sent to the main thread")
def test_an_interrupt_stops_the_scans_nothing_further_is_sent():
  """Ctrl-C while eight of twelve tables are being scanned: the
  interrupt leaves planning at once, the four tables not yet started are
  never scanned, and a scan in flight sends nothing after the statement
  it had already sent."""
  assert threading.current_thread() is threading.main_thread()
  names = [f"table_{i:02d}" for i in range(SCAN_THREADS + 4)]
  bq = _InterruptedBq(names, hold=SCAN_THREADS)
  handler = signal.signal(signal.SIGINT, signal.default_int_handler)
  try:
    with pytest.raises(KeyboardInterrupt):
      _standalone_plan(bq, names)
  finally:
    signal.signal(signal.SIGINT, handler)
    bq.release.set()  # the statements in flight come back
    for thread in _scan_threads():
      thread.join(PATIENCE)
  assert not _scan_threads()  # every scan thread has ended
  assert bq.interrupted.is_set()
  assert [late for _, late in bq.sent] == [False] * SCAN_THREADS
  assert sorted(name for name, _ in bq.sent) == names[:SCAN_THREADS]


class _SlowCounter(Counter):
  """A `Counter` whose read lets another thread run before the caller
  writes the sum back: an unguarded `counts[kind] += 1` loses updates
  under it every time, not once in a million runs."""

  def __getitem__(self, key: Any) -> int:
    value = super().__getitem__(key)
    time.sleep(0.001)
    return value


def test_the_statement_count_is_exact_when_threads_share_the_client():
  bq = _GatedBq()
  counted = planning._CountedBq(bq)  # pylint: disable=protected-access  # the counter under test
  counted.counts = _SlowCounter()
  threads, each = SCAN_THREADS, 25
  start = threading.Barrier(threads)

  def scan() -> None:
    start.wait(PATIENCE)
    for _ in range(each):
      counted.table(f"{DS}.users")

  workers = [threading.Thread(target=scan) for _ in range(threads)]
  for worker in workers:
    worker.start()
  for worker in workers:
    worker.join(PATIENCE * 4)
  assert counted.counts["metadata reads"] == threads * each
  assert counted.summary().startswith(f"{threads * each} BigQuery statements")


# --------------------------------------------------------------------------
# the one client, shared: the real `google.cloud.bigquery.Client`
# --------------------------------------------------------------------------
class _ByStatementApi(FakeApi):
  """The REST API for a real client, answering a query job by its
  STATEMENT: `SELECT <n> AS n` returns the one row n. Another thread is
  let in between a job and its results."""

  def __init__(self) -> None:
    super().__init__({})
    self._lock = threading.Lock()
    self._statements: dict[str, str] = {}

  def _insert(self, data: Mapping[str, Any]) -> dict[str, Any]:
    with self._lock:
      job = data["jobReference"]["jobId"]
      self._statements[job] = data["configuration"]["query"]["query"]
    return super()._insert(data)

  def _results(self, job: str, params: Mapping[str, Any]) -> dict[str, Any]:
    time.sleep(0.001)
    found = re.fullmatch(r"SELECT (\d+) AS n", self._statements[job])
    assert found, self._statements[job]
    self.query_result = ([{
        "name": "n",
        "type": "INTEGER",
        "mode": "NULLABLE"
    }], [])
    answer = super()._results(job, params)
    answer["totalRows"] = "1"
    answer["rows"] = [{"f": [{"v": found.group(1)}]}]
    return answer


def test_one_real_client_answers_every_thread_its_own_query():
  """`Bq.query` from eight threads over ONE real BigQuery client (its
  HTTP connection is the fake): every query gets the rows of its own
  job. This is the client's own code above the transport; the HTTP
  session of a real connection is not exercised here."""
  bq = Bq("demo-project", client=client_over(_ByStatementApi()))
  each = 20
  got: dict[int, list[dict]] = {}
  errors: list[BaseException] = []
  start = threading.Barrier(SCAN_THREADS)

  def scan(thread: int) -> None:
    try:
      start.wait(PATIENCE)
      for k in range(each):
        n = thread * 1000 + k
        got[n] = bq.query(f"SELECT {n} AS n", max_bytes=1 << 30)
    except BaseException as exc:  # pylint: disable=broad-exception-caught  # reported by the test's own thread below
      errors.append(exc)

  workers = [
      threading.Thread(target=scan, args=(thread,))
      for thread in range(SCAN_THREADS)
  ]
  for worker in workers:
    worker.start()
  for worker in workers:
    worker.join(PATIENCE * 4)
  assert not errors, errors
  assert len(got) == SCAN_THREADS * each
  assert all(rows == [{"n": n}] for n, rows in got.items())
