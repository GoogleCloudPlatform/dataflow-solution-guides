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
"""`sdfb-eval run` end to end on the DirectRunner over a fixture
directory (the hidden `--fixture_dir`), then `report` and `compare` over
what it wrote (Task 27).

The fixture is the acceptance generator's `users` → `orders` pair at a
reduced size (60 users), with the bad twin's defects on `orders` (amount
+ 500, status collapsed): one real pipeline run for the whole module,
never the four-minute acceptance itself (Ruling R88i). Nothing here is
real data.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from apache_beam.options.pipeline_options import StandardOptions
from beam.acceptance_data import (
    plant_defects,)
from unit.context.plan_fakes import (
    JOB_ID,
    RUN_IDS,
    TABLES,
    thelook_launch,
    thelook_models,
)

from sdfb_evaluation.beam import pipeline as beam_pipeline
from sdfb_evaluation.beam.io import LOAD_ORDER, InMemorySources
from sdfb_evaluation.cli import driver
from sdfb_evaluation.cli.main import main
from sdfb_evaluation.report import store
from sdfb_evaluation.report.render import (
    NOT_COMPARABLE,
    WITHIN_NOISE,
    render_json,
    render_markdown,
)
from sdfb_evaluation.schemas import load_schema
from sdfb_evaluation.scoring import is_aggregate

from .conftest import catalogue
from .helpers import (
    FIXTURE_PROJECT as PROJECT,
    NOW,
    RecordingBq,
    check_row,
    fixture_rows,
    make_env,
    write_fixture,
)

DATASET = "synthetic_data_quality"
USERS = 60
PANEL = 30
METRICS, PROFILES, REGISTRY = store.METRICS, store.PROFILES, store.REGISTRY
SECOND_ID = "eval-20260914T090000Z-0badc0de"


class _LoaderBq:
  """The `Bq` a `--fixture_dir` run still needs with `--sink bq_client`:
  it records the load jobs, in order."""

  def __init__(self) -> None:
    self.loads: list[tuple[str, list[dict]]] = []

  def load_json(self, fqn: str, rows: Sequence[Mapping[str, Any]],
                schema: list[dict]) -> str:
    project_dataset, table = fqn.rsplit(".", 1)
    assert project_dataset == f"{PROJECT}.{DATASET}"
    assert schema == load_schema(table)
    self.loads.append((table, [dict(row) for row in rows]))
    return f"load_{len(self.loads)}"


class _Ran:
  """What the module's one run left behind."""

  def __init__(self, tmp: Path, stdout: str, code: int, bq: _LoaderBq,
               source: Mapping[str, list], synthetic: Mapping[str, list],
               pipelines: Sequence[Any]):
    self.fixture = tmp / "fixture"
    self.out = tmp / "out"
    self.stdout = stdout
    self.code = code
    self.bq = bq
    self.pipelines = pipelines
    self.source = source
    self.synthetic = synthetic
    (self.directory,) = list(self.out.iterdir())
    self.evaluation_id = self.directory.name
    self.stored = store.read_local(str(self.directory))

  def failing_ids(self) -> list[str]:
    return sorted({
        r["metric_id"]
        for r in self.stored.metrics
        if r["status"] == "fail" and not is_aggregate(r["metric_id"])
    })


@pytest.fixture(scope="module", name="ran")
def fixture_ran(tmp_path_factory) -> _Ran:
  tmp = tmp_path_factory.mktemp("cli_run")
  source = fixture_rows(USERS, seed=5, id_base=100_000)
  synthetic = plant_defects(source,
                            fixture_rows(USERS, seed=6, id_base=500_000))
  write_fixture(tmp / "fixture", source, synthetic, panel=PANEL)
  bq = _LoaderBq()
  pipelines: list[Any] = []
  make = driver.Env().make_pipeline  # the driver's own, recorded

  def make_pipeline(options):
    pipelines.append(make(options))
    return pipelines[-1]

  env = driver.Env(
      make_bq=lambda project: bq,
      now=lambda: NOW,
      token=lambda: "0badc0de",
      make_pipeline=make_pipeline)
  stdout = io.StringIO()
  with contextlib.redirect_stdout(stdout):
    code = main([
        "run", "--fixture_dir",
        str(tmp / "fixture"), "--mode", "exact", "--sink", "bq_client",
        "--output_local",
        str(tmp / "out"), "--fail_on", "fail"
    ], env)
  return _Ran(tmp, stdout.getvalue(), code, bq, source, synthetic, pipelines)


