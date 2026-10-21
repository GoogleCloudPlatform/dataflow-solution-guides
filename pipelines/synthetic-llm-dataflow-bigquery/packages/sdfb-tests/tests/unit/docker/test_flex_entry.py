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
"""Tests for `docker/flex_entry.py`, the image's one Flex Template entry.

The launcher runs that file for every launch of the template. It reads one
selector, `--sdfb_job`, removes it and hands every other argument, in order,
to the chosen entry:

    arguments                      entry called               exit code
    ─────────────────────────────  ─────────────────────────  ─────────────
    (no selector), =generation,    sdfb_beam.cli              the entry's
    = (empty)                        .run_pipeline.main
    =evaluation                    sdfb_evaluation.cli        the entry's
                                     .run_evaluation.main
    any other value, no value      none                       2
    =evaluation, evaluator absent  none                       2

A generation launch must reach `run_pipeline` exactly as it did when that
file was the entry, so three kinds of test live here: the dispatcher against
stub entries (in this process and as a real process), the dispatcher against
the real `run_pipeline`, and the shape of `run_pipeline`'s own
`__main__` block, which is what "calling its `main`" stands in for.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import ast
import importlib
import importlib.abc
import importlib.util
import json
import logging
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = next(
    p for p in Path(__file__).resolve().parents
    if (p / "docker" / "entrypoint.sh").is_file())
_FLEX_ENTRY = _REPO_ROOT / "docker" / "flex_entry.py"
_RUN_PIPELINE = (
    _REPO_ROOT / "packages" / "sdfb-beam" / "src" / "sdfb_beam" / "cli" /
    "run_pipeline.py")

_GENERATION = "sdfb_beam.cli.run_pipeline"
_EVALUATION = "sdfb_evaluation.cli.run_evaluation"


def _load():
  spec = importlib.util.spec_from_file_location("sdfb_flex_entry", _FLEX_ENTRY)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


@pytest.fixture(name="entry")
def _entry():
  return _load()


@pytest.fixture(autouse=True)
def _evaluator_modules_as_found():
  """What a test imports under `sdfb_evaluation` does not outlive it. Some
    tests here import a stand-in package from a temporary folder; left in
    `sys.modules` it would shadow the real evaluator for every later test of
    the session. monkeypatch restores the entries a test removed, not the
    ones its imports added."""
  before = {
      name: module
      for name, module in sys.modules.items()
      if name.split(".")[0] == "sdfb_evaluation"
  }
  yield
  for name in [m for m in sys.modules if m.split(".")[0] == "sdfb_evaluation"]:
    if name not in before:
      del sys.modules[name]
  sys.modules.update(before)


@pytest.fixture(name="calls")
def _calls(monkeypatch):
  """Both entries replaced by stubs that record their arguments and return
    a code of their own (7 generation, 5 evaluation)."""
  seen: list[tuple[str, object]] = []

  def stub(name: str, kind: str, code: int) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = f"/stub/{kind}.py"

    def main(argv=None):
      seen.append((kind, argv))
      return code

    module.main = main
    return module

  monkeypatch.setitem(sys.modules, _GENERATION,
                      stub(_GENERATION, "generation", 7))
  monkeypatch.setitem(sys.modules, _EVALUATION,
                      stub(_EVALUATION, "evaluation", 5))
  return seen


# --------------------------------------------------------------------------
# the selector
# --------------------------------------------------------------------------
def test_no_selector_is_a_generation_launch(entry, calls):
  assert entry.main(["--run_id=r1", "--num_rows=10"]) == 7
  assert calls == [("generation", ["--run_id=r1", "--num_rows=10"])]


@pytest.mark.parametrize("selector", [
    ["--sdfb_job=generation"],
    ["--sdfb_job", "generation"],
    ["--sdfb_job="],
    ["--sdfb_job", ""],
    ["--sdfb_job=  "],
])
def test_generation_spellings_and_the_empty_value(entry, calls, selector):
  assert entry.main([*selector, "--run_id=r1"]) == 7
  assert calls == [("generation", ["--run_id=r1"])]


@pytest.mark.parametrize("selector", [
    ["--sdfb_job=evaluation"],
    ["--sdfb_job", "evaluation"],
])
def test_evaluation_spellings(entry, calls, selector):
  assert entry.main([*selector, "--job_id=J"]) == 5
  assert calls == [("evaluation", ["--job_id=J"])]


@pytest.mark.parametrize("position", [0, 1, 2, 3])
def test_the_selector_is_removed_and_the_rest_keeps_its_order(
    entry, calls, position):
  rest = ["--a=1", "--b", "two words", "--experiments=x=y"]
  for selector in (["--sdfb_job=evaluation"], ["--sdfb_job", "evaluation"]):
    calls.clear()
    given = [*rest[:position], *selector, *rest[position:]]
    assert entry.main(given) == 5
    assert calls == [("evaluation", rest)]


def test_split_selector_returns_the_job_and_the_other_arguments(entry):
  assert entry.split_selector([]) == ("generation", [])
  assert entry.split_selector(["--x", "--sdfb_job=evaluation",
                               "y"]) == ("evaluation", ["--x", "y"])
  # only the exact flag is the selector: a longer or shorter name is not
  assert entry.split_selector(["--sdfb_jobs=evaluation", "--sdfb_jo=x"
                              ]) == ("generation",
                                     ["--sdfb_jobs=evaluation", "--sdfb_jo=x"])
  # given twice, the last one counts (as argparse reads a repeated flag)
  assert entry.split_selector(
      ["--sdfb_job=evaluation", "--sdfb_job=generation"]) == ("generation", [])


def test_the_entry_gets_a_list_it_may_keep(entry, calls):
  given = ("--sdfb_job=evaluation", "--job_id=J")
  entry.main(given)
  (_, forwarded), = calls
  assert isinstance(forwarded, list) and forwarded == ["--job_id=J"]


def test_a_none_exit_code_is_success(entry, monkeypatch):
  stub = types.ModuleType(_GENERATION)
  stub.main = lambda argv=None: None
  monkeypatch.setitem(sys.modules, _GENERATION, stub)
  assert entry.main(["--x"]) == 0


# --------------------------------------------------------------------------
# usage errors (exit 2): nothing is called
# --------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["evalution", "both", "Evaluation", "1"])
def test_an_unknown_value_is_a_usage_error_naming_both(entry, calls, capsys,
                                                       value):
  assert entry.main([f"--sdfb_job={value}", "--job_id=J"]) == 2
  assert not calls
  message = capsys.readouterr().err
  assert "generation" in message and "evaluation" in message
  assert repr(value) in message


@pytest.mark.parametrize("given", [
    ["--job_id=J", "--sdfb_job"],
    ["--sdfb_job", "--job_id=J"],
])
def test_a_selector_without_a_value_is_a_usage_error(entry, calls, capsys,
                                                     given):
  assert entry.main(given) == 2
  assert not calls
  assert "--sdfb_job" in capsys.readouterr().err


class _NoEvaluator(importlib.abc.MetaPathFinder):
  """The import system of an image whose build context had no
    packages/sdfb-evaluation: the top-level package is not found."""

  def find_spec(self, fullname, path, target=None):
    del path, target
    if fullname.split(".")[0] == "sdfb_evaluation":
      raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)


def _forget_the_evaluator(monkeypatch) -> None:
  for name in [m for m in sys.modules if m.split(".")[0] == "sdfb_evaluation"]:
    monkeypatch.delitem(sys.modules, name)


def test_evaluation_without_the_evaluator_is_one_clear_sentence(
    entry, monkeypatch, capsys):
  _forget_the_evaluator(monkeypatch)
  monkeypatch.setattr(sys, "meta_path", [_NoEvaluator(), *sys.meta_path])
  assert entry.main(["--sdfb_job=evaluation", "--job_id=J"]) == 2
  message = capsys.readouterr().err.strip()
  assert "built without packages/sdfb-evaluation" in message
  assert len(message.splitlines()) == 1


def test_a_missing_dependency_of_the_evaluator_is_not_hidden(
    entry, monkeypatch, tmp_path):
  """Only the package's own absence is the usage error. An evaluator that
    is there and cannot import what it needs fails with its traceback."""
  package = tmp_path / "sdfb_evaluation" / "cli"
  package.mkdir(parents=True)
  (tmp_path / "sdfb_evaluation" / "__init__.py").write_text("")
  (package / "__init__.py").write_text("")
  (package / "run_evaluation.py").write_text("import sdfb_no_such_package\n")
  _forget_the_evaluator(monkeypatch)
  monkeypatch.syspath_prepend(str(tmp_path))
  importlib.invalidate_caches()
  with pytest.raises(ModuleNotFoundError) as raised:
    entry.main(["--sdfb_job=evaluation"])
  assert raised.value.name == "sdfb_no_such_package"


def test_no_stand_in_evaluator_is_left_for_later_tests():
  """The tests above import stand-in `sdfb_evaluation` packages. One left in
    `sys.modules` shadows the real evaluator for every later test of the
    session that imports it. Runs after them, in file order."""
  real = _REPO_ROOT / "packages" / "sdfb-evaluation" / "src"
  left = {
      name: getattr(module, "__file__", None)
      for name, module in sys.modules.items()
      if name.split(".")[0] == "sdfb_evaluation"
  }
  assert all(
      path and Path(path).is_relative_to(real) for path in left.values()), left


# --------------------------------------------------------------------------
# as the launcher runs it: a process of its own
# --------------------------------------------------------------------------
_STUB = """
import json
import sys


