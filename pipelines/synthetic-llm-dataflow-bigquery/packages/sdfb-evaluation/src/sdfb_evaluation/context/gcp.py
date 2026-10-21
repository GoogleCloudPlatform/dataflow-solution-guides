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
"""REST context for one generation job: the Dataflow job resource and the
`SDFB_MILESTONE` entries its launcher and workers logged.

Re-implemented from `scripts/e2e/e2e_gcp_probe.py` (this package imports
nothing from the repository's scripts or generator packages):

- `make_session` is the probe's ADC session: Application Default
  Credentials plus the `x-goog-user-project` header, which routes API quota
  to the target project. The one endpoint known to reject that header is
  OpenID `userinfo` (probe `_whoami`); a caller hitting it must pass
  `headers={"x-goog-user-project": None}` on that request.
- `DataflowJobs.params` mirrors the probe's `_job_params` (display data,
  first occurrence of a key wins) and drops the same infrastructure keys.
- `LogMilestones` mirrors the probe's `_worker_log_milestones` (padded job
  window, bounded paging, 429/5xx backoff) and adds the launcher.

Which log streams are read. The worker stream is filtered by its logName
(`…/logs/dataflow.googleapis.com%2Fworker`, the probe's filter; a
`resource.type` filter alone returns nothing under some projects' log
routing). The launcher stream is matched both by its logName
(`…/logs/dataflow.googleapis.com%2Flauncher`) and through `resource.type=
"dataflow_step"` + `resource.labels.job_id`, the filter the FK/PK validator
prompt uses to pull `launch_config` and `relationships_loaded`, so a
routing that hides either still finds it. The flex-template launcher forwards its Python
process's output ONE LINE PER ENTRY, so a pretty milestone
(`log_milestone_pretty`: header line, then indent-2 JSON) arrives as a
header entry followed by body entries. `find` reassembles those from the
header entry's own logName; a worker record already holds the whole text.

The milestone format is the generator's `sdfb_core.observability`
contract: `SDFB_MILESTONE name=<[a-z0-9_]+>` then `key=value` tokens in
sorted order, each value `shlex.quote`d, newlines flattened to spaces.
The generator documents pretty entries as "for eyes, never for tooling";
`launch_config` has no compact twin carrying the arguments, so this
module parses it best-effort and callers fall back when it fails.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import json
import re
import shlex
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

__all__ = [
    "JOB_NOT_FOUND_HINT",
    "DataflowJobs",
    "GcpApiError",
    "JobNotFoundError",
    "LogMilestones",
    "make_session",
    "parse_milestone_fields",
    "parse_pretty_milestone",
]

_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
_DATAFLOW_BASE = "https://dataflow.googleapis.com/v1b3"
_LOGGING_URL = "https://logging.googleapis.com/v2/entries:list"

_HTTP_OK = 200
_HTTP_FORBIDDEN = 403
_HTTP_NOT_FOUND = 404
_RETRYABLE = frozenset({429, 500, 502, 503, 504})
_TIMEOUT_S = 60

_TERMINAL_STATES = frozenset({
    "JOB_STATE_DONE",
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_DRAINED",
    "JOB_STATE_UPDATED",
})
# Infrastructure identifiers with no analytical value (the probe's
# `_PARAM_DROP_KEYS`): never carried into generation_params.
_PARAM_DROP_KEYS = frozenset({
    "dataflow_kms_key",
    "subnetwork",
    "network",
    "use_network_tags",
    "use_network_tags_for_flex_templates",
    "service_account_email",
    "impersonate_service_account",
})
_DISPLAY_VALUE_FIELDS = (
    "value",
    "strValue",
    "int64Value",
    "boolValue",
    "floatValue",
    "timestampValue",
    "durationValue",
    "javaClassValue",
    "shortStrValue",
)

_PROJECT_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
_REGION_RE = re.compile(r"[a-z]+(?:-[a-z0-9]+)+")
_JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MILESTONE_NAME_RE = re.compile(r"[a-z0-9_]+")
_LOG_NAME_RE = re.compile(r"projects/[a-z0-9-]+/logs/[A-Za-z0-9._%/-]+")
_RFC3339_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})")

_HEADER_RE = re.compile(r"SDFB_MILESTONE name=(?P<name>[a-z0-9_]+)"
                        r"(?P<fields>[^\n]*)")
# A new log record: the launcher's `%(asctime)s [%(levelname)s] …` prefix,
# or a milestone line printed bare.
_RECORD_START_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
                              r"|SDFB_MILESTONE name=")

_WINDOW_PAD_BEFORE_S = 120
_WINDOW_PAD_AFTER_S = 180


class GcpApiError(RuntimeError):
  """A Google REST call failed after retries (or with a non-retryable
  status other than 403/404)."""


class JobNotFoundError(LookupError):
  """The Dataflow job does not exist in the project/region asked."""


JOB_NOT_FOUND_HINT = (
    "A job id is only visible in the region it ran in: pass --region "
    "<region> (for example --region us-central1). Dataflow also keeps a "
    "job's metadata only for its retention period (about 30 days after the "
    "job ends); for an older job pass the tables and parameters manually.")


def make_session(project: str) -> requests.Session:
  """An authorised session: ADC, refreshed on use, quota billed to
  `project` through `x-goog-user-project`.

  Raises:
    RuntimeError: Application Default Credentials are missing or cannot
      mint a token, with the commands that fix it.
  """
  import google.auth  # pylint: disable=import-outside-toplevel  # credentials only when a live session is asked for
  from google.auth.exceptions import GoogleAuthError  # pylint: disable=import-outside-toplevel  # same
  from google.auth.transport.requests import AuthorizedSession, Request  # pylint: disable=import-outside-toplevel  # same

  try:
    credentials, _ = google.auth.default(scopes=list(_SCOPES))
    credentials.refresh(Request())
  except GoogleAuthError as exc:
    raise RuntimeError(
        "Application Default Credentials are not usable "
        f"({type(exc).__name__}: {exc}). Run:\n"
        "  gcloud auth application-default login\n"
        f"  gcloud auth application-default set-quota-project {project}"
    ) from exc
  session = AuthorizedSession(credentials)
  session.headers.update({"x-goog-user-project": project})
  return session


def _error_message(payload: Any, status: int) -> str:
  if isinstance(payload, Mapping):
    error = payload.get("error")
    if isinstance(error, Mapping) and error.get("message"):
      return str(error["message"])
  return f"HTTP {status}"


def _decoded(response: Any, what: str) -> dict:
  status = int(response.status_code)
  try:
    payload = response.json()
  except ValueError:
    payload = None
  if status == _HTTP_OK and isinstance(payload, dict):
    return payload
  message = f"{what}: HTTP {status}: {_error_message(payload, status)}"
  if status == _HTTP_FORBIDDEN:
    raise PermissionError(message)
  if status == _HTTP_NOT_FOUND:
    raise LookupError(message)
  raise GcpApiError(message)


@dataclass(frozen=True)
class _Retry:
  """Exponential backoff: `backoff_seconds`, doubling per retry, at most
  `max_retries` retries."""
  max_retries: int
  backoff_seconds: float
  sleep: Callable[[float], None]


def _request_json(send: Callable[[], Any], *, what: str, retry: _Retry) -> dict:
  """One REST call, retried with backoff on 429/5xx and dropped
  connections; 403 → `PermissionError`, 404 → `LookupError`, any other
  failure → `GcpApiError`."""
  delay = retry.backoff_seconds
  for attempt in range(retry.max_retries + 1):
    last = attempt == retry.max_retries
    try:
      response = send()
    except (requests.ConnectionError, requests.Timeout) as exc:
      if last:
        raise GcpApiError(f"{what}: {type(exc).__name__}: {exc}") from exc
    else:
      if int(response.status_code) not in _RETRYABLE or last:
        return _decoded(response, what)
    retry.sleep(delay)
    delay *= 2
  raise GcpApiError(f"{what}: max_retries must be ≥ 0, got {retry.max_retries}")


def _check(pattern: re.Pattern[str], value: str, what: str) -> str:
  if not isinstance(value, str) or pattern.fullmatch(value) is None:
    raise ValueError(f"unsafe or malformed {what}: {value!r}")
  return value


def _shift(ts: str | None, seconds: int) -> str | None:
  if not ts:
    return None
  moment = datetime.fromisoformat(_check(_RFC3339_RE, ts, "timestamp"))
  moment = (moment + timedelta(seconds=seconds)).astimezone(UTC)
  return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class DataflowJobs:
  """Dataflow `jobs.get` for one project/region, plus pure readers over
  the returned job resource."""

  def __init__(self,
               session: Any,
               project: str,
               region: str,
               *,
               max_retries: int = 5,
               backoff_seconds: float = 2.0,
               sleep: Callable[[float], None] = time.sleep):
    self._session = session
    self.project = _check(_PROJECT_RE, project, "project id")
    self.region = _check(_REGION_RE, region, "Dataflow region")
    self._retry = _Retry(max_retries, backoff_seconds, sleep)

  def get(self, job_id: str) -> dict:
    """The job resource (`JOB_VIEW_ALL`).

    Raises:
      JobNotFoundError: no such job in this project/region, naming the
        region used and the `--region` flag.
      PermissionError: the caller lacks `roles/dataflow.viewer`.
    """
    _check(_JOB_ID_RE, job_id, "Dataflow job id")
    url = (f"{_DATAFLOW_BASE}/projects/{self.project}/locations/"
           f"{self.region}/jobs/{job_id}")
    try:
      return _request_json(
          lambda: self._session.get(
              url, params={"view": "JOB_VIEW_ALL"}, timeout=_TIMEOUT_S),
          what=f"Dataflow jobs.get {job_id}",
          retry=self._retry)
    except PermissionError as exc:
      raise PermissionError(
          f"{exc} — reading a Dataflow job needs roles/dataflow.viewer on "
          f"project {self.project}") from exc
    except LookupError as exc:
      raise JobNotFoundError(
          f"Dataflow job {job_id} was not found in project {self.project}, "
          f"region {self.region}. {JOB_NOT_FOUND_HINT} ({exc})") from exc

  @staticmethod
  def params(job: Mapping[str, Any]) -> dict[str, Any]:
    """Launch parameters from the job's display data.

    `environment.sdkPipelineOptions.display_data` (plain `value`) is read
    before `pipelineDescription.displayData` (typed `strValue`,
    `int64Value`, …); the first occurrence of a key wins. Infrastructure
    identifiers (network, service account, KMS key) are dropped.
    """
    entries: list[Any] = []
    environment = job.get("environment") or {}
    entries.extend(
        (environment.get("sdkPipelineOptions") or {}).get("display_data") or [])
    entries.extend((job.get("pipelineDescription") or {}).get("displayData") or
                   [])
    params: dict[str, Any] = {}
    for entry in entries:
      if not isinstance(entry, Mapping):
        continue
      key = entry.get("key")
      if not key or key in params or key in _PARAM_DROP_KEYS:
        continue
      for field in _DISPLAY_VALUE_FIELDS:
        if entry.get(field) is not None:
          params[str(key)] = entry[field]
          break
    return params

  @staticmethod
  def window(job: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """(create time, end time); the end is `None` until the job reaches a
    terminal state."""
    end = (
        job.get("currentStateTime")
        if job.get("currentState") in _TERMINAL_STATES else None)
    return job.get("createTime"), end

  @staticmethod
  def labels(job: Mapping[str, Any]) -> dict[str, str]:
    """The job's user labels."""
    return dict(job.get("labels") or {})