@pytest.fixture(scope="module", name="tiny")
def fixture_tiny(tmp_path_factory) -> Path:
  """The cheapest real pipeline: `users` alone, 12 rows a side."""
  tmp = tmp_path_factory.mktemp("cli_tiny")
  return write_fixture(
      tmp / "fixture",
      fixture_rows(12, seed=5, id_base=100_000),
      fixture_rows(12, seed=6, id_base=500_000),
      tables=("users",))


def _registry_lines(directory: Path) -> list[dict]:
  return [
      json.loads(line)
      for path in sorted((directory / REGISTRY).glob("*.jsonl"))
      for line in path.read_text().splitlines()
  ]


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------
def test_run_over_fixtures_writes_every_table_and_trips_the_gate(ran):
  assert ran.code == 1  # --fail_on fail, and the bad twin fails metrics
  assert ran.evaluation_id == "eval-20260914T080000Z-0badc0de"
  assert ran.directory == ran.out / ran.evaluation_id  # <DIR>/<evaluation_id>
  tables = [table for table, _ in ran.bq.loads]
  # the RUNNING row first, the outputs in LOAD_ORDER, the FINAL row last
  assert tables[0] == REGISTRY and tables[-1] == REGISTRY
  assert tables[1:] == [t for t in LOAD_ORDER if t in tables[1:]]
  assert {METRICS, PROFILES} <= set(tables)
  running, final = ran.bq.loads[0][1][0], ran.bq.loads[-1][1][0]
  assert (running["event"], running["status"]) == ("RUNNING", "RUNNING")
  assert final["event"] == "FINAL"
  assert final["status"] in ("SUCCEEDED", "SUCCEEDED_WITH_WARNINGS")
  assert final["evaluation_id"] == running["evaluation_id"] == ran.evaluation_id
  assert final["recorded_at"] > running["recorded_at"]
  assert (final["mode"], final["runner"],
          final["trigger"]) == ("exact", "DirectRunner", "cli")
  assert final["params_source"] == "manual" and final["engine"] == "b1_rag"
  assert final["evaluation_params"]["label_key_mode"] == "ephemeral"
  assert [t["name"] for t in final["tables"]] == ["users", "orders"]
  assert [t["role"] for t in final["tables"]] == ["root", "driven"]
  assert all(t["reference_verified"] for t in final["tables"])
  for table, rows in ran.bq.loads:
    for row in rows:
      check_row(table, row, ordered=table == REGISTRY and row is running)
      assert row["evaluation_id"] == ran.evaluation_id
  measured = [r for r in ran.stored.metrics if not is_aggregate(r["metric_id"])]
  assert final["metrics_total"] == len(measured) > 0
  assert final["metrics_fail"] == sum(r["status"] == "fail" for r in measured)
  assert final["metrics_fail"] > 0
  # the planted defects of `orders` are among the FAIL rows
  failed = {(r["table_name"], r["metric_id"], r["column_name"])
            for r in measured
            if r["status"] == "fail"}
  assert ("orders", "column.ks", "amount") in failed
  assert ("orders", "column.top1_share_delta", "status") in failed
  # the edge was evaluated, both sides read from the fixture
  orphan = next(
      r for r in measured if r["metric_id"] == "relationship.orphan_rate")
  assert orphan["table_name"] == "orders" and orphan["status"] == "pass"
  assert ran.evaluation_id in ran.stdout
  assert "gate (--fail_on fail): TRIPPED" in ran.stdout
  assert f"written to: {PROJECT}.{DATASET}" in ran.stdout


def test_a_local_run_is_in_process_and_recorded_as_asked(ran):
  """`--runner DirectRunner` (the default): the real pipeline was built
  on the runner the evaluator's defaults give — not on Beam's own
  `DirectRunner`, which is Prism and which the pipeline refuses — while
  the registry records the runner as the operator named it."""
  (pipeline,) = ran.pipelines
  assert type(pipeline.runner).__name__ == "FnApiRunner"
  wanted = beam_pipeline.pipeline_options_defaults("DirectRunner")["runner"]
  assert pipeline.options.view_as(StandardOptions).runner == wanted
  assert [
      row["runner"]
      for _, rows in ran.bq.loads
      for row in rows
      if "runner" in row
  ] == ["DirectRunner"] * 2
  assert "DirectRunner" in ran.stdout and "Prism" not in ran.stdout


