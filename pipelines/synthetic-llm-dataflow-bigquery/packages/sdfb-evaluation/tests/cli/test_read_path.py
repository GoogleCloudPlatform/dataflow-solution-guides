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
"""Tests for how `sdfb-eval run` chooses its read path (Task 44): the
probe of the Storage Read API after the prepare statements, the paged
read it selects on a refusal and what the launch says about it, and the
timed lines inside the submission.

The probe here is a fake (the laptop has no Google Cloud); the read
itself is tested in `beam/test_io_paged.py`.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any

import apache_beam as beam
import pytest
from google.api_core import exceptions as api_exceptions
from unit.context.plan_fakes import JOB_ID, PROJECT

from sdfb_evaluation.beam.io import (
    BigQuerySources,
    InMemorySources,
    RoutedBigQuerySources,
    read_table_of,
)
from sdfb_evaluation.cli import driver
from sdfb_evaluation.cli.main import main
from sdfb_evaluation.context.bq import BqApiError
from sdfb_evaluation.types import Side

from .helpers import (
    fixture_rows,
    make_env,
    tiny_pipeline,
    write_fixture,
    write_stand_in,
)

REGION = "europe-west1"
TARGET = ["--project", PROJECT, "--region", REGION, "--job_id", JOB_ID]
PUBLIC = "bigquery-public-data"  # where the thelook sources live
DENIED = ("request failed: the user does not have "
          "'bigquery.readsessions.create' permission for "
          f"'projects/{PROJECT}'")
_STEP = re.compile(r"evaluation (?P<id>\S+): (?P<step>[A-Za-z ]+) "
                   r"(?P<seconds>\d+\.\d+)s")


class _Probe:
  """`Env.probe_read`: records its calls and answers `error` (raised)
  for a read table of one of `projects` (every project when None)."""

  def __init__(self,
               error: BaseException | None = None,
               projects: tuple[str, ...] | None = None):
    self.error = error
    self.projects = projects
    self.calls: list[tuple[str, tuple[str, ...]]] = []

  def __call__(self, read_table: str, columns: Any) -> None:
    self.calls.append((read_table, tuple(columns)))
    project = read_table.split(".", 1)[0]
    if self.error is not None and (self.projects is None or
                                   project in self.projects):
      raise self.error


def _drive(bq, monkeypatch, probe: _Probe, *argv: str) -> tuple[int, Any]:
  """One run over the thelook launch whose sources are the real
  `BigQuerySources` and whose probe is `probe`; (exit code, the built
  plan and pipeline arguments)."""
  build = tiny_pipeline(fast=True)
  monkeypatch.setattr(driver, "build_evaluation_pipeline", build)
  env = make_env(
      bq, submit=write_stand_in, make_sources=BigQuerySources, probe_read=probe)
  code = main(["run", *TARGET, *argv], env)
  return code, build


def _read_tables(plan: Any) -> list[tuple[str, str, str]]:
  """(table, side, read table) of everything `plan` reads from BigQuery:
  both sides of its launch tables and of its read-only parent."""
  return [(t.name, side.value, read_table_of(t, side))
          for t in plan.tables
          for side in (Side.SOURCE, Side.SYNTHETIC)]


def _projects(plan: Any) -> list[str]:
  return sorted({read.split(".", 1)[0] for _, _, read in _read_tables(plan)})


def _built(sources: Any, plan: Any) -> dict[tuple[str, str], str]:
  """Which read `sources` builds for each (table, side) of `plan`:
  `paged` or `direct`. The graph is composed, never run (DIRECT_READ
  cannot run here): a paged side has the paged read's `Page` step."""
  p = beam.Pipeline()
  for table in plan.tables:
    for side in (Side.SOURCE, Side.SYNTHETIC):
      sources.read(p, table, side)
  labels = set(p.applied_labels)
  built = {}
  for table in plan.tables:
    for side in ("source", "synthetic"):
      label = f"Read[{table.landing_table}/{side}]"
      assert label in labels or f"{label}/Page" in labels, label
      path = "paged" if f"{label}/Page" in labels else "direct"
      built[(table.name, side)] = path
  return built


