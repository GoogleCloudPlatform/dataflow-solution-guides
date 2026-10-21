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
"""Tests for the launch surfaces (Task 28): the evaluator's half of the
repository's one template and the one image it runs in.

The deployment uses ONE image and ONE template for generation and
evaluation (ADR 0041, amendment of 2026-10-06), and one file declares the
template's parameters (the evaluator's own image, template metadata and
build script were removed, note of 2026-10-15):

    file at the repository root           what this suite checks
    ────────────────────────────────────  ──────────────────────────────────
    docker/flex_template_metadata.json    every `sdfb-eval run` flag a
                                          launch can supply is declared, as
                                          the CLI parses it; every parameter
                                          marked "Evaluation only" is one
    docker/Dockerfile                     the Beam SDK and Python the
                                          evaluator is pinned to; the
                                          worker-image variable its driver
                                          reads

The generator's half of the same metadata is checked by
`packages/sdfb-tests/tests/unit/docker/test_launch_surfaces.py`. Neither
suite imports the other's package: a parameter whose help text starts
with the marker is the evaluation's alone, and every other one is the
generator's, the selector or Beam's disk (that suite checks them). Static
checks only: nothing is built and nothing reaches the network. A copy of
the package without the repository around it skips this module.

Design: docs/DESIGN.md §11 Evaluation (sdfb-evaluation)
(ADR 0041).
"""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path

import pytest
from apache_beam.options.pipeline_options import (
    PipelineOptions,
    WorkerOptions,
)

from sdfb_evaluation.cli import driver
from sdfb_evaluation.cli.main import (
    _flag,
    _subparsers,
    parse_args,
    public_run_flags,
)

PACKAGE = Path(__file__).resolve().parents[2]
REPO_ROOT = PACKAGE.parents[1]
METADATA = REPO_ROOT / "docker" / "flex_template_metadata.json"
DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile"

if not (METADATA.is_file() and DOCKERFILE.is_file()):
  pytest.skip(
      "the repository's docker/ is not beside this copy of the package",
      allow_module_level=True)

# The template launcher passes these to the entry itself (the template
# declares none of them): the runner is DataflowRunner, project and region
# are the launch's. A template parameter of the same name could contradict
# it, in an order-dependent way (Ruling R97).
LAUNCHER_SUPPLIED = frozenset({"--runner", "--project", "--region"})

# Not a flag of the CLI: the selector the image's entry
# (docker/flex_entry.py) reads and removes before the CLI sees the
# arguments (Ruling R118).
SELECTOR = "--sdfb_job"

# Not flags of the CLI either: Beam's own options, which `run` hands to Beam
# unchanged. They are declared so a launch may pass them. The worker boot
# disk is the one an evaluation launch must pass: the image is multi-GB and
# Dataflow's default disk overflows while a worker unpacks it.
BEAM_PASSTHROUGH = frozenset({"--disk_size_gb"})

# How a parameter only the evaluation job reads begins its help text (the
# metadata's description says so).
EVALUATION_ONLY = "Evaluation only"

# The image's Beam SDK base: its Python minor, then its Beam version.
BEAM_SDK_IMAGE = re.compile(
    r'^ARG BEAM_SDK_IMAGE="apache/beam_python(\d+\.\d+)_sdk:(\S+)"$', re.M)


def _dockerfile() -> str:
  return DOCKERFILE.read_text(encoding="utf-8")


def _metadata() -> dict:
  return json.loads(METADATA.read_text(encoding="utf-8"))


def _parameters() -> dict[str, dict]:
  parameters = _metadata()["parameters"]
  by_name = {p["name"]: p for p in parameters}
  assert len(by_name) == len(parameters), "a parameter is declared twice"
  return by_name


def _launch_flags() -> set[str]:
  """The `run` flags a template launch can supply, without dashes."""
  flags = set(public_run_flags())
  assert flags, "no public flags: the parser changed shape"
  assert flags >= LAUNCHER_SUPPLIED
  return {flag[2:] for flag in flags - LAUNCHER_SUPPLIED}


