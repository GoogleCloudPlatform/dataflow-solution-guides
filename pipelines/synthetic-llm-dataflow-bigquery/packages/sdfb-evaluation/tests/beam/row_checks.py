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
"""The persisted-row validator the Beam and CLI tests share: every row of
an evaluation table has exactly the schema's fields, each of its type
(a finite FLOAT64, an RFC 3339 TIMESTAMP, JSON that holds no NaN).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from sdfb_evaluation import schemas

_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)")


def _check_scalar(field: Mapping[str, Any], value: Any, name: str) -> None:
  kind = field["type"]
  if kind == "RECORD":  # NDJSON lines are key-sorted: compare the names
    assert isinstance(value, dict), name
    assert set(value) == {f["name"] for f in field["fields"]}, name
    for sub in field["fields"]:
      _check_value(sub, value[sub["name"]], f"{name}.")
  elif kind == "STRING":
    assert isinstance(value, str), name
  elif kind == "INT64":
    assert isinstance(value, int) and not isinstance(value, bool), name
  elif kind == "FLOAT64":
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    assert math.isfinite(value), name
  elif kind == "BOOL":
    assert isinstance(value, bool), name
  elif kind == "TIMESTAMP":
    assert isinstance(value, str) and _TIMESTAMP_RE.fullmatch(value), name
  elif kind == "JSON":
    json.dumps(value, allow_nan=False)
  else:
    raise AssertionError(f"{name}: unchecked type {kind}")


def _check_value(field: Mapping[str, Any], value: Any, where: str) -> None:
  name = where + field["name"]
  if field.get("mode") == "REPEATED":
    assert isinstance(value, list), name
    for item in value:
      _check_scalar(field, item, name)
    return
  if value is None:
    assert field.get("mode") != "REQUIRED", f"{name} is REQUIRED"
    return
  _check_scalar(field, value, name)


def check_rows(table: str, rows: Sequence[Mapping[str, Any]]) -> None:
  fields = schemas.load_schema(table)
  names = {f["name"] for f in fields}
  for row in rows:
    assert set(row) == names, (table, sorted(set(row) ^ names))
    for field in fields:
      _check_value(field, row[field["name"]], f"{table}.")
