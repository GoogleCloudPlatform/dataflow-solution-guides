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
"""Tests for `sdfb-eval run` as a driver (Task 27, Rulings R88, R90, R93):
the registry events it writes around the pipeline and who owns the one
terminal row, the exit codes, the `--fail_on` gate, the free-text pools
read, the temporary directory and the flex-template entry.

The pipeline here is a stand-in (`helpers.tiny_pipeline`: only a FINAL
registry row, or nothing) and, for a submitted job, a result whose state
the test controls (`helpers.FakeJob`); the real graph runs in
`test_run.py`.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import json
import re
from types import SimpleNamespace
from typing import Any

import pytest
from apache_beam.options.pipeline_options import (
    DebugOptions,
    GoogleCloudOptions,
    SetupOptions,
    StandardOptions,
    WorkerOptions,
)
from apache_beam.runners.dataflow.dataflow_runner import DataflowPipelineResult
from unit.context.plan_fakes import (
    JOB_ID,
    PROJECT,
    QDS,
    thelook_launch,
    thelook_models,
)

from sdfb_evaluation.beam.census import FreeTextPool
from sdfb_evaluation.cli import driver, gate, run_evaluation
from sdfb_evaluation.cli.main import main, parse_args
from sdfb_evaluation.context.bq import BqApiError
from sdfb_evaluation.context.gcp import JobNotFoundError
from sdfb_evaluation.context.relationships import RelationshipError
from sdfb_evaluation.context.plan import (
    Knobs,
    PlanError,
    PrepareStatement,
    build_plan,
)

from .conftest import catalogue
from .helpers import (
    NOW,
    REGISTRY,
    FakeJob,
    FakeResult,
    check_row,
    make_env,
    tiny_pipeline,
    write_stand_in,
)

REGION = "europe-west1"
TARGET = ["--project", PROJECT, "--region", REGION, "--job_id", JOB_ID]
DATAFLOW = [
    *TARGET, "--runner", "DataflowRunner", "--temp_location",
    "gs://demo-bucket/tmp"
]
LABEL_KEY_URI = "projects/demo-project/secrets/sdfb-eval-label/versions/3"
POOLS_TABLE = f"{PROJECT}.synthetic_rag.freetext_pools"


def _statuses(bq) -> list[str]:
  return [row["status"] for row in bq.registry_rows()]


def _holder(options):
  """A pipeline that is only a holder of its options: making a real one
  with Dataflow options makes Beam ask Cloud Storage about the bucket."""
  return SimpleNamespace(options=options)


# --------------------------------------------------------------------------
# the registry events around the pipeline
# --------------------------------------------------------------------------
def test_run_passes_the_prepared_plan_to_options_and_pipeline(
    bq, resolved, monkeypatch, capsys):
  seen: dict[str, Any] = {}
  real_prepare, real_defaults = (driver.prepare_evaluation,
                                 driver.pipeline_options_defaults)

  def prepare(plan, client):
    seen["prepared"] = real_prepare(plan, client)
    return seen["prepared"]

  def defaults(runner, plan=None):
    seen["defaults_plan"] = plan
    return real_defaults(runner, plan)

  build = tiny_pipeline()
  monkeypatch.setattr(driver, "prepare_evaluation", prepare)
  monkeypatch.setattr(driver, "pipeline_options_defaults", defaults)
  monkeypatch.setattr(driver, "build_evaluation_pipeline", build)
  submitted: list[Any] = []

  def submit(pipeline):
    submitted.append(pipeline)
    return driver.submit_pipeline(pipeline)

  code = main(["run", *TARGET, "--label_key_uri", LABEL_KEY_URI],
              make_env(bq, submit=submit))
  assert code == 0
  assert resolved[0]["job_id"] == JOB_ID
  assert (resolved[0]["project"], resolved[0]["region"]) == (PROJECT, REGION)
  assert len(build.built) == 1
  built_plan, kwargs = build.built[0]
  assert built_plan is seen["prepared"] is seen["defaults_plan"]
  assert built_plan.label_key_uri == LABEL_KEY_URI
  assert kwargs["stats_query"] == bq.query
  assert len(submitted) == 1
  options = submitted[0].options
  cache = options.view_as(WorkerOptions).max_cache_memory_usage_mb
  assert cache == real_defaults("DirectRunner",
                                built_plan)["max_cache_memory_usage_mb"]
  assert options.view_as(SetupOptions).save_main_session is False
  capsys.readouterr()


def test_the_driver_logs_the_steps_around_planning(bq, resolved, monkeypatch,
                                                   caplog, capsys):
  """The launch lookup before planning and the RUNNING row, the prepare
  statements, the read path and the submission after it say how long
  they took, in the planner's line shape; the planner's own lines sit
  between them. The submission's line follows the four steps it is the
  sum of (`test_read_path.py`)."""
  del resolved
  monkeypatch.setattr(driver, "build_evaluation_pipeline",
                      tiny_pipeline(fast=True))
  with caplog.at_level("INFO", logger="sdfb_evaluation"):
    assert main(["run", *TARGET], make_env(bq, submit=write_stand_in)) == 0
  capsys.readouterr()
  lines = [r.getMessage() for r in caplog.records]
  steps = [
      m["step"] for m in (re.fullmatch(
          r"evaluation (?P<id>\S+): (?P<step>[A-Za-z ]+) "
          r"(?P<seconds>\d+\.\d+)s", line) for line in lines) if m
  ]
  assert steps == [
      "target check", "launch lookup", "relationship models", "RUNNING row",
      "prepare statements", "read path", "free text pools", "pipeline options",
      "graph", "pipeline run", "submission"
  ]
  planning = [i for i, line in enumerate(lines) if line.startswith("planning ")]
  running = lines.index(next(x for x in lines if "RUNNING row" in x))
  assert planning and max(planning) < running  # planning finished first


def test_progress_lines_reach_stderr_once_the_command_asks_for_them(capsys):
  """Python drops INFO records without a handler: the console log of a
  launch would never show the planner's lines."""
  import logging  # pylint: disable=import-outside-toplevel  # only here

  from sdfb_evaluation.cli.main import show_progress  # pylint: disable=import-outside-toplevel  # only here
  show_progress()
  show_progress()  # idempotent: one handler
  logging.getLogger("sdfb_evaluation.context.plan").info("planning x: probe")
  err = capsys.readouterr().err
  assert err.count("planning x: probe") == 1


def test_a_fresh_evaluation_id_per_attempt(bq, resolved, monkeypatch, capsys):
  monkeypatch.setattr(driver, "build_evaluation_pipeline",
                      tiny_pipeline(fast=True))
  env = make_env(bq, submit=write_stand_in)
  assert main(["run", *TARGET], env) == 0
  assert main(["run", *TARGET], env) == 0
  ids = [row["evaluation_id"] for row in bq.registry_rows()]
  first, second = ids[0], ids[-1]
  assert first != second  # the same launch, the same second: two attempts
  for evaluation_id in (first, second):
    assert driver.EVALUATION_ID_RE.fullmatch(evaluation_id)
    assert evaluation_id.startswith("eval-20260914T080000Z-")
  assert ids == [first, first, second, second]  # RUNNING, FINAL each
  # the temp tables an attempt creates carry its own id
  created = [sql for sql, _ in bq.executed if sql.startswith("CREATE")]
  assert any(first in sql for sql in created)
  assert any(second in sql for sql in created)
  assert len(resolved) == 2
  out = capsys.readouterr().out
  assert first in out and second in out
  assert re.fullmatch(r"eval-\d{8}T\d{6}Z-[0-9a-f]{8}",
                      driver.mint_evaluation_id(NOW, "0a1b2c3d"))
  assert driver.mint_evaluation_id(driver.Env().now(),
                                   driver.Env().token()) != (
                                       driver.mint_evaluation_id(
                                           driver.Env().now(),
                                           driver.Env().token()))


