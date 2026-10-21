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
"""The evaluation's Dataflow flex-template entry, behind the dispatcher.

The template launcher runs the image's `FLEX_TEMPLATE_PYTHON_PY_FILE`, a
dispatcher at the repository root (`docker/flex_entry.py`), with the
template's parameters as `--name=value` flags plus Beam's own
(`--runner=DataflowRunner`, `--project`, `--region`, `--temp_location`,
…). When the launch passes `sdfb_job=evaluation` the dispatcher removes
it and calls `main` here with the rest (this package has no image or
template of its own). It is `sdfb-eval run` with two
differences (Ruling R88f):

    sdfb-eval run                      this entry
    ─────────────────────────────────  ─────────────────────────────────
    waits for the pipeline             submits the Dataflow job and
                                       returns: the launcher must exit
    applies --fail_on (exit 1)         never does (the flag is accepted,
                                       so one parameter set serves both)

The driver still writes the RUNNING row before it submits, and a FAILED
row if anything raises up to the submission. After that the job is on
its own: it writes its rows and the FINAL row, and a job that dies
leaves its RUNNING row for the orchestrator's failure callback to close
(Composer appends the FAILED row).

Run with a sink that needs the driver afterwards (`bq_client`,
`local_json`: a local runner), it waits for the pipeline like `run`
does, so no result is ever left unloaded; a FINAL row that reads FAILED
then exits 3, as it does for `run` (Ruling R90g).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence

from sdfb_evaluation.cli import driver
from sdfb_evaluation.cli.driver import Env
from sdfb_evaluation.cli.main import parse_args, show_progress

__all__ = ["main"]


def main(argv: Sequence[str] | None = None, env: Env | None = None) -> int:
  """Submit one evaluation (module docstring); returns 0 once the
  pipeline is submitted, whatever `--fail_on` says — or, when it waited
  (a local sink), 3 if the FINAL row reads FAILED."""
  show_progress()
  flags = list(sys.argv[1:] if argv is None else argv)
  args, extras = parse_args(["run", *flags])
  return driver.run(args, extras, env, wait=args.sink != "bq", gated=False)


if __name__ == "__main__":
  logging.getLogger().setLevel(logging.INFO)
  sys.exit(main())