# a failed DirectRunner pipeline lets its worker threads die noisily
@pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_pipeline_failing_at_run_time_keeps_its_failed_row_locally(
    tiny, tmp_path, monkeypatch, capsys):
  """`--sink local_json`, the real pipeline, its FINAL step raising: Beam
  leaves its temp directory beside the driver's RUNNING shard, and the
  FAILED row must still land there (review item 1)."""

  def explode(summary, registry, failures, *signals):
    del summary, registry, failures, signals
    raise RuntimeError("the FINAL step failed on a worker")

  monkeypatch.setattr(beam_pipeline, "_final_row", explode)
  out = tmp_path / "out"
  argv = ["run", "--fixture_dir", str(tiny), "--output_local", str(out)]
  assert main(argv) == 3
  (directory,) = list(out.iterdir())
  leftovers = [p.name for p in (directory / REGISTRY).iterdir() if p.is_dir()]
  assert leftovers and all(n.startswith("beam-temp-") for n in leftovers)
  rows = _registry_lines(directory)
  assert sorted(r["status"] for r in rows) == ["FAILED", "RUNNING"]
  failed = next(r for r in rows if r["status"] == "FAILED")
  # Beam words the runner's error in more than one way: only its class
  assert failed["status_reason"].startswith("RuntimeError: ")
  check_row(REGISTRY, failed, ordered=False)
  captured = capsys.readouterr()
  assert "could not be written" not in captured.err
  # and the evaluation reads back as FAILED
  assert main(["report", "--local", str(directory)]) == 0
  assert f"# Evaluation `{directory.name}` — FAILED" in capsys.readouterr().out


def test_a_run_where_no_table_can_be_evaluated_exits_3(monkeypatch, capsys):
  """The real pipeline writes FINAL = FAILED itself when no launch table
  can be evaluated (here: no side of `users` can be read) and ends
  normally; `run` reads that row and exits 3, adding none. Only the
  FINAL status and the exit code are asserted here."""
  bq = RecordingBq()
  launch = thelook_launch(bq, tables=TABLES[:1], run_ids=RUN_IDS[:1])
  monkeypatch.setattr(driver, "resolve_launch", lambda **kwargs: launch)
  monkeypatch.setattr(driver, "load_models", lambda uri: thelook_models())
  env = make_env(
      bq,
      submit=driver.submit_pipeline,
      make_sources=lambda: InMemorySources({}))
  argv = [
      "run", "--project", PROJECT, "--region", "europe-west1", "--job_id",
      JOB_ID, "--fail_on", "none"
  ]
  assert main(argv, env) == 3
  events = [(r["event"], r["status"]) for r in bq.registry_rows()]
  assert events == [("RUNNING", "RUNNING"), ("FINAL", "FAILED")]
  capsys.readouterr()


def test_plan_of_a_fixture_touches_nothing(ran, capsys):

  def no_bq(project: str):
    raise AssertionError(f"a fixture plan asked for BigQuery ({project})")

  argv = ["plan", "--fixture_dir", str(ran.fixture), "--format", "json"]
  assert main(argv, driver.Env(make_bq=no_bq)) == 0
  document = json.loads(capsys.readouterr().out)
  assert [t["name"] for t in document["tables"]] == ["users", "orders"]
  assert document["launch"]["params_source"] == "manual"
  assert document["prepare_sql"] == [] and document["planning_ddl"] == []
  assert document["tables"][1]["edges"] == ["orders(user_id) -> users(id)"]
  assert document["tables"][0]["panel"]["reference_n"] == PANEL


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def test_report_renders_every_failing_metric_with_its_interpretation(
    ran, capsys):
  assert main(["report", "--local", str(ran.directory)]) == 0
  text = capsys.readouterr().out
  failing = ran.failing_ids()
  assert len(failing) > 5
  packaged = catalogue()
  for metric_id in failing:
    metric = packaged.get(metric_id)
    assert f"### `{metric_id}` — {metric.title}" in text
    assert " ".join(metric.interpretation.bad.split()) in text
    assert " ".join(metric.purpose.split()) in text
  final = ran.stored.final
  status, fails, total = (
      final[k] for k in ("status", "metrics_fail", "metrics_total"))
  assert f"# Evaluation `{ran.evaluation_id}` — {status}" in text
  assert f"## Failing metrics ({fails} rows, {len(failing)} metrics)" in text
  assert f"{total:,} total" in text
  assert "## Not evaluated" in text and "## Run warnings" in text
  assert "| orders | driven |" in text
  # the --output_local parent and an id name the same evaluation
  assert main(
      ["report", "--local",
       str(ran.out), "--evaluation_id", ran.evaluation_id]) == 0
  assert capsys.readouterr().out == text
  with pytest.raises(LookupError, match="no evaluation output for eval-none"):
    main(["report", "--local", str(ran.out), "--evaluation_id", "eval-none"])
  with pytest.raises(LookupError, match="no evaluation output"):
    main(["report", "--local", str(ran.fixture)])
  with pytest.raises(LookupError, match="no registry row for evaluation"):
    main([
        "report", "--local",
        str(ran.directory), "--evaluation_id", "eval-none"
    ])


