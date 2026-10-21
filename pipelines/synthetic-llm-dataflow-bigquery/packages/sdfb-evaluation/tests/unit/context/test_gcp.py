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
"""REST context: session, Dataflow jobs, Cloud Logging milestones and the
milestone-line parsers (`sdfb_evaluation.context.gcp`)."""

from __future__ import annotations

import json
import shlex

import google.auth
import pytest
from google.auth.exceptions import DefaultCredentialsError
from hypothesis import given
from hypothesis import strategies as st

from sdfb_evaluation.context import gcp
from sdfb_evaluation.context.gcp import (
    DataflowJobs,
    GcpApiError,
    JobNotFoundError,
    LogMilestones,
    parse_milestone_fields,
    parse_pretty_milestone,
)

PROJECT = "demo-project"
REGION = "europe-west1"
JOB_ID = "2026-09-13_06_10_16-9000000000000000017"
WINDOW = ("2026-09-13T13:10:16.512345Z", "2026-09-13T14:02:41.118204Z")


def _format_milestone(name: str, **fields) -> str:
  """The generator's `format_milestone`, restated for the round-trip test:
  sorted keys, `shlex.quote` per value, newlines flattened to spaces."""
  parts = [f"SDFB_MILESTONE name={name}"]
  for key in sorted(fields):
    value = str(fields[key]).replace("\n", " ")
    parts.append(f"{key}={shlex.quote(value)}")
  return " ".join(parts)


def _no_sleep(seconds: float) -> None:
  del seconds


# --------------------------------------------------------------------------
# milestone parsers
# --------------------------------------------------------------------------
@given(
    name=st.from_regex(r"[a-z0-9_]{1,20}", fullmatch=True),
    fields=st.dictionaries(
        st.from_regex(r"[a-z][a-z0-9_]{0,15}",
                      fullmatch=True).filter(lambda k: k != "name"),
        st.text(
            alphabet=st.characters(blacklist_categories=("Cs",)), max_size=30),
        max_size=6,
    ),
)
def test_parse_milestone_fields_round_trips_format_milestone_quoting(
    name, fields):
  line = _format_milestone(name, **fields)
  expected = {k: str(v).replace("\n", " ") for k, v in fields.items()}
  assert parse_milestone_fields(line) == {"name": name, **expected}


def test_parse_milestone_fields_skips_log_prefix_and_reads_header_line_only():
  text = ("2026-09-13 13:12:03,114 [INFO] sdfb.milestone - "
          "SDFB_MILESTONE name=launch_config note='two words' "
          "scenario=relational_closure\n{\n  \"x\": \"k=v\"\n}")
  assert parse_milestone_fields(text) == {
      "name": "launch_config",
      "note": "two words",
      "scenario": "relational_closure",
  }


@pytest.mark.parametrize("text", [
    "no milestone here",
    "SDFB_MILESTONE name=x broken='unterminated",
])
def test_parse_milestone_fields_rejects_non_milestone_text(text):
  with pytest.raises(ValueError):
    parse_milestone_fields(text)


def test_parse_pretty_milestone_rejects_missing_or_truncated_body():
  with pytest.raises(ValueError, match="no JSON body"):
    parse_pretty_milestone("SDFB_MILESTONE name=launch_config scenario=x")
  with pytest.raises(ValueError, match="launch_config"):
    parse_pretty_milestone(
        "SDFB_MILESTONE name=launch_config scenario=x\n{\n  \"a\": 1,\n")
  with pytest.raises(ValueError, match="JSON object"):
    parse_pretty_milestone("SDFB_MILESTONE name=launch_config\n[1, 2]")


def test_launch_config_pretty_entry_parsed(fake_session, fixture_data):
  expected = fixture_data("launch_config")

  # 1. One log record holding header + indent-2 body (the worker shape).
  single = ("2026-09-13 13:12:03,114 [INFO] sdfb.milestone - " +
            _format_milestone("launch_config", scenario="relational_closure") +
            "\n" +
            json.dumps(expected, indent=2, ensure_ascii=False, sort_keys=True))
  fields, payload = parse_pretty_milestone(single)
  assert fields == {"name": "launch_config", "scenario": "relational_closure"}
  assert payload == expected

  # 2. The flex-template launcher splits the record into one entry per line;
  #    `find` reassembles it from the header entry's own log stream.
  logs = LogMilestones(fake_session(), PROJECT, sleep=_no_sleep)
  texts = logs.find(JOB_ID, "launch_config", WINDOW)
  assert len(texts) == 1
  fields, payload = parse_pretty_milestone(texts[0])
  assert fields["scenario"] == "relational_closure"
  assert payload == expected
  assert payload["resolved"]["tables_in_order"] == [
      "demo-project.thelook_synthetic.users",
      "demo-project.thelook_synthetic.orders",
      "demo-project.thelook_synthetic.order_items",
  ]
  assert not logs.warnings


