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