def test_run_writes_running_prepares_runs_then_loads_final_last(
    bq, resolved, monkeypatch, capsys):
  del resolved
  monkeypatch.setattr(
      driver, "build_evaluation_pipeline",
      tiny_pipeline(fast=True, counts={
          "total": 3,
          "pass": 2,
          "warn": 1
      }))
  code = main(["run", *TARGET], make_env(bq, submit=write_stand_in))
  assert code == 0
  running, final = bq.registry_rows()
  for row in (running, final):
    check_row(REGISTRY, row, ordered=row is running)
  assert (running["event"], running["status"]) == ("RUNNING", "RUNNING")
  assert (final["event"], final["status"]) == ("FINAL", "SUCCEEDED")
  assert running["evaluation_id"] == final["evaluation_id"]
  assert final["recorded_at"] > running["recorded_at"]
  assert running["trigger"] == "cli" and running["runner"] == "DirectRunner"
  assert running["mode"] == "sampled"  # the DirectRunner default
  assert running["evaluation_params"]["label_key_mode"] == "ephemeral"
  assert running["generation_job_id"] == JOB_ID
  # RUNNING is loaded before any prepare DDL; the registry's FINAL last
  kinds = [kind for kind, _ in bq.events]
  first_load = kinds.index("load")
  assert all(kind != "execute" for kind in kinds[:first_load])
  assert "execute" in kinds[first_load:]
  assert kinds[-1] == "load" and kinds.count("load") == 2
  assert all(fqn == f"{QDS}.{REGISTRY}" for fqn, _ in bq.loads)
  out = capsys.readouterr().out
  assert final["evaluation_id"] in out and "SUCCEEDED" in out


def test_a_driver_exception_writes_failed_and_exits_3(bq, resolved, stub,
                                                      capsys):
  """R93-5: `driver.run` writes FAILED and re-raises; `main` prints the
  traceback and exits 3 — never 1, which is the gate's alone."""
  del resolved, stub

  def explode(pipeline):
    del pipeline
    raise RuntimeError("worker pool exhausted in europe-west1")

  env = make_env(bq, submit=explode)
  argv = ["run", *TARGET, "--label_key_uri", LABEL_KEY_URI, "--fail_on", "warn"]
  assert main(argv, env) == 3
  running, failed = bq.registry_rows()
  check_row(REGISTRY, running)
  check_row(REGISTRY, failed)
  assert (running["event"], running["status"]) == ("RUNNING", "RUNNING")
  assert (failed["event"], failed["status"]) == ("FINAL", "FAILED")
  assert failed["status_reason"] == (
      "RuntimeError: worker pool exhausted in europe-west1")
  assert failed["evaluation_id"] == running["evaluation_id"]
  assert failed["recorded_at"] > running["recorded_at"]
  assert failed["finished_at"] == failed["recorded_at"]
  assert failed["metrics_total"] is None and failed["overall_score"] is None
  assert [t["name"] for t in failed["tables"]
         ] == ["products", "users", "orders", "order_items"]
  captured = capsys.readouterr()
  assert "Traceback (most recent call last)" in captured.err
  assert "RuntimeError: worker pool exhausted in europe-west1" in captured.err
  assert LABEL_KEY_URI not in captured.out + captured.err
  # the driver itself re-raises (the flex entry lets it through)
  with pytest.raises(RuntimeError, match="worker pool exhausted"):
    driver.run(*parse_args(["run", *TARGET]), env)
  with pytest.raises(RuntimeError, match="worker pool exhausted"):
    run_evaluation.main(TARGET, env)
  assert _statuses(bq) == ["RUNNING", "FAILED"] * 3
  capsys.readouterr()


def test_an_interrupt_before_submission_is_failed_and_stays_an_interrupt(
    bq, resolved, stub):
  """No job is running yet: the driver owns the outcome. KeyboardInterrupt
  keeps its conventional behaviour (`main` does not turn it into 3)."""
  del resolved, stub

  def interrupted(pipeline):
    del pipeline
    raise KeyboardInterrupt

  with pytest.raises(KeyboardInterrupt):
    main(["run", *TARGET], make_env(bq, submit=interrupted))
  assert _statuses(bq) == ["RUNNING", "FAILED"]
  assert bq.registry_rows()[-1]["status_reason"] == "KeyboardInterrupt: "


def test_a_transient_prepare_error_writes_failed_then_propagates(
    bq, resolved, stub, capsys):
  """R90(g): whatever `prepare_evaluation` raises sits inside the
  driver's FAILED-then-re-raise path — one FAILED row, then the error."""
  del resolved
  # sampled mode with a small --sample_rows plans sample tables; their
  # CTAS is no source pin, so its failure is never degraded
  bq.execute_failures["FARM_FINGERPRINT"] = BqApiError(
      "DDL: 503 backend error", status=503)
  args, extras = parse_args(["run", *TARGET, "--sample_rows", "50"])
  with pytest.raises(BqApiError, match="503 backend error"):
    driver.run(args, extras, make_env(bq))
  running, failed = bq.registry_rows()
  assert running["status"] == "RUNNING"
  assert (failed["event"], failed["status"]) == ("FINAL", "FAILED")
  assert failed["status_reason"].startswith("BqApiError: DDL: 503")
  check_row(REGISTRY, failed)
  assert stub.built == []  # the pipeline was never built
  # a transient error on the source pin itself raises too (it is only a
  # refusal that degrades the pin)
  bq.loads.clear()
  bq.execute_failures.clear()
  bq.execute_failures["CREATE SNAPSHOT TABLE"] = BqApiError(
      "DDL: 503 the source pin could not be created", status=503)
  assert main(["run", *TARGET], make_env(bq)) == 3
  assert _statuses(bq) == ["RUNNING", "FAILED"]
  assert "source pin could not be created" in capsys.readouterr().err
  assert stub.built == []


def test_a_pipeline_that_ends_without_a_final_row_is_failed(
    bq, resolved, stub, capsys):
  del resolved, stub
  # built, never run, reported DONE: no FINAL row was written
  assert main(["run", *TARGET], make_env(bq)) == 3
  assert _statuses(bq) == ["RUNNING", "FAILED"]
  assert "FINAL" in bq.registry_rows()[-1]["status_reason"]
  assert "FINAL registry row" in capsys.readouterr().err


def test_a_failed_row_that_cannot_be_written_never_hides_the_error(
    bq, resolved, stub, capsys):
  del resolved, stub

  def explode(pipeline):
    del pipeline
    bq.load_failures[REGISTRY] = PermissionError("403 tables.updateData")
    raise RuntimeError("the pipeline failed first")

  assert main(["run", *TARGET], make_env(bq, submit=explode)) == 3
  assert _statuses(bq) == ["RUNNING"]
  err = capsys.readouterr().err
  assert "FAILED registry row could not be written" in err
  assert "403 tables.updateData" in err
  assert "RuntimeError: the pipeline failed first" in err


def test_a_planning_failure_writes_a_schema_valid_failed_row(
    bq, monkeypatch, capsys):

  def missing(**kwargs: Any):
    job_id, region = kwargs["job_id"], kwargs["region"]
    raise JobNotFoundError(
        f"Dataflow job {job_id} was not found in region {region}")

  monkeypatch.setattr(driver, "resolve_launch", missing)
  argv = ["run", *TARGET, "--mode", "exact", "--trigger", "agent"]
  assert main(argv, make_env(bq)) == 3
  (row,) = bq.registry_rows()
  check_row(REGISTRY, row)
  assert (row["event"], row["status"]) == ("FINAL", "FAILED")
  assert row["status_reason"].startswith("JobNotFoundError: Dataflow job")
  assert driver.EVALUATION_ID_RE.fullmatch(row["evaluation_id"])
  assert row["evaluated_at"] == "2026-09-14T08:00:00.000000Z"
  assert row["recorded_at"] > row["evaluated_at"]
  assert row["finished_at"] == row["recorded_at"]
  assert (row["mode"], row["trigger"], row["runner"]) == ("exact", "agent",
                                                          "DirectRunner")
  assert row["generation_job_id"] == JOB_ID
  assert row["generation_region"] == REGION
  assert row["tables"] == [] and row["run_ids"] == []
  assert row["params_source"] is None and row["bq_bytes_processed"] is None
  assert row["evaluation_params"]["sample_rows"] == 200_000
  assert row["evaluation_params"]["label_key_mode"] == "ephemeral"
  assert row["catalogue_version"] == catalogue().version
  assert len(row["evaluation_key"]) == 32
  assert bq.executed == []  # nothing was prepared
  assert "JobNotFoundError: Dataflow job" in capsys.readouterr().err


