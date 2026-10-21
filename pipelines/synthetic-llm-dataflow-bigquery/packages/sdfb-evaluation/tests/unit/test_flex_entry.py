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
"""The shared image's dispatcher against the real evaluator entry.

In the repository's one image the template launcher no longer runs
`run_evaluation.py` itself: it runs `docker/flex_entry.py`, which calls
`run_evaluation.main` when the launch passes `sdfb_job=evaluation`. These
tests hold the two together from the evaluator's side, in the evaluator's
own environment, where the generator is not installed at all:

    what the launcher did before        what the dispatcher does
    ──────────────────────────────────  ──────────────────────────────────
    python run_evaluation.py ARGS       python flex_entry.py ARGS + selector
    root logger set to INFO             the same
    sys.exit(main())                    sys.exit(run_evaluation.main(ARGS))

The dispatcher lives at the repository root (`docker/`); when the package
is copied out as a standalone unit it is absent and the module skips.
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import sys
from pathlib import Path

import pytest

from sdfb_evaluation.cli import run_evaluation

PACKAGE = Path(__file__).resolve().parents[2]
REPO_ROOT = PACKAGE.parents[1]
FLEX_ENTRY = REPO_ROOT / "docker" / "flex_entry.py"
ENTRY = PACKAGE / "src" / "sdfb_evaluation" / "cli" / "run_evaluation.py"

if not FLEX_ENTRY.is_file():
  pytest.skip(
      "docker/flex_entry.py is not part of this copy of the package",
      allow_module_level=True)


def _load():
  spec = importlib.util.spec_from_file_location("sdfb_flex_entry", FLEX_ENTRY)
  assert spec and spec.loader
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


@pytest.fixture(name="seen")
def fixture_seen(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
  """`run_evaluation.main` replaced by a recorder that returns 0."""
  calls: list[list[str]] = []

  def main(argv=None, env=None) -> int:
    del env
    calls.append(argv)
    return 0

  monkeypatch.setattr(run_evaluation, "main", main)
  return calls


def test_the_selector_reaches_the_real_entry_without_itself(
    seen: list[list[str]]) -> None:
  flags = ["--job_id=2026-01-01_00_00_00-1", "--trigger=chained", "--mode="]
  assert _load().main(["--sdfb_job=evaluation", *flags]) == 0
  assert seen == [flags]


def test_an_evaluation_launch_needs_no_generator(seen: list[list[str]]) -> None:
  """This environment has no `sdfb_beam` (the evaluator stands alone): an
  evaluation launch through the dispatcher must not look for it."""
  assert importlib.util.find_spec("sdfb_beam") is None
  assert _load().main(["--sdfb_job", "evaluation", "--run_id=r1"]) == 0
  assert seen == [["--run_id=r1"]]
  assert not [m for m in sys.modules if m.split(".")[0] == "sdfb_beam"]


def test_the_entrys_own_usage_error_comes_through_unchanged(
    capsys: pytest.CaptureFixture[str]) -> None:
  """No target named: the evaluator's parser refuses, exit 2, as it does
  when its file is the entry."""
  direct, through = [], []
  for call, into in ((lambda: run_evaluation.main([]), direct),
                     (lambda: _load().main(["--sdfb_job=evaluation"]),
                      through)):
    with pytest.raises(SystemExit) as raised:
      call()
    into.extend([raised.value.code, capsys.readouterr().err])
  assert direct == through
  assert direct[0] == 2 and "name exactly one of" in direct[1]


def test_as_a_process_the_root_logger_is_set_as_the_entrys_main_block_does(
    seen: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
  root = logging.getLogger()
  monkeypatch.setattr(root, "level", logging.WARNING)
  monkeypatch.setattr(
      sys, "argv", ["/opt/sdfb/flex_entry.py", "--sdfb_job=evaluation", "--x"])
  assert _load().main() == 0
  assert seen == [["--x"]]
  assert root.level == logging.INFO
  assert sys.argv == [run_evaluation.__file__, "--x"]


def test_called_with_arguments_it_leaves_the_process_alone(
    seen: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
  root = logging.getLogger()
  monkeypatch.setattr(root, "level", logging.WARNING)
  before = list(sys.argv)
  assert _load().main(["--sdfb_job=evaluation", "--x"]) == 0
  assert seen == [["--x"]]
  assert root.level == logging.WARNING and sys.argv == before


def test_the_entrys_main_block_is_what_the_dispatcher_reproduces() -> None:
  """The dispatcher stands in for running the file. If the file's
  `__main__` block gains a statement, the dispatcher must gain it too."""
  tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
  (block,) = [
      node for node in tree.body if isinstance(node, ast.If) and
      ast.unparse(node.test) == "__name__ == '__main__'"
  ]
  assert [ast.unparse(statement) for statement in block.body] == [
      "logging.getLogger().setLevel(logging.INFO)", "sys.exit(main())"
  ]
  assert not block.orelse
  assert "logging.getLogger().setLevel(logging.INFO)" in FLEX_ENTRY.read_text(
      encoding="utf-8")
