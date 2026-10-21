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
"""BigQuery's REST API as it answers, for a REAL `google.cloud.bigquery`
client: the fake is the HTTP connection, so `get_table`, `list_rows`,
`query` and every cell conversion in the tests are the client's own.

    FakeApi.api_request(method, path, query_params, data)
      GET  …/datasets/D/tables/T        tables.get      numRows, numBytes,
                                                        lastModifiedTime,
                                                        the schema
      GET  …/datasets/D/tables/T/data   tabledata.list  startIndex,
                                                        maxResults,
                                                        selectedFields;
                                                        totalRows and a
                                                        pageToken when the
                                                        response is cut
      POST …/jobs                       jobs.insert     a query job, DONE
      GET  …/queries/JOB                jobs.getQueryResults

Rows travel as the API sends them: `{"f": [{"v": …}, …]}` with every
scalar a string (`wire_rows` from the REST-typed all-types fixture,
`typed_rows` from rows held in the client's Python types), a TIMESTAMP
as integer microseconds (both read paths ask for
`formatOptions.useInt64Timestamp`), a RECORD a nested `f` list, a
REPEATED field a list of `{"v": …}`, JSON as text.

A response is cut at `page_rows` rows, as the API cuts one at its byte
limit: fewer rows than `maxResults` asked for, and a `pageToken`.

Nothing here is real data (`demo-project`, the invented all-types rows).
"""

from __future__ import annotations

import base64
import dataclasses
import json
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from google.api_core import exceptions as api_exceptions
from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery

from .tables import PROJECT

_TABLE_PARTS = 6  # /projects/P/datasets/D/tables/T, split on "/"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MODIFIED_MS = 1_767_323_045_123  # an arbitrary lastModifiedTime


