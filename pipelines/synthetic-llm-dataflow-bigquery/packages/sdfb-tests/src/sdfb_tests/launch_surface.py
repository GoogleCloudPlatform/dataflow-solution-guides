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
"""The generator's command line as the launch surfaces must mirror it.

The Flex Template declares every parameter optional (one template serves
two jobs, and a launch for one cannot be made to supply the other's), so
the template no longer says which flags a generation launch needs:
`run_pipeline`'s own parser does. Tests that tie a launch surface (the
template metadata, the Composer DAG, the personal-GCP tiers) to the CLI read
the parser through this module.

`run_pipeline.parse_args` builds its parser inside the function and offers
no factory, so the parser is taken from one parse that is stopped before it
reads anything.
"""

from __future__ import annotations

import argparse
from unittest import mock

__all__ = ["generator_flags", "generator_parser", "generator_required"]


class _CapturedError(Exception):
  """Carries the parser out of `parse_args`."""


def generator_parser() -> argparse.ArgumentParser:
  """The argument parser `sdfb_beam.cli.run_pipeline` parses a launch with."""
  from sdfb_beam.cli import run_pipeline  # pylint: disable=import-outside-toplevel  # imports Beam: only the tests that ask pay for it

  def capture(parser, *args, **kwargs):
    del args, kwargs
    raise _CapturedError(parser)

  with mock.patch.object(argparse.ArgumentParser, "parse_known_args", capture):
    try:
      run_pipeline.parse_args([])
    except _CapturedError as captured:
      parser = captured.args[0]
      return parser
  raise AssertionError(
      "run_pipeline.parse_args no longer parses with parse_known_args")


def _names(actions) -> set[str]:
  return {
      option[2:]
      for action in actions
      for option in action.option_strings
      if option.startswith("--") and option != "--help"
  }


def _actions() -> list[argparse.Action]:
  # argparse has no public list of a parser's options
  return generator_parser()._actions  # pylint: disable=protected-access


def generator_flags() -> set[str]:
  """Every flag of the generator's CLI, without the leading dashes."""
  return _names(_actions())


def generator_required() -> set[str]:
  """The flags the generator's parser refuses a launch without."""
  return _names(action for action in _actions() if action.required)