def test_report_json_goes_to_a_file(ran, tmp_path, capsys):
  path = tmp_path / "evaluation_metrics.json"
  assert main([
      "report", "--local",
      str(ran.directory), "--format", "json", "--out",
      str(path)
  ]) == 0
  assert str(path) in capsys.readouterr().out
  document = json.loads(path.read_text())
  final = ran.stored.final
  assert document["evaluation_id"] == ran.evaluation_id
  assert document["status"] == final["status"]
  assert document["evaluation"] == final
  assert [e["event"] for e in document["events"]] == ["FINAL"]
  assert [g["metric_id"] for g in document["failing"]] == sorted(
      ran.failing_ids(), key=catalogue().ids().index)
  for group in document["failing"]:
    assert group["interpretation"]["bad"] and group["rows"]
    assert all(r["status"] == "fail" for r in group["rows"])
  assert document["counts"]["fail"] == final["metrics_fail"]
  assert document["counts"]["total"] == final["metrics_total"]
  assert len(document["metrics"]) == len(ran.stored.metrics)


class _StoreBq:
  """`Bq.query` over one stored evaluation, typed as the client types
  it: TIMESTAMP columns as `datetime`, a JSON column as its text."""

  def __init__(self, stored: store.Evaluation):
    self._stored = stored
    self.queries: list[tuple[str, dict[str, Any]]] = []

  @staticmethod
  def _moment(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))

  def query(self, sql: str, params: Mapping[str, Any]) -> list[dict]:
    self.queries.append((sql, dict(params)))
    if params["evaluation_id"] != self._stored.evaluation_id:
      return []
    if f".{REGISTRY}`" in sql:
      return [{
          **event,
          "recorded_at": self._moment(event["recorded_at"]),
          "evaluated_at": self._moment(event["evaluated_at"]),
          "evaluation_params": json.dumps(event["evaluation_params"]),
      } for event in self._stored.events]
    rows = (
        self._stored.metrics
        if f".{METRICS}`" in sql else self._stored.profiles)
    return [{
        **row, "detail": json.dumps(row["detail"])
    } if row.get("detail") is not None else dict(row) for row in rows]


def test_report_reads_the_same_evaluation_from_bigquery(ran, capsys):
  bq = _StoreBq(ran.stored)
  env = driver.Env(make_bq=lambda project: bq)
  argv = ["report", "--project", PROJECT, "--evaluation_id", ran.evaluation_id]
  assert main(argv, env) == 0
  from_bq = capsys.readouterr().out
  assert main(["report", "--local", str(ran.directory)]) == 0
  local = capsys.readouterr().out
  assert from_bq.replace(f"{PROJECT}.{DATASET}", str(ran.directory)) == local
  assert len(bq.queries) == 2  # the registry, then the metrics: no profiles
  registry_sql, registry_params = bq.queries[0]
  metrics_sql, metrics_params = bq.queries[1]
  assert f"`{PROJECT}.{DATASET}.{REGISTRY}`" in registry_sql
  assert registry_params == {"evaluation_id": ran.evaluation_id}
  assert f"`{PROJECT}.{DATASET}.{METRICS}`" in metrics_sql
  assert "evaluated_at = @evaluated_at" in metrics_sql  # partition pruning
  assert metrics_params["evaluated_at"] == NOW
  with pytest.raises(LookupError, match="no registry row"):
    main(["report", "--project", PROJECT, "--evaluation_id", "eval-none"], env)


def test_a_running_only_evaluation_says_so(ran):
  running = {**ran.bq.loads[0][1][0]}
  text = render_markdown(
      store.Evaluation(
          evaluation_id=ran.evaluation_id,
          events=[running],
          metrics=[],
          origin="memory"))
  assert f"# Evaluation `{ran.evaluation_id}` — RUNNING" in text
  assert "No FINAL event is recorded" in text
  assert "## Failing metrics (0 rows, 0 metrics)" in text
  assert "No metric is at FAIL." in text


