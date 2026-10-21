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
"""Tests for the `sdfb-eval` command line (Task 27) short of running an
evaluation: the flag surface and its defaults, usage errors (exit 2,
nothing started), the pipeline options, planning (`plan`), `schemas` and
`catalogue`. `run` as a driver is in `test_driver.py`, the real
DirectRunner run in `test_run.py`.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest
from apache_beam.options.pipeline_options import (
    DebugOptions,
    GoogleCloudOptions,
    SetupOptions,
    StandardOptions,
    WorkerOptions,
)
from unit.context.plan_fakes import (
    BASE,
    DS,
    JOB_ID,
    MODEL_YAML,
    PROJECT,
    QDS,
    SRC,
    TABLES,
    thelook_launch,
    thelook_models,
)

from sdfb_evaluation.beam import census
from sdfb_evaluation.beam import pipeline as beam_pipeline
from sdfb_evaluation.beam.label_key import is_label_key_uri
from sdfb_evaluation.cli import driver
from sdfb_evaluation.cli.main import main, parse_args, public_run_flags
from sdfb_evaluation.context import relationships
from sdfb_evaluation.context.plan import (
    Knobs,
    build_plan,
)
from sdfb_evaluation.schemas import CLUSTERING, TABLES as EVAL_TABLES

from .conftest import catalogue
from .helpers import (
    NOW,
    JobOnlySession,
    check_row,
    make_env,
)

REGION = "europe-west1"
TARGET = ["--project", PROJECT, "--region", REGION, "--job_id", JOB_ID]
LABEL_KEY_URI = "projects/demo-project/secrets/sdfb-eval-label/versions/3"


def _usage_error(argv: list[str], capsys) -> str:
  with pytest.raises(SystemExit) as info:
    parse_args(argv)
  assert info.value.code == 2
  return capsys.readouterr().err


# --------------------------------------------------------------------------
# the flag surface
# --------------------------------------------------------------------------
def test_runner_defaults():
  assert driver.runner_defaults("DirectRunner") == ("sampled", "bq_client")
  assert driver.runner_defaults("DataflowRunner") == ("exact", "bq")
  args, beam_args = parse_args(["run", *TARGET])
  assert (args.runner, args.mode, args.sink) == ("DirectRunner", "sampled",
                                                 "bq_client")
  assert beam_args == []
  args, beam_args = parse_args([
      "run", *TARGET, "--runner", "DataflowRunner", "--temp_location",
      "gs://demo-bucket/tmp"
  ])
  assert (args.mode, args.sink) == ("exact", "bq")
  assert beam_args == ["--temp_location", "gs://demo-bucket/tmp"]
  args, _ = parse_args([
      "run", *TARGET, "--mode", "exact", "--sink", "local_json",
      "--output_local", "/tmp/sdfb-eval-out"
  ])
  assert (args.mode, args.sink) == ("exact", "local_json")  # explicit wins


def test_run_flags_default_to_the_brief():
  args, _ = parse_args(["run", *TARGET])
  assert (args.sample_rows, args.privacy_sample_rows,
          args.detection_sample_rows) == (200_000, 50_000, 50_000)
  assert (args.pair_max_columns, args.row_flags_top_k) == (20, 100)
  assert args.row_flags_source_keys == "hashed"
  assert args.max_bytes_billed == 1_099_511_627_776
  assert args.max_shuffle_gb == 500.0
  assert args.output_dataset == "synthetic_data_quality"
  assert args.temp_dataset is None  # the planner's <project>.<output_dataset>
  assert (args.scope, args.allow_contaminated) == ("auto", False)
  assert (args.fail_on, args.trigger) == ("none", "cli")
  assert args.label_key_uri is None and args.thresholds_uri is None
  knobs = driver.knobs_from_args(args, "eval-20260914T080000Z-00000001")
  assert knobs == Knobs(evaluation_id="eval-20260914T080000Z-00000001")
  flags = public_run_flags()
  assert {
      "--project", "--region", "--generation_job_id", "--run_id",
      "--relationships_uri", "--tables", "--seed_table", "--reference_dataset",
      "--reference_rows_limit", "--validation_runs_table", "--landing_dataset",
      "--mode", "--scope", "--allow_contaminated", "--sample_rows",
      "--privacy_sample_rows", "--detection_sample_rows", "--pair_max_columns",
      "--row_flags_top_k", "--row_flags_source_keys", "--max_bytes_billed",
      "--max_shuffle_gb", "--output_dataset", "--temp_dataset", "--sink",
      "--output_local", "--thresholds_uri", "--fail_on", "--trigger",
      "--label_key_uri", "--runner"
  } == set(flags)
  assert "--fixture_dir" not in flags  # hidden
  assert "--evaluation_id" not in flags  # minted, never passed (R88c)


def test_the_top_k_size_is_a_constant_not_a_knob():
  """Ruling R113 (I7): `--topk_profile` did nothing — the census keeps
  `TOPK_ITEMS` values a profile — yet it was a knob of the evaluation
  key, so changing it changed the salt and every sample for no effect.
  It is gone from the flags, the knobs, the key and the template."""
  args, _ = parse_args(["run", *TARGET])
  assert not hasattr(args, "topk_profile")
  assert "--topk_profile" not in public_run_flags()
  knobs = driver.knobs_from_args(args, "eval-20260914T080000Z-00000001")
  assert "topk_profile" not in knobs.to_dict()
  assert "topk_profile" not in knobs.key_dict()
  with pytest.raises(TypeError):
    Knobs(topk_profile=1000)  # pylint: disable=unexpected-keyword-arg  # the removed knob
  # the repository's one template (absent from a copy of the package alone)
  root = Path(driver.__file__).resolve().parents[5]
  template = root / "docker" / "flex_template_metadata.json"
  if template.is_file():
    metadata = json.loads(template.read_text(encoding="utf-8"))
    assert "topk_profile" not in {p["name"] for p in metadata["parameters"]}
  assert census.TOPK_ITEMS == 50


@pytest.mark.parametrize(
    "extra, needle",
    [
        (["--row_flags_source_keys", "raw"], "row_flags_source_keys"),  # R88a
        (["--output_local", "/tmp/out[1]"], "glob"),
        (["--output_local", "/tmp/out*"], "glob"),
        (["--evaluation_id", "eval-mine"], "evaluation_id"),  # R88c
        (["--experiments=enable_data_sampling"], "enable_data_sampling"),
        (["--experiments", "use_runner_v2,enable_data_sampling"
         ], "enable_data_sampling"),
        (["--experiment=enable_data_sampling=true"], "enable_data_sampling"),
        (["--runner", "DataflowRunner", "--sink", "bq_client"
         ], "DataflowRunner"),
        (["--sink", "local_json"], "output_local"),
        (["--sink", "bq", "--output_local", "/tmp/out"], "output_local"),
        (["--label_key_uri", "relative/key.bin"], "label_key_uri"),
        (["--run_id", "thelook-0913-a1b2c3"], "one of"),
        (["--mode", "approximate"], "mode"),
        (["--fixture_dir", "/tmp/fixture"], "fixture_dir"),
        (["--fail_on", "error"], "fail_on"),
        (["--sample_rows", "0"], "sample_rows"),
    ])
def test_run_usage_errors_exit_2(extra, needle, capsys):
  assert needle in _usage_error(["run", *TARGET, *extra], capsys)


def test_a_malformed_beam_argument_is_a_usage_error_before_anything_starts(
    bq, resolved, stub, capsys):
  """Exit 2 always means nothing was started: no launch resolved, no
  registry row, no DDL — also when the bad argument is one of Beam's."""
  for extra in (["--num_workers",
                 "abc"], ["--max_num_workers=many"], ["--temp_location"]):
    with pytest.raises(SystemExit) as info:
      main(["run", *TARGET, *extra], make_env(bq))
    assert info.value.code == 2, extra
    err = capsys.readouterr().err
    assert "sdfb-eval run: error: Beam arguments:" in err
    assert extra[0].split("=", maxsplit=1)[0] in err
  assert resolved == [] and stub.built == []
  assert bq.loads == [] and bq.executed == [] and bq.queries == []
  # a well-formed Beam argument passes through untouched
  _, extras = parse_args(["run", *TARGET, "--num_workers", "3"])
  assert extras == ["--num_workers", "3"]


