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
"""`--thresholds_uri` (Ruling R93-6): the file and its validation before
anything starts; the override on its way to the pipeline and into the
registry; what a real run stores under it, row by row, against the same
run without it; and what `report` and `compare` say about it.

The real pair is the cheapest real pipeline twice (`users` alone, 12
rows a side, `age` planted to fail for certain): once under the
catalogue, once under a file that lifts every metric the first run
failed — so the two runs differ in what the `--fail_on` gate exits with.
Nothing here is real data.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from beam.acceptance_data import USERS_FIELDS, rescored
from unit.context.plan_fakes import JOB_ID, PROJECT

from sdfb_evaluation.beam.assemble import RowContext, metric_row
from sdfb_evaluation.cli import driver, gate, run_evaluation
from sdfb_evaluation.cli.main import main
from sdfb_evaluation.cli.thresholds import load_thresholds, thresholds_digest
from sdfb_evaluation.context.gcp import JobNotFoundError
from sdfb_evaluation.report import store
from sdfb_evaluation.scoring import is_aggregate
from sdfb_evaluation.types import MetricValue

from .conftest import catalogue
from .helpers import (
    NOW,
    REGISTRY,
    check_row,
    fixture_rows,
    make_env,
    tiny_pipeline,
    write_fixture,
    write_stand_in,
)

REGION = "europe-west1"
TARGET = ["--project", PROJECT, "--region", REGION, "--job_id", JOB_ID]
GOOD = """
thresholds:
  row.coverage: {warn: 0.9, fail: 0.8}
  column.ks: {warn: 1, fail: 2}
