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
"""The chained evaluation verifies the generator's reference sample (R136).

`--seed_table T --generation_job_id J --validation_runs_table V` reads the
Dataflow job (for its window) and this launch's rows of V (one query), and
nothing else about the job. The digest on the row is what makes the panel
`verified`; these tests follow it down to the two places that decide
whether the reference-based metrics run:
`beam.privacy._panel_reason` (the nearest-neighbour metrics) and
`beam.membership.PanelRefs.from_table` (the panel-based match rates and
lifts). The tables are big enough for a real panel: R and H hold
`MIN_PANEL_ROWS` rows each.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta

import pytest
from unit.context.plan_fakes import BASE, DS, JOB_ID, PROJECT, SRC, PlanBq

from sdfb_evaluation.beam import membership, privacy
from sdfb_evaluation.cli import driver
from sdfb_evaluation.cli.main import parse_args

from .helpers import JobOnlySession, make_env

REGION = "europe-west1"
PANEL = privacy.MIN_PANEL_ROWS  # R and H, rows each
RUNS_TABLE = f"{PROJECT}.synthetic_data_quality.validation_runs"
_COUNTRIES = ("Spain", "France", "Brazil", "Japan", "Kenya")


def _users(seed: int, n: int) -> list[dict]:
  rng = random.Random(seed)
  return [{
      "id":
          i,
      "email":
          f"user{seed}x{i}@example.com",
      "age":
          rng.randint(18, 70),
      "gender":
          rng.choice(("F", "M")),
      "country":
          rng.choice(_COUNTRIES),
      "signup_date":
          date(2025, 1, 1) + timedelta(days=rng.randint(0, 300)),
      "created_at":
          datetime(2026, 1, 1, tzinfo=UTC) +
          timedelta(minutes=rng.randint(0, 90_000)),
  } for i in range(1, n + 1)]


class _RunsBq(PlanBq):
  """`PlanBq` that also answers the one validation_runs query."""

  def __init__(self, runs: list[dict]):
    super().__init__(
        source={"users": _users(1, 2 * PANEL + 100)},
        landing={"users": _users(2, 2 * PANEL + 100)},
        location="EU")
    self.runs = runs

  def query(self, sql, params=None, *, max_bytes=None):
    if "validation_runs" in sql:
      self.queries.append((sql, dict(params or {})))
      return [dict(r) for r in self.runs]
    return super().query(sql, params, max_bytes=max_bytes)


def _run(digest: str,
         base: str = BASE,
         created: str = "2026-09-13T13:41:02.000000Z") -> dict:
  return {
      "run_id": base,
      "landing_table": f"{DS}.users",
      "reference_table": f"{SRC}.users",
      "reference_digest": digest,
      "valid_count": 2 * PANEL + 100,
      "num_rows_requested": 2 * PANEL + 100,
      "status": "PASSED",
      "created_at": created,
  }


def _plan(bq: _RunsBq, load_fixture, *extra: str):
  session = JobOnlySession(load_fixture("context/dataflow_job.json"))
  args, _ = parse_args([
      "plan", "--project", PROJECT, "--region", REGION, "--seed_table", "users",
      "--generation_job_id", JOB_ID, "--landing_dataset", "thelook_synthetic",
      "--reference_dataset", SRC, "--scope", "manual", "--reference_rows_limit",
      str(PANEL), *extra
  ])
  planned, _ = driver.plan(
      args, make_env(bq, session_factory=lambda project: session))
  return planned, session


def _digest(bq: PlanBq) -> str:
  return bq.digest("users", PANEL)


def _decisions(planned):
  """(the panel, privacy's reason, membership's lift and near reasons) of
  the one table, as the pipeline's steps compute them."""
  table = next(t for t in planned.tables if t.name == "users")
  panel = table.panel
  assert panel is not None
  reason = privacy._panel_reason(  # pylint: disable=protected-access  # the decision under test
      table, len(panel.r_rows), len(panel.h_rows))
  refs = membership.PanelRefs.from_table(
      table, membership.MembershipSpec.from_table(table, salt=planned.salt))
  return panel, reason, refs.lift_reason, refs.near_reason