def test_relationships_loaded_sha_extracted(fake_session):
  logs = LogMilestones(fake_session(), PROJECT, sleep=_no_sleep)
  texts = logs.find(JOB_ID, "relationships_loaded", WINDOW)
  assert len(texts) == 1
  fields = parse_milestone_fields(texts[0])
  assert fields["sha"] == "5e1f0c2a9b3d"
  assert fields["models"] == "thelook_ecommerce"
  assert fields["uri"] == "gs://demo-bucket/synthetic/relationships/"


def test_find_returns_text_body_records_and_single_line_worker_records(
    fake_session):
  logs = LogMilestones(fake_session(), PROJECT, sleep=_no_sleep)
  found = logs.find(JOB_ID, "model_adjustment_model", WINDOW)
  assert len(found) == 1
  adjusted = found[0]
  fields = parse_milestone_fields(adjusted)
  assert fields["uri"].endswith("thelook_ecommerce.yaml")
  assert adjusted.splitlines()[1:] == [
      "# effective model: orders pk removed (measured 0.4% repeats)",
      "model: thelook_ecommerce",
      "tables:",
      "  users:",
      "    pk: [id]",
  ]
  # jsonPayload.message and textPayload worker entries are both read.
  assert len(logs.find(JOB_ID, "relational_single_job", WINDOW)) == 1
  assert len(logs.find(JOB_ID, "fanout_bound", WINDOW)) == 1
  # An exact-name match: `launch_config` never matches `launch_scenario`.
  assert not logs.find(JOB_ID, "launch", WINDOW)


def test_find_filter_covers_worker_and_launcher_routes(fake_session):
  session = fake_session()
  LogMilestones(
      session, PROJECT, sleep=_no_sleep).find(JOB_ID, "launch_config", WINDOW)
  method, url, body = session.calls[0]
  assert method == "POST" and url.endswith("/v2/entries:list")
  assert body["resourceNames"] == [f"projects/{PROJECT}"]
  assert body["orderBy"] == "timestamp asc"
  log_filter = body["filter"]
  assert f'resource.labels.job_id="{JOB_ID}"' in log_filter
  # The worker logName (e2e_gcp_probe) OR the dataflow_step resource route
  # (the FK/PK validator's recipe, which is where launcher entries land).
  assert (f'logName="projects/{PROJECT}/logs/dataflow.googleapis.com%2Fworker"'
          in log_filter)
  assert 'resource.type="dataflow_step"' in log_filter
  assert ('logName="projects/demo-project/logs/'
          'dataflow.googleapis.com%2Flauncher"') in log_filter
  # Window padded: create - 120 s … end + 180 s.
  assert 'timestamp>="2026-09-13T13:08:16Z"' in log_filter
  assert 'timestamp<="2026-09-13T14:05:41Z"' in log_filter
  # The continuation read stays on the header entry's own stream.
  _, _, follow = session.calls[1]
  assert ('logName="projects/demo-project/logs/'
          'dataflow.googleapis.com%2Flauncher"') in follow["filter"]
  assert "SDFB_MILESTONE" not in follow["filter"]


def test_find_window_offsets_are_normalised_to_utc(fake_session):
  session = fake_session()
  LogMilestones(
      session, PROJECT,
      sleep=_no_sleep).find(JOB_ID, "relationships_loaded",
                            ("2026-09-13T15:10:16.512345+02:00",
                             "2026-09-13T16:02:41.118204+02:00"))
  log_filter = session.calls[0][2]["filter"]
  assert 'timestamp>="2026-09-13T13:08:16Z"' in log_filter
  assert 'timestamp<="2026-09-13T14:05:41Z"' in log_filter


def test_find_open_window_has_no_upper_bound(fake_session):
  session = fake_session()
  LogMilestones(
      session, PROJECT, sleep=_no_sleep).find(JOB_ID, "relationships_loaded",
                                              (WINDOW[0], None))
  assert "timestamp<=" not in session.calls[0][2]["filter"]


def test_find_backs_off_on_429_and_5xx_then_succeeds(fake_session):
  slept: list[float] = []
  session = fake_session(status_script=[429, 503, 200])
  logs = LogMilestones(
      session, PROJECT, sleep=slept.append, backoff_seconds=1.5, max_retries=3)
  assert len(logs.find(JOB_ID, "relationships_loaded", WINDOW)) == 1
  assert slept == [1.5, 3.0]


def test_find_gives_up_after_max_retries(fake_session):
  slept: list[float] = []
  session = fake_session(status_script=[500, 500, 500])
  logs = LogMilestones(session, PROJECT, sleep=slept.append, max_retries=2)
  with pytest.raises(GcpApiError, match="500"):
    logs.find(JOB_ID, "relationships_loaded", WINDOW)
  assert len(slept) == 2