def test_a_plan_error_keeps_what_planning_created(bq, monkeypatch, capsys):
  launch = thelook_launch(bq)
  monkeypatch.setattr(driver, "resolve_launch", lambda **kwargs: launch)
  monkeypatch.setattr(driver, "load_models", lambda uri: thelook_models())
  created = "CREATE SNAPSHOT TABLE `demo-project.synthetic_data_quality.s`"

  def refuse(**kwargs: Any):
    del kwargs
    raise PlanError("orders: the as_of_diff start snapshot already exists",
                    (PrepareStatement(created, {}),))

  monkeypatch.setattr(driver, "build_plan", refuse)
  assert main(["run", *TARGET], make_env(bq)) == 3
  (row,) = bq.registry_rows()
  check_row(REGISTRY, row)
  assert row["status"] == "FAILED"
  assert row["status_reason"].startswith("PlanError: orders")
  assert f"planning created {created}" in row["warnings"]
  # the launch did resolve: the row records it, under the planner's key
  assert row["run_ids"] == list(launch.run_ids)
  assert row["params_source"] == "jobs_labels+logs"
  assert row["relationship_model"] == "thelook"
  plan = build_plan(
      launch=launch,
      models=thelook_models(),
      bq=bq,
      knobs=Knobs(temp_dataset=QDS),
      mode="sampled",
      trigger="cli",
      runner="DirectRunner",
      now=NOW)
  assert row["evaluation_key"] == plan.evaluation_key
  capsys.readouterr()


# --------------------------------------------------------------------------
# the relationship model follows what the generation did (R120-2)
# --------------------------------------------------------------------------
def _samples_only(tmp_path) -> str:
  """A folder holding documentation samples only: the generator logs
  `relationships_absent` for it and generates without relationships."""
  folder = tmp_path / "relationships"
  folder.mkdir()
  (folder / "example_orders.yaml").write_text("model: example\n")
  return str(folder)


def test_a_launch_that_loaded_no_model_is_evaluated_without_relationships(
    bq, monkeypatch, tmp_path, capsys):
  folder = _samples_only(tmp_path)
  launch = thelook_launch(
      bq, relationships_uri=folder, model_name=None, model_sha=None)
  monkeypatch.setattr(driver, "resolve_launch", lambda **kwargs: launch)
  planned, _ = driver.plan(parse_args(["plan", *TARGET])[0], make_env(bq))
  assert planned.models == ()
  assert all(t.edges == () for t in planned.tables)
  assert not any(t.role != "standalone" for t in planned.tables)
  (note,) = [w for w in planned.warnings if "loaded no relationship model" in w]
  assert folder in note and "no model file" in note
  assert any(
      "relationships are off for this launch" in w for w in planned.warnings)
  seed = planned.registry_seed()
  check_row(REGISTRY, seed)
  assert note in seed["warnings"] and seed["relationship_model"] is None
  capsys.readouterr()


def test_a_log_that_could_not_be_read_is_said_to_be_unread(
    bq, monkeypatch, tmp_path, capsys):
  folder = _samples_only(tmp_path)
  launch = thelook_launch(
      bq,
      relationships_uri=folder,
      model_name=None,
      model_sha=None,
      model_adjusted=None)  # no milestones: the launch log was not read
  monkeypatch.setattr(driver, "resolve_launch", lambda **kwargs: launch)
  planned, _ = driver.plan(parse_args(["plan", *TARGET])[0], make_env(bq))
  assert planned.models == ()
  (note,) = [w for w in planned.warnings if "could not be read" in w]
  assert "unknown" in note and folder in note
  assert not any("its log shows it loaded no" in w for w in planned.warnings)
  seed = planned.registry_seed()
  check_row(REGISTRY, seed)
  assert note in seed["warnings"]
  capsys.readouterr()


@pytest.mark.parametrize("log_read", [False, True])
def test_an_explicit_uri_that_resolves_to_nothing_still_raises(
    bq, monkeypatch, tmp_path, capsys, log_read):
  """A hand-named target (no log: `params_source` manual, nothing on
  record) and a job whose record named no URI both carry the operator's
  `--relationships_uri`: it must resolve."""
  folder = _samples_only(tmp_path)
  launch = thelook_launch(
      bq,
      relationships_uri=folder,
      model_name=None,
      model_sha=None,
      model_adjusted=False if log_read else None,
      params_source="jobs_labels+logs" if log_read else "manual")
  monkeypatch.setattr(driver, "resolve_launch", lambda **kwargs: launch)
  args = parse_args(["plan", *TARGET, "--relationships_uri", folder])[0]
  with pytest.raises(RelationshipError) as info:
    driver.plan(args, make_env(bq))
  assert folder in str(info.value) and "no model files there" in str(info.value)
  capsys.readouterr()


def test_a_launch_that_loaded_a_model_whose_files_are_missing_still_raises(
    bq, monkeypatch, tmp_path, capsys):
  folder = _samples_only(tmp_path)
  launch = thelook_launch(
      bq, relationships_uri=folder, model_name="thelook", model_sha="abc123")
  monkeypatch.setattr(driver, "resolve_launch", lambda **kwargs: launch)
  with pytest.raises(RelationshipError) as info:
    driver.plan(parse_args(["plan", *TARGET])[0], make_env(bq))
  assert folder in str(info.value) and "no model files there" in str(info.value)
  assert "thelook" in str(info.value) and "abc123" in str(info.value)
  capsys.readouterr()


def test_the_normal_case_loads_the_recorded_model(bq, resolved, capsys):
  del resolved
  planned, _ = driver.plan(parse_args(["plan", *TARGET])[0], make_env(bq))
  assert planned.models
  assert not any("loaded no relationship model" in w for w in planned.warnings)
  capsys.readouterr()


def test_label_key_uri_is_operator_mode_and_never_printed(
    bq, resolved, monkeypatch, capsys):
  del resolved
  build = tiny_pipeline(fast=True)
  monkeypatch.setattr(driver, "build_evaluation_pipeline", build)
  code = main(["run", *TARGET, "--label_key_uri", LABEL_KEY_URI],
              make_env(bq, submit=write_stand_in))
  assert code == 0
  assert len(build.built) == 1
  assert build.built[0][0].label_key_uri == LABEL_KEY_URI
  rows = bq.registry_rows()
  assert [r["evaluation_params"]["label_key_mode"] for r in rows
         ] == ["operator", "operator"]
  assert LABEL_KEY_URI not in json.dumps(rows)
  captured = capsys.readouterr()
  assert LABEL_KEY_URI not in captured.out + captured.err
  assert "label key: operator" in captured.out


def test_the_label_key_uri_is_redacted_before_the_reason_is_cut(
    bq, resolved, stub, capsys):
  """A FAILED row's reason is cut at 1,000 characters: the URI is taken
  out of the message first, so the cut can never leave a piece of it."""
  del resolved, stub
  padding = "x" * 950

  def explode(pipeline):
    del pipeline
    # the URI straddles the 1,000th character of "RuntimeError: …"
    raise RuntimeError(f"{padding} could not read {LABEL_KEY_URI} (HTTP 403)")

  argv = ["run", *TARGET, "--label_key_uri", LABEL_KEY_URI]
  assert main(argv, make_env(bq, submit=explode)) == 3
  reason = bq.registry_rows()[-1]["status_reason"]
  assert len(reason) == 1000
  assert reason.startswith("RuntimeError: xxxx")
  assert reason.endswith("could not read <label key uri> (HTTP 403)")
  for piece in ("projects", "demo-project/", "secrets", "sdfb-eval-label",
                "versions"):
    assert piece not in reason
  capsys.readouterr()