# --------------------------------------------------------------------------
# the probe decides
# --------------------------------------------------------------------------
def test_an_accepted_read_session_keeps_direct_read(bq, resolved, monkeypatch,
                                                    caplog, capsys):
  del resolved
  probe = _Probe()
  with caplog.at_level("INFO", logger="sdfb_evaluation"):
    code, build = _drive(bq, monkeypatch, probe)
  capsys.readouterr()
  assert code == 0
  plan, kwargs = build.built[0]
  assert isinstance(kwargs["sources"], BigQuerySources)
  # one read session per project of the read tables: the thelook launch
  # reads its parent's source twin from the public project, the rest
  # from its own
  assert _projects(plan) == sorted([PROJECT, PUBLIC])
  assert sorted(
      read.split(".", 1)[0] for read, _ in probe.calls) == _projects(plan)
  by_read = {read: name for name, _, read in _read_tables(plan)}
  for read_table, columns in probe.calls:
    table = next(t for t in plan.tables if t.name == by_read[read_table])
    assert columns == tuple(c.name for c in table.columns)
  assert set(_built(kwargs["sources"], plan).values()) == {"direct"}
  assert not any("tabledata.list" in w for w in plan.warnings)
  assert not any(
      r.levelno >= logging.WARNING and "tabledata.list" in r.getMessage()
      for r in caplog.records)


def test_the_probe_runs_after_the_prepare_statements(bq, resolved, monkeypatch,
                                                     capsys):
  """It asks for a read table the prepare statements build, so they have
  run by then — and the graph has not been built."""
  del resolved
  order: list[str] = []
  real_prepare = driver.prepare_evaluation

  def prepare(plan, client):
    order.append("prepare")
    return real_prepare(plan, client)

  def probe(read_table, columns):
    del read_table, columns
    order.append("probe")

  build = tiny_pipeline(fast=True)

  def recording_build(p, plan, **kwargs):
    order.append("graph")
    return build(p, plan, **kwargs)

  monkeypatch.setattr(driver, "prepare_evaluation", prepare)
  monkeypatch.setattr(driver, "build_evaluation_pipeline", recording_build)
  env = make_env(
      bq, submit=write_stand_in, make_sources=BigQuerySources, probe_read=probe)
  assert main(["run", *TARGET], env) == 0
  capsys.readouterr()
  # one probe per project of the read tables (two in the thelook launch)
  assert order == ["prepare", "probe", "probe", "graph"]


@pytest.mark.parametrize("error", [
    api_exceptions.PermissionDenied(DENIED),
    api_exceptions.Forbidden("BigQuery Storage API has not been used in "
                             "project 1234 before or it is disabled"),
    PermissionError("request is prohibited by organization's policy"),
])
def test_every_project_refusing_pages_every_side_and_says_so(
    bq, resolved, monkeypatch, caplog, capsys, error):
  """The owner's deployment: no read session anywhere."""
  del resolved
  probe = _Probe(error)
  with caplog.at_level("INFO", logger="sdfb_evaluation"):
    code, build = _drive(bq, monkeypatch, probe)
  capsys.readouterr()
  assert code == 0
  plan, kwargs = build.built[0]
  sources = kwargs["sources"]
  assert isinstance(sources, RoutedBigQuerySources)
  assert sources.refused == {PROJECT, PUBLIC}
  assert set(_built(sources, plan).values()) == {"paged"}
  notes = [w for w in plan.warnings if "tabledata.list" in w]
  assert len(notes) == 1
  note = notes[0]
  assert "\n" not in note
  # who refused, and what restores the fast read: said of "a project",
  # never as an order to grant a role on a project nobody here runs
  assert f"on projects {PUBLIC}, {PROJECT}: " in note
  assert ("The fast read comes back for a project once the account the job "
          "runs as holds roles/bigquery.readSessionUser on it") in note
  assert "grant roles/bigquery.readSessionUser on project" not in note
  # why (the error's first line), per refusing project
  reason = f"({type(error).__name__}: {str(error).splitlines()[0]})"
  assert f"Project {PUBLIC} {reason} pages products source: " in note
  assert f"Project {PROJECT} {reason} pages products synthetic: " in note
  # what the launch does instead, and the quota to do the arithmetic with
  assert "are paged through tabledata.list" in note
  assert ("3.7 GB of row data per minute (7.5 GB in the US and EU "
          "multi-regions) for the project that contains the table") in note
  assert "shared with every other reader of that project" in note
  # the sizes are whole tables', and only the plan's columns are paged
  assert "at most that is paged: only the plan's columns are read" in note
  # every table side it pages, with its rows
  for name, side, _ in _read_tables(plan):
    assert re.search(rf"{name} {side}: [\d,]+ rows", note), (name, side)
  # one WARNING on the launcher console, the same text
  warned = [
      r for r in caplog.records
      if r.levelno == logging.WARNING and r.name == "sdfb_evaluation.cli.driver"
  ]
  assert [r.getMessage() for r in warned] == [note]
  # and it travels into the registry's FINAL row
  final = bq.registry_rows()[-1]
  assert final["event"] == "FINAL" and note in final["warnings"]