def full_schema(fields: Sequence[Mapping[str, Any]],
                rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
  """`fields` as a table's schema resource: a RECORD the fixture leaves
  untyped gets STRING sub-fields named after the keys its rows hold (the
  API never sends a RECORD without its fields)."""
  out = []
  for declared in fields:
    field = {k: v for k, v in declared.items() if k != "kind"}
    if field["type"] == "RECORD" and "fields" not in field:
      names = list(
          dict.fromkeys(
              key for row in rows if row.get(field["name"]) is not None
              for key in row[field["name"]]))
      field["fields"] = [{
          "name": name,
          "type": "STRING",
          "mode": "NULLABLE"
      } for name in names]
    out.append(field)
  return out


def _micros(text: str) -> str:
  moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
  delta = moment - _EPOCH
  return str((delta.days * 86_400 + delta.seconds) * 1_000_000 +
             delta.microseconds)


def _wire_scalar(field: Mapping[str, Any], raw: Any) -> Any:
  kind = field["type"]
  if kind == "RECORD":
    return {
        "f": [wire_cell(sub, raw.get(sub["name"])) for sub in field["fields"]]
    }
  if kind == "JSON":
    return json.dumps(raw)
  if kind == "TIMESTAMP":
    return _micros(raw)
  if kind == "BOOL":
    return "true" if raw else "false"
  return str(raw)


def wire_cell(field: Mapping[str, Any], raw: Any) -> dict[str, Any]:
  """One fixture cell (REST-typed: `tests/fixtures/beam/all_types.json`)
  as the `{"v": …}` the API puts in a row."""
  if field.get("mode") == "REPEATED":
    scalar = {**field, "mode": "NULLABLE"}
    return {"v": [{"v": _wire_scalar(scalar, item)} for item in raw or []]}
  if raw is None:
    return {"v": None}
  return {"v": _wire_scalar(field, raw)}


def wire_rows(fields: Sequence[Mapping[str, Any]],
              rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
  """`rows` as `tabledata.list` and `jobs.getQueryResults` send them."""
  return [{
      "f": [wire_cell(field, row.get(field["name"])) for field in fields]
  } for row in rows]


def _typed_scalar(  # noqa: PLR0911 — type dispatch, clearer flat than nested
    field: Mapping[str, Any], value: Any) -> Any:
  kind = field["type"]
  if kind == "RECORD":
    return {
        "f": [
            typed_cell(sub, value.get(sub["name"])) for sub in field["fields"]
        ]
    }
  if kind == "JSON":
    return json.dumps(value)
  if kind == "TIMESTAMP":
    return _micros(value.isoformat())
  if kind == "BOOL":
    return "true" if value else "false"
  if kind == "BYTES":
    return base64.b64encode(value).decode("ascii")
  if kind == "FLOAT64":
    if math.isnan(value):
      return "NaN"
    if math.isinf(value):
      return "Infinity" if value > 0 else "-Infinity"
    return repr(float(value))
  if kind in ("DATETIME", "DATE", "TIME"):
    return value.isoformat()
  return str(value)


def typed_cell(field: Mapping[str, Any], value: Any) -> dict[str, Any]:
  """One cell held as the BigQuery client returns it (an aware
  `datetime`, a `Decimal`, `bytes`…) as the `{"v": …}` the API sends for
  it: the inverse of the client's conversion."""
  if field.get("mode") == "REPEATED":
    scalar = {**field, "mode": "NULLABLE"}
    return {"v": [{"v": _typed_scalar(scalar, item)} for item in value or []]}
  if value is None:
    return {"v": None}
  return {"v": _typed_scalar(field, value)}


def typed_rows(fields: Sequence[Mapping[str, Any]],
               rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
  """Client-typed `rows` as `tabledata.list` sends them."""
  return [{
      "f": [typed_cell(field, row.get(field["name"])) for field in fields]
  } for row in rows]


@dataclasses.dataclass
class ApiTable:
  """One table behind the fake API: its schema resource, its rows on the
  wire (in schema order) and what `tables.get` says about it."""
  fields: list[dict[str, Any]]
  rows: list[dict[str, Any]]
  modified_ms: int = MODIFIED_MS
  kind: str = "TABLE"
  num_bytes: int | None = None
  # what tables.get says, when it must differ from the rows there are
  num_rows: int | None = None
  # the keys tables.get leaves out of the resource (`numRows`,
  # `lastModifiedTime`…)
  omit: tuple[str, ...] = ()

  def resource(self, project: str, dataset: str, name: str) -> dict[str, Any]:
    size = self.num_bytes
    if size is None:
      size = sum(len(json.dumps(row)) for row in self.rows)
    count = len(self.rows) if self.num_rows is None else self.num_rows
    resource = {
        "tableReference": {
            "projectId": project,
            "datasetId": dataset,
            "tableId": name
        },
        "schema": {
            "fields": self.fields
        },
        "numRows": str(count),
        "numBytes": str(size),
        "lastModifiedTime": str(self.modified_ms),
        "creationTime": str(MODIFIED_MS - 1000),
        "type": self.kind,
    }
    return {k: v for k, v in resource.items() if k not in self.omit}


def api_error(status: int, reason: str, message: str) -> Exception:
  """The exception the client raises for an API error of `status` whose
  first error carries `reason` (`rateLimitExceeded`, `accessDenied`…)."""
  error = api_exceptions.from_http_status(
      status, message, errors=[{
          "reason": reason,
          "message": message
      }])
  return error


class FakeApi:
  """The REST API over `tables` (`project.dataset.table` → `ApiTable`).

  `page_rows` cuts every `tabledata.list` response (None: only at
  `maxResults`). `failures` are raised, one per `tabledata.list` request,
  before it is answered; `page_failures` only for a request that names a
  row (`startIndex`: a page of a range, never the count request that
  cuts the ranges). `on_list(api, request number)` runs before each
  `tabledata.list` answer, the first request being number 1 (a table
  that changes while it is read). `total_rows` is the `totalRows` every
  answer carries instead of the rows there are, and the answers from
  request number `no_total_rows_from` on carry none. `query_result` is
  what the one query of a test returns.
  """

  def __init__(self,
               tables: Mapping[str, ApiTable],
               *,
               page_rows: int | None = None):
    self.tables = dict(tables)
    self.page_rows = page_rows
    self.failures: list[Exception] = []
    self.page_failures: list[Exception] = []
    self.total_rows: int | None = None
    self.no_total_rows_from: int | None = None
    self.on_list: Callable[[FakeApi, int], None] | None = None
    self.query_result: tuple[list[dict[str, Any]], list[dict[str,
                                                             Any]]] = ([], [])
    self.requests: list[dict[str, Any]] = []

  # -- what a test reads back ------------------------------------------------
  def lists(self) -> list[dict[str, Any]]:
    """The `query_params` of every `tabledata.list` request, in order."""
    return [
        dict(r.get("query_params") or {})
        for r in self.requests
        if r["path"].endswith("/data")
    ]

  def pages(self) -> list[dict[str, Any]]:
    """The `tabledata.list` requests that name a row (`startIndex`): the
    pages of the ranges."""
    return [params for params in self.lists() if "startIndex" in params]

  def counts(self) -> list[dict[str, Any]]:
    """The `tabledata.list` requests that name no row: the one that
    counts a table when its ranges are cut."""
    return [params for params in self.lists() if "startIndex" not in params]

  def gets(self) -> int:
    """How many `tables.get` requests were made."""
    return sum(1 for r in self.requests if r["method"] == "GET" and
               "/tables/" in r["path"] and not r["path"].endswith("/data"))

  # -- the connection ----------------------------------------------------------
  def api_request(self, **request: Any) -> dict[str, Any]:
    self.requests.append(request)
    method, path = request["method"], request["path"]
    params = dict(request.get("query_params") or {})
    if method == "POST" and path.endswith("/jobs"):
      return self._insert(request["data"])
    if method == "GET" and "/queries/" in path:
      return self._results(path.rsplit("/", 1)[1], params)
    if method == "GET" and path.endswith("/data"):
      return self._list(path[:-len("/data")], params)
    if method == "GET" and "/tables/" in path:
      project, dataset, name, table = self._table(path)
      return table.resource(project, dataset, name)
    raise AssertionError(f"the fake API has no {method} {path}")

  def _table(self, path: str) -> tuple[str, str, str, ApiTable]:
    parts = path.strip("/").split("/")
    assert len(parts) == _TABLE_PARTS, path
    project, dataset, name = parts[1], parts[3], parts[5]
    fqn = f"{project}.{dataset}.{name}"
    if fqn not in self.tables:
      raise api_exceptions.NotFound(f"Not found: Table {fqn}")
    return project, dataset, name, self.tables[fqn]

  def _list(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
    number = len(self.lists())
    if self.failures:
      raise self.failures.pop(0)
    if "startIndex" in params and self.page_failures:
      raise self.page_failures.pop(0)
    if self.on_list is not None:
      self.on_list(self, number)
    table = self._table(path)[3]
    start = int(params.get("startIndex") or 0)
    assert "pageToken" not in params, "a page was asked for by token"
    asked = params.get("maxResults")
    stop = len(table.rows) if asked is None else start + int(asked)
    if self.page_rows is not None:
      stop = min(stop, start + self.page_rows)
    names = [f["name"] for f in table.fields]
    selected = str(params.get("selectedFields") or "").split(",")
    keep = [
        i for i, name in enumerate(names)
        if name in selected or selected == [""]
    ]
    rows = [{
        "f": [row["f"][i] for i in keep]
    } for row in table.rows[start:stop]]
    response: dict[str, Any] = {
        "kind": "bigquery#tableDataList",
        "rows": rows,
    }
    if self.no_total_rows_from is None or number < self.no_total_rows_from:
      total = len(table.rows) if self.total_rows is None else self.total_rows
      response["totalRows"] = str(total)
    if start + len(rows) < len(table.rows):
      response["pageToken"] = f"token-{start + len(rows)}"
    return response

  def _insert(self, data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "jobReference": dict(data["jobReference"]),
        "configuration": dict(data["configuration"]),
        "status": {
            "state": "DONE"
        },
        "statistics": {
            "query": {
                "statementType": "SELECT"
            }
        },
    }

  def _results(self, job: str, params: Mapping[str, Any]) -> dict[str, Any]:
    fields, rows = self.query_result
    asked = params.get("maxResults")
    response: dict[str, Any] = {
        "jobComplete": True,
        "jobReference": {
            "projectId": PROJECT,
            "jobId": job
        },
        "schema": {
            "fields": fields
        },
        "totalRows": str(len(rows)),
    }
    if asked is None or int(asked) > 0:
      response["rows"] = rows
    return response


# The API the DoFns of a running test talk to: a DoFn is pickled into the
# pipeline, so its client factory is named by reference (`make_client`)
# and finds the test's fake here. The in-process runner shares the module.
_STATE: dict[str, FakeApi] = {}


def install(api: FakeApi) -> FakeApi:
  _STATE["api"] = api
  return api


def uninstall() -> None:
  _STATE.clear()


def client_over(api: FakeApi, project: str = PROJECT) -> bigquery.Client:
  """A real BigQuery client whose HTTP connection is `api`."""
  client = bigquery.Client(project=project, credentials=AnonymousCredentials())
  client._connection = api  # pylint: disable=protected-access  # the client has no public seam for its connection
  return client


def make_client(project: str) -> bigquery.Client:
  """The paged read's client factory in tests: a real client over the
  installed fake API."""
  return client_over(_STATE["api"], project)
