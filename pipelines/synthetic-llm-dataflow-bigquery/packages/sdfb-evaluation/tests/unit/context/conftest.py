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
"""Fakes for the GCP context tests: a `FakeBq` standing in for
`context.bq.Bq` and a `FakeSession` standing in for an authorised
`requests.Session` (Dataflow `jobs.get` + Logging `entries:list`).

Both serve the recorded-shape JSON under `tests/fixtures/context/`
(invented ids, the thelook tables, `demo-project`). No credentials, no
network.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest

PROJECT = "demo-project"
REGION = "europe-west1"
JOB_ID = "2026-09-13_06_10_16-9000000000000000017"


class FakeResponse:
  """The slice of `requests.Response` the context modules read."""

  def __init__(self, status_code: int, payload: Any):
    self.status_code = status_code
    self._payload = payload

  def json(self) -> Any:
    return copy.deepcopy(self._payload)

  @property
  def text(self) -> str:
    return json.dumps(self._payload)


def _error(status: int, message: str) -> FakeResponse:
  return FakeResponse(
      status, {"error": {
          "code": status,
          "message": message,
          "status": "ERR"
      }})


def _entry_text(entry: Mapping[str, Any]) -> str:
  if "textPayload" in entry:
    return str(entry["textPayload"])
  return str((entry.get("jsonPayload") or {}).get("message", ""))


class FakeSession:
  """Dataflow `jobs.get` and Logging `entries:list` over fixture data.

  The Logging emulation understands the two filter shapes `LogMilestones`
  sends: a header search (the filter names `SDFB_MILESTONE name=<x>`) and a
  continuation read (one exact `logName`, entries at or after a
  timestamp). `status_script` is consumed one status per request before
  any real answer, to exercise the retry path.
  """

  def __init__(self,
               *,
               jobs: Mapping[tuple[str, str], dict] | None = None,
               entries: Sequence[dict] = (),
               status_script: Sequence[int] = ()):
    self.jobs = dict(jobs or {})
    self.entries = list(entries)
    self.status_script = list(status_script)
    self.calls: list[tuple[str, str, Any]] = []
    self.headers: dict[str, str] = {}

  def _scripted(self) -> FakeResponse | None:
    if self.status_script:
      status = self.status_script.pop(0)
      if status != 200:
        return _error(status, f"scripted {status}")
    return None

  def get(self, url: str, params=None, timeout=None) -> FakeResponse:
    del timeout
    self.calls.append(("GET", url, params))
    scripted = self._scripted()
    if scripted is not None:
      return scripted
    match = re.search(r"/locations/([^/]+)/jobs/([^/?]+)$", url)
    assert match, url
    job = self.jobs.get((match.group(1), match.group(2)))
    if job is None:
      return _error(404, f"(fake) job {match.group(2)} not found")
    return FakeResponse(200, job)

  def post(self, url: str, json=None, timeout=None) -> FakeResponse:  # pylint: disable=redefined-outer-name  # mirrors requests.Session.post
    del timeout
    body = dict(json or {})
    self.calls.append(("POST", url, body))
    scripted = self._scripted()
    if scripted is not None:
      return scripted
    selected = self._select(body["filter"])
    size = int(body.get("pageSize", 1000))
    start = int(body.get("pageToken") or 0)
    page = selected[start:start + size]
    payload: dict[str, Any] = {"entries": page}
    if start + size < len(selected):
      payload["nextPageToken"] = str(start + size)
    return FakeResponse(200, payload)

  def _select(self, log_filter: str) -> list[dict]:
    job = re.search(r'resource\.labels\.job_id="([^"]+)"', log_filter)
    lo = re.search(r'timestamp>="([^"]+)"', log_filter)
    hi = re.search(r'timestamp<="([^"]+)"', log_filter)
    name = re.search(r"SDFB_MILESTONE name=([a-z0-9_]+)", log_filter)
    log_name = re.search(r'logName="([^"]+)"', log_filter)
    out = []
    for entry in self.entries:
      if job and entry["resource"]["labels"]["job_id"] != job.group(1):
        continue
      if lo and entry["timestamp"] < lo.group(1):
        continue
      if hi and entry["timestamp"] > hi.group(1):
        continue
      if name:
        if f"SDFB_MILESTONE name={name.group(1)}" not in _entry_text(entry):
          continue
      elif log_name and entry["logName"] != log_name.group(1):
        continue
      out.append(entry)
    return sorted(out, key=lambda e: e["timestamp"])


def _run_matches(row: Mapping[str, Any], bound: Mapping[str, Any]) -> bool:
  """runs_for's WHERE: landing table in the array, OR the run id is the
  base / starts with `base-`."""
  if row["landing_table"] in set(bound.get("landing_tables") or ()):
    return True
  base = bound.get("base")
  return bool(base) and (row["run_id"] == base or
                         str(row["run_id"]).startswith(f"{base}-"))


class FakeBq:
  """`context.bq.Bq` over canned rows.

  `responses` maps a SQL substring to the rows returned for any query
  containing it (first match wins); `failures` maps a SQL substring to an
  exception raised instead. Every query is recorded with its parameters.
  """

  def __init__(self,
               *,
               project: str = PROJECT,
               location: str | None = None,
               responses: Sequence[tuple[str, Sequence[dict]]] = (),
               job_stats: Mapping[str, dict] | None = None,
               tables: Mapping[str, dict] | None = None):
    self.project = project
    self.location = location
    self.responses = list(responses)
    self.failures: dict[str, BaseException] = {}
    self.stats = dict(job_stats or {})
    self.stats_failure: BaseException | None = None
    self.tables = dict(tables or {})
    self.queries: list[tuple[str, dict[str, Any]]] = []
    self.stats_calls: list[tuple[str, str]] = []

  def query(self,
            sql: str,
            params: Mapping[str, Any] | None = None,
            *,
            max_bytes: int | None = None) -> list[dict]:
    del max_bytes
    bound = dict(params or {})
    self.queries.append((sql, bound))
    for needle, exc in self.failures.items():
      if needle in sql:
        raise exc
    for needle, rows in self.responses:
      if needle in sql:
        if "@landing_tables" in sql or "@base" in sql:
          return [dict(r) for r in rows if _run_matches(r, bound)]
        return [dict(r) for r in rows]
    raise AssertionError(f"FakeBq: unexpected SQL {sql!r}")

  def job_stats(self, job_id: str, location: str) -> dict:
    self.stats_calls.append((job_id, location))
    if self.stats_failure is not None:
      raise self.stats_failure
    return copy.deepcopy(self.stats[job_id])

  def table(self, fqn: str) -> dict:
    return copy.deepcopy(self.tables[fqn])


@pytest.fixture(name="fixture_data")
def _fixture_data(load_fixture) -> Callable[[str], Any]:
  """`fixture_data("jobs_by_project")` → the parsed recorded-shape JSON."""

  def _get(name: str) -> Any:
    return load_fixture(f"context/{name}.json")

  return _get


@pytest.fixture
def fake_session(fixture_data) -> Callable[..., FakeSession]:
  """Factory: a `FakeSession` holding the recorded job and log entries."""

  def _make(**overrides: Any) -> FakeSession:
    kwargs: dict[str, Any] = {
        "jobs": {
            (REGION, JOB_ID): fixture_data("dataflow_job")
        },
        "entries": fixture_data("log_entries")["entries"],
    }
    kwargs.update(overrides)
    return FakeSession(**kwargs)

  return _make


@pytest.fixture
def fake_bq(fixture_data) -> Callable[..., FakeBq]:
  """Factory: a `FakeBq` answering the JOBS and validation_runs queries."""

  def _make(**overrides: Any) -> FakeBq:
    kwargs: dict[str, Any] = {
        "responses": [
            ("@exclude_job", []),
            ("JOBS_BY_PROJECT", fixture_data("jobs_by_project")),
            ("validation_runs", fixture_data("validation_runs")),
        ],
        "job_stats": fixture_data("job_stats"),
        "tables": {
            f"{PROJECT}.thelook_synthetic.{t}": {
                "location": "EU"
            } for t in ("users", "orders", "order_items")
        },
    }
    kwargs.update(overrides)
    return FakeBq(**kwargs)

  return _make