def test_report_survives_a_failing_metric_the_catalogue_does_not_know(ran):
  """A stored evaluation can be older (or newer) than the packaged
  catalogue: a failing id the catalogue lacks is listed with its rows
  and a line saying so, never a KeyError."""
  known = next(r for r in ran.stored.metrics
               if r["status"] == "fail" and not is_aggregate(r["metric_id"]))
  retired = {**known, "metric_id": "column.retired_metric"}
  evaluation = store.Evaluation(
      evaluation_id=ran.evaluation_id,
      events=ran.stored.events,
      metrics=[known, retired],
      origin="memory")
  text = render_markdown(evaluation)
  assert "## Failing metrics (2 rows, 2 metrics)" in text
  title = catalogue().get(known["metric_id"]).title
  metric_id = known["metric_id"]
  assert f"### `{metric_id}` — {title}" in text
  assert "### `column.retired_metric` — not in the packaged catalogue" in text
  assert text.index(f"`{metric_id}`") < text.index("`column.retired_metric`")
  document = json.loads(render_json(evaluation))
  listed = {g["metric_id"]: g for g in document["failing"]}
  assert listed["column.retired_metric"]["title"] is None
  assert listed["column.retired_metric"]["interpretation"] is None
  assert len(listed["column.retired_metric"]["rows"]) == 1
  assert listed[metric_id]["title"] == title


def test_compare_reads_bigquery_with_profiles(ran, capsys):
  bq = _StoreBq(ran.stored)
  env = driver.Env(make_bq=lambda project: bq)
  argv = [
      "compare", "--project", PROJECT, "--evaluation_ids",
      f"{ran.evaluation_id},{ran.evaluation_id}"
  ]
  assert main(argv, env) == 0
  text = capsys.readouterr().out
  assert "No shared metric row differs." in text
  assert "same evaluation_key: a re-run of the same evaluation" in text
  assert NOT_COMPARABLE not in text
  tables = [sql.split("`")[1].rsplit(".", 1)[1] for sql, _ in bq.queries]
  assert tables == [REGISTRY, METRICS, PROFILES] * 2


# --------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------
def _rewrite(directory: Path, table: str, change) -> None:
  """Rewrite one table of a local evaluation, row by row."""
  for path in sorted((directory / table).glob("*.jsonl")):
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    path.write_text("".join(json.dumps(change(row)) + "\n" for row in rows))


def _key(row: Mapping[str, Any]) -> tuple:
  return (row["table_name"], row["metric_id"], row["column_name"],
          row["column_name_2"], row["edge"])


@pytest.fixture(scope="module", name="second")
def fixture_second(ran) -> dict[str, Any]:
  """A second evaluation beside the first: the same rows under another
  id, with one value nudged inside its noise floor, one moved far beyond
  it, one interval-method value moved inside its interval, one histogram
  re-binned (another `edges_digest`) and one with its counts shifted."""
  directory = ran.out / SECOND_ID
  shutil.copytree(ran.directory, directory)
  measured = [
      r for r in ran.stored.metrics
      if not is_aggregate(r["metric_id"]) and r["value"] is not None
  ]
  scalar = [r for r in measured if (r["noise_floor"] or 0) > 0]
  interval = [
      r for r in measured
      if r["noise_floor"] is None and r["ci_low"] is not None and
      r["ci_high"] is not None and r["ci_low"] < r["value"] < r["ci_high"]
  ]
  within, beyond, covered = scalar[0], scalar[1], interval[0]
  histograms = [
      p for p in ran.stored.profiles if p["profile_kind"] == "histogram" and
      p["edges_digest"] and sum(p["payload"]["counts"]) > 0
  ]
  rebinned = histograms[0]
  shifted = next(
      p for p in histograms[1:] if len(set(p["payload"]["counts"])) > 1)

  def metric(row: dict) -> dict:
    row["evaluation_id"] = SECOND_ID
    if _key(row) == _key(within):
      row["value"] += 0.5 * row["noise_floor"]
    elif _key(row) == _key(beyond):
      row["value"] += 10 * row["noise_floor"] + 0.5
    elif _key(row) == _key(covered):
      row["value"] = (row["value"] + row["ci_high"]) / 2
    return row

  def profile(row: dict) -> dict:
    row["evaluation_id"] = SECOND_ID
    same = (row["profile_kind"], row["table_name"], row["column_name"],
            row["side"])
    if same == ("histogram", rebinned["table_name"], rebinned["column_name"],
                rebinned["side"]):
      row["edges_digest"] = "f" * 32
    elif same == ("histogram", shifted["table_name"], shifted["column_name"],
                  shifted["side"]):
      counts = row["payload"]["counts"]
      row["payload"]["counts"] = counts[1:] + counts[:1]
    return row

  def event(row: dict) -> dict:
    row["evaluation_id"] = SECOND_ID
    return row

  _rewrite(directory, METRICS, metric)
  _rewrite(directory, PROFILES, profile)
  _rewrite(directory, REGISTRY, event)
  return {
      "within": within,
      "beyond": beyond,
      "covered": covered,
      "rebinned": rebinned,
      "shifted": shifted,
  }