def test_an_empty_value_means_the_flag_was_not_given():
  """A flex template passes every parameter as `--name=value`, the unset
  ones empty: text, choice, number and boolean flags alike fall back to
  their default."""
  baseline, _ = parse_args(["run", *TARGET])
  empties = [
      "--output_dataset=", "--temp_dataset=", "--relationships_uri=",
      "--run_id=", "--tables=", "--label_key_uri=", "--thresholds_uri=",
      "--output_local=", "--mode=", "--scope=", "--sink=", "--fail_on=",
      "--trigger=", "--row_flags_source_keys=", "--runner=", "--sample_rows=",
      "--privacy_sample_rows=", "--pair_max_columns=", "--max_bytes_billed=",
      "--max_shuffle_gb=", "--allow_contaminated="
  ]
  args, extras = parse_args(["run", *TARGET, *empties])
  assert vars(args) == vars(baseline) and extras == []
  assert (args.output_dataset, args.mode, args.sample_rows,
          args.runner) == ("synthetic_data_quality", "sampled", 200_000,
                           "DirectRunner")
  # the two-token form, and whitespace
  args, extras = parse_args([
      "run", *TARGET, "--output_dataset", "", "--sample_rows", " ", "--mode",
      "", "--allow_contaminated", ""
  ])
  assert vars(args) == vars(baseline) and extras == []
  # a value is still a value
  args, _ = parse_args(
      ["run", *TARGET, "--sample_rows=7", "--mode=exact", "--fail_on=warn"])
  assert (args.sample_rows, args.mode, args.fail_on) == (7, "exact", "warn")
  # an empty Beam argument is Beam's to read
  _, extras = parse_args(["run", *TARGET, "--temp_location="])
  assert extras == ["--temp_location="]
  # the other commands follow the same rule
  args, _ = parse_args([
      "plan", *TARGET, "--format=", "--dry_run=", "--scope=", "--sample_rows="
  ])
  assert (args.format, args.dry_run, args.scope,
          args.sample_rows) == ("text", False, "auto", 200_000)
  args, _ = parse_args([
      "report", "--project", PROJECT, "--evaluation_id", "eval-x", "--format=",
      "--output_dataset=", "--out=", "--local="
  ])
  assert (args.format, args.output_dataset, args.out,
          args.local) == ("md", "synthetic_data_quality", None, None)
  args, _ = parse_args(
      ["schemas", "--project", PROJECT, "--dataset=", "--apply="])
  assert (args.dataset, args.apply) == ("synthetic_data_quality", False)


def test_an_empty_required_value_is_still_missing(capsys):
  err = _usage_error(
      ["run", "--project=", "--job_id", JOB_ID, "--region", REGION], capsys)
  assert "--project is required" in err
  err = _usage_error(["schemas", "--project="], capsys)
  assert "--project" in err


def test_a_malformed_thresholds_file_is_a_usage_error(tmp_path, capsys):
  path = tmp_path / "broken.yaml"
  path.write_text("thresholds: {column.ks: [unclosed\n")
  err = _usage_error(["run", *TARGET, "--thresholds_uri", str(path)], capsys)
  assert "sdfb-eval run: error: --thresholds_uri:" in err
  assert "Traceback" not in err


def test_the_label_key_uri_forms_have_one_definition():
  for uri in ("projects/demo-project/secrets/sdfb-eval-label/versions/3",
              "gs://demo-bucket/keys/label.key", "/etc/sdfb/label.key"):
    assert is_label_key_uri(uri)
    args, _ = parse_args(["run", *TARGET, "--label_key_uri", uri])
    assert args.label_key_uri == uri
  for uri in ("relative/key.bin", "projects/demo-project/secrets/label",
              "https://example.com/key"):
    assert not is_label_key_uri(uri)