def test_a_matching_digest_verifies_the_panel_down_to_both_decisions(
    load_fixture):
  bq = _RunsBq([])
  bq.runs = [_run(_digest(bq))]
  planned, session = _plan(bq, load_fixture, "--validation_runs_table",
                           RUNS_TABLE)
  # what was read: the Dataflow job and one validation_runs query
  assert [m for m, _ in session.calls] == ["GET"]
  assert len([1 for sql, _ in bq.queries if "validation_runs" in sql]) == 1
  assert not [1 for sql, _ in bq.queries if "JOBS_BY_PROJECT" in sql]
  panel, privacy_reason, lift_reason, near_reason = _decisions(planned)
  assert panel.verified and panel.reason is None
  assert len(panel.r_rows) == len(panel.h_rows) == PANEL
  assert privacy_reason is None  # the five nearest-neighbour metrics run
  assert lift_reason is None  # the panel-based rates and lifts run
  assert near_reason is None  # and the near matches
  assert planned.launch.base_run_id == BASE
  assert not [w for w in planned.warnings if "reference sample" in w]


def test_a_digest_that_does_not_match_leaves_the_panel_unverified(load_fixture):
  bq = _RunsBq([])
  bq.runs = [_run("0" * 64)]
  planned, _ = _plan(bq, load_fixture, "--validation_runs_table", RUNS_TABLE)
  panel, privacy_reason, lift_reason, near_reason = _decisions(planned)
  assert not panel.verified and "digest mismatch" in panel.reason
  assert privacy_reason.startswith(privacy.UNVERIFIED_REASON)
  assert lift_reason.startswith(membership.UNVERIFIED_REASON)
  assert near_reason.startswith(membership.UNVERIFIED_REASON)


def test_without_the_runs_table_the_panel_stays_unverified(load_fixture):
  bq = _RunsBq([])
  bq.runs = [_run(_digest(bq))]
  planned, _ = _plan(bq, load_fixture)
  assert not [1 for sql, _ in bq.queries if "validation_runs" in sql]
  panel, privacy_reason, lift_reason, _ = _decisions(planned)
  assert not panel.verified and "no reference digest recorded" in panel.reason
  assert privacy_reason.startswith(privacy.UNVERIFIED_REASON)
  assert lift_reason.startswith(membership.UNVERIFIED_REASON)


@pytest.mark.parametrize("failure", ["not found", "denied"])
def test_without_the_job_window_validation_runs_is_not_read(failure):
  bq = _RunsBq([])
  bq.runs = [_run(_digest(bq))]
  session = JobOnlySession(None if failure == "not found" else "denied")
  args, _ = parse_args([
      "plan", "--project", PROJECT, "--region", REGION, "--seed_table", "users",
      "--generation_job_id", JOB_ID, "--landing_dataset", "thelook_synthetic",
      "--reference_dataset", SRC, "--scope", "manual", "--reference_rows_limit",
      str(PANEL), "--validation_runs_table", RUNS_TABLE
  ])
  planned, _ = driver.plan(
      args, make_env(bq, session_factory=lambda project: session))
  assert not [1 for sql, _ in bq.queries if "validation_runs" in sql]
  panel, privacy_reason, _, _ = _decisions(planned)
  assert not panel.verified and privacy_reason.startswith(
      privacy.UNVERIFIED_REASON)
  (note,) = [w for w in planned.warnings if JOB_ID in w]
  assert "reference sample cannot be verified" in note


def test_two_launches_in_the_window_use_the_latest(load_fixture):
  bq = _RunsBq([])
  right = _digest(bq)
  bq.runs = [
      _run(
          "1" * 64,
          base="earlier-0913-aaaaaa",
          created="2026-09-13T13:20:00.000000Z"),
      _run(right, created="2026-09-13T13:41:02.000000Z"),
  ]
  planned, _ = _plan(bq, load_fixture, "--validation_runs_table", RUNS_TABLE)
  panel, privacy_reason, lift_reason, _ = _decisions(planned)
  assert panel.verified and privacy_reason is None and lift_reason is None
  assert [w for w in planned.warnings if "2 launches" in w]