"""
OVERRIDES = {"column.ks": (1.0, 2.0), "row.coverage": (0.9, 0.8)}
_GRADED = ("status", "score", "threshold_warn", "threshold_fail")
_MEASURED = ("value", "source_value", "synthetic_value", "noise_floor",
             "ci_low", "ci_high", "n_source", "n_synthetic", "method")


def _file(directory: Path, text: str, name: str = "thresholds.yaml") -> str:
  path = directory / name
  path.write_text(text)
  return str(path)


def _yaml(overrides: dict[str, tuple[float, float]]) -> str:
  lines = ["thresholds:"]
  lines += [
      f"  {metric_id}: {{warn: {warn}, fail: {fail}}}"
      for metric_id, (warn, fail) in overrides.items()
  ]
  return "\n".join(lines) + "\n"


def _recorded(row: dict[str, Any]) -> tuple[Any, Any]:
  params = row["evaluation_params"]
  return params["thresholds_uri"], params["thresholds_digest"]


# --------------------------------------------------------------------------
# the file
# --------------------------------------------------------------------------
def test_a_thresholds_file_is_read_normalised(tmp_path):
  overrides = load_thresholds(_file(tmp_path, GOOD))
  assert overrides == OVERRIDES
  assert list(overrides) == ["column.ks", "row.coverage"]  # by metric id
  assert all(isinstance(b, float) for pair in overrides.values() for b in pair)
  digest = thresholds_digest(overrides)
  assert re.fullmatch(r"[0-9a-f]{32}", digest)
  # the digest is of the overrides, not of the file's text
  respelt = ("# the same bounds, spelt another way\nthresholds:\n"
             "  column.ks: {fail: 2.0, warn: 1.0}\n"
             "  row.coverage:\n    warn: 0.90\n    fail: 0.8\n")
  again = load_thresholds(_file(tmp_path, respelt, "respelt.yaml"))
  assert thresholds_digest(again) == digest
  assert thresholds_digest({**overrides, "column.ks": (1.0, 2.5)}) != digest
  assert thresholds_digest({"column.ks": (1.0, 2.0)}) != digest
  # a file may override nothing: the catalogue's thresholds, on record
  nothing = load_thresholds(_file(tmp_path, "thresholds: {}\n", "none.yaml"))
  assert nothing == {} and thresholds_digest(nothing) != digest
  # equal bounds are a step, 0 / 0 the zero-tolerance rule
  step = "thresholds:\n  column.ks: {warn: 0.3, fail: 0.3}\n"
  assert load_thresholds(_file(tmp_path, step, "step.yaml")) == {
      "column.ks": (0.3, 0.3)
  }
  zero = "thresholds:\n  column.ks: {warn: 0, fail: 0}\n"
  assert load_thresholds(_file(tmp_path, zero, "zero.yaml")) == {
      "column.ks": (0.0, 0.0)
  }


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("column.ks: {warn: 0.1, fail: 0.2}\n", "top-level `thresholds`"),
        ("thresholds: [column.ks]\n", "top-level `thresholds`"),
        ("- thresholds\n", "top-level `thresholds`"),
        ("thresholds: {}\nnotes: mine\n", "unknown top-level key"),
        # an id the catalogue does not have
        ("thresholds:\n  column.kolmogorov: {warn: 0.1, fail: 0.2}\n",
         "'column.kolmogorov' is not a catalogue metric id"),
        # a missing bound
        ("thresholds:\n  column.ks: 0.2\n", "column.ks: expected a mapping"),
        ("thresholds:\n  column.ks: {fail: 0.2}\n",
         "column.ks: warn is missing"),
        ("thresholds:\n  column.ks: {warn: 0.1}\n",
         "column.ks: fail is missing"),
        ("thresholds:\n  column.ks: {warn: 0.1, fail: 0.2, source: mine}\n",
         "column.ks: unknown key"),
        # a bound that is no number
        ("thresholds:\n  column.ks: {warn: null, fail: 0.2}\n",
         "column.ks.warn: expected a number"),
        ("thresholds:\n  column.ks: {warn: '0.1', fail: 0.2}\n",
         "column.ks.warn: expected a number"),
        ("thresholds:\n  column.ks: {warn: true, fail: 0.2}\n",
         "column.ks.warn: expected a number"),
        ("thresholds:\n  column.ks: {warn: .nan, fail: 0.2}\n",
         "column.ks.warn: expected a number"),
        ("thresholds:\n  column.ks: {warn: 0.1, fail: .inf}\n",
         "column.ks.fail: expected a number"),
        ("thresholds:\n  column.ks: {warn: -0.1, fail: 0.2}\n",
         "column.ks.warn: expected a number"),
        # an integer no float can hold
        ("thresholds:\n  column.ks: {warn: 0.1, fail: " + "9" * 400 + "}\n",
         "column.ks.fail: expected a number"),
        # warn and fail in the wrong order for the direction
        ("thresholds:\n  column.ks: {warn: 0.3, fail: 0.2}\n",
         "column.ks is lower_better: expected warn <= fail"),
        ("thresholds:\n  row.coverage: {warn: 0.7, fail: 0.8}\n",
         "row.coverage is higher_better: expected warn >= fail"),
        ("thresholds:\n  column.std_ratio: {warn: 0.3, fail: 0.2}\n",
         "column.std_ratio is target: expected warn <= fail"),
        # what the scorer could not grade as asked
        ("thresholds:\n  row.coverage: {warn: 0, fail: 0}\n",
         "row.coverage is higher_better: warn and fail cannot both be 0"),
        ("thresholds:\n  column.wasserstein: {warn: 1, fail: 2}\n",
         "column.wasserstein is reported, never graded"),
    ])
def test_a_bad_thresholds_file_is_refused(tmp_path, text, message):
  path = _file(tmp_path, text)
  with pytest.raises(ValueError, match=re.escape(message)) as info:
    load_thresholds(path)
  assert path in str(info.value)  # the file is named


def test_a_bad_thresholds_file_stops_a_run_before_anything_starts(
    tmp_path, bq, resolved, capsys):
  made: list[str] = []

  def make_bq(project: str):
    made.append(project)
    return bq

  env = make_env(bq, make_bq=make_bq)
  wrong_order = _file(tmp_path, _yaml({"column.ks": (0.3, 0.2)}), "order.yaml")
  unknown = _file(tmp_path, _yaml({"column.kolmogorov": (0.1, 0.2)}), "id.yaml")
  missing = str(tmp_path / "absent.yaml")
  huge = _file(tmp_path,
               "thresholds:\n  column.ks: {warn: 1, fail: " + "9" * 400 + "}\n",
               "huge.yaml")
  for path, said in ((wrong_order, "expected warn <= fail"),
                     (unknown, "is not a catalogue metric id"), (missing,
                                                                 "absent.yaml"),
                     (huge, "column.ks.fail: expected a number")):
    argv = [*TARGET, "--thresholds_uri", path]
    for entry, flags in ((main, ["run", *argv]), (run_evaluation.main, argv)):
      with pytest.raises(SystemExit) as info:
        entry(flags, env)
      assert info.value.code == 2
      err = capsys.readouterr().err
      assert "sdfb-eval run: error: --thresholds_uri:" in err and said in err
      assert "Traceback" not in err
  assert not made and not resolved  # no client, no launch lookup
  assert not bq.registry_rows() and not bq.executed and not bq.queries


# --------------------------------------------------------------------------
# the seam: driver → pipeline → row context → scoring
# --------------------------------------------------------------------------
def test_the_row_context_grades_under_the_runs_thresholds():
  mv = MetricValue("column.ks", "users", 0.15, column="age", noise_floor=0.0)
  plain = RowContext("e1", "2026-09-14T08:00:00.000000Z", {}, {})
  assert plain.thresholds is None
  assert [metric_row(mv, plain)[k] for k in _GRADED] == ["warn", 0.85, 0.1, 0.2]
  tight = RowContext(
      "e1",
      "2026-09-14T08:00:00.000000Z", {}, {},
      thresholds={"column.ks": (0.01, 0.02)})
  assert [metric_row(mv, tight)[k] for k in _GRADED
         ] == ["fail", 0.85, 0.01, 0.02]


def test_run_hands_the_overrides_to_the_pipeline_and_records_them(
    bq, resolved, monkeypatch, tmp_path, capsys):
  del resolved
  build = tiny_pipeline(fast=True)
  monkeypatch.setattr(driver, "build_evaluation_pipeline", build)
  env = make_env(bq, submit=write_stand_in)
  path = _file(tmp_path, GOOD)
  digest = thresholds_digest(OVERRIDES)
  assert main(["run", *TARGET, "--thresholds_uri", path], env) == 0
  assert main(["run", *TARGET], env) == 0
  assert run_evaluation.main([*TARGET, "--thresholds_uri", path], env) == 0
  (given, plain, flex) = [kwargs for _, kwargs in build.built]
  assert given["thresholds"] == flex["thresholds"] == OVERRIDES
  assert plain["thresholds"] is None
  rows = bq.registry_rows()
  assert [r["status"] for r in rows] == ["RUNNING", "SUCCEEDED"] * 3
  for row in rows:
    check_row(REGISTRY, row, ordered=row["status"] == "RUNNING")
  # RUNNING and FINAL alike: the URI and the digest of what it held
  name = os.path.basename(path)  # a local path is recorded by base name
  assert [_recorded(r) for r in rows[:2]] == [(name, digest)] * 2
  assert [_recorded(r) for r in rows[2:4]] == [(None, None)] * 2
  assert [_recorded(r) for r in rows[4:]] == [(name, digest)] * 2
  # thresholds move no measured value: the same evaluation, by its key
  assert len({r["evaluation_key"] for r in rows}) == 1
  capsys.readouterr()


def test_only_a_gcs_uri_is_recorded_whole():
  held = argparse.Namespace(
      thresholds_uri="/home/someone/private/limits.yaml", thresholds=OVERRIDES)
  digest = thresholds_digest(OVERRIDES)
  override = driver._override  # pylint: disable=protected-access  # the recorded form is the unit under test
  assert override(held) == ("limits.yaml", digest)
  held.thresholds_uri = "gs://bucket/dir/limits.yaml"
  assert override(held) == ("gs://bucket/dir/limits.yaml", digest)
  held.thresholds_uri = "limits.yaml"
  assert override(held) == ("limits.yaml", digest)
  assert override(argparse.Namespace()) == (None, None)


def test_a_failed_evaluation_records_the_override_too(bq, resolved, stub,
                                                      monkeypatch, tmp_path,
                                                      capsys):
  del resolved, stub
  path = _file(tmp_path, GOOD)
  wanted = (os.path.basename(path), thresholds_digest(OVERRIDES))
  argv = ["run", *TARGET, "--thresholds_uri", path]

  def refuse(pipeline):
    raise RuntimeError("the runner refused the pipeline")

  assert main(argv, make_env(bq, submit=refuse)) == 3
  running, failed = bq.registry_rows()
  assert (running["status"], failed["status"]) == ("RUNNING", "FAILED")
  assert _recorded(running) == _recorded(failed) == wanted

  # and an attempt that never got a plan
  def unknown_job(**kwargs: Any):
    job_id = kwargs["job_id"]
    raise JobNotFoundError(f"Dataflow job {job_id} was not found")

  monkeypatch.setattr(driver, "resolve_launch", unknown_job)
  assert main(argv, make_env(bq)) == 3
  unplanned = bq.registry_rows()[-1]
  assert unplanned["status"] == "FAILED" and unplanned["tables"] == []
  assert _recorded(unplanned) == wanted
  check_row(REGISTRY, unplanned)
  assert main(["run", *TARGET], make_env(bq)) == 3
  assert _recorded(bq.registry_rows()[-1]) == (None, None)
  capsys.readouterr()


# --------------------------------------------------------------------------
# a real run with an override, and the same run without it
# --------------------------------------------------------------------------
def _loosest(metric_id: str) -> tuple[float, float]:
  """The loosest thresholds an override may give a metric: a share that
  only fails at 0, a distance that never reaches a million."""
  if catalogue().get(metric_id).direction == "higher_better":
    return (0.001, 0.0)
  return (1000000.0, 2000000.0)


class _Pair:
  """The same evaluation run twice: `default` under the catalogue's
  thresholds, `overridden` under a file (`uri`) that gives every metric
  with a FAIL row in the default run the loosest thresholds an override
  may hold (`overrides`). `codes` are the two exit codes under
  `--fail_on fail`.

  The table is `users` without `street_address`: free text of which no
  synthetic value is a source value scores exactly 0 on two
  higher-is-better metrics, and no threshold of 0 or more lets a 0 pass.
  It has 12 rows a side and a 6-row reference panel, so the run is not
  PARTIAL and only its counts speak to the gate. `age` is planted: one
  synthetic row at the source's youngest, the rest at its oldest."""

  def __init__(self, tmp: Path):
    source = fixture_rows(12, seed=5, id_base=100_000)
    synthetic = fixture_rows(12, seed=6, id_base=500_000)
    ages = sorted(row["age"] for row in source["users"])
    for k, row in enumerate(synthetic["users"]):
      row["age"] = ages[0] if k == 0 else ages[-1]
    spec = {
        "users": {
            "name": "users",
            "schema": [
                f for f in USERS_FIELDS if f["name"] != "street_address"
            ],
            "pk": ["id"],
            "identity": ["email"],
        }
    }
    fixture = write_fixture(
        tmp / "fixture",
        source,
        synthetic,
        tables=("users",),
        panel=6,
        specs=spec)
    self.codes: list[int] = []
    self.default = self._run(tmp, fixture, "default", [])
    failing = sorted({
        row["metric_id"]
        for row in self.default.metrics
        if row["status"] == "fail" and not is_aggregate(row["metric_id"])
    })
    self.overrides = {metric_id: _loosest(metric_id) for metric_id in failing}
    self.uri = _file(tmp, _yaml(self.overrides))
    self.overridden = self._run(tmp, fixture, "overridden",
                                ["--thresholds_uri", self.uri])

  def _run(self, tmp: Path, fixture: Path, name: str,
           extra: list[str]) -> store.Evaluation:
    # one clock and one token: the two runs are the same evaluation
    env = driver.Env(now=lambda: NOW, token=lambda: "0badc0de")
    argv = [
        "run", "--fixture_dir",
        str(fixture), "--mode", "exact", "--fail_on", "fail", "--output_local",
        str(tmp / name), *extra
    ]
    self.codes.append(main(argv, env))
    (directory,) = list((tmp / name).iterdir())
    return store.read_local(str(directory))