@pytest.mark.parametrize("command", ["plan", "run"])
def test_generation_job_id_is_the_job_id_under_the_templates_name(
    command, capsys):
  """The template launcher drops a parameter named `job_id`, so a template
  launch names the generation job `--generation_job_id`: the same argument."""
  rest = ["--project", PROJECT, "--region", REGION]
  by_old_name, _ = parse_args([command, *rest, "--job_id", JOB_ID])
  by_new_name, _ = parse_args([command, *rest, "--generation_job_id", JOB_ID])
  assert vars(by_new_name) == vars(by_old_name)
  assert by_new_name.job_id == JOB_ID
  by_template, _ = parse_args([command, *rest, f"--generation_job_id={JOB_ID}"])
  assert vars(by_template) == vars(by_old_name)
  # an empty value is "not given", so the usage error is today's
  err = _usage_error([command, *rest, "--generation_job_id="], capsys)
  assert "name exactly one of --job_id, --run_id, --tables" in err
  assert "(given: none)" in err
  # with another target named, the empty one is simply absent
  args, _ = parse_args(
      [command, *rest, "--generation_job_id=", "--run_id", "base"])
  assert (args.job_id, args.run_id) == (None, "base")


def test_a_run_needs_exactly_one_target(capsys):
  err = _usage_error(["run", "--project", PROJECT], capsys)
  assert "--job_id" in err and "--run_id" in err and "--tables" in err
  err = _usage_error(["run", "--project", PROJECT, "--job_id", JOB_ID], capsys)
  assert "--region" in err  # a job id is only visible in its region
  err = _usage_error(["run", "--job_id", JOB_ID, "--region", REGION], capsys)
  assert "--project" in err


def test_other_commands_take_no_beam_arguments(capsys):
  for argv in (["plan", *TARGET, "--temp_location", "gs://demo-bucket/tmp"], [
      "report", "--project", PROJECT, "--evaluation_id", "eval-x", "--streaming"
  ], ["catalogue", "--runner", "DataflowRunner"]):
    assert "unrecognized arguments" in _usage_error(argv, capsys)
  err = _usage_error(["report", "--project", PROJECT], capsys)
  assert "--evaluation_id" in err
  err = _usage_error(
      ["compare", "--project", PROJECT, "--evaluation_ids", "eval-a"], capsys)
  assert "two" in err


def test_manual_target_maps_to_the_resolver_input():
  args, _ = parse_args([
      "run", "--project", PROJECT, "--tables", "users,orders",
      "--landing_dataset", "thelook_synthetic", "--reference_dataset",
      "bigquery-public-data.thelook_ecommerce", "--relationships_uri",
      "gs://demo-bucket/synthetic/relationships/", "--scope", "manual"
  ])
  job_id, manual = driver.launch_request(args)
  assert job_id is None
  assert manual == {
      "tables_in_order": [f"{DS}.users", f"{DS}.orders"],
      "reference_table": f"{SRC}.users",
      "relationships_uri": "gs://demo-bucket/synthetic/relationships/",
      "params": {
          "relationships_uri": "gs://demo-bucket/synthetic/relationships/"
      },
  }
  args, _ = parse_args(["run", *TARGET])
  assert driver.launch_request(args) == (JOB_ID, None)


# --------------------------------------------------------------------------
# pipeline options (R88e, the Task 26 review note)
# --------------------------------------------------------------------------
def _big_plan(bq):
  """The thelook plan with 9 M rows a side: side inputs past the 512 MB
  cache floor."""
  plan = build_plan(
      launch=thelook_launch(bq),
      models=thelook_models(),
      bq=bq,
      knobs=Knobs(temp_dataset=QDS),
      mode="exact",
      trigger="cli",
      runner="DataflowRunner",
      now=NOW)
  return dataclasses.replace(
      plan,
      tables=tuple(
          dataclasses.replace(
              t, rows_source=9_000_000, rows_synthetic=9_000_000)
          for t in plan.tables))


def test_options_carry_the_plan_sized_cache_and_keep_upload_graph(bq):
  plan = _big_plan(bq)
  wanted = beam_pipeline.pipeline_options_defaults(
      "DataflowRunner", plan)["max_cache_memory_usage_mb"]
  assert wanted > beam_pipeline.MIN_CACHE_MB
  args, beam_args = parse_args([
      "run", *TARGET, "--runner", "DataflowRunner", "--temp_location",
      "gs://demo-bucket/tmp", "--experiments=use_runner_v2", "--experiments",
      "shuffle_mode=service", "--save_main_session"
  ])
  options = driver.pipeline_options(args, beam_args, plan,
                                    "eval-20260914T080000Z-00000001")
  assert options.view_as(WorkerOptions).max_cache_memory_usage_mb == wanted
  assert options.view_as(SetupOptions).save_main_session is False
  experiments = options.view_as(DebugOptions).experiments
  assert experiments == [
      "use_runner_v2", "shuffle_mode=service", "upload_graph"
  ]
  assert options.view_as(StandardOptions).runner == "DataflowRunner"
  cloud = options.view_as(GoogleCloudOptions)
  assert (cloud.project, cloud.region) == (PROJECT, REGION)
  assert cloud.temp_location == "gs://demo-bucket/tmp"
  assert cloud.job_name == "sdfb-eval-20260914t080000z-00000001"
  # a larger cache the operator asked for is kept; a smaller one is raised
  args, beam_args = parse_args([
      "run", *TARGET, "--runner", "DataflowRunner",
      f"--max_cache_memory_usage_mb={wanted + 1000}", "--job_name", "mine"
  ])
  options = driver.pipeline_options(args, beam_args, plan, "eval-x")
  assert options.view_as(
      WorkerOptions).max_cache_memory_usage_mb == wanted + 1000
  assert options.view_as(GoogleCloudOptions).job_name == "mine"
  args, beam_args = parse_args(["run", *TARGET, "--experiments=use_fastavro"])
  options = driver.pipeline_options(args, beam_args, plan, "eval-x")
  assert options.view_as(DebugOptions).experiments == ["use_fastavro"]
  assert options.view_as(WorkerOptions).max_cache_memory_usage_mb == wanted
  with pytest.raises(ValueError, match="enable_data_sampling"):
    driver.pipeline_options(args, ["--experiments=enable_data_sampling"], plan,
                            "eval-x")