def _entry_text(entry: Mapping[str, Any]) -> str:
  if "textPayload" in entry:
    return str(entry["textPayload"])
  payload = entry.get("jsonPayload") or {}
  return str(payload.get("message") or "")


class LogMilestones:
  """`SDFB_MILESTONE` records of one Dataflow job from Cloud Logging.

  Args:
    session: an authorised session (`make_session`) or a fake.
    project: the project whose logs are read.
    max_pages: page budget PER LIST CALL; hitting it is recorded in
      `warnings` rather than silently truncating.
    page_size: entries per page.
    max_retries / backoff_seconds / sleep: the 429/5xx backoff (delays
      `backoff_seconds`, doubling per retry).

  `warnings` accumulates every truncation or reassembly problem met, for
  the caller to surface.
  """

  def __init__(self,
               session: Any,
               project: str,
               *,
               max_pages: int = 10,
               page_size: int = 1000,
               max_retries: int = 5,
               backoff_seconds: float = 2.0,
               sleep: Callable[[float], None] = time.sleep):
    if max_pages < 1 or page_size < 1:
      raise ValueError("max_pages and page_size must be ≥ 1")
    self._session = session
    self.project = _check(_PROJECT_RE, project, "project id")
    self.max_pages = max_pages
    self.page_size = page_size
    self._retry = _Retry(max_retries, backoff_seconds, sleep)
    self.warnings: list[str] = []

  def find(self, job_id: str, name: str,
           window: tuple[str | None, str | None]) -> list[str]:
    """Every `name` milestone the job logged, oldest first, each as its
    full record text (header line, then any body lines).

    Raises:
      PermissionError: the caller lacks `roles/logging.viewer`.
      GcpApiError: Cloud Logging kept failing after retries.
      ValueError: `job_id`, `name` or the window is not safe to put in a
        filter.
    """
    _check(_JOB_ID_RE, job_id, "Dataflow job id")
    _check(_MILESTONE_NAME_RE, name, "milestone name")
    lo = _shift(window[0], -_WINDOW_PAD_BEFORE_S)
    hi = _shift(window[1], _WINDOW_PAD_AFTER_S)
    worker_log = (f"projects/{self.project}/logs/"
                  "dataflow.googleapis.com%2Fworker")
    launcher_log = (f"projects/{self.project}/logs/"
                    "dataflow.googleapis.com%2Flauncher")
    clauses = [
        f'resource.labels.job_id="{job_id}"',
        (f'(logName="{worker_log}" OR logName="{launcher_log}" OR '
         'resource.type="dataflow_step")'),
        (f'(textPayload:"SDFB_MILESTONE name={name}" OR '
         f'jsonPayload.message:"SDFB_MILESTONE name={name}")'),
    ]
    if lo:
      clauses.append(f'timestamp>="{lo}"')
    if hi:
      clauses.append(f'timestamp<="{hi}"')
    exact = re.compile(rf"SDFB_MILESTONE name={name}(?:\s|$)")
    texts = []
    for entry in self._entries(" ".join(clauses), what=f"{name} headers"):
      text = _entry_text(entry)
      if not exact.search(text):
        continue
      if "\n" not in text and entry.get("logName") != worker_log:
        text = self._reassemble(entry, text, job_id=job_id, hi=hi)
      texts.append(text)
    return texts

  def _entries(self, log_filter: str, *, what: str) -> Iterator[dict]:
    body: dict[str, Any] = {
        "resourceNames": [f"projects/{self.project}"],
        "filter": log_filter,
        "orderBy": "timestamp asc",
        "pageSize": self.page_size,
    }
    for _ in range(self.max_pages):
      try:
        page = _request_json(
            lambda: self._session.post(
                _LOGGING_URL, json=dict(body), timeout=_TIMEOUT_S),
            what=f"Cloud Logging entries.list ({what})",
            retry=self._retry)
      except PermissionError as exc:
        raise PermissionError(
            f"{exc} — reading Dataflow job logs needs roles/logging.viewer "
            f"on project {self.project}") from exc
      yield from page.get("entries") or []
      token = page.get("nextPageToken")
      if not token:
        return
      body["pageToken"] = token
    self.warnings.append(
        f"Cloud Logging ({what}): stopped after {self.max_pages} pages of "
        f"{self.page_size} entries; later entries were not read")

  def _reassemble(self, header: Mapping[str, Any], text: str, *, job_id: str,
                  hi: str | None) -> str:
    """Header text + the body lines the launcher logged after it."""
    log_name = _check(_LOG_NAME_RE, str(header.get("logName", "")), "logName")
    start = _check(_RFC3339_RE, str(header.get("timestamp", "")), "timestamp")
    clauses = [
        f'logName="{log_name}"',
        f'resource.labels.job_id="{job_id}"',
        f'timestamp>="{start}"',
    ]
    if hi:
      clauses.append(f'timestamp<="{hi}"')
    lines = [text]
    seen_header = False
    json_body: bool | None = None  # decided by the first body line
    for entry in self._entries(" ".join(clauses), what="continuation"):
      if not seen_header:
        seen_header = entry.get("insertId") == header.get("insertId")
        continue
      line = _entry_text(entry)
      if _RECORD_START_RE.search(line):
        break
      lines.append(line)
      if json_body is None:
        json_body = line == "{"
      elif json_body and line == "}":
        break  # indent-2 JSON closes its top-level object at column 0
    if not seen_header:
      insert_id = header.get("insertId")
      self.warnings.append(f"could not re-read the log lines after {insert_id} "
                           f"({log_name}); the record is the header line alone")
    return "\n".join(lines)