def test_the_label_key_uri_is_redacted_in_a_failed_rows_warnings(
    bq, monkeypatch, stub, capsys):
  """A warning the plan carries may quote the URI as an error message
  can: the driver's FAILED row names it in neither."""
  del stub
  launch = dataclasses.replace(
      thelook_launch(bq),
      warnings=(f"the launch log names the key at {LABEL_KEY_URI}",))
  monkeypatch.setattr(driver, "resolve_launch", lambda **kwargs: launch)
  monkeypatch.setattr(driver, "load_models", lambda uri: thelook_models())

  def explode(pipeline):
    del pipeline
    raise RuntimeError("the runner refused the pipeline")

  argv = ["run", *TARGET, "--label_key_uri", LABEL_KEY_URI]
  assert main(argv, make_env(bq, submit=explode)) == 3
  failed = bq.registry_rows()[-1]
  assert failed["status"] == "FAILED"
  assert "the launch log names the key at <label key uri>" in failed["warnings"]
  assert LABEL_KEY_URI not in json.dumps(failed)
  capsys.readouterr()


def test_a_cut_reason_keeps_the_line_that_names_the_failure(
    bq, resolved, stub, capsys):
  """A runner reports a worker's failure as a long traceback: the
  registry's 1,000 characters hold its beginning and the line on which
  Beam names the failing step (else the last line)."""
  del resolved, stub
  frames = "\n".join(
      f'  File "worker_{i}.py", line {i}, in step' for i in range(60))
  messages = [
      ("Pipeline job-001 failed in state FAILED: bundle inst150 "
       f"failed:Traceback (most recent call last):\n{frames}\n"
       "ValueError: users: the FINAL step failed [while running 'Final']\n\n"
       "During handling of the above exception, another exception "
       f"occurred:\n\n{frames}\nRuntimeError: Bundle processing has "
       "failed. Check prior failing response.\n"),
      f"the launcher gave up:\n{frames}\nValueError: no such bucket",
      "line one\nline two",
  ]

  def explode(pipeline):
    del pipeline
    raise RuntimeError(messages.pop(0))

  env = make_env(bq, submit=explode)
  assert main(["run", *TARGET], env) == 3
  reason = bq.registry_rows()[-1]["status_reason"]
  assert len(reason) == 1000
  assert reason.startswith("RuntimeError: Pipeline job-001 failed in state")
  assert reason.endswith(" […] ValueError: users: the FINAL step failed "
                         "[while running 'Final']")
  # no step named: the last line
  assert main(["run", *TARGET], env) == 3
  reason = bq.registry_rows()[-1]["status_reason"]
  assert len(reason) == 1000
  assert reason.endswith(" […] ValueError: no such bucket")
  # a reason that fits is left exactly as it is
  assert main(["run", *TARGET], env) == 3
  assert bq.registry_rows()[-1]["status_reason"] == (
      "RuntimeError: line one\nline two")
  capsys.readouterr()


def test_local_json_keeps_every_event_under_the_evaluation_directory(
    bq, resolved, monkeypatch, tmp_path, capsys):
  del resolved
  monkeypatch.setattr(driver, "build_evaluation_pipeline", tiny_pipeline())
  out = tmp_path / "out"
  env = make_env(bq, submit=driver.submit_pipeline)
  argv = ["run", *TARGET, "--sink", "local_json", "--output_local", str(out)]
  assert main(argv, env) == 0
  assert bq.loads == []  # nothing goes to BigQuery
  (directory,) = list(out.iterdir())
  assert driver.EVALUATION_ID_RE.fullmatch(directory.name)  # <DIR>/<id>
  lines = [
      json.loads(line)
      for path in sorted((directory / REGISTRY).glob("*.jsonl"))
      for line in path.read_text().splitlines()
  ]
  assert sorted(r["status"] for r in lines) == ["RUNNING", "SUCCEEDED"]
  for row in lines:
    check_row(REGISTRY, row, ordered=False)
    assert row["evaluation_id"] == directory.name
  assert str(directory) in capsys.readouterr().out

  def explode(pipeline):
    raise RuntimeError("boom")

  env.submit = explode
  assert main(argv, env) == 3
  failed_dir = next(d for d in out.iterdir() if d != directory)
  statuses = sorted(
      json.loads(line)["status"]
      for path in (failed_dir / REGISTRY).glob("*.jsonl")
      for line in path.read_text().splitlines())
  assert statuses == ["FAILED", "RUNNING"]
  capsys.readouterr()


# --------------------------------------------------------------------------
# one terminal row, written by whoever owns the outcome (R93-3)
# --------------------------------------------------------------------------
def _final(status: str = "SUCCEEDED", **fields: Any) -> dict[str, Any]:
  """A FINAL registry row as a read-back returns one (what the driver
  reads of it)."""
  return {
      "evaluation_id": "eval-read-back",
      "event": "FINAL",
      "status": status,
      "status_reason": None,
      "evaluated_at": "2026-09-14T08:00:00.000000Z",
      "recorded_at": "2026-09-14T08:30:00.000000Z",
      "metrics_total": 4,
      "metrics_pass": 3,
      "metrics_warn": 1,
      "metrics_fail": 0,
      **fields,
  }


def _dataflow_env(bq, job: FakeJob):
  return make_env(bq, submit=lambda pipeline: job, make_pipeline=_holder)


def test_an_interrupted_wait_leaves_the_job_and_writes_no_row(
    bq, resolved, stub, capsys):
  """Ctrl-C while a submitted job is still running: the job is not
  cancelled, no terminal row is written (the job will write its own),
  the operator is told how to read the result, and the interrupt stays
  an interrupt."""
  del resolved, stub
  job = FakeJob(state="RUNNING", error=KeyboardInterrupt())
  with pytest.raises(KeyboardInterrupt):
    main(["run", *DATAFLOW], _dataflow_env(bq, job))
  assert _statuses(bq) == ["RUNNING"]
  assert job.waits == 1 and job.cancels == 0
  captured = capsys.readouterr()
  evaluation_id = bq.registry_rows()[0]["evaluation_id"]
  assert job.job in captured.err and evaluation_id in captured.err
  assert "continues" in captured.err and "not cancelled" in captured.err
  assert (f"sdfb-eval report --project {PROJECT} --evaluation_id "
          f"{evaluation_id}") in captured.err


def test_a_polling_error_leaves_the_job_and_writes_no_row(
    bq, resolved, stub, capsys):
  del resolved, stub
  job = FakeJob(
      state="RUNNING",
      error=AssertionError("Job did not reach to a terminal state"))
  assert main(["run", *DATAFLOW, "--fail_on", "fail"], _dataflow_env(bq,
                                                                     job)) == 3
  assert _statuses(bq) == ["RUNNING"]
  assert job.cancels == 0
  err = capsys.readouterr().err
  assert job.job in err and "continues" in err
  assert "AssertionError: Job did not reach to a terminal state" in err


def test_a_job_whose_state_cannot_be_read_is_treated_as_running(
    bq, resolved, stub, capsys):
  del resolved, stub

  class Unreachable(FakeJob):

    @property
    def state(self):
      raise ConnectionError("the Dataflow API is unreachable")

    @state.setter
    def state(self, value):
      del value

  job = Unreachable(error=ConnectionError("polling failed"))
  assert main(["run", *DATAFLOW], _dataflow_env(bq, job)) == 3
  assert _statuses(bq) == ["RUNNING"]  # it may still write its own FINAL
  assert "continues" in capsys.readouterr().err


def test_a_job_that_failed_is_closed_by_the_driver(bq, resolved, stub, capsys):
  """The job is terminal and not DONE, and the registry, read back,
  holds only the RUNNING row: the job will never write a FINAL row, so
  the driver does."""
  del resolved, stub
  bq.canned.append((REGISTRY, [_final(event="RUNNING", status="RUNNING")]))
  job = FakeJob(
      state="FAILED",
      error=RuntimeError("Dataflow pipeline failed. State: FAILED"))
  assert main(["run", *DATAFLOW], _dataflow_env(bq, job)) == 3
  running, failed = bq.registry_rows()
  assert (failed["event"], failed["status"]) == ("FINAL", "FAILED")
  assert failed["status_reason"].startswith(
      "RuntimeError: Dataflow pipeline failed")
  assert failed["evaluation_id"] == running["evaluation_id"]
  assert "continues" not in capsys.readouterr().err
  # the same when the wait returns the terminal state instead of raising
  bq.loads.clear()
  job = FakeJob(state="RUNNING", final="CANCELLED")
  assert main(["run", *DATAFLOW], _dataflow_env(bq, job)) == 3
  assert _statuses(bq) == ["RUNNING", "FAILED"]
  reason = bq.registry_rows()[-1]["status_reason"]
  assert "CANCELLED" in reason and job.job in reason
  capsys.readouterr()