def test_a_local_run_takes_its_runner_from_the_defaults(bq, monkeypatch):
  """`--runner DirectRunner` is the operator's word and the registry's;
  the pipeline runs on the runner `pipeline_options_defaults` returns
  (Beam hands a `DirectRunner` batch pipeline to Prism, which the
  evaluation refuses). The operator's Beam arguments are the only flags
  parsed: never `sys.argv`."""
  plan = _big_plan(bq)
  monkeypatch.setattr("sys.argv", ["sdfb-eval", "--runner", "PrismRunner"])
  for spelling in ([], ["--runner", "DirectRunner"], ["--runner", "direct"],
                   ["--runner", "SwitchingDirectRunner"]):
    args, beam_args = parse_args(["run", *TARGET, *spelling])
    wanted = beam_pipeline.pipeline_options_defaults(args.runner, plan)
    assert wanted["runner"] == "FnApiRunner"
    options = driver.pipeline_options(args, beam_args, plan, "eval-x")
    assert options.view_as(StandardOptions).runner == "FnApiRunner"
    # the pipeline the driver makes of them is one the evaluation admits
    pipeline = driver.Env().make_pipeline(options)
    assert type(pipeline.runner).__name__ == "FnApiRunner"
    beam_pipeline._checked_runner(pipeline)  # pylint: disable=protected-access  # the refusal itself
  # what the operator asked for is kept as asked
  assert args.runner == "SwitchingDirectRunner"
  args, beam_args = parse_args(["run", *TARGET, "--runner", "FnApiRunner"])
  options = driver.pipeline_options(args, beam_args, plan, "eval-x")
  assert options.view_as(StandardOptions).runner == "FnApiRunner"


@pytest.mark.parametrize("spelling", [
    "PrismRunner", "prism",
    "apache_beam.runners.portability.prism_runner.PrismRunner"
])
def test_the_prism_runner_is_a_usage_error_before_anything_starts(
    spelling, bq, resolved, stub, capsys):
  for command in ("run", "plan"):
    with pytest.raises(SystemExit) as info:
      main([command, *TARGET, "--runner", spelling], make_env(bq))
    assert info.value.code == 2
    err = capsys.readouterr().err
    assert f"sdfb-eval {command}: error: " in err
    assert "does not run on Prism" in err and "DirectRunner" in err
  assert not resolved and not stub.built
  assert not bq.loads and not bq.executed and not bq.queries


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------
def _copy_launch(bq):
  launch = thelook_launch(bq, write_disposition="append")
  copies = tuple(dataclasses.replace(w, job_type="COPY") for w in launch.writes)
  return dataclasses.replace(launch, writes=copies)


def test_plan_prints_tables_scopes_methods_bytes_and_shuffle(
    bq, resolved, capsys):
  del resolved
  assert main(["plan", *TARGET, "--dry_run"], make_env(bq)) == 0
  out = capsys.readouterr().out
  assert "eval-20260914T080000Z-00000001" in out
  for landing in TABLES:
    assert landing in out
  assert f"{SRC}.orders" in out
  assert "scope table / ok" in out
  assert "BigQuery dry-run bytes" in out and "predicted shuffle" in out
  assert re.search(r"email\s+STRING\s+identifier", out)
  assert re.search(r"status\s+STRING\s+categorical\s+census exact", out)
  assert re.search(r"num_of_item\s+INT(64|EGER)\s+numeric", out)
  assert "orders(user_id) -> users(id)" in out
  assert "read-only parent" in out  # products
  # a plan writes nothing: no registry row, no prepare DDL (R5)
  assert bq.loads == [] and bq.executed == []
  assert main(["plan", *TARGET, "--format", "json"], make_env(bq)) == 0
  document = json.loads(capsys.readouterr().out)
  assert document["mode"] == "sampled" and document["runner"] == "DirectRunner"
  assert document["bq_bytes_estimate"] > 0
  assert document["predicted_shuffle_gb"] >= 0
  orders = next(t for t in document["tables"] if t["name"] == "orders")
  assert orders["scope"] == {
      "mode": "table",
      "status": "ok",
      "reason": None,
      "read_table": f"{DS}.orders"
  }
  methods = {c["name"]: c for c in orders["columns"]}
  assert methods["status"]["kind"] == "categorical"
  assert methods["status"]["census"] == "exact"
  assert methods["order_id"]["is_key"] is True
  assert len(document["prepare_sql"]) == 3  # the source pins a run creates
  assert document["planning_ddl"] == []


def test_plan_dry_run_still_creates_as_of_diff_start_snapshots(
    bq, monkeypatch, capsys):
  monkeypatch.setattr(driver, "resolve_launch",
                      lambda **kwargs: _copy_launch(bq))
  monkeypatch.setattr(driver, "load_models", lambda uri: thelook_models())
  assert main(["plan", *TARGET, "--dry_run", "--format", "json"],
              make_env(bq)) == 0
  document = json.loads(capsys.readouterr().out)
  executed = [sql for sql, _ in bq.executed]
  assert len(executed) == 3 and all(
      sql.startswith("CREATE SNAPSHOT TABLE") and "_start_" in sql
      for sql in executed)  # R57: the one thing planning creates
  assert document["planning_ddl"] == executed
  assert bq.loads == []
  scopes = {t["name"]: t["scope"] for t in document["tables"]}
  assert scopes["orders"]["mode"] == "as_of_diff"
  assert scopes["orders"]["status"] == "ok"
  assert main(["plan", *TARGET, "--dry_run"], make_env(bq)) == 0
  text = capsys.readouterr().out
  assert "planning already created" in text and "--no_planning_snapshots" in text