def _keyed(evaluation: store.Evaluation) -> dict[tuple, dict[str, Any]]:
  keyed = {
      (r["table_name"], r["metric_id"], r["column_name"], r["column_name_2"],
       r["edge"]):
          r for r in evaluation.metrics
  }
  assert len(keyed) == len(evaluation.metrics)
  return keyed


@pytest.fixture(scope="module", name="pair")
def fixture_pair(tmp_path_factory) -> _Pair:
  return _Pair(tmp_path_factory.mktemp("cli_thresholds"))


def test_an_overridden_run_stores_its_thresholds_and_what_they_imply(
    pair, capsys):
  before, after = _keyed(pair.default), _keyed(pair.overridden)
  assert before.keys() == after.keys()
  lifted, untouched = 0, set()
  for key, row in after.items():
    was = before[key]
    metric_id = row["metric_id"]
    if is_aggregate(metric_id):
      continue
    # an override moves no measured value
    assert [row[k] for k in _MEASURED] == [was[k] for k in _MEASURED], key
    if metric_id in pair.overrides:
      # the override's thresholds on its rows, the catalogue's without it
      metric = catalogue().get(metric_id)
      assert (was["threshold_warn"],
              was["threshold_fail"]) == (metric.warn, metric.fail), key
      assert (row["threshold_warn"],
              row["threshold_fail"]) == pair.overrides[metric_id], key
      assert row["status"] != "fail", key
      lifted += was["status"] == "fail"
    else:
      assert row == was, key  # nor any other metric's row
      untouched.add(metric_id)
  assert lifted == pair.default.final["metrics_fail"] > 10
  assert len(pair.overrides) > 5 and len(untouched) > 5
  # the planted ages: FAIL by the catalogue, PASS under the override
  ks = ("users", "column.ks", "age", None, None)
  assert before[ks]["value"] > 0.8 and before[ks]["status"] == "fail"
  assert after[ks]["status"] == "pass"
  assert after[ks]["score"] == before[ks]["score"] < 0.2  # `complement`
  smd = ("users", "column.smd", "age", None, None)
  assert (before[smd]["status"], before[smd]["score"]) == ("fail", 0.0)
  assert (after[smd]["status"], after[smd]["score"]) == ("pass", 1.0)
  # the roll-ups follow the rows: `linear` scores moved, so fidelity did
  fidelity = ("users", "table.fidelity_score", None, None, None)
  assert after[fidelity]["value"] > before[fidelity]["value"]
  capsys.readouterr()