def main(argv=None):
  print(json.dumps({{
      "entry": "{kind}",
      "argv": argv,
      "sys_argv": sys.argv,
      "sdfb_modules": sorted(m for m in sys.modules if m.startswith("sdfb_")),
  }}))
  return {code}
"""


def _stub_packages(root: Path) -> None:
  for package, module, kind, code in (
      ("sdfb_beam", "run_pipeline", "generation", 7),
      ("sdfb_evaluation", "run_evaluation", "evaluation", 5),
  ):
    cli = root / package / "cli"
    cli.mkdir(parents=True)
    (root / package / "__init__.py").write_text("")
    (cli / "__init__.py").write_text("")
    (cli / f"{module}.py").write_text(_STUB.format(kind=kind, code=code))


def _launch(tmp_path: Path, *arguments: str) -> tuple[int, dict]:
  _stub_packages(tmp_path)
  process = subprocess.run(
      [sys.executable, str(_FLEX_ENTRY), *arguments],
      capture_output=True,
      text=True,
      check=False,
      cwd=tmp_path,
      # first on the path: the stubs stand in for both packages
      env={
          **os.environ, "PYTHONPATH": str(tmp_path)
      },
  )
  assert process.stdout, process.stderr
  return process.returncode, json.loads(process.stdout)


def test_a_generation_launch_never_imports_the_evaluator(tmp_path):
  code, seen = _launch(tmp_path, "--run_id=r1", "--num_rows=10")
  assert code == 7  # the entry's exit code is the process's
  assert seen["entry"] == "generation"
  assert seen["argv"] == ["--run_id=r1", "--num_rows=10"]
  assert not [m for m in seen["sdfb_modules"] if "evaluation" in m]
  assert "sdfb_beam.cli.run_pipeline" in seen["sdfb_modules"]


def test_an_evaluation_launch_never_imports_the_generator(tmp_path):
  code, seen = _launch(tmp_path, "--job_id=J", "--sdfb_job=evaluation",
                       "--trigger=chained")
  assert code == 5
  assert seen["entry"] == "evaluation"
  assert seen["argv"] == ["--job_id=J", "--trigger=chained"]
  assert not [m for m in seen["sdfb_modules"] if m.startswith("sdfb_beam")]
  assert "sdfb_evaluation.cli.run_evaluation" in seen["sdfb_modules"]


@pytest.mark.parametrize("selector,module", [
    ((), "run_pipeline.py"),
    (("--sdfb_job=generation",), "run_pipeline.py"),
    (("--sdfb_job=evaluation",), "run_evaluation.py"),
])
def test_the_process_arguments_are_the_entrys_own(tmp_path, selector, module):
  """`sys.argv` is what the entry saw when the launcher ran its file: the
    file's path, then the arguments without the selector. A parser's usage
    line and any later read of `sys.argv` (Beam's default options) are then
    the ones of the entry it replaced."""
  _, seen = _launch(tmp_path, *selector, "--a=1", "--b=2")
  assert Path(seen["sys_argv"][0]).name == module
  assert seen["sys_argv"][1:] == ["--a=1", "--b=2"]


def test_a_usage_error_exits_2_as_a_process(tmp_path):
  process = subprocess.run(
      [sys.executable, str(_FLEX_ENTRY), "--sdfb_job=neither"],
      capture_output=True,
      text=True,
      check=False,
      cwd=tmp_path,
  )
  assert process.returncode == 2
  assert not process.stdout
  assert "generation" in process.stderr and "evaluation" in process.stderr


# --------------------------------------------------------------------------
# the real generator entry
# --------------------------------------------------------------------------
class _ReachedError(Exception):
  """Raised by the recording `parse_args`: the entry got this far."""


def test_run_pipeline_parses_the_same_arguments_through_the_dispatcher(
    entry, monkeypatch):
  """What reaches `run_pipeline`'s parser is the launch's arguments,
    whether its file is the entry (before) or the dispatcher is (now)."""
  from sdfb_beam.cli import run_pipeline

  parsed: list[list[str]] = []

  def recording_parse_args(argv):
    parsed.append(list(argv))
    raise _ReachedError

  monkeypatch.setattr(run_pipeline, "parse_args", recording_parse_args)
  monkeypatch.setattr(run_pipeline, "log_build_info", lambda role: None)
  monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: None)
  launch = [
      "--runner=DataflowRunner", "--reference_table=p.d.t", "--num_rows=10",
      "--run_id=manual__2026-01-01T00:00:00+00:00-abcd1234",
      "--experiments=use_runner_v2"
  ]
  with pytest.raises(_ReachedError):
    run_pipeline.main(list(launch))
  for given in (launch, ["--sdfb_job=generation", *launch],
                [*launch[:2], "--sdfb_job", "generation", *launch[2:]]):
    with pytest.raises(_ReachedError):
      entry.main(given)
  assert parsed == [launch] * 4


def test_running_run_pipeline_as_a_file_only_calls_its_main():
  """The dispatcher calls `run_pipeline.main`; that equals running the file
    only while the file's `__main__` block does nothing else."""
  tree = ast.parse(_RUN_PIPELINE.read_text(encoding="utf-8"))
  (block,) = [
      node for node in tree.body if isinstance(node, ast.If) and
      ast.unparse(node.test) == "__name__ == '__main__'"
  ]
  assert [ast.unparse(statement) for statement in block.body
         ] == ["sys.exit(main())"]
  assert not block.orelse


# --------------------------------------------------------------------------
# the file itself
# --------------------------------------------------------------------------
def test_the_dispatcher_imports_only_the_standard_library_at_the_top():
  tree = ast.parse(_FLEX_ENTRY.read_text(encoding="utf-8"))
  imported = set()
  for node in tree.body:
    if isinstance(node, ast.Import):
      imported |= {alias.name.split(".")[0] for alias in node.names}
    elif isinstance(node, ast.ImportFrom):
      imported.add((node.module or "").split(".")[0])
  assert imported <= set(sys.stdlib_module_names) | {"__future__"}
  assert not {"sdfb_beam", "sdfb_core", "sdfb_evaluation"} & imported


def test_the_dispatcher_is_the_only_file_that_names_both_packages():
  """Neither package learns about the other (the evaluator stands alone;
    the generator never imports it)."""
  text = _FLEX_ENTRY.read_text(encoding="utf-8")
  assert _GENERATION in text and _EVALUATION in text
  for package in ("sdfb-core", "sdfb-beam"):
    for path in (_REPO_ROOT / "packages" / package / "src").rglob("*.py"):
      source = path.read_text(encoding="utf-8")
      assert "sdfb_evaluation" not in source, path