def test_a_job_cancelled_after_its_final_row_is_not_closed_twice(
    bq, resolved, stub, capsys):
  """The final review's M1: a job cancelled (or failed) after its FINAL
  load already holds its terminal row. The driver reads the registry
  back before it appends FAILED, and appends nothing when a FINAL row is
  there — never two terminal rows."""
  del resolved, stub
  bq.canned.append((REGISTRY, [_final()]))
  job = FakeJob(state="RUNNING", final="CANCELLED")
  assert main(["run", *DATAFLOW], _dataflow_env(bq, job)) == 3
  assert _statuses(bq) == ["RUNNING"]
  err = capsys.readouterr().err
  assert "CANCELLED" in err and "already holds a FINAL row" in err
  # the same when the wait raises on the terminal state
  bq.loads.clear()
  job = FakeJob(
      state="FAILED",
      error=RuntimeError("Dataflow pipeline failed. State: FAILED"))
  assert main(["run", *DATAFLOW], _dataflow_env(bq, job)) == 3
  assert _statuses(bq) == ["RUNNING"]
  capsys.readouterr()


def test_the_late_cancel_notice_redacts_the_label_key_uri(
    bq, resolved, stub, capsys):
  """The notice quotes the job's error: the label key's URI in it goes
  through the redaction a failed row's reason gets (the traceback that
  follows is the error's own)."""
  del resolved, stub
  bq.canned.append((REGISTRY, [_final()]))
  job = FakeJob(
      state="FAILED",
      error=RuntimeError(f"the job failed reading {LABEL_KEY_URI}"))
  env = _dataflow_env(bq, job)
  assert main(["run", *DATAFLOW, "--label_key_uri", LABEL_KEY_URI], env) == 3
  assert _statuses(bq) == ["RUNNING"]
  (notice,) = [
      line for line in capsys.readouterr().err.splitlines()
      if "already holds a FINAL row" in line
  ]
  assert LABEL_KEY_URI not in notice
  assert "the job failed reading <label key uri>" in notice


def test_a_terminal_job_whose_registry_cannot_be_read_is_not_closed(
    bq, resolved, stub, capsys):
  """M1, the other half: the read-back errors, so a FINAL row may well
  exist — the driver writes nothing, as elsewhere."""
  del resolved, stub
  bq.query_failures[REGISTRY] = PermissionError("403 tables.getData denied")
  job = FakeJob(state="RUNNING", final="CANCELLED")
  assert main(["run", *DATAFLOW], _dataflow_env(bq, job)) == 3
  assert _statuses(bq) == ["RUNNING"]
  err = capsys.readouterr().err
  assert "the registry could not be read" in err and "CANCELLED" in err


def test_a_wait_that_returns_before_the_job_ends_writes_no_row(
    bq, resolved, stub, capsys):
  del resolved, stub
  job = FakeJob(state="RUNNING", final="RUNNING")
  assert main(["run", *DATAFLOW], _dataflow_env(bq, job)) == 3
  assert _statuses(bq) == ["RUNNING"]
  assert "continues" in capsys.readouterr().err


def test_a_done_job_owns_its_final_row(bq, resolved, stub, capsys):
  """The pipeline wrote FINAL: the driver reads it back for the exit
  code and writes nothing more."""
  del resolved, stub
  bq.canned.append((REGISTRY, [_final()]))
  env = _dataflow_env(bq, FakeJob())
  assert main(["run", *DATAFLOW], env) == 0
  assert main(["run", *DATAFLOW, "--fail_on", "warn"], env) == 1
  assert _statuses(bq) == ["RUNNING", "RUNNING"]  # one per attempt
  sql, params = next((s, p) for s, p in bq.queries if REGISTRY in s)
  assert f"`{QDS}.{REGISTRY}`" in sql
  assert params == {"evaluation_id": bq.registry_rows()[0]["evaluation_id"]}
  out = capsys.readouterr().out
  assert "gate (--fail_on warn): TRIPPED" in out
  assert f"written to: {QDS}" in out


def test_a_done_job_without_a_final_row_is_closed_by_the_driver(
    bq, resolved, stub, capsys):
  """The read-back worked and found no FINAL row: the job is over and
  wrote none, so the driver appends FAILED with that reason."""
  del resolved, stub
  bq.canned.append((REGISTRY, [_final(event="RUNNING", status="RUNNING")]))
  assert main(["run", *DATAFLOW], _dataflow_env(bq, FakeJob())) == 3
  assert _statuses(bq) == ["RUNNING", "FAILED"]
  reason = bq.registry_rows()[-1]["status_reason"]
  assert "the job finished without a FINAL row" in reason
  capsys.readouterr()
  # no row at all came back: the same
  bq.loads.clear()
  bq.canned[:] = [(REGISTRY, [])]
  assert main(["run", *DATAFLOW], _dataflow_env(bq, FakeJob())) == 3
  assert _statuses(bq) == ["RUNNING", "FAILED"]
  capsys.readouterr()


def test_a_read_back_that_fails_writes_no_row(bq, resolved, stub, capsys):
  """The registry could not be read: a FINAL row may well exist, so the
  driver writes nothing and says the registry could not be read."""
  del resolved, stub
  bq.query_failures[REGISTRY] = PermissionError("403 tables.getData denied")
  assert main(["run", *DATAFLOW], _dataflow_env(bq, FakeJob())) == 3
  assert _statuses(bq) == ["RUNNING"]
  err = capsys.readouterr().err
  assert "the registry could not be read" in err
  assert "403 tables.getData denied" in err
  assert "sdfb-eval report" in err


def test_a_local_run_that_dies_in_its_wait_is_closed_by_the_driver(
    bq, resolved, stub):
  """A result with no job id cannot outlive the driver: an interrupt in
  its wait ends the run, and the driver writes FAILED."""
  del resolved, stub
  result = FakeResult(state="RUNNING", error=KeyboardInterrupt())
  with pytest.raises(KeyboardInterrupt):
    main(["run", *TARGET], make_env(bq, submit=lambda pipeline: result))
  assert _statuses(bq) == ["RUNNING", "FAILED"]


# --------------------------------------------------------------------------
# a finished job owns its FINAL row, whatever happened to the wait (R98-1)
# --------------------------------------------------------------------------
def _registry_reads(bq) -> int:
  return sum(REGISTRY in sql for sql, _ in bq.queries)


def test_a_polling_error_on_a_finished_job_does_not_fail_the_evaluation(
    bq, resolved, stub, capsys):
  """The wait raised, and the job, looked at again, is DONE: the
  evaluation completed. The driver warns, reads the job's FINAL row back
  and exits by the gate; it writes no row of its own."""
  del resolved, stub
  bq.canned.append((REGISTRY, [_final()]))
  polling = ConnectionError("polling the job failed")
  env = _dataflow_env(bq, FakeJob(state="DONE", error=polling))
  assert main(["run", *DATAFLOW], env) == 0
  assert main(["run", *DATAFLOW, "--fail_on", "warn"], env) == 1  # the gate's
  assert _statuses(bq) == ["RUNNING", "RUNNING"]  # one per attempt, no FAILED
  assert _registry_reads(bq) == 2
  captured = capsys.readouterr()
  assert captured.err.count("ConnectionError: polling the job failed") == 2
  assert "finished (DONE)" in captured.err and "Traceback" not in captured.err
  assert "gate (--fail_on warn): TRIPPED" in captured.out