def test_the_registry_of_an_overridden_run_counts_and_records_it(pair):
  default, overridden = pair.default.final, pair.overridden.final
  for evaluation in (pair.default, pair.overridden):
    measured = [
        r for r in evaluation.metrics if not is_aggregate(r["metric_id"])
    ]
    final = evaluation.final
    for status in ("pass", "warn", "fail"):
      assert final[f"metrics_{status}"] == sum(
          r["status"] == status for r in measured), status
    for event in evaluation.events:
      check_row(REGISTRY, event, ordered=False)
  assert default["metrics_fail"] > 10 and overridden["metrics_fail"] == 0
  assert overridden["metrics_total"] == default["metrics_total"]
  assert overridden["fidelity_score"] > default["fidelity_score"]
  assert overridden["evaluation_key"] == default["evaluation_key"]
  assert overridden["catalogue_version"] == default["catalogue_version"]
  wanted = (os.path.basename(pair.uri), thresholds_digest(pair.overrides))
  assert [_recorded(e) for e in pair.overridden.events] == [wanted, wanted]
  assert [_recorded(e) for e in pair.default.events] == [(None, None)] * 2


def test_an_override_flips_the_exit_code_of_the_gate(pair):
  """The gate grades nothing: it reads the FINAL row. The two FINAL rows
  of the pair, one evaluation under two sets of thresholds, exit
  differently under `--fail_on fail`."""
  default, overridden = pair.default.final, pair.overridden.final
  # neither run is PARTIAL (that alone would trip the gate): the counts decide
  assert default["status"] == overridden["status"]
  assert default["status"] in ("SUCCEEDED", "SUCCEEDED_WITH_WARNINGS")

  def code(final: dict[str, Any], fail_on: str) -> int:
    counts = {key: final[f"metrics_{key}"] for key in ("fail", "warn")}
    return gate.final_exit_code(final, fail_on, counts)

  assert (code(default, "fail"), code(overridden, "fail")) == (1, 0)
  assert pair.codes == [1, 0]  # and what the two `run --fail_on fail` exited
  assert (code(default, "none"), code(overridden, "none")) == (0, 0)
  # WARN rows of metrics the file does not name still trip `--fail_on warn`
  assert overridden["metrics_warn"] > 0 and code(overridden, "warn") == 1


