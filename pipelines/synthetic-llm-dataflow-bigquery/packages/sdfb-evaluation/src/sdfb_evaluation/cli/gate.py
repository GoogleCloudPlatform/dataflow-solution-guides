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
"""What a finished `sdfb-eval run` exits with (Rulings R88g, R90g, R93).

    exit   meaning
    ────   ──────────────────────────────────────────────────────────────
    0      the evaluation finished and no gate tripped
    1      the `--fail_on` gate tripped — and nothing else
    2      a usage error: nothing was started (argparse, `cli.main`)
    3      the evaluation failed: its FINAL row reads FAILED (here), or
           the driver raised (`cli.main` maps that, after the FAILED row)

    FINAL status                 --fail_on none   --fail_on warn | fail
    ───────────────────────────  ───────────────  ─────────────────────────
    FAILED                       3                3
    PARTIAL                      0                1: the gate cannot vouch
                                                  for a launch table that
                                                  was not evaluated
    SKIPPED                      0                0: an empty scope is a
                                                  planned outcome
    SUCCEEDED[_WITH_WARNINGS]    0                by the metric counts:
                                                  fail → any FAIL;
                                                  warn → any WARN or FAIL

A FINAL row can read FAILED without anything having raised: when no
launch table could be evaluated, the pipeline writes that row itself and
ends normally. `final_exit_code` therefore reads the row's status first
— 3 is not a gate and `--fail_on none` does not mask it.

The metric counts are the registry's own (`metrics_fail`, `metrics_warn`
of the FINAL row): the headline counts of the measured rows, aggregate
`*_score` rows excluded.

The gate grades nothing itself. A run under `--thresholds_uri` stores
its statuses already graded against the override (`cli.thresholds`), so
the counts read here are the override's too.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "EXIT_FAILED",
    "EXIT_TRIPPED",
    "FAIL_ON",
    "exit_code",
    "final_exit_code",
]

FAIL_ON = ("none", "warn", "fail")
EXIT_TRIPPED = 1
EXIT_FAILED = 3  # 2 is argparse's usage error
_FAILED = "FAILED"
_PARTIAL = "PARTIAL"


def exit_code(fail_on: str, counts: Mapping[str, Any]) -> int:
  """0, or 1 when the gate trips (module docstring).

  Raises:
    ValueError: `fail_on` is not none | warn | fail.
  """
  if fail_on not in FAIL_ON:
    raise ValueError(f"fail_on {fail_on!r}: expected one of {list(FAIL_ON)}")
  failing = int(counts.get("fail") or 0)
  warning = int(counts.get("warn") or 0)
  if fail_on == "fail" and failing:
    return EXIT_TRIPPED
  if fail_on == "warn" and (failing or warning):
    return EXIT_TRIPPED
  return 0


def final_exit_code(final: Mapping[str, Any],
                    fail_on: str,
                    counts: Mapping[str, Any],
                    *,
                    gated: bool = True) -> int:
  """The exit code of a run whose FINAL registry row is `final` (module
  docstring): 3 when it reads FAILED; otherwise the gate's 0 or 1 — a
  PARTIAL run trips an active gate whatever its counts — or 0 when the
  caller applies no gate (`gated=False`, the flex entry).

  Raises:
    ValueError: `fail_on` is not none | warn | fail.
  """
  code = exit_code(fail_on, counts)  # validates fail_on either way
  status = final.get("status")
  if status == _FAILED:
    return EXIT_FAILED
  if not gated:
    return 0
  if status == _PARTIAL and fail_on != "none":
    return EXIT_TRIPPED
  return code
