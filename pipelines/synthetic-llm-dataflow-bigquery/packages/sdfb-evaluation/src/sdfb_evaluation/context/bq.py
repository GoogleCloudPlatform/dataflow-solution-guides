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
"""A thin, fakeable wrapper over `google.cloud.bigquery.Client`.

Every BigQuery call the evaluator's driver makes goes through `Bq`, so the
tests swap in a fake with the same five methods (plus `job_stats`) and
never need credentials. `google.cloud.bigquery` arrives with
`apache-beam[gcp]` (it is not a direct dependency) and is imported lazily,
inside the methods that build its config objects.

Two rules hold for every caller:

- SQL values are bound as query parameters (`@name`), never formatted into
  the text. `query` and `execute` infer each parameter's BigQuery type
  from its Python type and refuse `None` (an untyped NULL). The one
  exception is `FOR SYSTEM_TIME AS OF`: query parameters are constant
  expressions and would do, but `context.scope` keeps a `TIMESTAMP`
  literal re-rendered from a parsed datetime there, for safety.
- Table identifiers that must be interpolated go through `normalize_fqn` /
  `quote_fqn`, which accept only a strict `project.dataset.table`.

Access errors surface as the builtin `PermissionError` (HTTP 403),
missing resources as `LookupError` (HTTP 404) and every other API failure
(a 400 such as an unknown location, a 5xx after the client's own retries)
as `BqApiError`, so callers can degrade or add a remediation hint without
importing `google.api_core`.

`is_refusal` says which of those a caller may degrade on: BigQuery
REFUSED the call (403, 404, or a 400), as opposed to a failure a retry
may remove (a 5xx, a 429, a conflict, an error with no status).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any, TypeVar

from sdfb_evaluation.canonical import json_safe

__all__ = [
    "REFUSED_STATUSES",
    "Bq",
    "BqApiError",
    "is_refusal",
    "normalize_fqn",
    "quote_fqn",
]

_PROJECT = r"[a-z][a-z0-9-]{4,28}[a-z0-9]"
_DATASET = r"[A-Za-z0-9_]{1,1024}"
_TABLE = r"[A-Za-z0-9_$-]{1,1024}"
_FQN_RE = re.compile(rf"({_PROJECT})[.:]({_DATASET})\.({_TABLE})")

# BigQuery's documented default when a dataset sets no time-travel window.
_DEFAULT_TIME_TRAVEL_HOURS = 168
_HTTP_BAD_REQUEST = 400
_HTTP_FORBIDDEN = 403
_HTTP_NOT_FOUND = 404
REFUSED_STATUSES = frozenset(
    {_HTTP_BAD_REQUEST, _HTTP_FORBIDDEN, _HTTP_NOT_FOUND})

# A TypeVar, not PEP 695 type parameters: yapf 0.43 cannot parse those.
_T = TypeVar("_T")


class BqApiError(RuntimeError):
  """A BigQuery API call failed with neither a 403 nor a 404.

  `status` is the HTTP status `_translated` already computed for this
  failure (e.g. 409 for a conflict), so a caller that needs to
  discriminate between different failures carrying the same status (a
  409 Already Exists vs. a 409 concurrent-job conflict) reads it instead
  of re-parsing the message text (Ruling R61). `None` when the
  underlying error carried no numeric status of its own (a
  `GoogleAPIError` `_translated` still recognises by type).
  """

  def __init__(self, message: str, *, status: int | None = None) -> None:
    super().__init__(message)
    self.status = status


def is_refusal(exc: BaseException) -> bool:
  """Whether BigQuery REFUSED the call `exc` came from (module
  docstring): a 403 (`PermissionError`), a 404 (`LookupError`) or a
  `BqApiError` carrying one of `REFUSED_STATUSES`. A 5xx, a 429, a
  conflict or an error with no status is not a refusal: it may pass on a
  retry, so a caller must not degrade on it (Ruling R89, M8)."""
  if isinstance(exc, BqApiError):
    return exc.status in REFUSED_STATUSES
  return isinstance(exc, (PermissionError, LookupError))


def normalize_fqn(fqn: str) -> str:
  """`project.dataset.table` (also accepting `project:dataset.table`).

  Raises:
    ValueError: anything else — the value is about to be interpolated into
      SQL between backticks, so nothing outside the strict alphabet passes.
  """
  match = _FQN_RE.fullmatch(fqn or "")
  if match is None:
    raise ValueError(
        f"expected a BigQuery table as project.dataset.table, got {fqn!r}")
  return ".".join(match.groups())


def quote_fqn(fqn: str) -> str:
  """The validated table reference, backtick-quoted for SQL."""
  return f"`{normalize_fqn(fqn)}`"


def _status(exc: BaseException) -> int | None:
  code = getattr(exc, "code", None)
  return int(code) if isinstance(code, int) else None


def _is_google_api_error(exc: BaseException) -> bool:
  from google.api_core import exceptions  # pylint: disable=import-outside-toplevel  # only reached on a failing real call

  return isinstance(exc, exceptions.GoogleAPIError)


def _translated(call: Callable[[], _T], what: str) -> _T:  # noqa: UP047
  """Run a client call; an API failure becomes `PermissionError` (403),
  `LookupError` (404) or `BqApiError` (anything else), its message
  starting with `what`."""
  try:
    return call()
  except (PermissionError, LookupError, BqApiError):
    raise
  except Exception as exc:
    status = _status(exc)
    if status == _HTTP_FORBIDDEN:
      raise PermissionError(f"{what}: {exc}") from exc
    if status == _HTTP_NOT_FOUND:
      raise LookupError(f"{what}: {exc}") from exc
    if status is not None or _is_google_api_error(exc):
      raise BqApiError(f"{what}: {exc}", status=status) from exc
    raise


def _scalar_type(name: str, value: Any) -> str:
  # bool before int: bool is an int subclass.
  if isinstance(value, bool):
    return "BOOL"
  if isinstance(value, int):
    return "INT64"
  if isinstance(value, float):
    return "FLOAT64"
  if isinstance(value, str):
    return "STRING"
  if isinstance(value, datetime):
    return "TIMESTAMP"
  if isinstance(value, date):
    return "DATE"
  raise TypeError(f"query parameter @{name}: cannot bind {type(value).__name__}"
                  f" ({value!r}) — pass a str/int/float/bool/datetime/date "
                  f"or a list of one of those")


def _utc(value: Any) -> Any:
  if isinstance(value, datetime) and value.tzinfo is None:
    return value.replace(tzinfo=UTC)
  return value


def _query_parameters(params: Mapping[str, Any]) -> list[Any]:
  from google.cloud import bigquery  # pylint: disable=import-outside-toplevel  # optional heavy client, loaded on first real query

  out: list[Any] = []
  for name, value in params.items():
    if isinstance(value, (list, tuple)):
      kind = _scalar_type(name, value[0]) if value else "STRING"
      out.append(
          bigquery.ArrayQueryParameter(name, kind, [_utc(v) for v in value]))
    else:
      out.append(
          bigquery.ScalarQueryParameter(name, _scalar_type(name, value),
                                        _utc(value)))
  return out


def _ms_to_iso(value: Any) -> str | None:
  if value in (None, ""):
    return None
  moment = datetime.fromtimestamp(int(value) / 1000, UTC)
  return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Bq:
  """The evaluator's only door to BigQuery.

  Args:
    project: the billing/quota project queries run in.
    client: an existing `google.cloud.bigquery.Client` (or a fake); built
      from Application Default Credentials when omitted.
    location: the BigQuery location jobs run in (`EU`, `US`,
      `europe-west1`…); `None` lets BigQuery infer it from the tables.
  """

  def __init__(self,
               project: str,
               client: Any = None,
               location: str | None = None):
    if client is None:
      from google.cloud import bigquery  # pylint: disable=import-outside-toplevel  # credentials are resolved only when no client is injected

      client = bigquery.Client(project=project, location=location)
    self.project = project
    self.location = location
    self._client = client

  def query(self,
            sql: str,
            params: Mapping[str, Any] | None = None,
            *,
            max_bytes: int | None = None) -> list[dict]:
    """Run `sql` with bound `params`; every row as a plain dict.

    `max_bytes` caps the bytes billed (the job fails rather than overrun).
    """
    from google.cloud import bigquery  # pylint: disable=import-outside-toplevel  # optional heavy client

    config = bigquery.QueryJobConfig(
        query_parameters=_query_parameters(params or {}))
    if max_bytes is not None:
      config.maximum_bytes_billed = int(max_bytes)
    job = _translated(
        lambda: self._client.query(
            sql, job_config=config, location=self.location), "query")
    rows = _translated(job.result, "query")
    return [dict(row.items()) for row in rows]

  def dry_run_bytes(self,
                    sql: str,
                    params: Mapping[str, Any] | None = None) -> int:
    """Bytes `sql` would process, from a dry run (nothing is billed)."""
    from google.cloud import bigquery  # pylint: disable=import-outside-toplevel  # optional heavy client

    config = bigquery.QueryJobConfig(
        query_parameters=_query_parameters(params or {}),
        dry_run=True,
        use_query_cache=False,
    )
    job = _translated(
        lambda: self._client.query(
            sql, job_config=config, location=self.location), "dry run")
    return int(job.total_bytes_processed or 0)

  def table(self, fqn: str) -> dict:
    """What planning and scoping need to know about one table.

    Returns:
      `schema` (BigQuery JSON field list), `numRows` and `numBytes`
      (int; the table's logical bytes), `location`, `timePartitioning`
      (dict or None), `lastModified` and `created` (RFC 3339 UTC) and
      `timeTravelHours` (the dataset's window; 168 when unset).
    """
    name = normalize_fqn(fqn)
    resource = _translated(lambda: self._client.get_table(name),
                           f"table {name}").to_api_repr()
    dataset = _translated(
        lambda: self._client.get_dataset(name.rsplit(".", 1)[0]),
        f"dataset of {name}")
    hours = getattr(dataset, "max_time_travel_hours", None)
    rows, size = resource.get("numRows"), resource.get("numBytes")
    return {
        "schema":
            list((resource.get("schema") or {}).get("fields") or []),
        "numRows":
            int(rows) if rows is not None else None,
        "numBytes":
            int(size) if size is not None else None,
        "location":
            resource.get("location"),
        "timePartitioning":
            resource.get("timePartitioning"),
        "lastModified":
            _ms_to_iso(resource.get("lastModifiedTime")),
        "created":
            _ms_to_iso(resource.get("creationTime")),
        "timeTravelHours":
            int(hours) if hours is not None else _DEFAULT_TIME_TRAVEL_HOURS,
    }

  def load_json(self, fqn: str, rows: Sequence[dict],
                schema: list[dict]) -> str:
    """Append `rows` with a load job (WRITE_APPEND, CREATE_NEVER).

    Rows are made JSON-safe first (NaN/±Inf → NULL: load jobs reject
    them). Blocks until the job commits.

    Returns:
      The load job id.
    """
    from google.cloud import bigquery  # pylint: disable=import-outside-toplevel  # optional heavy client

    config = bigquery.LoadJobConfig(
        schema=[bigquery.SchemaField.from_api_repr(f) for f in schema],
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        create_disposition=bigquery.CreateDisposition.CREATE_NEVER,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    safe = [json_safe(dict(row)) for row in rows]
    name = normalize_fqn(fqn)
    job = _translated(
        lambda: self._client.load_table_from_json(
            safe, name, job_config=config), f"load into {name}")
    _translated(job.result, f"load into {name}")
    return str(job.job_id)

  def execute(self,
              sql: str,
              params: Mapping[str, Any] | None = None,
              *,
              max_bytes: int | None = None) -> None:
    """Run a DDL statement (snapshot clone, CTAS, view) to completion.

    `params` are bound exactly as `query` binds them — e.g. the
    `@start_…`/`@end_…` inside an `APPENDS` CTAS's query. `max_bytes` caps
    the bytes billed: a CTAS over `APPENDS` or an AS OF difference can
    scan a lot.
    """
    from google.cloud import bigquery  # pylint: disable=import-outside-toplevel  # optional heavy client

    config = bigquery.QueryJobConfig(
        query_parameters=_query_parameters(params or {}))
    if max_bytes is not None:
      config.maximum_bytes_billed = int(max_bytes)
    job = _translated(
        lambda: self._client.query(
            sql, job_config=config, location=self.location), "DDL")
    _translated(job.result, "DDL")

  def job_stats(self, job_id: str, location: str) -> dict:
    """`statistics` of one BigQuery job (`jobs.get`), in REST shape.

    Reading another principal's job needs `bigquery.jobs.get` on the
    project; a denial is raised as `PermissionError`.
    """
    job = _translated(lambda: self._client.get_job(job_id, location=location),
                      f"jobs.get {job_id}")
    return dict(job.to_api_repr().get("statistics") or {})