def _evaluation_parameters() -> dict[str, dict]:
  """The parameters the evaluation job reads: its launch flags, the
  selector and Beam's disk."""
  names = _launch_flags() | {SELECTOR[2:]} | {f[2:] for f in BEAM_PASSTHROUGH}
  return {n: p for n, p in _parameters().items() if n in names}


def _locked(name: str) -> str:
  lock = tomllib.loads((PACKAGE / "uv.lock").read_text(encoding="utf-8"))
  versions = [p["version"] for p in lock["package"] if p["name"] == name]
  assert len(versions) == 1, f"{name} locked {len(versions)} times"
  return versions[0]


def test_every_flag_a_launch_can_supply_is_declared() -> None:
  flags = _launch_flags()
  assert SELECTOR[2:] not in flags
  assert not {f[2:] for f in BEAM_PASSTHROUGH} & flags
  assert flags <= set(_parameters()), sorted(flags - set(_parameters()))


def test_every_parameter_marked_evaluation_only_is_a_run_flag() -> None:
  """The evaluator's half of "no parameter is unowned": what the marker
  gives to the evaluation, its CLI must take. Every unmarked parameter is
  the generator's, the selector or Beam's disk, which the generator's
  suite checks."""
  marked = {
      name for name, parameter in _parameters().items()
      if parameter["helpText"].startswith(EVALUATION_ONLY)
  }
  assert marked, "no parameter carries the marker: it changed spelling"
  assert marked <= _launch_flags(), sorted(marked - _launch_flags())
  assert f"'{EVALUATION_ONLY}'" in _metadata()["description"]


def test_a_flag_the_generator_also_takes_says_what_it_means_here() -> None:
  """A launch flag without the marker is a name both jobs use; its help
  text gives the evaluation's meaning beside the generation's."""
  template = _parameters()
  for name in sorted(_launch_flags()):
    text = template[name]["helpText"]
    if not text.startswith(EVALUATION_ONLY):
      assert "Generation:" in text and "Evaluation:" in text, name


def test_the_selector_is_optional_and_admits_the_two_jobs() -> None:
  selector = _parameters()[SELECTOR[2:]]
  assert selector["isOptional"] is True
  (pattern,) = selector["regexes"]
  for value in ("evaluation", "generation", ""):
    assert re.fullmatch(pattern, value), value
  assert not re.fullmatch(pattern, "both")
  assert "evaluation" in selector["helpText"]


def test_the_worker_disk_is_beams_own_option_and_never_empty() -> None:
  """`disk_size_gb` reaches Beam as it is given; Beam reads an integer, so
  the pattern refuses the empty string a template passes for "unset"."""
  args, extras = parse_args([
      "run", "--job_id", "J", "--project", "p", "--region", "r", "--runner",
      "DataflowRunner", "--disk_size_gb=200"
  ])
  assert not hasattr(args, "disk_size_gb")
  assert PipelineOptions(extras).view_as(WorkerOptions).disk_size_gb == 200
  pattern = _regex("disk_size_gb")
  assert re.fullmatch(pattern, "200") and re.fullmatch(pattern, "25")
  for refused in ("", "0", "200GB", "-1"):
    assert not re.fullmatch(pattern, refused), refused
  with pytest.raises(SystemExit) as raised:
    parse_args([
        "run", "--job_id", "J", "--project", "p", "--region", "r",
        "--disk_size_gb="
    ])
  assert raised.value.code == 2


def test_metadata_leaves_launcher_supplied_flags_out() -> None:
  names = {"--" + name for name in _parameters()}
  assert not names & LAUNCHER_SUPPLIED


def _run_choices() -> dict[str, tuple[str, ...]]:
  run = _subparsers()[1]["run"]
  actions = run._actions  # pylint: disable=protected-access  # no public list
  return {
      option[2:]: tuple(action.choices) for action in actions
      if action.choices and action.help is not argparse.SUPPRESS
      for option in action.option_strings
      if option.startswith("--") and option != "--help"
  }