def test_no_planning_snapshots_creates_nothing_and_marks_scopes_unplanned(
    bq, monkeypatch, capsys):
  monkeypatch.setattr(driver, "resolve_launch",
                      lambda **kwargs: _copy_launch(bq))
  monkeypatch.setattr(driver, "load_models", lambda uri: thelook_models())
  argv = ["plan", *TARGET, "--dry_run", "--no_planning_snapshots"]
  assert main([*argv, "--format", "json"], make_env(bq)) == 0
  document = json.loads(capsys.readouterr().out)
  assert bq.executed == [] and bq.loads == []
  assert document["planning_ddl"] == []
  for table in document["tables"]:
    if table["role"] == "external":
      continue
    assert table["scope"]["status"] == "unknown", table["name"]
    assert "--no_planning_snapshots" in table["scope"]["reason"]
    assert table["evaluated"] is False
    assert "--no_planning_snapshots" in table["skip_reason"]
  assert main(argv, make_env(bq)) == 0
  text = capsys.readouterr().out
  assert text.count("UNPLANNED") == 3
  assert "nothing can be evaluated" in text


def test_plan_of_a_manual_target_resolves_and_plans(bq, tmp_path, capsys):
  model = tmp_path / "thelook.yaml"
  model.write_text(MODEL_YAML)
  argv = [
      "plan", "--project", PROJECT, "--tables", "users,orders,order_items",
      "--landing_dataset", "thelook_synthetic", "--reference_dataset", SRC,
      "--relationships_uri",
      str(model), "--scope", "manual", "--format", "json"
  ]
  assert main(argv, make_env(bq)) == 0
  document = json.loads(capsys.readouterr().out)
  assert document["launch"]["params_source"] == "manual"
  assert document["launch"]["generation_job_id"] is None
  tables = {t["name"]: t for t in document["tables"]}
  assert list(tables) == ["products", "users", "orders", "order_items"]
  assert tables["orders"]["source_table"] == f"{SRC}.orders"
  assert tables["orders"]["scope"]["mode"] == "manual"
  assert tables["orders"]["evaluated"] is True
  assert tables["order_items"]["edges"] == [
      "order_items(order_id) -> orders(order_id)",
      "order_items(product_id) -> synthetic_data.products(id)"
  ]
  # no panel: a manual launch carries no reference_rows_limit
  assert tables["users"]["panel"] is None


def _derived_argv(model: Path, *extra: str) -> list[str]:
  return [
      "plan", "--project", PROJECT, "--landing_dataset", "thelook_synthetic",
      "--reference_dataset", SRC, "--relationships_uri",
      str(model), "--scope", "manual", *extra
  ]


def test_relationships_uri_alone_names_the_tables(bq, tmp_path, capsys):
  """No job, no log, no `--tables`: the model's enabled tables, parents
  first, plan exactly as the same list given by hand."""
  model = tmp_path / "thelook.yaml"
  model.write_text(MODEL_YAML)
  assert main(_derived_argv(model, "--format", "json"), make_env(bq)) == 0
  derived = json.loads(capsys.readouterr().out)
  by_hand = [
      "plan", "--project", PROJECT, "--tables", "users,orders,order_items",
      "--landing_dataset", "thelook_synthetic", "--reference_dataset", SRC,
      "--relationships_uri",
      str(model), "--scope", "manual", "--format", "json"
  ]
  assert main(by_hand, make_env(bq)) == 0
  listed = json.loads(capsys.readouterr().out)
  assert derived["launch"]["generation_job_id"] is None
  assert [t["name"] for t in derived["tables"]
         ] == [t["name"] for t in listed["tables"]]
  assert derived["tables"][1]["source_table"] == f"{SRC}.users"


def test_derived_tables_skip_a_disabled_table_and_put_parents_first():
  models = relationships.from_sources([("m.yaml", """
model: m
tables:
  child:
    pk: [id]
    fk:
      - {cols: [p], ref: parent, ref_cols: [id]}
  parent:
    pk: [id]
  parked:
    pk: [id]
    enabled: false
""")])
  assert driver.derived_tables(models) == ["parent", "child"]


def test_derived_tables_with_nothing_readable_raise_and_list_everything(
    bq, tmp_path):
  model = tmp_path / "thelook.yaml"
  model.write_text(MODEL_YAML)
  for name in ("users", "orders", "order_items"):
    del bq.tables[f"{DS}.{name}"]  # nothing landed
  args, _ = parse_args(_derived_argv(model))
  with pytest.raises(driver.TargetCheckError) as raised:
    driver.plan(args, make_env(bq))
  message = str(raised.value)
  for name in ("users", "orders", "order_items"):
    assert f"{name}: the landing table" in message


def test_relationships_uri_alone_needs_both_datasets(capsys):
  err = _usage_error(
      ["run", "--project", PROJECT, "--relationships_uri", "gs://b/m.yaml"],
      capsys)
  assert "--landing_dataset and --reference_dataset" in err
  err = _usage_error(["run", "--project", PROJECT], capsys)
  assert "name exactly one of" in err


OTHER_YAML = """
model: other
tables:
  alpha:
    pk: [id]
  beta:
    pk: [id]
    fk:
      - {cols: [a], ref: alpha, ref_cols: [id]}
"""


def _seed_args(seed: str, uri: str | None, *extra: str):
  argv = [
      "plan", "--project", PROJECT, "--seed_table", seed, "--landing_dataset",
      "thelook_synthetic", "--reference_dataset", SRC, *extra
  ]
  if "--scope" not in extra:  # a hand-named target has no write disposition
    argv += ["--scope", "manual"]
  if uri is not None:
    argv += ["--relationships_uri", uri]
  args, _ = parse_args(argv)
  return args


def _seed_plan(bq, seed: str, uri: str | None, *extra: str):
  planned, _ = driver.plan(_seed_args(seed, uri, *extra), make_env(bq))
  return planned


def _evaluated(planned) -> list[str]:
  """The launch's tables planning will evaluate (not merely plan)."""
  return [
      t.name for t in planned.tables if t.role != "external" and t.evaluated
  ]


def _two_groups(tmp_path: Path) -> str:
  folder = tmp_path / "models"
  folder.mkdir()
  (folder / "thelook.yaml").write_text(MODEL_YAML)
  (folder / "other.yaml").write_text(OTHER_YAML)
  return str(folder)