def test_the_ignored_polling_error_warning_redacts_the_label_key_uri(
    bq, resolved, stub, capsys):
  """The warning prints the exception text: the label key's URI, when the
  error quotes it, goes through the same redaction a failed row's reason
  gets."""
  del resolved, stub
  bq.canned.append((REGISTRY, [_final()]))
  polling = ConnectionError(f"polling failed reading {LABEL_KEY_URI}")
  env = _dataflow_env(bq, FakeJob(state="DONE", error=polling))
  assert main(["run", *DATAFLOW, "--label_key_uri", LABEL_KEY_URI], env) == 0
  err = capsys.readouterr().err
  assert "ConnectionError: polling failed reading" in err
  assert "finished (DONE)" in err
  assert LABEL_KEY_URI not in err


def test_a_job_that_turns_done_after_its_wait_died_owns_its_final_row(
    bq, resolved, stub, capsys):
  """Beam's polling gave up while the job was RUNNING; by the time the
  driver looks again it is DONE."""
  del resolved, stub
  bq.canned.append((REGISTRY, [_final()]))
  job = FakeJob(
      state=["RUNNING", "DONE"],
      error=AssertionError("Job did not reach to a terminal state"))
  assert main(["run", *DATAFLOW, "--fail_on", "fail"], _dataflow_env(bq,
                                                                     job)) == 0
  assert _statuses(bq) == ["RUNNING"] and job.cancels == 0
  assert _registry_reads(bq) == 1
  err = capsys.readouterr().err
  assert "AssertionError: Job did not reach to a terminal state" in err
  assert "continues" not in err


def test_a_polling_error_on_a_finished_job_without_a_final_row_is_failed(
    bq, resolved, stub, capsys):
  """DONE, and the read-back worked and found no FINAL row: that, and
  not the polling error, is what the driver's FAILED row says."""
  del resolved, stub
  bq.canned.append((REGISTRY, []))
  job = FakeJob(state="DONE", error=ConnectionError("polling the job failed"))
  assert main(["run", *DATAFLOW], _dataflow_env(bq, job)) == 3
  assert _statuses(bq) == ["RUNNING", "FAILED"]
  reason = bq.registry_rows()[-1]["status_reason"]
  assert "the job finished without a FINAL row" in reason
  assert "polling the job failed" not in reason
  capsys.readouterr()


def test_an_interrupt_after_the_job_finished_writes_no_row(
    bq, resolved, stub, capsys):
  """Ctrl-C lands after the job ended DONE: its FINAL row is the
  pipeline's. The driver writes nothing, does not read the registry,
  says the job finished and how to read its result, and the interrupt
  stays an interrupt."""
  del resolved, stub
  bq.canned.append((REGISTRY, [_final()]))
  job = FakeJob(state="DONE", error=KeyboardInterrupt())
  with pytest.raises(KeyboardInterrupt):
    main(["run", *DATAFLOW], _dataflow_env(bq, job))
  assert _statuses(bq) == ["RUNNING"]
  assert _registry_reads(bq) == 0 and job.cancels == 0
  captured = capsys.readouterr()
  evaluation_id = bq.registry_rows()[0]["evaluation_id"]
  assert f"job {job.job} finished (DONE)" in captured.err
  assert "no registry row was written here" in captured.err
  assert (f"sdfb-eval report --project {PROJECT} --evaluation_id "
          f"{evaluation_id}") in captured.err
  assert "continues" not in captured.err


def test_a_local_run_found_done_is_the_pipelines_too(bq, resolved, stub,
                                                     capsys):
  """The same rule without a job id. An interrupt after a local
  pipeline ended DONE appends no FAILED row (with a local sink the
  pipeline's FINAL row is already on disk); an ordinary error goes on to
  read that row, and its absence is what fails the run."""
  del resolved, stub
  done = FakeResult(state="DONE", error=KeyboardInterrupt())
  with pytest.raises(KeyboardInterrupt):
    main(["run", *TARGET], make_env(bq, submit=lambda pipeline: done))
  assert _statuses(bq) == ["RUNNING"]
  err = capsys.readouterr().err
  assert "the pipeline finished (DONE)" in err
  assert "were not loaded into BigQuery" in err
  bq.loads.clear()
  done = FakeResult(state="DONE", error=OSError("the wait broke"))
  assert main(["run", *TARGET], make_env(bq, submit=lambda pipeline: done)) == 3
  assert _statuses(bq) == ["RUNNING", "FAILED"]
  reason = bq.registry_rows()[-1]["status_reason"]
  assert "without writing a FINAL registry row" in reason
  assert "OSError: the wait broke" in capsys.readouterr().err


# --------------------------------------------------------------------------
# exit codes and the gate (R93-5, R93-8)
# --------------------------------------------------------------------------
def test_fail_on_exit_codes(bq, resolved, monkeypatch, capsys):
  del resolved
  counts = {"total": 4, "pass": 2, "warn": 1, "fail": 1}
  assert gate.exit_code("none", counts) == 0
  assert gate.exit_code("warn", counts) == 1
  assert gate.exit_code("fail", counts) == 1
  only_warn = {"total": 3, "pass": 2, "warn": 1, "fail": 0}
  assert gate.exit_code("fail", only_warn) == 0
  assert gate.exit_code("warn", only_warn) == 1
  assert gate.exit_code("warn", {"total": 2, "pass": 2}) == 0
  with pytest.raises(ValueError, match="fail_on"):
    gate.exit_code("error", counts)
  env = make_env(bq, submit=write_stand_in)
  monkeypatch.setattr(driver, "build_evaluation_pipeline",
                      tiny_pipeline(fast=True, counts=only_warn))
  assert main(["run", *TARGET, "--fail_on", "fail"], env) == 0
  assert main(["run", *TARGET, "--fail_on", "warn"], env) == 1
  assert "gate (--fail_on warn): TRIPPED" in capsys.readouterr().out


def test_a_final_row_reading_failed_exits_3_whatever_fail_on_says(
    bq, resolved, monkeypatch, capsys):
  """R90(g): the pipeline can write FINAL = FAILED and end normally (no
  launch table evaluated). `run` reads the row back: exit 3, no second
  registry row, and `--fail_on none` does not mask it."""
  del resolved
  none_evaluated = {"total": 4, "not_evaluated": 4}
  failed = {"status": "FAILED"}
  assert gate.final_exit_code(failed, "none", none_evaluated) == 3
  assert gate.final_exit_code(failed, "warn", none_evaluated) == 3
  assert gate.final_exit_code(failed, "none", none_evaluated, gated=False) == 3
  failing = {"total": 2, "pass": 1, "fail": 1}
  for status in ("SUCCEEDED", "SUCCEEDED_WITH_WARNINGS", "SKIPPED"):
    assert gate.final_exit_code({"status": status}, "none", failing) == 0
    assert gate.final_exit_code({"status": status}, "fail", failing) == 1
    assert gate.final_exit_code({"status": status},
                                "fail",
                                failing,
                                gated=False) == 0
  assert (gate.EXIT_TRIPPED, gate.EXIT_FAILED) == (1, 3)
  monkeypatch.setattr(
      driver, "build_evaluation_pipeline",
      tiny_pipeline(fast=True, counts=none_evaluated, status="FAILED"))
  env = make_env(bq, submit=write_stand_in)
  assert main(["run", *TARGET, "--fail_on", "none"], env) == 3
  # RUNNING, then the pipeline's own FINAL: the driver appends nothing
  assert [(r["event"], r["status"]) for r in bq.registry_rows()
         ] == [("RUNNING", "RUNNING"), ("FINAL", "FAILED")]
  out = capsys.readouterr().out
  assert "the evaluation finished FAILED: exit 3" in out
  assert main(["run", *TARGET, "--fail_on", "warn"], env) == 3
  assert "TRIPPED" not in capsys.readouterr().out  # 3 is not the gate
  # the template entry applies no gate, and still reports the failure
  assert run_evaluation.main(TARGET, env) == 3
  assert len(bq.registry_rows()) == 6
  capsys.readouterr()