@pytest.mark.parametrize("every_size", [True, False])
def test_the_note_gives_rows_and_bytes_of_the_read_tables(
    bq, resolved, monkeypatch, capsys, every_size):
  """The sizes are read from the read tables themselves (they are built
  by the prepare statements): one `tables.get` each. The fake's own
  tables carry a size only when `every_size`."""
  del resolved
  seen: list[str] = []
  real_table = bq.table

  def table(fqn):
    seen.append(fqn)
    try:
      info = real_table(fqn)
    except LookupError:
      # a table the prepare statements built (the fake creates none)
      return {"numRows": 1_234_567, "numBytes": 1_500_000_000}
    if every_size:
      info["numBytes"] = 2_000_000
    return info

  monkeypatch.setattr(bq, "table", table)
  code, build = _drive(bq, monkeypatch,
                       _Probe(api_exceptions.PermissionDenied(DENIED)))
  capsys.readouterr()
  assert code == 0
  plan = build.built[0][0]
  note = next(w for w in plan.warnings if "tabledata.list" in w)
  reads = _read_tables(plan)
  for _, _, read in reads:
    assert read in seen
  built = [r for r in reads if r[2] not in bq.tables]
  assert built, "the plan reads no table the prepare statements build"
  for name, side, _ in built:
    assert f"{name} {side}: 1,234,567 rows, 1.50 GB" in note
  # the totals are per refusing project: the public one pages one side
  # (the parent's source twin), the job's own the other seven
  own = len(reads) - 1
  if every_size:
    assert ("pages products source: 30 rows, 2.00 MB; 1 table side, 30 "
            "rows, at most 2.00 MB in all.") in note
    assert "products synthetic: 30 rows, 2.00 MB" in note
    assert re.search(
        rf"{own} table sides, [\d,]+ rows, at most 4\.5\d GB in all\.", note)
  else:
    assert ("pages products source: 30 rows, size not read; 1 table side, "
            "30 rows in all.") in note
    assert "products synthetic: 30 rows, size not read" in note
    assert re.search(
        rf"{own} table sides, [\d,]+ rows in all, at most 4\.50 GB in the "
        rf"{len(built)} whose size was read\.", note)


def test_a_size_that_cannot_be_read_falls_back_to_the_plans_rows(
    bq, resolved, monkeypatch, capsys):
  """The fake BigQuery has no table the prepare statements build
  (`LookupError`, a refusal): the note still names every side, with the
  rows the plan counted and no size."""
  del resolved
  code, build = _drive(bq, monkeypatch,
                       _Probe(api_exceptions.PermissionDenied(DENIED)))
  capsys.readouterr()
  assert code == 0
  plan = build.built[0][0]
  note = next(w for w in plan.warnings if "tabledata.list" in w)
  missing = [r for r in _read_tables(plan) if r[2] not in bq.tables]
  assert missing
  for name, side, _ in missing:
    assert re.search(rf"{name} {side}: [\d,]+ rows, size not read", note)


def test_a_size_read_that_fails_for_another_reason_fails_the_launch(
    bq, resolved, monkeypatch, stub, capsys):
  del resolved
  real_table = bq.table
  armed: list[bool] = []

  def table(fqn):
    if armed:
      raise BqApiError("table: backend error", status=503)
    return real_table(fqn)

  def probe(read_table, columns):
    del read_table, columns
    armed.append(True)
    raise api_exceptions.PermissionDenied(DENIED)

  monkeypatch.setattr(bq, "table", table)
  env = make_env(bq, make_sources=BigQuerySources, probe_read=probe)
  assert main(["run", *TARGET], env) == 3
  assert "BqApiError: table: backend error" in capsys.readouterr().err
  assert [r["status"] for r in bq.registry_rows()] == ["RUNNING", "FAILED"]
  assert not stub.built