def test_a_seed_table_is_a_target_with_both_datasets(capsys):
  base = ["run", "--project", PROJECT, "--seed_table", "orders"]
  datasets = ["--landing_dataset", "d", "--reference_dataset", "s"]
  args, _ = parse_args([*base, *datasets])
  assert args.seed_table == "orders"
  # a job id may accompany a seed (R134, below); a second target may not
  for other in (["--tables", "orders"], ["--run_id", "r1"]):
    err = _usage_error([*base, *datasets, *other], capsys)
    assert "name exactly one of" in err and "--seed_table" in err
  err = _usage_error(base, capsys)
  assert "--landing_dataset and --reference_dataset" in err
  err = _usage_error([*base, "--landing_dataset", "d"], capsys)
  assert "--landing_dataset and --reference_dataset" in err
  # the datasets alone are no target
  err = _usage_error(["run", "--project", PROJECT, *datasets], capsys)
  assert "name exactly one of" in err


def test_a_seed_evaluates_only_its_component_parents_first(bq, tmp_path):
  folder = _two_groups(tmp_path)
  assert _evaluated(_seed_plan(bq, "orders",
                               folder)) == ["users", "orders", "order_items"]
  # another table of the same component: the same launch
  assert _evaluated(_seed_plan(bq, "users",
                               folder)) == ["users", "orders", "order_items"]


def test_a_seed_no_model_names_is_evaluated_alone(bq, tmp_path):
  model = tmp_path / "other.yaml"
  model.write_text(OTHER_YAML)  # a model, but not one that names `users`
  assert _evaluated(_seed_plan(bq, "users", str(model))) == ["users"]


def test_a_disabled_seed_is_evaluated_alone(bq, tmp_path):
  model = tmp_path / "thelook.yaml"
  model.write_text(
      MODEL_YAML.replace("  orders:\n", "  orders:\n    enabled: false\n"))
  assert _evaluated(_seed_plan(bq, "orders", str(model))) == ["orders"]


def test_a_seed_without_a_relationships_uri_is_evaluated_alone(bq):
  planned = _seed_plan(bq, "orders", None)
  assert _evaluated(planned) == ["orders"]
  assert planned.launch.relationships_uri is None


def test_a_uri_with_no_model_file_evaluates_the_seed_alone_and_says_so(
    bq, tmp_path):
  """The repository's default folder holds samples only, which a scan
  skips: a single-table launch is still evaluated."""
  empty = tmp_path / "samples"
  empty.mkdir()
  (empty / "example_model.yaml").write_text(MODEL_YAML)
  planned = _seed_plan(bq, "orders", str(empty))
  assert _evaluated(planned) == ["orders"]
  assert planned.launch.relationships_uri is None
  (note,) = [w for w in planned.warnings if "no model file" in w]
  assert str(empty) in note and "alone" in note
  assert note in planned.registry_seed()["warnings"]


def test_a_malformed_model_still_raises_for_a_seed(bq, tmp_path):
  model = tmp_path / "broken.yaml"
  model.write_text("""
model: broken
tables:
  orders:
    pk: [order_id]
    fk:
      - {cols: [user_id], ref: nobody, ref_cols: [id]}
""")
  with pytest.raises(relationships.RelationshipError):
    _seed_plan(bq, "orders", str(model))


def test_a_uri_with_no_model_file_still_raises_for_every_other_target(
    bq, tmp_path):
  empty = tmp_path / "samples"
  empty.mkdir()
  args, _ = parse_args(_derived_argv(empty))
  with pytest.raises(relationships.RelationshipError, match="no model files"):
    driver.plan(args, make_env(bq))
  args, _ = parse_args([
      "plan", "--project", PROJECT, "--tables", "users", "--landing_dataset",
      "thelook_synthetic", "--reference_dataset", SRC, "--relationships_uri",
      str(empty), "--scope", "manual"
  ])
  with pytest.raises(relationships.RelationshipError, match="no model files"):
    driver.plan(args, make_env(bq))


def test_one_unreadable_table_is_skipped_by_planning_and_warned(bq, tmp_path):
  """R61/R132: the component's other tables are still evaluated, and the
  pre-flight's finding reaches the plan's and the registry row's warnings."""
  folder = _two_groups(tmp_path)
  del bq.tables[f"{DS}.order_items"]  # a child that never landed
  planned = _seed_plan(bq, "orders", folder)
  assert _evaluated(planned) == ["users", "orders"]
  (note,) = [
      w for w in planned.warnings
      if w.startswith("order_items: the landing table")
  ]
  assert note in planned.registry_seed()["warnings"]


def test_a_seed_with_nothing_readable_raises_naming_only_its_tables(
    bq, tmp_path):
  folder = _two_groups(tmp_path)  # alpha and beta have no tables at all
  for name in ("users", "orders", "order_items"):
    del bq.tables[f"{DS}.{name}"]
  with pytest.raises(driver.TargetCheckError) as raised:
    _seed_plan(bq, "orders", folder)
  message = str(raised.value)
  for name in ("users", "orders", "order_items"):
    assert f"{name}: the landing table" in message
  assert "alpha" not in message and "beta" not in message


def test_an_empty_table_and_a_missing_column_are_left_to_planning(bq, tmp_path):
  folder = _two_groups(tmp_path)
  bq.tables[f"{DS}.users"]["numRows"] = 0
  bq.tables[f"{DS}.orders"]["schema"] = bq.tables[f"{DS}.orders"]["schema"][:2]
  planned = _seed_plan(bq, "orders", folder)  # no TargetCheckError
  assert not [w for w in planned.warnings if "is empty" in w]
  assert not [w for w in planned.warnings if "lacks source column" in w]


@pytest.mark.parametrize("name", ["demo-project.ds.orders", "or ders/x", "a:b"])
def test_a_malformed_seed_name_names_the_value(bq, name):
  with pytest.raises(driver.TargetCheckError) as raised:
    _seed_plan(bq, name, None)
  assert repr(name) in str(raised.value) and "--seed_table" in str(raised.value)


def test_a_seed_plan_without_scope_manual_evaluates_nothing_and_says_why(
    bq, tmp_path):
  """The reason the chained launch passes `scope=manual`: a hand-named
  target has no write disposition to derive a scope from."""
  folder = _two_groups(tmp_path)
  args = _seed_args("orders", folder)
  args.scope = "auto"
  planned, _ = driver.plan(args, make_env(bq))
  assert _evaluated(planned) == []
  assert any("scope unknown" in w and "--scope" in w for w in planned.warnings)