def test_a_partial_run_trips_an_active_gate(bq, resolved, monkeypatch, capsys):
  """R93-8: the gate cannot vouch for a launch table that was not
  evaluated. PARTIAL trips `--fail_on warn|fail` even with no failing
  metric; `--fail_on none` never trips."""
  del resolved
  clean = {"total": 2, "pass": 2}
  reason = "orders: not evaluated — the source rows could not be encoded"
  partial = {"status": "PARTIAL", "status_reason": reason}
  assert gate.final_exit_code(partial, "none", clean) == 0
  assert gate.final_exit_code(partial, "warn", clean) == 1
  assert gate.final_exit_code(partial, "fail", clean) == 1
  assert gate.final_exit_code(partial, "fail", clean, gated=False) == 0
  monkeypatch.setattr(
      driver, "build_evaluation_pipeline",
      tiny_pipeline(fast=True, counts=clean, status="PARTIAL", reason=reason))
  env = make_env(bq, submit=write_stand_in)
  assert main(["run", *TARGET], env) == 0
  assert "TRIPPED" not in capsys.readouterr().out
  assert main(["run", *TARGET, "--fail_on", "fail"], env) == 1
  out = capsys.readouterr().out
  assert "gate (--fail_on fail): TRIPPED" in out
  assert "the run is PARTIAL" in out and reason in out


def test_a_skipped_run_does_not_trip_the_gate_and_says_so(
    bq, resolved, monkeypatch, capsys):
  """R93-8: an empty scope is a planned outcome: exit 0, and one line on
  stderr that nothing was evaluated, with the reason."""
  del resolved
  nothing = {"total": 0}
  reason = "no table can be evaluated — users: scope empty: the job wrote 0"
  skipped = {"status": "SKIPPED", "status_reason": reason}
  for fail_on in ("none", "warn", "fail"):
    assert gate.final_exit_code(skipped, fail_on, nothing) == 0
  monkeypatch.setattr(
      driver, "build_evaluation_pipeline",
      tiny_pipeline(fast=True, counts=nothing, status="SKIPPED", reason=reason))
  env = make_env(bq, submit=write_stand_in)
  assert main(["run", *TARGET, "--fail_on", "warn"], env) == 0
  captured = capsys.readouterr()
  lines = [
      line for line in captured.err.splitlines()
      if "nothing was evaluated" in line
  ]
  assert len(lines) == 1 and reason in lines[0]
  assert "TRIPPED" not in captured.out


# --------------------------------------------------------------------------
# the free-text pools (Task 22's handoff; the refusal rule of R89/M8)
# --------------------------------------------------------------------------
@pytest.fixture(name="pooled")
def fixture_pooled(monkeypatch, bq):
  """The thelook launch, naming the generator's free-text pools table."""
  launch = thelook_launch(bq, params={"freetext_pools_table": POOLS_TABLE})
  monkeypatch.setattr(driver, "resolve_launch", lambda **kwargs: launch)
  monkeypatch.setattr(driver, "load_models", lambda uri: thelook_models())
  return launch


def test_run_reads_the_free_text_pools_per_table(bq, pooled, monkeypatch,
                                                 capsys):
  build = tiny_pipeline(fast=True)
  monkeypatch.setattr(driver, "build_evaluation_pipeline", build)
  model_uri = pooled.params["model_uri"]
  bq.canned.append(("freetext_pools", [{
      "column": "status",
      "target": 64,
      "values": ["Complete", "Shipped"],
      "reference_digest": "ignored-by-the-fake",
      "model_uri": model_uri,
  }]))
  assert main(["run", *TARGET], make_env(bq, submit=write_stand_in)) == 0
  pools = build.built[0][1]["pools"]
  assert sorted(pools) == ["order_items", "orders", "users"]
  assert pools["orders"] == {
      "status":
          FreeTextPool(
              column="status",
              values=("Complete", "Shipped"),
              target=64,
              reference_digest="ignored-by-the-fake",
              model_uri=model_uri)
  }
  reads = [
      (sql, params) for sql, params in bq.queries if "freetext_pools" in sql
  ]
  assert len(reads) == 3  # one per launch table, never the read-only parent
  assert all(f"`{POOLS_TABLE}`" in sql for sql, _ in reads)
  assert sorted(p["reference_digest"] for _, p in reads) == sorted(
      bq.digest(name) for name in ("users", "orders", "order_items"))
  assert {p["model_uri"] for _, p in reads} == {model_uri}
  capsys.readouterr()


def test_a_launch_without_a_pools_table_reads_none(bq, resolved, monkeypatch,
                                                   capsys):
  del resolved
  build = tiny_pipeline(fast=True)
  monkeypatch.setattr(driver, "build_evaluation_pipeline", build)
  assert main(["run", *TARGET], make_env(bq, submit=write_stand_in)) == 0
  assert build.built[0][1]["pools"] is None
  assert not [sql for sql, _ in bq.queries if "freetext_pools" in sql]
  capsys.readouterr()


def test_refused_pools_degrade_with_a_warning(bq, pooled, monkeypatch, capsys):
  """BigQuery refuses the read (403/404/400): the run goes on without
  pools — `field.pool_memorization_lift` says so itself — and the plan
  the pipeline runs carries the warning into the FINAL row."""
  del pooled
  build = tiny_pipeline(fast=True)
  monkeypatch.setattr(driver, "build_evaluation_pipeline", build)
  bq.query_failures["freetext_pools"] = PermissionError(
      "query: 403 tables.getData denied")
  assert main(["run", *TARGET], make_env(bq, submit=write_stand_in)) == 0
  plan, kwargs = build.built[0]
  assert kwargs["pools"] is None
  notes = [w for w in plan.warnings if "free-text pools" in w]
  assert len(notes) == 3 and all("403 tables.getData" in w for w in notes)
  assert all("field.pool_memorization_lift" in w for w in notes)
  final = bq.registry_rows()[-1]
  assert final["event"] == "FINAL" and set(notes) <= set(final["warnings"])
  capsys.readouterr()


def test_a_transient_pools_error_fails_the_run(bq, pooled, stub, capsys):
  """A 5xx or a 429 is not a refusal: degrading would publish a run
  whose pool metric is silently missing for a reason a retry removes."""
  del pooled
  bq.query_failures["freetext_pools"] = BqApiError(
      "query: 503 backend error", status=503)
  assert main(["run", *TARGET], make_env(bq)) == 3
  assert _statuses(bq) == ["RUNNING", "FAILED"]
  assert bq.registry_rows()[-1]["status_reason"].startswith(
      "BqApiError: query: 503")
  assert stub.built == []
  capsys.readouterr()


# --------------------------------------------------------------------------
# the temporary directory of --sink bq_client
# --------------------------------------------------------------------------
def _kept(scratch) -> list[str]:
  return sorted(p.name for p in scratch.glob("sdfb-eval-*"))


def test_the_temporary_directory_goes_with_a_successful_run(
    bq, resolved, monkeypatch, scratch, capsys):
  del resolved
  monkeypatch.setattr(driver, "build_evaluation_pipeline",
                      tiny_pipeline(fast=True))
  assert main(["run", *TARGET], make_env(bq, submit=write_stand_in)) == 0
  assert _kept(scratch) == []
  captured = capsys.readouterr()
  assert str(scratch) not in captured.out + captured.err


def test_a_failed_load_keeps_the_local_outputs_and_says_where(
    bq, resolved, monkeypatch, scratch, capsys):
  """The pipeline ran and its files are the only copy of the result."""
  del resolved
  monkeypatch.setattr(driver, "build_evaluation_pipeline",
                      tiny_pipeline(fast=True, ks=[0.01]))
  bq.load_failures["evaluation_metrics"] = BqApiError(
      "load into evaluation_metrics: 503", status=503)
  assert main(["run", *TARGET], make_env(bq, submit=write_stand_in)) == 3
  assert _statuses(bq) == ["RUNNING", "FAILED"]
  (kept,) = _kept(scratch)
  err = capsys.readouterr().err
  assert err.count(str(scratch / kept)) == 1
  assert list((scratch / kept).rglob("*.jsonl"))


def test_a_run_that_fails_while_it_is_set_up_leaves_no_temporary_directory(
    bq, resolved, stub, scratch, capsys):
  """The temporary directory exists before the BigQuery client does: a
  client that cannot be made must not leave it behind."""
  del resolved, stub

  def no_client(project: str):
    raise PermissionError(f"no credentials for {project}")

  assert main(["run", *TARGET], make_env(bq, make_bq=no_client)) == 3
  assert _kept(scratch) == []
  captured = capsys.readouterr()
  assert "PermissionError: no credentials for" in captured.err
  assert str(scratch) not in captured.out + captured.err
  assert not bq.registry_rows()  # nothing to write a row with