def _header(text: str) -> re.Match[str]:
  match = _HEADER_RE.search(text or "")
  if match is None:
    raise ValueError("no SDFB_MILESTONE header in the text")
  return match


def parse_milestone_fields(text: str) -> dict[str, str]:
  """The `name` and `key=value` fields of a milestone's header line.

  `text` may carry a logging prefix and body lines; only the first
  `SDFB_MILESTONE` header is read. Values are unquoted exactly as
  `format_milestone` quoted them (`shlex`); tokens without `=` are skipped,
  as the generator's own `parse_milestone` does.

  Raises:
    ValueError: no header, or quoting that does not balance.
  """
  match = _header(text)
  name = match.group("name")
  try:
    tokens = shlex.split(match.group("fields"))
  except ValueError as exc:
    raise ValueError(f"milestone {name}: unbalanced quoting ({exc})") from exc
  out = {"name": name}
  for token in tokens:
    key, sep, value = token.partition("=")
    if sep:
      out[key] = value
  return out


def parse_pretty_milestone(text: str) -> tuple[dict[str, str], dict]:
  """(header fields, JSON body) of a `log_milestone_pretty` record.

  Raises:
    ValueError: no header, no body, a body that is not one complete JSON
      object (truncated, reordered or interleaved lines) — callers treat
      this as "fall back to another source".
  """
  fields = parse_milestone_fields(text)
  body = text[_header(text).end():].strip()
  name = fields["name"]
  if not body:
    raise ValueError(f"milestone {name}: no JSON body after the header")
  try:
    payload, _ = json.JSONDecoder().raw_decode(body)
  except json.JSONDecodeError as exc:
    raise ValueError(
        f"milestone {name}: body is not valid JSON ({exc})") from exc
  if not isinstance(payload, dict):
    raise ValueError(f"milestone {name}: body is not a JSON object")
  return fields, payload