def _regex(name: str) -> str:
  (pattern,) = _parameters()[name]["regexes"]
  return pattern


@pytest.mark.parametrize("name", sorted(_run_choices()))
def test_regex_of_a_choices_flag_admits_exactly_the_choices(name: str) -> None:
  pattern = _regex(name)
  for choice in (*_run_choices()[name], ""):
    assert re.fullmatch(pattern, choice), (name, choice)
  assert not re.fullmatch(pattern, "no-such-choice")


def _boolean_flags() -> list[str]:
  run = _subparsers()[1]["run"]
  return sorted(action.option_strings[0][2:]
                for action in run._actions  # pylint: disable=protected-access  # argparse has no public list of options
                if action.type is _flag)


def test_boolean_flags_exist() -> None:
  assert "allow_contaminated" in _boolean_flags()


@pytest.mark.parametrize("name", _boolean_flags())
def test_boolean_regex_accepts_what_the_cli_parser_accepts(name: str) -> None:
  pattern = _regex(name)
  spellings = [
      "", "true", "false", "True", "False", "TRUE", "FALSE", "1", "0", "yes",
      "No", "ON", "off", "maybe", "2", "tru"
  ]
  for value in spellings:
    try:
      _flag(value)
      accepted = True
    except argparse.ArgumentTypeError:
      accepted = False
    assert bool(re.fullmatch(pattern, value)) == accepted, (name, value)


def test_evaluation_parameters_are_well_formed() -> None:
  metadata = _metadata()
  assert metadata["name"] and metadata["description"]
  for name, parameter in _evaluation_parameters().items():
    assert parameter["label"] and parameter["helpText"], name
    assert re.fullmatch(r"[a-z][a-z0-9_]*", name)
    # A flex template passes an unset optional parameter as an empty
    # string, which the CLI reads as "not given"; no parameter is required.
    assert parameter.get("isOptional") is True, name
    for pattern in parameter.get("regexes", []):
      re.compile(pattern)


def test_metadata_has_no_launch_only_parameters() -> None:
  launch_only = {"experiments", "enable_data_sampling", "fixture_dir"}
  assert not set(_parameters()) & launch_only


def test_the_image_bakes_the_worker_image_variable_the_driver_reads() -> None:
  """The driver applies this variable as the workers' image when a launch
  gives none; the image sets it from its one build argument."""
  variable = driver._WORKER_IMAGE_ENV  # pylint: disable=protected-access  # the name the image must set
  assert re.search(rf"^ENV {variable}=\$\w+_ARG$", _dockerfile(), re.M)


def test_beam_sdk_tag_equals_locked_apache_beam() -> None:
  match = BEAM_SDK_IMAGE.search(_dockerfile())
  assert match
  assert match.group(2) == _locked("apache-beam")


def test_python_minor_matches_the_package() -> None:
  minor = (PACKAGE / ".python-version").read_text(encoding="utf-8").strip()
  requires = tomllib.loads((PACKAGE / "pyproject.toml").read_text(
      encoding="utf-8"))["project"]["requires-python"]
  assert minor in requires
  text = _dockerfile()
  base = BEAM_SDK_IMAGE.search(text)
  launcher = re.search(r"python(\d)(\d+)-template-launcher-base", text)
  assert base and launcher
  assert base.group(1) == minor
  assert f"{launcher.group(1)}.{launcher.group(2)}" == minor
  # the venv path on the launcher's PYTHONPATH carries the same minor
  pythonpath = re.search(r'^ENV PYTHONPATH="([^"]+)"$', text, re.M)
  assert pythonpath and f"python{minor}/site-packages" in pythonpath.group(1)


def test_image_never_enables_data_sampling() -> None:
  for path in (DOCKERFILE, METADATA):
    assert "enable_data_sampling" not in path.read_text(encoding="utf-8")
