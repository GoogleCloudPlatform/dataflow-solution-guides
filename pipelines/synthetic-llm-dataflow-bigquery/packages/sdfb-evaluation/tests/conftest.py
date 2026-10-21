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
"""Pytest config + shared fixtures.

One `netguard.NetworkGuard` is installed for the whole session: no test
(and no fixture of any scope) can connect off this machine, resolve a
host name or look for Google credentials. It is lifted only around a
test marked `gcp`. The `no_network` fixture (autouse) fails a test whose
code swallowed an attempt, and gives a test the list of attempts.

The default Beam runner of the session is `FnApiRunner`, Beam's
in-process Python runner, never the switching `DirectRunner`: a test
that builds a pipeline without naming a runner (`TestPipeline()`,
`beam.Pipeline()`) would otherwise hand a batch pipeline to Prism
wherever the Prism binary is cached or can be downloaded, and Prism
starts a step before its batch side input is complete
(`sdfb_evaluation.beam.pipeline`, Runners) — every transform here reads
side inputs. On `FnApiRunner` a stage runs to completion before the
stages reading it, the batch contract Dataflow holds too. It is set
here, at the package root, so every test directory gets it whatever the
collection order.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from apache_beam.options.pipeline_options import StandardOptions
from netguard import NetworkGuard

_FIXTURES = Path(__file__).parent / "fixtures"
IN_PROCESS_RUNNER = "FnApiRunner"


@pytest.fixture(scope="session", autouse=True, name="network_guard")
def fixture_network_guard() -> Iterator[NetworkGuard]:
  """The session's guard, installed before any other fixture runs."""
  guard = NetworkGuard()
  guard.install()
  yield guard
  guard.remove()


@pytest.fixture(autouse=True)
def no_network(request: pytest.FixtureRequest,
               network_guard: NetworkGuard) -> Iterator[list[str]]:
  """The network attempts of this test (none allowed): any still in the
  list when the test ends fails it, including one a higher-scoped
  fixture made while it was set up. A `gcp` test runs unguarded."""
  if request.node.get_closest_marker("gcp") is not None:
    network_guard.remove()
    yield network_guard.attempts
    network_guard.install()
    return
  yield network_guard.attempts
  network_guard.verify()


@pytest.fixture(autouse=True, scope="session")
def in_process_default_runner() -> Iterator[None]:
  """`StandardOptions.DEFAULT_RUNNER` is `FnApiRunner` for the session
  (restored after it)."""
  default = StandardOptions.DEFAULT_RUNNER
  StandardOptions.DEFAULT_RUNNER = IN_PROCESS_RUNNER
  yield
  StandardOptions.DEFAULT_RUNNER = default


@pytest.fixture
def load_fixture() -> Callable[[str], Any]:
  """A loader for JSON fixtures kept under `tests/fixtures/`.

  Returns a function taking a path relative to `tests/fixtures/` (e.g.
  `"parity/goldens.json"`) and returning its parsed JSON content. Fixtures
  are `.json` only (repo-wide data-hygiene convention).
  """

  def _load(relative_path: str) -> Any:
    return json.loads((_FIXTURES / relative_path).read_text(encoding="utf-8"))

  return _load
