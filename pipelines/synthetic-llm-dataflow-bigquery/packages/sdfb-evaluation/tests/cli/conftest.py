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
"""Fixtures of the CLI tests.

The packaged catalogue is immutable, and parsing its YAML takes about a
quarter of a second; a CLI run asks for it several times (the planner,
the registry rows, the thresholds file, the renderers). `cached_catalogue` parses
it once for the whole session, which keeps these tests inside their
60-second budget (Ruling R88i).

`scratch` points `tempfile` at the test's own directory, so a run that
keeps its temporary output (a failed `--sink bq_client` run does) leaves
nothing in the machine's temp directory.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import functools
import tempfile
from pathlib import Path
from typing import Any

import pytest
from unit.context.plan_fakes import thelook_launch, thelook_models

from sdfb_evaluation.beam import assemble
from sdfb_evaluation.catalogue import Catalogue, load_catalogue
from sdfb_evaluation.cli import driver, fixture, main, thresholds
from sdfb_evaluation.context import plan
from sdfb_evaluation.report import render

from .helpers import RecordingBq, tiny_pipeline

_MODULES = (assemble, driver, fixture, main, plan, render, thresholds)


@functools.cache
def catalogue() -> Catalogue:
  """The packaged catalogue, parsed once."""
  return load_catalogue()


@pytest.fixture(autouse=True)
def cached_catalogue(monkeypatch) -> Catalogue:
  """Every module the CLI tests drive loads the session's one parse."""
  for module in _MODULES:
    monkeypatch.setattr(module, "load_catalogue", catalogue)
  return catalogue()


@pytest.fixture(autouse=True, name="scratch")
def fixture_scratch(tmp_path: Path, monkeypatch) -> Path:
  """`tempfile`'s directory for this test: what `tempfile.mkdtemp`
  creates lands under the test's own `tmp_path`."""
  directory = tmp_path / "tmp"
  directory.mkdir()
  monkeypatch.setattr(tempfile, "tempdir", str(directory))
  return directory


@pytest.fixture(name="bq")
def fixture_bq() -> RecordingBq:
  return RecordingBq()


@pytest.fixture(name="resolved")
def fixture_resolved(monkeypatch, bq) -> list[dict[str, Any]]:
  """The launch and models the CLI resolves, without the Dataflow and
  Logging APIs: the invented thelook launch of `bq`'s rows. Returns the
  calls `resolve_launch` received."""
  calls: list[dict[str, Any]] = []

  def resolve(**kwargs: Any):
    calls.append(kwargs)
    return thelook_launch(bq)

  monkeypatch.setattr(driver, "resolve_launch", resolve)
  monkeypatch.setattr(driver, "load_models", lambda uri: thelook_models())
  return calls


@pytest.fixture(name="stub")
def fixture_stub(monkeypatch):
  """`build_evaluation_pipeline` replaced by an empty pipeline: for runs
  whose pipeline never executes (the real graph is `test_run.py`'s)."""
  build = tiny_pipeline(write=False)
  monkeypatch.setattr(driver, "build_evaluation_pipeline", build)
  return build
