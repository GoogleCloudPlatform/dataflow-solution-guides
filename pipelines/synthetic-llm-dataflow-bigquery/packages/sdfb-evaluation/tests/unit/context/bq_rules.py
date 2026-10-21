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
"""BigQuery statement rules the context fakes enforce, so a statement
BigQuery would reject fails the laptop suite too.

Time travel (query syntax, FOR SYSTEM_TIME AS OF): "A single query
statement can't reference a single table at more than one point in time,
including the current time." Every table reference in a statement is one
reference — at the `[TIMESTAMP] '…'` literal of a directly following
`FOR SYSTEM_TIME AS OF` (case- and whitespace-insensitive, an optional
`[AS] alias` allowed in between), else at the current time. A reference
is `` `project.dataset.table` ``: fully or partially backtick-quoted, or
bare, and `project:dataset.table` normalises to `project.dataset.table`
(the same table read under either spelling is the same reference).

This must fail CLOSED: a `FOR SYSTEM_TIME AS OF` this module cannot parse
(a query parameter, an expression such as `TIMESTAMP_SUB(...)`, anything
that is not a literal) is never silently read as the current time — that
would hide a real violation. `points_in_time` raises `AssertionError` on
one instead.
"""

from __future__ import annotations

import re

from sdfb_evaluation.context.bq import BqApiError

_PROJECT = r"[a-z][a-z0-9-]{4,28}[a-z0-9]"
_DATASET = r"[A-Za-z0-9_]+"
_TABLE = r"[A-Za-z0-9_$-]+"
# Each segment's backticks are matched independently, so a fully
# backtick-quoted FQN, a partially quoted one (`` `project.dataset`.table
# ``, common when a project id holds hyphens) and a bare one all match the
# same way; `[.:]` between project and dataset accepts either spelling.
_FQN = (rf"`?(?P<project>{_PROJECT})`?[.:]`?(?P<dataset>{_DATASET})`?"
        rf"\.`?(?P<table>{_TABLE})`?")
# An optional alias, `AS x` or bare `x`, but never the `FOR` that starts
# the AS OF clause this same reference may carry next.
_ALIAS = r"(?:\s+(?:AS\s+)?(?!FOR\b)[A-Za-z_][A-Za-z0-9_]*)?"
# The TIMESTAMP keyword is optional (a bare quoted literal still parses);
# anything else after AS OF (a parameter, an expression) does not match,
# so this whole optional group contributes nothing — caught below.
_AS_OF = (r"(?:\s+FOR\s+SYSTEM_TIME\s+AS\s+OF\s+(?:TIMESTAMP\s+)?"
          r"'(?P<as_of>[^']*)')?")
_REFERENCE_RE = re.compile(rf"{_FQN}{_ALIAS}{_AS_OF}", re.IGNORECASE)
# Every FOR SYSTEM_TIME AS OF in the statement, parsed or not — checked
# against how many `_REFERENCE_RE` actually parsed (fail-closed).
_ANY_AS_OF_RE = re.compile(r"\bFOR\s+SYSTEM_TIME\s+AS\s+OF\b", re.IGNORECASE)


def points_in_time(sql: str) -> dict[str, set[str]]:
  """Each table the statement references → the points it reads it at
  (module docstring).

  Raises:
    AssertionError: the statement holds a `FOR SYSTEM_TIME AS OF` this
      cannot parse — it is never assumed to read at the current time.
  """
  seen: dict[str, set[str]] = {}
  parsed = 0
  for match in _REFERENCE_RE.finditer(sql):
    # Hoisted: a quote nested in an f-string trips the py3.14 W1405 gate.
    project, dataset, name = match["project"], match["dataset"], match["table"]
    table = f"{project}.{dataset}.{name}"
    seen.setdefault(table, set()).add(match["as_of"] or "current")
    if match["as_of"] is not None:
      parsed += 1
  found = len(_ANY_AS_OF_RE.findall(sql))
  if found != parsed:
    raise AssertionError(
        f"{found - parsed} of {found} FOR SYSTEM_TIME AS OF clause(s) in "
        "this statement could not be parsed (expected `[TIMESTAMP] '…'` "
        f"right after AS OF) — never assumed to read at the current time: "
        f"{sql!r}")
  return seen


def check_one_point_in_time_per_table(sql: str) -> None:
  """Raise what BigQuery raises (a 400, here `BqApiError`) when one
  statement reads the same table at two points in time."""
  clash = {t: sorted(p) for t, p in points_in_time(sql).items() if len(p) > 1}
  if clash:
    raise BqApiError(
        "400 (fake) A single query statement can't reference a single table "
        f"at more than one point in time, including the current time: {clash}")
