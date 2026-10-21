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
"""The image's one Flex Template entry (`FLEX_TEMPLATE_PYTHON_PY_FILE`).

One image and one template serve two jobs (ADR 0009; ADR 0041, amendment
of 2026-10-06). The template launcher runs this file with the launch's
parameters as `--name=value` flags; one of them, `--sdfb_job`, says which
job the launch is:

    --sdfb_job            entry called
    ────────────────────  ──────────────────────────────────────────
    absent, empty,        sdfb_beam.cli.run_pipeline.main
    generation              (the generator: synthetic rows on GPU workers)
    evaluation            sdfb_evaluation.cli.run_evaluation.main
                            (the evaluator: scores a landed run, CPU workers)

The selector is removed and every other argument reaches the entry
unchanged and in order, so a launch without the selector behaves as it did
when `run_pipeline.py` was the entry itself. Both spellings are read:
`--sdfb_job=evaluation` and `--sdfb_job evaluation`.

    exit code   meaning
    ─────────   ──────────────────────────────────────────────────────
    (entry's)   whatever the chosen entry returned
    2           the selector names neither job, or has no value; or
                `evaluation` was asked of an image built from a tree
                without `packages/sdfb-evaluation`. Nothing was started.

The chosen entry is imported after the selector is read, and only that one:
a generation launch never imports the evaluator, an evaluation launch never
imports the generator. Neither package knows the other; this file is the
only place that names both.

Design: docs/DESIGN.md §1 Architecture; §11 Evaluation (sdfb-evaluation)
(ADR 0009, 0041).
"""

from __future__ import annotations

import importlib
import logging
import sys
from collections.abc import Sequence

__all__ = ["EVALUATION", "GENERATION", "SELECTOR", "main", "split_selector"]

SELECTOR = "--sdfb_job"
GENERATION = "generation"
EVALUATION = "evaluation"
EXIT_USAGE = 2

_ENTRIES = {
    GENERATION: "sdfb_beam.cli.run_pipeline",
    EVALUATION: "sdfb_evaluation.cli.run_evaluation",
}
_EVALUATOR_PACKAGE = "sdfb_evaluation"


def split_selector(argv: Sequence[str]) -> tuple[str, list[str]]:
  """(the job, every other argument in order).

  The job is `generation` when the selector is absent or its value is
  empty; given twice, the last one counts.

  The selector is recognised anywhere in `argv` (after a `--` too) and its
  value is stripped from what is passed on. That is harmless: the template
  launcher emits single `--name=value` tokens, so no other flag's value can
  be mistaken for it.

  Raises:
    ValueError: the selector has no value (it is the last argument, or
      another flag follows it).
  """
  job = ""
  rest: list[str] = []
  index = 0
  while index < len(argv):
    token = argv[index]
    name, equals, value = token.partition("=")
    if name != SELECTOR:
      rest.append(token)
      index += 1
      continue
    if equals:
      job = value
      index += 1
      continue
    following = argv[index + 1] if index + 1 < len(argv) else None
    if following is None or following.startswith("-"):
      raise ValueError(f"{SELECTOR} needs a value: {GENERATION} or "
                       f"{EVALUATION}")
    job = following
    index += 2
  return job.strip() or GENERATION, rest


def _usage_error(message: str) -> int:
  sys.stderr.write(f"flex_entry.py: error: {message}\n")
  return EXIT_USAGE


def main(argv: Sequence[str] | None = None) -> int:
  """Run the job the launch selects; returns the exit code (module
  docstring). Without `argv` the process's own arguments are read."""
  as_process = argv is None
  try:
    job, rest = split_selector(sys.argv[1:] if as_process else list(argv))
  except ValueError as exc:
    return _usage_error(str(exc))
  if job not in _ENTRIES:
    return _usage_error(f"{SELECTOR} must be {GENERATION} or {EVALUATION}, "
                        f"got {job!r}")
  try:
    entry = importlib.import_module(_ENTRIES[job])
  except ModuleNotFoundError as exc:
    if exc.name != _EVALUATOR_PACKAGE:
      raise  # a package that is there and cannot import what it needs
    return _usage_error(
        f"{SELECTOR}={EVALUATION}: this image was built without "
        "packages/sdfb-evaluation, so it can only run generation")
  if as_process:
    # What the entry saw while the launcher ran its file directly: its own
    # path, then the launch's arguments. Its parser's usage line and any
    # later read of sys.argv (Beam's default options) stay the same.
    sys.argv = [entry.__file__ or sys.argv[0], *rest]
    if job == EVALUATION:
      # run_evaluation.py's own `__main__` block, which no longer runs.
      logging.getLogger().setLevel(logging.INFO)
  code = entry.main(rest)
  return 0 if code is None else code


if __name__ == "__main__":
  sys.exit(main())