@pytest.mark.parametrize("error", [
    api_exceptions.ServiceUnavailable("backend unavailable"),
    api_exceptions.DeadlineExceeded("deadline exceeded"),
    api_exceptions.TooManyRequests("quota"),
    api_exceptions.NotFound("table not found"),
    api_exceptions.InvalidArgument("the table cannot be read by this API"),
    RuntimeError("no status at all"),
])
def test_any_other_probe_error_fails_the_launch(bq, resolved, stub, capsys,
                                                error):
  """Only a refusal changes how the evaluation reads: an error a retry
  may remove raises, and the driver closes the run FAILED."""
  del resolved
  env = make_env(bq, make_sources=BigQuerySources, probe_read=_Probe(error))
  assert main(["run", *TARGET], env) == 3
  assert f"{type(error).__name__}: {error}" in capsys.readouterr().err
  rows = bq.registry_rows()
  assert [r["status"] for r in rows] == ["RUNNING", "FAILED"]
  assert str(error) in rows[-1]["status_reason"]
  assert not stub.built  # no graph was built


def test_a_run_whose_sources_are_not_bigquerys_does_not_probe(
    bq, resolved, monkeypatch, capsys):
  del resolved

  def probe(read_table, columns):
    raise AssertionError(f"probed {read_table} {columns}")

  build = tiny_pipeline(fast=True)
  monkeypatch.setattr(driver, "build_evaluation_pipeline", build)
  env = make_env(bq, submit=write_stand_in, probe_read=probe)
  assert main(["run", *TARGET], env) == 0
  capsys.readouterr()
  assert isinstance(build.built[0][1]["sources"], InMemorySources)


def test_a_fixture_run_does_not_probe(tmp_path, monkeypatch, capsys):

  def probe(read_table, columns):
    raise AssertionError(f"probed {read_table} {columns}")

  source = fixture_rows(12, seed=5, id_base=100_000)
  synthetic = fixture_rows(12, seed=6, id_base=500_000)
  directory = write_fixture(
      tmp_path / "fixture", source, synthetic, tables=("users",))
  build = tiny_pipeline(fast=True)
  monkeypatch.setattr(driver, "build_evaluation_pipeline", build)
  env = dataclasses.replace(
      driver.Env(), submit=write_stand_in, probe_read=probe)
  code = main([
      "run", "--fixture_dir",
      str(directory), "--sink", "local_json", "--output_local",
      str(tmp_path / "out")
  ], env)
  capsys.readouterr()
  assert code == 0 and len(build.built) == 1


# --------------------------------------------------------------------------
# one probe per project of the read tables
# --------------------------------------------------------------------------
def test_only_the_sides_of_a_refusing_project_are_paged(bq, resolved,
                                                        monkeypatch, capsys):
  """The read session's parent is the READ TABLE's project (Beam's
  `_CustomBigQueryStorageSource.split`), so each project answers for
  itself, and a read table is paged if and only if its project refused:
  here the public project refuses (as one is expected to: the caller
  holds no role there) and the job's own accepts."""
  del resolved
  probe = _Probe(api_exceptions.PermissionDenied(DENIED), projects=(PUBLIC,))
  code, build = _drive(bq, monkeypatch, probe)
  capsys.readouterr()
  assert code == 0
  plan, kwargs = build.built[0]
  sources = kwargs["sources"]
  assert isinstance(sources, RoutedBigQuerySources)
  assert sources.refused == {PUBLIC}
  # both projects were asked: a refusal does not stop the probing
  assert sorted(read.split(".", 1)[0] for read, _ in probe.calls) == sorted(
      [PROJECT, PUBLIC])
  # the read built for each side: paged where the read table is in the
  # refusing project, DIRECT_READ everywhere else
  reads = {(name, side): read for name, side, read in _read_tables(plan)}
  built = _built(sources, plan)
  assert built == {
      key: "paged" if read.startswith(f"{PUBLIC}.") else "direct"
      for key, read in reads.items()
  }
  assert built[("products", "source")] == "paged"
  assert sum(1 for path in built.values() if path == "paged") == 1
  # the WARNING names the refusing project and its sides, and no other
  note = next(w for w in plan.warnings if "tabledata.list" in w)
  assert f"on project {PUBLIC}: " in note
  # nobody here can grant a role on that project: the line does not say to
  assert f"readSessionUser on project {PUBLIC}" not in note
  assert "holds roles/bigquery.readSessionUser on it" in note
  assert f"Project {PUBLIC} (PermissionDenied: 403 {DENIED}) pages " in note
  assert "pages products source: 30 rows" in note
  assert "1 table side, 30 rows" in note
  assert "every other side keeps the fast read" in note
  assert f"roject {PROJECT}" not in note  # the accepting project: not named
  for name, side in built:
    if (name, side) != ("products", "source"):
      assert f"{name} {side}: " not in note