def test_a_stored_row_rescores_to_its_status_under_its_own_thresholds(pair):
  """Any reader can reproduce a stored status from the row alone: its
  measured fields and the two thresholds it carries."""
  for evaluation in (pair.default, pair.overridden):
    for row in evaluation.metrics:
      own = None
      if row["threshold_warn"] is not None:
        own = {row["metric_id"]: (row["threshold_warn"], row["threshold_fail"])}
      again = rescored(row, thresholds=own)
      assert [again[k] for k in _GRADED] == [row[k] for k in _GRADED], row
      assert again["detail"] == row["detail"], row
  # the catalogue's thresholds alone do not: the stored ones are needed
  moved = [
      row for row in pair.overridden.metrics
      if rescored(row)["status"] != row["status"]
  ]
  assert {row["metric_id"] for row in moved} == set(pair.overrides)


# --------------------------------------------------------------------------
# report and compare
# --------------------------------------------------------------------------
def test_report_and_compare_show_the_override(bq, resolved, monkeypatch,
                                              tmp_path, capsys):
  del resolved
  env = make_env(bq, submit=write_stand_in)
  tight = {"column.ks": (0.01, 0.02)}
  path = _file(tmp_path, _yaml(tight))
  recorded = os.path.basename(path)
  digest = thresholds_digest(tight)
  out = tmp_path / "out"
  directories: dict[str, Path] = {}
  for name, extra, counts in (("plain", [], {
      "total": 1,
      "warn": 1
  }), ("tight", ["--thresholds_uri", path], {
      "total": 1,
      "fail": 1
  })):
    monkeypatch.setattr(driver, "build_evaluation_pipeline",
                        tiny_pipeline(fast=True, counts=counts, ks=[0.15]))
    argv = [
        "run", *TARGET, "--sink", "local_json", "--output_local",
        str(out), *extra
    ]
    assert main(argv, env) == 0
    (directories[name],) = set(out.iterdir()) - set(directories.values())
  capsys.readouterr()
  assert main(["report", "--local", str(directories["tight"])]) == 0
  text = capsys.readouterr().out
  assert f"| Thresholds | overridden by `{recorded}` (digest `{digest}`)" in text
  title = catalogue().get("column.ks").title
  assert f"### `column.ks` — {title}" in text
  assert ("warn 0.01 · fail 0.02 (this run's thresholds; the packaged "
          "catalogue has warn 0.1, fail 0.2)") in text
  assert main(
      ["report", "--local",
       str(directories["tight"]), "--format", "json"]) == 0
  document = json.loads(capsys.readouterr().out)
  (failing,) = document["failing"]
  assert (failing["metric_id"], failing["threshold_warn"],
          failing["threshold_fail"]) == ("column.ks", 0.01, 0.02)
  assert _recorded(document["evaluation"]) == (recorded, digest)
  assert main(["report", "--local", str(directories["plain"])]) == 0
  text = capsys.readouterr().out
  assert "| Thresholds | the catalogue's |" in text
  assert "this run's thresholds" not in text
  # two runs graded against different thresholds are told apart
  plain, tight_id = directories["plain"].name, directories["tight"].name
  argv = ["compare", "--local", str(out), "--evaluation_ids"]
  assert main([*argv, f"{plain},{tight_id}"]) == 0
  text = capsys.readouterr().out
  assert (f"thresholds differ (the catalogue's vs `{recorded}`, digest "
          f"`{digest}`)") in text
  assert main([*argv, f"{tight_id},{tight_id}"]) == 0
  assert "thresholds differ" not in capsys.readouterr().out
