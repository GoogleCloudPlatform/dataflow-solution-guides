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
"""The reference panel staged beside the job graph (Ruling R154), end
to end:

- the package's fixture evaluation (`users` → `orders`, a panel each,
  rows copied from R so there are flags) run on the DirectRunner twice,
  its panel data embedded in the graph and staged under a local root:
  the rows written are the same rows;
- every one of the graph's four panel payloads is an object then, and
  the graph no longer grows with the panel;
- the driver gives a Dataflow run a root under its temp location, says
  what it staged, and closes the run as FAILED when a write fails. A
  local run is given none.

Nothing here is real data.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import apache_beam as beam
import pytest
from apache_beam.options.pipeline_options import (
    GoogleCloudOptions,
    PipelineOptions,
)
from beam.acceptance_data import heavy_copies
from unit.context.plan_fakes import JOB_ID, PROJECT

from sdfb_evaluation.beam import staged
from sdfb_evaluation.beam.io import LOAD_ORDER, LocalJsonSinks
from sdfb_evaluation.beam.pipeline import (
    build_evaluation_pipeline,
    pipeline_options_defaults,
)
from sdfb_evaluation.beam.staged import StagedObjectError, Staging
from sdfb_evaluation.cli import driver, run_evaluation
from sdfb_evaluation.cli.fixture import load_fixture
from sdfb_evaluation.cli.main import main
from sdfb_evaluation.context.plan import Knobs

from .helpers import (
    NOW,
    REGISTRY,
    FakeJob,
    fixture_rows,
    make_env,
    tiny_pipeline,
    write_fixture,
    write_stand_in,
)

USERS = 60
PANEL = 30
EVALUATION_ID = "eval-20260914T080000Z-0badc0de"
REGION = "europe-west1"
TARGET = ["--project", PROJECT, "--region", REGION, "--job_id", JOB_ID]
METRICS, PROFILES, FLAGS, _ = LOAD_ORDER
_WALL_CLOCK = {"recorded_at", "finished_at"}  # of a FINAL row: when it ran
_STAGING_LINE = re.compile(
    r"evaluation (?P<id>\S+): panel staging: (?P<objects>\d+) objects, "
    r"(?P<size>\d+(\.\d+)? (B|kB|MB|GB)), (?P<seconds>\d+\.\d+)s "
    r"\((?P<writes>\d+\.\d+)s in the writes\)")


def _fixture(directory: Path, panel: int = PANEL) -> Path:
  """The acceptance generator's users → orders pair, 60 users a side;
  eighteen synthetic users carry the content of distinct R rows."""
  source = fixture_rows(USERS, seed=5, id_base=100_000)
  good = fixture_rows(USERS, seed=6, id_base=500_000)
  synthetic = {**good, "users": heavy_copies(source["users"], good["users"])}
  return write_fixture(directory, source, synthetic, panel=panel)


def _plan(directory: Path, key: Path | None = None):
  fixture = load_fixture(str(directory))
  plan = fixture.plan(
      knobs=Knobs(),
      mode="exact",
      trigger="cli",
      runner="DirectRunner",
      now=NOW,
      evaluation_id=EVALUATION_ID)
  if key is not None:  # an ephemeral label key would differ per run
    plan = dataclasses.replace(plan, label_key_uri=str(key))
  return fixture, plan


def _graph(directory: Path,
           out: Path,
           staging: Staging | None,
           key: Path | None = None) -> tuple[beam.Pipeline, LocalJsonSinks]:
  fixture, plan = _plan(directory, key)
  sinks = LocalJsonSinks(str(out))
  options = PipelineOptions([],
                            **pipeline_options_defaults("DirectRunner", plan))
  p = beam.Pipeline(options=options)
  build_evaluation_pipeline(
      p, plan, sources=fixture.sources(), sinks=sinks, staging=staging)
  return p, sinks


def _line(row: dict[str, Any]) -> str:
  return json.dumps(row, sort_keys=True)


def _written(sinks: LocalJsonSinks) -> dict[str, list[str]]:
  """Every row the run wrote, per table, as sorted JSON lines (shard
  order is the runner's)."""
  return {t: sorted(_line(r) for r in sinks.read_rows(t)) for t in LOAD_ORDER}


def _kinds(staging: Staging) -> list[str]:
  """The staged objects by what they hold: an object's name without its
  digest."""
  return sorted(Path(o.uri).name.rsplit("-", 1)[0] for o in staging.objects)


# --------------------------------------------------------------------------
# the fixture evaluation, embedded and staged
# --------------------------------------------------------------------------
@pytest.fixture(scope="module", name="twice")
def fixture_twice(tmp_path_factory) -> SimpleNamespace:
  """The fixture evaluation run twice on the DirectRunner: its panel
  data embedded in the graph, and staged under a local root."""
  tmp = tmp_path_factory.mktemp("staged_panel")
  directory = _fixture(tmp / "fixture")
  key = tmp / "label.key"
  key.write_bytes(b"staged-panel-test-label-key-0123456789")
  staging = Staging(staged.panel_root(str(tmp / "temp"), EVALUATION_ID))
  runs = {}
  for name, how in (("embedded", None), ("staged", staging)):
    p, sinks = _graph(directory, tmp / name, how, key)
    p.run().wait_until_finish()
    runs[name] = _written(sinks)
  return SimpleNamespace(tmp=tmp, staging=staging, **runs)


def test_the_staged_evaluation_writes_the_rows_the_embedded_one_writes(twice):
  for table in (METRICS, PROFILES, FLAGS):
    assert twice.embedded[table], table  # the comparison is not of nothing
    assert twice.staged[table] == twice.embedded[table], table
  (embedded,) = [json.loads(line) for line in twice.embedded[REGISTRY]]
  (kept,) = [json.loads(line) for line in twice.staged[REGISTRY]]
  assert embedded["event"] == kept["event"] == "FINAL"
  assert embedded["status"] in ("SUCCEEDED", "SUCCEEDED_WITH_WARNINGS")
  differing = {name for name in embedded if embedded[name] != kept[name]}
  assert differing <= _WALL_CLOCK  # the FINAL row, but for when it was written
  assert set(embedded) == set(kept)


def test_the_panel_metrics_were_evaluated_from_the_staged_panel(twice):
  """The rows compared hold what only the panel gives: memorization
  lifts with a value, and flags that name reference rows."""
  metrics = [json.loads(line) for line in twice.staged[METRICS]]
  lifts = [
      m for m in metrics
      if "memorization" in m["metric_id"] and m["value"] is not None
  ]
  assert lifts
  assert {m["table_name"] for m in lifts} >= {"users"}


def test_every_panel_payload_of_the_graph_is_an_object(twice):
  kinds = _kinds(twice.staging)
  reads = [kind for kind in kinds if kind.startswith("Read_")]
  assert sorted(kind.rsplit("_", 1)[1] for kind in reads) == [
      "holdout", "holdout", "reference", "reference"
  ]
  assert sum("users_" in kind for kind in reads) == 2
  assert [kind for kind in kinds if not kind.startswith("Read_")] == [
      "Inputs_orders", "Inputs_users", "Panel_orders", "Panel_users", "Refs"
  ]
  root = Path(twice.staging.root)
  assert root == twice.tmp / "temp" / "sdfb-evaluation" / EVALUATION_ID / "panel"
  assert sorted(p.name for p in root.iterdir()) == sorted(
      Path(o.uri).name for o in twice.staging.objects)
  # the R rows object holds the panel's rows, plan columns only
  reference = next(
      o for o in twice.staging.objects if "users_reference" in o.uri)
  assert reference.elements == PANEL


def test_the_staged_graph_does_not_grow_with_the_panel(tmp_path):
  """Three times the panel rows: the embedded graph grows by their
  weight, the staged one by nothing that counts (two builds of one
  graph differ by a few hundred bytes; one payload left in the graph
  would be a third of the embedded growth)."""
  sizes: dict[tuple[str, int], int] = {}
  for panel in (10, 30):
    directory = _fixture(tmp_path / f"fixture-{panel}", panel)
    for name in ("embedded", "staged"):
      staging = None
      if name == "staged":
        staging = Staging(str(tmp_path / f"panel-{panel}"))
      p, _ = _graph(directory, tmp_path / f"out-{name}-{panel}", staging)
      sizes[(name, panel)] = len(p.to_runner_api().SerializeToString())
  embedded = sizes[("embedded", 30)] - sizes[("embedded", 10)]
  kept = sizes[("staged", 30)] - sizes[("staged", 10)]
  assert embedded > 20_000  # twenty more rows a side, two tables, twice over
  assert abs(kept) < embedded / 10
  assert sizes[("staged", 30)] < sizes[("embedded", 30)]


def test_a_panel_write_that_fails_fails_the_graph_not_the_table(
    tmp_path, monkeypatch):
  """Through the REAL graph builder. A table whose own data cannot be
  set up is set aside as not evaluated and the others go on; a panel
  object that cannot be written is not that: storage refuses the R rows
  of `users`, and building the graph raises instead of going on without
  the table."""
  directory = _fixture(tmp_path / "fixture")
  create = staged.FileSystems.create

  def refuse_one(path: str, *args: Any, **kwargs: Any):
    if "users_reference" in path:
      raise OSError("503 backend error")
    return create(path, *args, **kwargs)

  monkeypatch.setattr(staged.FileSystems, "create", refuse_one)
  staging = Staging(str(tmp_path / "panel"))
  with pytest.raises(StagedObjectError) as info:
    _graph(directory, tmp_path / "out", staging)
  assert "users/reference" in str(info.value)
  assert "503 backend error" in str(info.value)


# --------------------------------------------------------------------------
# the driver: a root for Dataflow, none for a local run
# --------------------------------------------------------------------------
def _holder(options):
  """A pipeline that only holds its options (a real one with Dataflow
  options makes Beam ask Cloud Storage about the bucket)."""
  return SimpleNamespace(options=options)


def _dataflow(temp: str) -> list[str]:
  return [*TARGET, "--runner", "DataflowRunner", "--temp_location", temp]


def _staging_lines(caplog) -> list[re.Match]:
  found = (_STAGING_LINE.fullmatch(r.getMessage()) for r in caplog.records)
  return [m for m in found if m]


def test_a_dataflow_run_is_given_a_root_under_its_temp_location(
    bq, resolved, stub, capsys, caplog):
  del resolved
  env = make_env(bq, submit=lambda p: FakeJob(), make_pipeline=_holder)
  with caplog.at_level("INFO", logger="sdfb_evaluation"):
    assert run_evaluation.main(_dataflow("gs://demo-bucket/tmp"), env) == 0
  capsys.readouterr()
  assert len(stub.built) == 1
  kwargs = stub.built[0][1]
  evaluation_id = bq.registry_rows()[0]["evaluation_id"]
  assert isinstance(kwargs["staging"], Staging)
  assert kwargs["staging"].root == (
      f"gs://demo-bucket/tmp/sdfb-evaluation/{evaluation_id}/panel/")
  # the stand-in graph staged nothing, and the launch says so
  (line,) = _staging_lines(caplog)
  assert (line["id"], line["objects"], line["size"]) == (evaluation_id, "0",
                                                         "0 B")


def test_a_local_run_is_given_none_and_says_nothing_of_staging(
    bq, resolved, monkeypatch, capsys, caplog):
  del resolved
  build = tiny_pipeline(fast=True)
  monkeypatch.setattr(driver, "build_evaluation_pipeline", build)
  with caplog.at_level("INFO", logger="sdfb_evaluation"):
    assert main(["run", *TARGET], make_env(bq, submit=write_stand_in)) == 0
  capsys.readouterr()
  assert len(build.built) == 1
  kwargs = build.built[0][1]
  assert kwargs["staging"] is None
  assert not _staging_lines(caplog)
  assert not [r for r in caplog.records if "panel staging" in r.getMessage()]


def _staging_build(payloads: dict[str, list[Any]]):
  """A stand-in for the graph that stages `payloads` as the real one
  stages its panel data, through the `Staging` the driver handed it."""

  def build(p: Any, plan: Any, **kwargs: Any) -> dict:
    del p, plan
    for label, values in payloads.items():
      kwargs["staging"].write(label, values)
    return {}

  return build


def test_the_launch_says_what_it_staged_between_graph_and_run(
    bq, resolved, monkeypatch, capsys, caplog, tmp_path):
  del resolved
  payloads = {"Panel[users]": [{"n": 1}], "Refs": [("users", 0), ("users", 1)]}
  monkeypatch.setattr(driver, "build_evaluation_pipeline",
                      _staging_build(payloads))
  env = make_env(bq, submit=lambda p: FakeJob(), make_pipeline=_holder)
  with caplog.at_level("INFO", logger="sdfb_evaluation"):
    assert run_evaluation.main(_dataflow(str(tmp_path / "temp")), env) == 0
  capsys.readouterr()
  evaluation_id = bq.registry_rows()[0]["evaluation_id"]
  root = tmp_path / "temp" / "sdfb-evaluation" / evaluation_id / "panel"
  written = sorted(root.iterdir())
  assert len(written) == 2
  (line,) = _staging_lines(caplog)
  size = sum(path.stat().st_size for path in written)
  assert (line["objects"], line["size"]) == ("2", f"{size} B")
  assert float(line["writes"]) <= float(line["seconds"])
  messages = [r.getMessage() for r in caplog.records]
  at = messages.index(line.group(0))
  graph = next(i for i, m in enumerate(messages) if ": graph " in m)
  run = next(i for i, m in enumerate(messages) if ": pipeline run " in m)
  assert graph < at < run


def test_a_staging_write_that_fails_closes_the_run_as_failed(
    bq, resolved, monkeypatch, capsys, tmp_path):
  """The launcher cannot write under the temp location: the error is
  raised before anything is submitted, and the driver appends FAILED."""
  del resolved
  blocker = tmp_path / "temp"
  blocker.write_text("a file where the temp location's directory should be")
  monkeypatch.setattr(driver, "build_evaluation_pipeline",
                      _staging_build({"Panel[users]": [{
                          "n": 1
                      }]}))
  submitted: list[Any] = []
  env = make_env(bq, submit=submitted.append, make_pipeline=_holder)
  with pytest.raises(StagedObjectError):
    driver.run(*_parsed(_dataflow(str(blocker))), env, wait=False)
  capsys.readouterr()
  assert [row["status"] for row in bq.registry_rows()] == ["RUNNING", "FAILED"]
  failed = bq.registry_rows()[-1]
  assert "StagedObjectError" in failed["status_reason"]
  assert "Panel[users]" in failed["status_reason"]
  assert not submitted


def _real(made: list[Any]):
  """`Env.make_pipeline` as the driver's own, recorded: a REAL
  `beam.Pipeline`. Beam deep-copies the options it is given and
  validates ITS copy, which is where it settles the temp location
  (`--dry_run=True` keeps its bucket check off the network)."""

  def make(options):
    made.append(driver.Env().make_pipeline(options))
    return made[-1]

  return make


def _settled(bq, stub, locations: list[str]) -> tuple[str, str]:
  """A Dataflow launch with `locations` through a real pipeline: (the
  temp location Beam settled on, the root the driver staged under)."""
  made: list[Any] = []
  env = make_env(bq, submit=lambda p: FakeJob(), make_pipeline=_real(made))
  argv = [*TARGET, "--runner", "DataflowRunner", "--dry_run=True", *locations]
  assert run_evaluation.main(argv, env) == 0
  assert [row["status"] for row in bq.registry_rows()] == ["RUNNING"]
  assert len(made) == 1 and len(stub.built) == 1
  temp = made[0].options.view_as(GoogleCloudOptions).temp_location
  return temp, stub.built[0][1]["staging"].root


def test_a_launch_with_a_staging_location_only_is_still_submitted(
    bq, resolved, stub, capsys):
  """No `--temp_location`: Beam makes the staging location the job's
  temp location, in its own copy of the options. The launch goes on as
  it did before the panel was staged, and the panel goes there."""
  del resolved
  temp, root = _settled(bq, stub,
                        ["--staging_location", "gs://demo-bucket/staging"])
  capsys.readouterr()
  evaluation_id = bq.registry_rows()[0]["evaluation_id"]
  assert temp == "gs://demo-bucket/staging"
  assert root == (
      f"gs://demo-bucket/staging/sdfb-evaluation/{evaluation_id}/panel/")


def test_the_panel_goes_under_the_temp_location_beam_settled_on(
    bq, resolved, stub, capsys):
  """A bucket with no path is no temp location to Beam, which takes the
  staging location instead: the panel is staged under the job's real
  temp location, not under what the flag said."""
  del resolved
  temp, root = _settled(bq, stub, [
      "--temp_location", "gs://other-bucket", "--staging_location",
      "gs://demo-bucket/staging"
  ])
  capsys.readouterr()
  assert temp == "gs://demo-bucket/staging"
  assert root.startswith("gs://demo-bucket/staging/sdfb-evaluation/")
  assert "other-bucket" not in root


@pytest.mark.parametrize("how", ["a local endpoint", "a stand-in"])
def test_a_pipeline_beam_did_not_validate_has_no_temp_location_and_fails(
    bq, resolved, stub, capsys, how):
  """Beam settles a temp location on every pipeline it validates as a
  Dataflow service pipeline (the two tests above). A pipeline with none
  is one it did not validate: a real one whose `--dataflow_endpoint` is
  on localhost (Beam then does not take it for the service), or a
  stand-in that only holds the options it was given. The launch fails
  rather than embed the panel without a word."""
  del resolved, stub
  made: list[Any] = []
  argv = [*TARGET, "--runner", "DataflowRunner"]
  make = _holder
  if how == "a local endpoint":
    argv += ["--dataflow_endpoint", "http://localhost:1", "--dry_run=True"]
    make = _real(made)
  env = make_env(bq, submit=lambda p: FakeJob(), make_pipeline=make)
  with pytest.raises(ValueError, match="temp_location"):
    driver.run(*_parsed(argv), env, wait=False)
  capsys.readouterr()
  assert [row["status"] for row in bq.registry_rows()] == ["RUNNING", "FAILED"]
  assert len(made) == (1 if how == "a local endpoint" else 0)


def _parsed(argv: list[str]):
  from sdfb_evaluation.cli.main import parse_args  # pylint: disable=import-outside-toplevel  # only these two tests

  return parse_args(["run", *argv])