def test_a_run_that_wrote_nothing_keeps_no_temporary_directory(
    bq, resolved, stub, monkeypatch, scratch, capsys):
  del resolved, stub

  def explode(pipeline):
    del pipeline
    raise RuntimeError("submission failed")

  assert main(["run", *TARGET], make_env(bq, submit=explode)) == 3
  assert _kept(scratch) == []

  def unresolved(**kwargs: Any):
    del kwargs
    raise ValueError("no such launch")

  monkeypatch.setattr(driver, "resolve_launch", unresolved)
  assert main(["run", *TARGET], make_env(bq)) == 3
  assert _kept(scratch) == []
  assert str(scratch) not in capsys.readouterr().err


# --------------------------------------------------------------------------
# the flex-template entry (R88f)
# --------------------------------------------------------------------------
def test_flex_entry_does_not_wait_and_never_applies_fail_on(
    bq, resolved, stub, capsys):
  del resolved, stub
  job = FakeJob(state="RUNNING")
  made: list[Any] = []

  def make_pipeline(options):
    made.append(options)
    return _holder(options)

  env = make_env(bq, submit=lambda pipeline: job, make_pipeline=make_pipeline)
  argv = [*DATAFLOW, "--fail_on", "fail", "--trigger", "composer"]
  assert run_evaluation.main(argv, env) == 0
  assert job.waits == 0
  assert len(made) == 1
  options = made[0]
  assert options.view_as(StandardOptions).runner == "DataflowRunner"
  assert options.view_as(DebugOptions).experiments == ["upload_graph"]
  assert options.view_as(
      GoogleCloudOptions).temp_location == "gs://demo-bucket/tmp"
  (running,) = bq.registry_rows()  # FINAL is the pipeline's, on Dataflow
  check_row(REGISTRY, running)
  assert (running["status"], running["trigger"]) == ("RUNNING", "composer")
  assert (running["runner"], running["mode"]) == ("DataflowRunner", "exact")
  out = capsys.readouterr().out
  assert job.job in out
  assert "--fail_on is not applied" in out
  # the same arguments through `sdfb-eval run` wait for the job
  bq.canned.append((REGISTRY, [_final()]))
  assert main(["run", *argv], env) == 0
  assert job.waits == 1
  capsys.readouterr()


def test_flex_entry_given_a_template_launchs_arguments_plans_that_job(
    bq, resolved, stub, capsys):
  """What the launcher really sends: the template's parameters as
  `--name=value` (the unset ones empty, the generation job as
  `--generation_job_id` because a parameter named `job_id` is dropped),
  its own `--runner`, `--project`, `--region` and `--template_location`,
  and Beam's `--temp_location`. `--sdfb_job` is already gone."""
  del stub
  env = make_env(
      bq, submit=lambda pipeline: _template_result(), make_pipeline=_holder)
  argv = [
      "--runner=DataflowRunner", f"--project={PROJECT}", f"--region={REGION}",
      "--job_name=sdfb-eval-chained",
      "--template_location=gs://demo-bucket/staging/template",
      "--temp_location=gs://demo-bucket/tmp", "--trigger=chained",
      "--output_dataset=synthetic_data_quality", "--mode=",
      f"--generation_job_id={JOB_ID}", "--run_id=", "--tables=",
      "--disk_size_gb=200"
  ]
  assert run_evaluation.main(argv, env) == 0
  assert resolved[0]["job_id"] == JOB_ID
  (running,) = bq.registry_rows()
  assert (running["generation_job_id"], running["trigger"]) == (JOB_ID,
                                                                "chained")
  capsys.readouterr()


def test_flex_entry_on_a_local_runner_still_loads_its_outputs(
    bq, resolved, monkeypatch, capsys):
  del resolved
  monkeypatch.setattr(driver, "build_evaluation_pipeline",
                      tiny_pipeline(fast=True, counts={
                          "total": 1,
                          "fail": 1
                      }))
  code = run_evaluation.main([*TARGET, "--fail_on", "fail"],
                             make_env(bq, submit=write_stand_in))
  assert code == 0  # never the gate's code
  # bq_client: the driver waited, then loaded the outputs
  assert _statuses(bq) == ["RUNNING", "SUCCEEDED"]
  assert "--fail_on is not applied here" in capsys.readouterr().out


# --------------------------------------------------------------------------
# a template launch: the runner builds the job, the launcher submits it
# --------------------------------------------------------------------------
def _template_result() -> DataflowPipelineResult:
  """What Beam's DataflowRunner returns when `--template_location` is set
  — how a flex-template launcher runs the entry: a result with no job."""
  return DataflowPipelineResult(None, runner=None)


def test_a_template_launch_has_no_job_id():
  """The final review's I1: `job_id()` of the job-less result raises."""
  result = _template_result()
  assert result.has_job is False
  with pytest.raises(AttributeError):
    result.job_id()
  assert driver._job_id(result) is None  # pylint: disable=protected-access  # the helper under test


@pytest.mark.parametrize("entry", ["flex", "run"])
def test_a_template_launch_is_handed_to_dataflow_and_leaves_running(
    bq, resolved, stub, capsys, entry):
  """The launch was handed to Dataflow: no job id, nothing to wait for.
  The driver writes no terminal row (the RUNNING row stays for the job
  to close) and ends 0 — it used to crash after writing RUNNING."""
  del resolved, stub
  env = make_env(
      bq, submit=lambda pipeline: _template_result(), make_pipeline=_holder)
  argv = [*DATAFLOW, "--fail_on", "fail", "--trigger", "composer"]
  if entry == "flex":
    assert run_evaluation.main(argv, env) == 0
  else:
    assert main(["run", *argv], env) == 0
  (running,) = bq.registry_rows()
  check_row(REGISTRY, running)
  assert (running["event"], running["status"]) == ("RUNNING", "RUNNING")
  assert not [s for s, _ in bq.queries if REGISTRY in s]  # no read-back
  captured = capsys.readouterr()
  assert "handed to DataflowRunner as a template" in captured.out
  assert "--fail_on is not applied" in captured.out
  assert "Traceback" not in captured.err


# --------------------------------------------------------------------------
# the worker image the image bakes (R97)
# --------------------------------------------------------------------------
def _options(argv, monkeypatch, baked=None):
  if baked is None:
    monkeypatch.delenv("SDFB_EVAL_SDK_CONTAINER_IMAGE", raising=False)
  else:
    monkeypatch.setenv("SDFB_EVAL_SDK_CONTAINER_IMAGE", baked)
  args, extras = parse_args(["run", *argv])
  return driver.pipeline_options(args, extras, None, "20260101t000000z-abcd")


def test_baked_worker_image_is_applied_on_dataflow(monkeypatch):
  options = _options(DATAFLOW, monkeypatch, baked="r/sdfb-evaluation:1")
  assert options.view_as(
      WorkerOptions).sdk_container_image == "r/sdfb-evaluation:1"


def test_explicit_worker_image_wins(monkeypatch):
  options = _options([*DATAFLOW, "--sdk_container_image", "r/other:2"],
                     monkeypatch,
                     baked="r/sdfb-evaluation:1")
  assert options.view_as(WorkerOptions).sdk_container_image == "r/other:2"


def test_worker_image_is_untouched_on_a_local_runner(monkeypatch, caplog):
  with caplog.at_level("WARNING"):
    options = _options(TARGET, monkeypatch, baked="r/sdfb-evaluation:1")
  assert options.view_as(WorkerOptions).sdk_container_image is None
  assert not caplog.records


def test_missing_worker_image_on_dataflow_warns_once(monkeypatch, caplog):
  with caplog.at_level("WARNING"):
    options = _options(DATAFLOW, monkeypatch)
  assert options.view_as(WorkerOptions).sdk_container_image is None
  (record,) = caplog.records
  assert "stock Beam SDK image" in record.getMessage()
