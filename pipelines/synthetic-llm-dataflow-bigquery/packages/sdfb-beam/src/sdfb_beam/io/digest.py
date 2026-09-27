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
"""Canonical reference-row digest — provenance hash.

The digest is computed eagerly (outside the Beam graph) on the
materialized reference sample. It's persisted to
`synthetic_data_quality.validation_runs` so a job can be re-traced to
the exact rows it saw, even though the source query is non-deterministic
(`SELECT … LIMIT N`).

REF: .claude/skills/reference-data.md
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable


def compute_reference_digest(rows: Iterable[dict]) -> str:
  """SHA-256 of canonical-encoded reference rows.

    Sorting + per-row hashing makes the operation associative — so the
    same function can serve as a CombineFn `extract_output` if the
    reference set ever outgrows worker memory.
    """
  sorted_rows = sorted(
      rows,
      key=lambda r: json.dumps(r, sort_keys=True, default=str),
  )
  h = hashlib.sha256()
  for row in sorted_rows:
    h.update(json.dumps(row, sort_keys=True, default=str).encode("utf-8"))
  return h.hexdigest()