def _entry(entries: Sequence[Mapping[str, Any]],
           row: Mapping[str, Any]) -> Mapping[str, Any]:
  first, second = row["column_name"], row["column_name_2"]
  scope = row["edge"] or (f"{first} & {second}"
                          if second else first or "(table)")
  (found,) = [
      e for e in entries
      if (e["table"], e["metric_id"], e["scope"]) == (row["table_name"],
                                                      row["metric_id"], scope)
  ]
  return found


def test_compare_marks_deltas_inside_the_noise_floor(ran, second, capsys):
  argv = [
      "compare", "--local",
      str(ran.out), "--evaluation_ids", f"{ran.evaluation_id},{SECOND_ID}"
  ]
  assert main([*argv, "--format", "json"]) == 0
  document = json.loads(capsys.readouterr().out)
  entries = document["metrics"]
  within = _entry(entries, second["within"])
  assert within["verdict"] == WITHIN_NOISE == "≈"
  assert within["noise"].startswith("floor ")
  beyond = _entry(entries, second["beyond"])
  assert beyond["verdict"] in ("worse", "better", "changed")
  covered = _entry(entries, second["covered"])
  assert (covered["verdict"], covered["noise"]) == ("≈", "CI overlap")
  moved = [e for e in entries if e["verdict"] not in ("=", "n/a")]
  assert len(moved) == 3  # every other shared row is identical
  assert document["metrics_only_a"] == document["metrics_only_b"] == 0
  assert any("same evaluation_key" in note for note in document["notes"])
  summary = document["summary"]
  assert summary["a"]["evaluation_id"] == ran.evaluation_id
  assert summary["b"]["evaluation_id"] == SECOND_ID
  assert main(argv) == 0
  text = capsys.readouterr().out
  assert f"# Compare `{ran.evaluation_id}` (A) → `{SECOND_ID}` (B)" in text
  rows = [line for line in text.splitlines() if line.startswith("| ")]
  marked = [line for line in rows if "| ≈ |" in line]
  assert len(marked) == 2
  metric_id = second["within"]["metric_id"]
  assert any(f"`{metric_id}`" in line for line in marked)
  assert "2 ≈" in text


def test_compare_refuses_psi_across_different_edges_digests(
    ran, second, capsys):
  argv = [
      "compare", "--local",
      str(ran.out), "--evaluation_ids", f"{ran.evaluation_id},{SECOND_ID}"
  ]
  assert main([*argv, "--format", "json"]) == 0
  profiles = json.loads(capsys.readouterr().out)["profiles"]

  def drift(profile: Mapping[str, Any]) -> Mapping[str, Any]:
    (found,) = [
        r for r in profiles["rows"]
        if (r["table"], r["column"],
            r["side"]) == (profile["table_name"], profile["column_name"],
                           profile["side"])
    ]
    return found

  rebinned = drift(second["rebinned"])
  assert rebinned["psi"] is None  # never re-binned, never interpolated
  assert rebinned["note"].startswith(NOT_COMPARABLE)
  assert "edges_digest" in rebinned["note"]
  assert rebinned["edges_digest_b"] == "f" * 32
  shifted = drift(second["shifted"])
  assert shifted["psi"] > 0 and shifted["edges_digest_a"] == shifted[
      "edges_digest_b"]
  untouched = [r for r in profiles["rows"] if r not in (rebinned, shifted)]
  assert untouched and all(r["psi"] == 0 for r in untouched)
  assert all(r["note"] == "stable" for r in untouched)
  assert profiles["only_a"] == profiles["only_b"] == 0
  assert main(argv) == 0
  text = capsys.readouterr().out
  assert "## Profile drift, B vs A" in text
  line = next(row for row in text.splitlines()
              if row.startswith("| ") and NOT_COMPARABLE in row)
  column = second["rebinned"]["column_name"]
  assert f"| {column} |" in line
  assert text.count(NOT_COMPARABLE) == 1
