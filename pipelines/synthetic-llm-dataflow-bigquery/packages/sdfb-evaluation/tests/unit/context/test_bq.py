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
"""The thin BigQuery wrapper (`sdfb_evaluation.context.bq.Bq`) over a fake
`google.cloud.bigquery.Client` — no credentials, no network."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import pytest
from google.api_core.exceptions import RetryError

from sdfb_evaluation.context.bq import Bq, BqApiError, normalize_fqn, quote_fqn


class _Row(dict):
  """`google.cloud.bigquery.Row` exposes `items()`; a dict does too."""


class _Job:

  def __init__(self, rows=(), total_bytes=0, job_id="job_fake_1", error=None):
    self._rows = list(rows)
    self.total_bytes_processed = total_bytes
    self.job_id = job_id
    self._error = error

  def result(self):
    if self._error is not None:
      raise self._error
    return [_Row(r) for r in self._rows]


class _Resource:

  def __init__(self, resource, **attrs):
    self._resource = resource
    for key, value in attrs.items():
      setattr(self, key, value)

  def to_api_repr(self):
    return dict(self._resource)


class _ForbiddenError(Exception):
  code = 403


class _NotFoundError(Exception):
  code = 404


class _FakeClient:
  """The `google.cloud.bigquery.Client` methods `Bq` calls."""

  def __init__(self):
    self.calls: list[tuple] = []
    self.next_job = _Job()
    self.raise_on_query: Exception | None = None

  def query(self, sql, job_config=None, location=None):
    self.calls.append(("query", sql, job_config, location))
    if self.raise_on_query is not None:
      raise self.raise_on_query
    return self.next_job

  def get_table(self, fqn):
    self.calls.append(("get_table", fqn))
    if fqn.endswith(".missing"):
      raise _NotFoundError("Not found: Table")
    return _Resource({
        "schema": {
            "fields": [{
                "name": "id",
                "type": "INTEGER",
                "mode": "REQUIRED"
            }]
        },
        "numRows": "18250",
        "numBytes": "3650000",
        "location": "EU",
        "timePartitioning": {
            "type": "DAY",
            "field": "created_at"
        },
        "lastModifiedTime": "1789307370500",
        "creationTime": "1788249600000",
    })

  def get_dataset(self, ref):
    self.calls.append(("get_dataset", ref))
    return _Resource({}, max_time_travel_hours="96")

  def load_table_from_json(self, rows, fqn, job_config=None):
    self.calls.append(("load", list(rows), fqn, job_config))
    return _Job(job_id="load_job_7")

  def get_job(self, job_id, location=None):
    self.calls.append(("get_job", job_id, location))
    if job_id == "denied":
      raise _ForbiddenError("Access Denied: bigquery.jobs.get")
    return _Resource({"statistics": {"load": {"outputRows": "5000"}}})


def _params_by_name(job_config):
  return {p.name: p for p in job_config.query_parameters}


def test_query_binds_typed_parameters_and_byte_cap():
  client = _FakeClient()
  client.next_job = _Job(rows=[{"n": 3}])
  bq = Bq("demo-project", client=client, location="EU")
  rows = bq.query(
      "SELECT @s, @i, @f, @b, @t, @d, @a",
      {
          "s": "orders",
          "i": 7,
          "f": 0.5,
          "b": True,
          "t": datetime(2026, 9, 13, 13, 10, tzinfo=UTC),
          "d": date(2026, 9, 13),
          "a": ["demo-project.thelook_synthetic.users"],
      },
      max_bytes=10_000_000,
  )
  assert rows == [{"n": 3}]
  _, sql, config, location = client.calls[0]
  assert sql == "SELECT @s, @i, @f, @b, @t, @d, @a"
  assert location == "EU"
  assert config.maximum_bytes_billed == 10_000_000
  params = _params_by_name(config)
  assert params["s"].type_ == "STRING"
  assert params["i"].type_ == "INT64"
  assert params["f"].type_ == "FLOAT64"
  assert params["b"].type_ == "BOOL"
  assert params["t"].type_ == "TIMESTAMP"
  assert params["d"].type_ == "DATE"
  assert params["a"].array_type == "STRING"
  assert params["a"].values == ["demo-project.thelook_synthetic.users"]


def test_query_rejects_untyped_parameters():
  bq = Bq("demo-project", client=_FakeClient())
  with pytest.raises(TypeError, match="x"):
    bq.query("SELECT @x", {"x": None})


def test_dry_run_bytes():
  client = _FakeClient()
  client.next_job = _Job(total_bytes=123_456)
  bq = Bq("demo-project", client=client)
  assert bq.dry_run_bytes("SELECT 1") == 123_456
  config = client.calls[0][2]
  assert config.dry_run is True and config.use_query_cache is False


def test_table_summary():
  bq = Bq("demo-project", client=_FakeClient())
  info = bq.table("demo-project.thelook_synthetic.orders")
  assert info["schema"] == [{
      "name": "id",
      "type": "INTEGER",
      "mode": "REQUIRED"
  }]
  assert info["numRows"] == 18250
  assert info["numBytes"] == 3_650_000  # the table's logical bytes
  assert info["location"] == "EU"
  assert info["timePartitioning"] == {"type": "DAY", "field": "created_at"}
  assert info["lastModified"] == "2026-09-13T13:49:30.500000Z"
  assert info["created"] == "2026-09-01T08:00:00.000000Z"
  assert info["timeTravelHours"] == 96


def test_table_not_found_is_lookup_error():
  bq = Bq("demo-project", client=_FakeClient())
  with pytest.raises(LookupError, match="missing"):
    bq.table("demo-project.thelook_synthetic.missing")


def test_load_json_append_create_never_and_json_safe():
  client = _FakeClient()
  bq = Bq("demo-project", client=client)
  schema = [{"name": "score", "type": "FLOAT64", "mode": "NULLABLE"}]
  job_id = bq.load_json(
      "demo-project.synthetic_data_quality.evaluation_metrics", [{
          "score": math.nan
      }, {
          "score": 0.25
      }], schema)
  assert job_id == "load_job_7"
  _, rows, fqn, config = client.calls[0]
  assert rows == [{"score": None}, {"score": 0.25}]
  assert fqn == "demo-project.synthetic_data_quality.evaluation_metrics"
  assert config.write_disposition == "WRITE_APPEND"
  assert config.create_disposition == "CREATE_NEVER"
  assert config.source_format == "NEWLINE_DELIMITED_JSON"
  assert [f.name for f in config.schema] == ["score"]


def test_execute_runs_ddl():
  client = _FakeClient()
  Bq("demo-project", client=client, location="EU").execute(
      "CREATE SNAPSHOT TABLE `demo-project.tmp.s` CLONE `demo-project.d.t`")
  assert client.calls[0][0] == "query"


def test_execute_binds_parameters():
  client = _FakeClient()
  start = datetime(2026, 9, 13, 13, 49, 20, tzinfo=UTC)
  Bq("demo-project", client=client, location="EU").execute(
      "CREATE TABLE `demo-project.tmp.a` AS SELECT * FROM "
      "APPENDS(TABLE `demo-project.d.t`, @start, NULL)", {"start": start},
      max_bytes=2_000_000_000)
  _, _, config, location = client.calls[0]
  assert location == "EU"
  assert config.maximum_bytes_billed == 2_000_000_000
  params = _params_by_name(config)
  assert params["start"].type_ == "TIMESTAMP"
  assert params["start"].value == start


def test_job_stats_returns_statistics():
  client = _FakeClient()
  bq = Bq("demo-project", client=client)
  assert bq.job_stats("job_1", "EU") == {"load": {"outputRows": "5000"}}
  assert client.calls[0] == ("get_job", "job_1", "EU")


def test_forbidden_becomes_permission_error():
  client = _FakeClient()
  client.raise_on_query = _ForbiddenError("Access Denied: jobs.listAll")
  bq = Bq("demo-project", client=client)
  with pytest.raises(PermissionError, match="Access Denied"):
    bq.query("SELECT 1")
  client.raise_on_query = None
  client.next_job = _Job(error=_ForbiddenError("Access Denied at result()"))
  with pytest.raises(PermissionError, match="result"):
    bq.query("SELECT 1")
  with pytest.raises(PermissionError, match=r"bigquery\.jobs\.get"):
    bq.job_stats("denied", "EU")


@pytest.mark.parametrize("fqn,expected", [
    ("demo-project.thelook_synthetic.users",
     "demo-project.thelook_synthetic.users"),
    ("demo-project:thelook_synthetic.users",
     "demo-project.thelook_synthetic.users"),
    ("bigquery-public-data.thelook_ecommerce.order_items",
     "bigquery-public-data.thelook_ecommerce.order_items"),
])
def test_normalize_fqn_accepts_both_separators(fqn, expected):
  assert normalize_fqn(fqn) == expected
  assert quote_fqn(fqn) == f"`{expected}`"


@pytest.mark.parametrize("bad", [
    "users",
    "demo-project.users",
    "demo-project.thelook_synthetic.users` WHERE TRUE; --",
    "demo-project.thelook synthetic.users",
    "Demo-Project.thelook_synthetic.users",
    "demo-project.thelook_synthetic.users.extra",
])
def test_normalize_fqn_rejects_anything_but_project_dataset_table(bad):
  with pytest.raises(ValueError):
    normalize_fqn(bad)


class _ApiError(Exception):
  """A google.api_core-style error carrying an HTTP status."""

  def __init__(self, code, message):
    super().__init__(message)
    self.code = code


@pytest.mark.parametrize("code", [400, 409, 500, 503])
def test_other_api_errors_become_bq_api_error(code):
  # R61: `_translated` already knows the status (it just branched on it
  # for 403/404); BqApiError keeps it instead of dropping it, so a
  # caller (e.g. plan.py's `_is_already_exists`) can read it back
  # structurally rather than re-parsing the message.
  client = _FakeClient()
  client.raise_on_query = _ApiError(code, f"{code} backend said no")
  with pytest.raises(
      BqApiError, match=f"query: {code} backend said no") as info:
    Bq("demo-project", client=client).query("SELECT 1")
  assert info.value.status == code


def test_google_api_error_without_status_becomes_bq_api_error():
  client = _FakeClient()
  client.raise_on_query = RetryError("deadline exceeded", cause=None)
  with pytest.raises(BqApiError, match="deadline exceeded") as info:
    Bq("demo-project", client=client).query("SELECT 1")
  assert info.value.status is None


def test_non_api_errors_propagate_untouched():
  client = _FakeClient()
  client.raise_on_query = KeyError("programming error")
  with pytest.raises(KeyError):
    Bq("demo-project", client=client).query("SELECT 1")