def test_a_reference_rows_limit_is_a_positive_integer(capsys):
  base = ["run", *TARGET]
  assert parse_args(base)[0].reference_rows_limit is None
  assert parse_args([
      *base,
      "--reference_rows_limit=",
  ])[0] == parse_args(base)[0]
  assert parse_args([*base, "--reference_rows_limit",
                     "10000"])[0].reference_rows_limit == 10_000
  for bad in ("0", "-5", "ten", "1.5"):
    err = _usage_error([*base, f"--reference_rows_limit={bad}"], capsys)
    assert "--reference_rows_limit" in err


def test_a_reference_rows_limit_fills_the_manual_params():
  args, _ = parse_args([
      "run", "--project", PROJECT, "--tables", "users", "--landing_dataset",
      "d", "--reference_dataset", "s", "--reference_rows_limit", "10000"
  ])
  _, manual = driver.launch_request(args)
  assert manual["params"]["reference_rows_limit"] == 10_000


def test_a_validation_runs_table_is_a_table_name(capsys):
  base = ["run", *TARGET]
  assert parse_args(base)[0].validation_runs_table is None
  assert parse_args([*base,
                     "--validation_runs_table="])[0] == parse_args(base)[0]
  ok = f"{PROJECT}.synthetic_data_quality.validation_runs"
  assert parse_args([*base, "--validation_runs_table",
                     ok])[0].validation_runs_table == ok
  colon = f"{PROJECT}:synthetic_data_quality.validation_runs"
  assert parse_args([*base, "--validation_runs_table",
                     colon])[0].validation_runs_table == ok
  for bad in ("validation_runs", "ds.validation_runs", "a.b.c.d",
              f"{PROJECT}.ds.run s", "`x`.y.z"):
    err = _usage_error([*base, f"--validation_runs_table={bad}"], capsys)
    assert "--validation_runs_table" in err


def test_a_validation_runs_table_fills_the_manual_params():
  table = f"{PROJECT}.synthetic_data_quality.validation_runs"
  args, _ = parse_args([
      "run", "--project", PROJECT, "--tables", "users", "--landing_dataset",
      "d", "--reference_dataset", "s", "--validation_runs_table", table
  ])
  _, manual = driver.launch_request(args)
  assert manual["params"]["validation_runs_table"] == table


def test_a_seed_plan_with_a_reference_rows_limit_plans_the_panel(bq, tmp_path):
  folder = _two_groups(tmp_path)
  note = "reference_rows_limit unknown"
  without = _seed_plan(bq, "orders", folder)
  assert [w for w in without.warnings if note in w]
  assert all(t.panel is None for t in without.tables)
  planned = _seed_plan(bq, "orders", folder, "--reference_rows_limit", "40")
  assert not [w for w in planned.warnings if note in w]
  assert [t.name for t in planned.tables if t.panel is not None]


def test_a_job_id_may_accompany_a_seed_table(capsys):
  seed = [
      "run", "--project", PROJECT, "--seed_table", "orders",
      "--landing_dataset", "d", "--reference_dataset", "s"
  ]
  for name in ("--generation_job_id", "--job_id"):
    args, _ = parse_args([*seed, name, JOB_ID, "--region", REGION])
    assert (args.job_id, args.seed_table) == (JOB_ID, "orders")
  # the job is read in a region
  assert "--region" in _usage_error([*seed, "--job_id", JOB_ID], capsys)
  # empty: not given, so no region is needed either
  args, _ = parse_args([*seed, "--generation_job_id="])
  assert args.job_id is None
  # still no second target
  for other in (["--tables", "orders"], ["--run_id", "r1"]):
    err = _usage_error([*seed, "--job_id", JOB_ID, "--region", REGION, *other],
                       capsys)
    assert "name exactly one of" in err
  # a job id with a model is the job id's own target, as before
  args, _ = parse_args(["run", *TARGET, "--relationships_uri", "gs://b/m.yaml"])
  assert args.job_id == JOB_ID and args.seed_table is None


def _job_plan(bq, load_fixture, tmp_path, session=None):
  """A seed plan with the generation job's id next to it, over the real
  `resolve_launch` and a session that serves only `jobs.get`."""
  job = load_fixture("context/dataflow_job.json")
  session = session or JobOnlySession(job)
  model = tmp_path / "thelook.yaml"
  model.write_text(MODEL_YAML)
  args, _ = parse_args([
      "run", "--project", PROJECT, "--region", REGION, "--seed_table", "orders",
      "--generation_job_id", JOB_ID, "--landing_dataset", "thelook_synthetic",
      "--reference_dataset", SRC, "--relationships_uri",
      str(model), "--scope", "manual", "--trigger", "chained"
  ])
  env = make_env(bq, session_factory=lambda project: session)
  planned, _ = driver.plan(args, env)
  return planned, session


def test_a_seed_with_a_job_id_keeps_the_job_and_reads_nothing_else(
    bq, load_fixture, tmp_path):
  planned, session = _job_plan(bq, load_fixture, tmp_path)
  job = load_fixture("context/dataflow_job.json")
  assert [m for m, _ in session.calls] == ["GET"]  # no Cloud Logging
  assert not [
      sql for sql, _ in bq.queries
      if "INFORMATION_SCHEMA.JOBS" in sql or "validation_runs" in sql
  ]
  launch = planned.launch
  assert (launch.generation_job_id, launch.region) == (JOB_ID, REGION)
  assert (launch.started_at, launch.finished_at) == (job["createTime"],
                                                     job["currentStateTime"])
  assert _evaluated(planned) == ["users", "orders", "order_items"]
  assert {t.scope.mode for t in planned.tables if t.evaluated} == {"manual"}
  # what the window changes: the source is pinned as of the job's create
  # time, within its time-travel window
  assert all(t.source_pin and t.source_pin.pinned
             for t in planned.tables
             if t.evaluated)
  row = planned.registry_seed()
  assert row["generation_job_id"] == JOB_ID and row["trigger"] == "chained"
  assert row["generation_started_at"] and row["generation_finished_at"]
  assert row["params_source"] == "manual"
  check_row("evaluation_data_history", row, ordered=False)