def test_find_paging_is_bounded_and_says_so(fake_session, fixture_data):
  entries = fixture_data("log_entries")["entries"]
  header = next(e for e in entries
                if "name=relationships_loaded" in e["jsonPayload"]["message"])
  many = []
  for i in range(5):
    clone = dict(
        header, insertId=f"dup{i}", timestamp=f"2026-09-13T13:12:0{i}.500000Z")
    many.append(clone)
  logs = LogMilestones(
      fake_session(entries=many),
      PROJECT,
      sleep=_no_sleep,
      max_pages=2,
      page_size=2)
  assert len(logs.find(JOB_ID, "relationships_loaded", WINDOW)) == 4
  assert any("2 pages" in w for w in logs.warnings)


def test_find_permission_denied_names_the_logging_role(fake_session):
  logs = LogMilestones(
      fake_session(status_script=[403]), PROJECT, sleep=_no_sleep)
  with pytest.raises(PermissionError, match=r"roles/logging\.viewer"):
    logs.find(JOB_ID, "launch_config", WINDOW)


@pytest.mark.parametrize("job_id,name", [
    ('x" OR logName:"', "launch_config"),
    (JOB_ID, 'launch_config" OR "'),
    (JOB_ID, "Launch-Config"),
])
def test_find_rejects_unsafe_filter_inputs(fake_session, job_id, name):
  logs = LogMilestones(fake_session(), PROJECT, sleep=_no_sleep)
  with pytest.raises(ValueError):
    logs.find(job_id, name, WINDOW)


# --------------------------------------------------------------------------
# Dataflow jobs
# --------------------------------------------------------------------------
def test_dataflow_jobs_get_params_window_labels(fake_session):
  session = fake_session()
  jobs = DataflowJobs(session, PROJECT, REGION, sleep=_no_sleep)
  job = jobs.get(JOB_ID)
  method, url, params = session.calls[0]
  assert method == "GET"
  assert url.endswith(
      f"/v1b3/projects/{PROJECT}/locations/{REGION}/jobs/{JOB_ID}")
  assert params == {"view": "JOB_VIEW_ALL"}
  assert jobs.window(job) == WINDOW
  assert jobs.labels(job) == {
      "goog-dataflow-provided-template-type": "flex",
      "sdfb_run": "thelook-0913",
  }
  params = jobs.params(job)
  # sdkPipelineOptions.display_data first; typed displayData second; first
  # occurrence of a key wins.
  assert params["engine"] == "b1_rag"
  assert params["num_rows"] == "5000"
  assert params["similarity"] == 0.5
  assert params["generate_fk_relationships"] is True
  assert params["landing_table"] == "demo-project.thelook_synthetic.order_items"
  # Infra identifiers are never carried into generation_params.
  assert "service_account_email" not in params
  assert "subnetwork" not in params


def test_window_of_a_running_job_is_open(fixture_data):
  job = dict(fixture_data("dataflow_job"), currentState="JOB_STATE_RUNNING")
  assert DataflowJobs.window(job) == (WINDOW[0], None)


def test_dataflow_job_not_found_names_region(fake_session):
  jobs = DataflowJobs(fake_session(), PROJECT, "us-central1", sleep=_no_sleep)
  with pytest.raises(JobNotFoundError) as info:
    jobs.get(JOB_ID)
  message = str(info.value)
  assert "us-central1" in message and "--region" in message
  assert JOB_ID in message


def test_dataflow_permission_denied_names_the_dataflow_role(fake_session):
  jobs = DataflowJobs(
      fake_session(status_script=[403]), PROJECT, REGION, sleep=_no_sleep)
  with pytest.raises(PermissionError, match=r"roles/dataflow\.viewer"):
    jobs.get(JOB_ID)


def test_dataflow_rejects_unsafe_job_id(fake_session):
  jobs = DataflowJobs(fake_session(), PROJECT, REGION, sleep=_no_sleep)
  with pytest.raises(ValueError):
    jobs.get("../../other")


# --------------------------------------------------------------------------
# session
# --------------------------------------------------------------------------
class _FakeCredentials:
  token = "fake-token"

  def __init__(self):
    self.refreshed = False

  def refresh(self, request) -> None:
    del request
    self.refreshed = True


def test_make_session_uses_adc_and_quota_project(monkeypatch):
  creds = _FakeCredentials()
  seen: dict = {}

  def _default(scopes=None):
    seen["scopes"] = scopes
    return creds, "adc-project"

  monkeypatch.setattr(google.auth, "default", _default)
  session = gcp.make_session(PROJECT)
  assert creds.refreshed
  assert seen["scopes"] and "cloud-platform" in seen["scopes"][0]
  assert session.headers["x-goog-user-project"] == PROJECT


def test_make_session_without_adc_gives_the_login_hint(monkeypatch):

  def _default(scopes=None):
    raise DefaultCredentialsError("no ADC")

  monkeypatch.setattr(google.auth, "default", _default)
  with pytest.raises(
      RuntimeError, match="gcloud auth application-default login"):
    gcp.make_session(PROJECT)