# --------------------------------------------------------------------------
# what is inside "submission"
# --------------------------------------------------------------------------
def test_the_submission_is_timed_step_by_step(bq, resolved, monkeypatch, caplog,
                                              capsys):
  del resolved
  with caplog.at_level("INFO", logger="sdfb_evaluation"):
    code, _ = _drive(bq, monkeypatch, _Probe())
  capsys.readouterr()
  assert code == 0
  steps = [
      m["step"] for m in (_STEP.fullmatch(r.getMessage())
                          for r in caplog.records) if m
  ]
  assert steps == [
      "target check", "launch lookup", "relationship models", "RUNNING row",
      "prepare statements", "read path", "free text pools", "pipeline options",
      "graph", "pipeline run", "submission"
  ]


def test_beams_own_upload_lines_reach_the_console_during_the_run(
    bq, resolved, monkeypatch, caplog, capsys):
  """`pipeline.run()` cannot be timed from outside beyond its total:
  what Beam's Dataflow client logs about its uploads is relayed, and
  nothing else it logs reaches anybody who did not see it before."""
  del resolved
  beam_logger = logging.getLogger(
      "apache_beam.runners.dataflow.internal.apiclient")
  level, filters = beam_logger.level, list(beam_logger.filters)
  assert not beam_logger.isEnabledFor(logging.INFO)  # as in a launcher
  heard: list[str] = []

  class _Root(logging.Handler):
    """What any other handler of the process would be shown."""

    def emit(self, record: logging.LogRecord) -> None:
      if record.name == beam_logger.name:
        heard.append(record.getMessage())

  root = _Root()
  logging.getLogger().addHandler(root)

  def submit(pipeline):
    beam_logger.info("Starting GCS upload to %s...",
                     "gs://demo-bucket/staging/pipeline.pb")
    beam_logger.info("Completed GCS upload to %s in %s seconds.",
                     "gs://demo-bucket/staging/pipeline.pb", 41)
    beam_logger.info("Create job: %s", "<Job with every pipeline option>")
    beam_logger.warning("a warning of Beam's own")
    return write_stand_in(pipeline)

  build = tiny_pipeline(fast=True)
  monkeypatch.setattr(driver, "build_evaluation_pipeline", build)
  env = make_env(bq, submit=submit)
  try:
    with caplog.at_level("INFO", logger="sdfb_evaluation"):
      assert main(["run", *TARGET], env) == 0
  finally:
    logging.getLogger().removeHandler(root)
  capsys.readouterr()
  relayed = [
      r.getMessage() for r in caplog.records if
      r.name == "sdfb_evaluation.cli.driver" and "GCS upload" in r.getMessage()
  ]
  assert len(relayed) == 2
  assert relayed[0].endswith(
      "pipeline run: Starting GCS upload to gs://demo-bucket/staging/"
      "pipeline.pb...")
  assert relayed[1].endswith("in 41 seconds.")
  assert not any("Create job" in r.getMessage()
                 for r in caplog.records
                 if r.name.startswith("sdfb_evaluation"))
  # every other handler saw what it would have seen without the relay:
  # Beam's warning, and none of its INFO lines
  assert heard == ["a warning of Beam's own"]
  # Beam's logger is left as it was
  assert beam_logger.level == level
  assert list(beam_logger.filters) == filters


def test_the_relay_is_removed_when_the_run_raises(bq, resolved, stub, capsys):
  del resolved, stub
  beam_logger = logging.getLogger(
      "apache_beam.runners.dataflow.internal.apiclient")
  level, filters = beam_logger.level, list(beam_logger.filters)

  def explode(pipeline):
    del pipeline
    raise RuntimeError("the runner refused the pipeline")

  assert main(["run", *TARGET], make_env(bq, submit=explode)) == 3
  capsys.readouterr()
  assert beam_logger.level == level
  assert list(beam_logger.filters) == filters