@pytest.mark.parametrize("failure", ["not found", "denied"])
def test_a_seed_with_an_unreadable_job_is_planned_without_a_window(
    bq, load_fixture, tmp_path, failure):
  session = JobOnlySession(None if failure == "not found" else "denied")
  planned, session = _job_plan(bq, load_fixture, tmp_path, session)
  assert [m for m, _ in session.calls] == ["GET"]
  launch = planned.launch
  assert launch.generation_job_id == JOB_ID
  assert (launch.started_at, launch.finished_at) == (None, None)
  assert _evaluated(planned) == ["users", "orders", "order_items"]
  (note,) = [w for w in planned.warnings if JOB_ID in w]
  assert "window" in note and "unknown" in note
  row = planned.registry_seed()
  assert row["generation_job_id"] == JOB_ID
  assert row["generation_started_at"] is None and note in row["warnings"]
  check_row("evaluation_data_history", row, ordered=False)


def test_a_run_id_target_reads_validation_runs(bq, capsys):
  bq.canned.append(("validation_runs", [{
      "run_id": f"{BASE}-{i:02d}-{name}",
      "landing_table": f"{DS}.{name}",
      "reference_table": f"{SRC}.{name}",
      "reference_digest": None,
      "valid_count": 120 * 2**i,
      "num_rows_requested": 120 * 2**i,
      "status": "PASSED",
      "created_at": "2026-09-13T13:45:00Z",
  } for i, name in enumerate(("users", "orders", "order_items"))]))
  argv = [
      "plan", "--project", PROJECT, "--run_id", BASE, "--scope", "manual",
      "--format", "json"
  ]
  assert main(argv, make_env(bq)) == 0
  document = json.loads(capsys.readouterr().out)
  sql, params = next((s, p) for s, p in bq.queries if "validation_runs" in s)
  assert f"`{QDS}.validation_runs`" in sql and params == {"base": BASE}
  assert document["launch"]["base_run_id"] == BASE
  assert document["launch"]["run_ids"] == [
      f"{BASE}-00-users", f"{BASE}-01-orders", f"{BASE}-02-order_items"
  ]
  tables = {t["name"]: t for t in document["tables"]}
  assert tables["order_items"]["rows_expected"] == 480
  assert tables["order_items"]["source_table"] == f"{SRC}.order_items"
  bq.canned.clear()
  bq.canned.append(("validation_runs", []))
  with pytest.raises(ValueError, match="no landing table"):
    main(argv, make_env(bq))
  capsys.readouterr()


# --------------------------------------------------------------------------
# schemas, catalogue
# --------------------------------------------------------------------------
def test_schemas_prints_the_commands_and_touches_nothing(bq, capsys):
  assert main(["schemas", "--project", PROJECT], make_env(bq)) == 0
  out = capsys.readouterr().out
  assert out.count("bq mk --table") == 4
  assert out.count("CREATE OR REPLACE VIEW") == 2
  for table in EVAL_TABLES:
    assert f"{PROJECT}:synthetic_data_quality.{table}" in out
  assert bq.executed == [] and bq.queries == []
  assert main(["schemas", "--project", PROJECT, "--dataset", "eval_dev"],
              make_env(bq)) == 0
  assert f"{PROJECT}:eval_dev.evaluation_metrics" in capsys.readouterr().out
  err = _usage_error(
      ["schemas", "--project", PROJECT, "--dataset", "d`; DROP TABLE x"],
      capsys)
  assert "do not name a BigQuery dataset" in err


def test_schemas_apply_issues_the_ddl(bq, capsys):
  assert main(["schemas", "--project", PROJECT, "--apply"], make_env(bq)) == 0
  executed = [sql for sql, _ in bq.executed]
  tables, views = executed[:4], executed[4:]
  assert [
      re.match(r"CREATE TABLE IF NOT EXISTS `([^`]+)`", sql).group(1)
      for sql in tables
  ] == [f"{QDS}.{name}" for name in EVAL_TABLES]
  assert [
      re.match(r"CREATE OR REPLACE VIEW `([^`]+)`", sql).group(1)
      for sql in views
  ] == [f"{QDS}.evaluation_latest", f"{QDS}.evaluation_latest_per_job"]
  history, metrics, _, flags = tables
  assert "PARTITION BY DATE(`recorded_at`)" in history
  assert "CLUSTER BY `relationship_model`, `engine`, `evaluation_id`" in history
  assert "`evaluation_id` STRING NOT NULL" in history
  assert "`run_ids` ARRAY<STRING>" in history
  assert re.search(
      r"`tables` ARRAY<STRUCT<`name` STRING[^>]*`table_score` FLOAT64>>",
      history)
  assert "`generation_params` JSON" in history
  assert "`check` STRING NOT NULL" in flags
  assert "PARTITION BY TIMESTAMP_TRUNC(`evaluated_at`, MONTH)" in metrics
  assert "CLUSTER BY " + ", ".join(
      f"`{c}`" for c in CLUSTERING["evaluation_metrics"]) in metrics
  assert "partition_expiration_days=180" in flags
  assert "partition_expiration_days" not in metrics
  assert 'OPTIONS(description="' in metrics
  out = capsys.readouterr().out
  assert out.count("created or kept") == 4 and out.count("replaced") == 2
  assert bq.loads == []


def test_catalogue_formats(capsys):
  assert main(["catalogue", "--format", "json"]) == 0
  out = capsys.readouterr().out
  assert out == catalogue().to_json()
  assert main(["catalogue", "--format", "md"]) == 0
  text = capsys.readouterr().out
  packaged = catalogue()
  assert f"catalogue {packaged.version}" in text
  for metric in packaged.metrics:
    assert f"`{metric.id}`" in text
  assert text.count("\n| `") == len(packaged.metrics)


def test_catalogue_out_writes_a_file(tmp_path: Path, capsys):
  path = tmp_path / "catalogue.json"
  assert main(["catalogue", "--format", "json", "--out", str(path)]) == 0
  assert path.read_text() == catalogue().to_json()
  assert str(path) in capsys.readouterr().out
