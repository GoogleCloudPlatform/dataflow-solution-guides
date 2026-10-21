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
"""A standalone mirror of the generator's reference-sample provenance —
the row digest and the exact query that produced the sample it digests —
and the R/E/H panel rebuilt from the same order (D3).

The generator's reference sample R is the first n rows of the source
under ONE order, `ORDER BY FARM_FINGERPRINT(TO_JSON_STRING(ref))`
(`REFERENCE_ORDER_BY`, shared by `reference_sql` and `panel_sql`). The
panel reads the first 2n rows of that same order from the pinned source:

    rank  1 ………… 1024 ………… n │ n+1 ………… n+1024 ………… 2n
          ├─── E ───┤           │ ├── H_E ───┤
          ├────────── R ────────┤ ├──────────── H ────────────┤
          = the generator's        = the next n rows: the holdout,
            LIMIT n sample           exchangeable with R

E (the first `EXPOSURE_ROWS` = 1024 ranks) is what the B.1 engine embeds
as row docs, i.e. what prompts could have seen. R is re-hashed with the
generator's digest and compared with the digest it recorded
(`Panel.verified`).

Ties. Two rows share a fingerprint only when their `TO_JSON_STRING` is
identical (duplicate rows) or on a 64-bit collision between distinct
rows. `LIMIT` and `ROW_NUMBER` order tied rows arbitrarily, on the
generator's side and here alike. Tied duplicates are interchangeable, so
R's content does not depend on which copy lands at rank n; a collision
straddling rank n could change it, and the digest check — `verified=False`
— is the guard for that and for every other way R can drift (the source
changed before the pin, or could not be pinned at all).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sdfb_evaluation.context.scope import SourcePin, from_item, read_params

__all__ = [
    "EXPOSURE_ROWS",
    "REFERENCE_ORDER_BY",
    "Panel",
    "fetch_panel",
    "panel_sql",
    "reference_digest",
    "reference_sql",
]

# The ONE order both the generator's sample and the panel use.
REFERENCE_ORDER_BY = "FARM_FINGERPRINT(TO_JSON_STRING(ref))"
# `sdfb_core.rag.chunking.MAX_ROW_DOC_ROWS`, pinned by the parity goldens.
EXPOSURE_ROWS = 1024
_RANK = "__sdfb_rk"


def reference_digest(rows: Iterable[Mapping[str, Any]]) -> str:
  """SHA-256 hex digest of canonical-encoded reference rows.

  A VERBATIM port of `sdfb_beam.io.digest.compute_reference_digest`:
  sorting the rows by their own canonical JSON before hashing makes the
  digest independent of the order a BigQuery client happened to return
  them in.
  """
  sorted_rows = sorted(
      rows,
      key=lambda r: json.dumps(r, sort_keys=True, default=str),
  )
  h = hashlib.sha256()
  for row in sorted_rows:
    h.update(json.dumps(row, sort_keys=True, default=str).encode("utf-8"))
  return h.hexdigest()


def reference_sql(table: str, limit: int) -> str:
  """The exact `SELECT` text `load_reference_rows` sends for `(table,
  limit)` with no `extra_filters` — a verbatim mirror of
  `sdfb_beam.io.bq_sources.load_reference_rows`'s query string.

  Note the double space between `` AS ref`` and ``ORDER BY``: the
  original's f-string always emits a space before AND after the `WHERE`
  clause slot, and an empty clause (no `extra_filters`, this mirror's
  only case) leaves both spaces in place.
  """
  where = ""
  return (f"SELECT * FROM `{table}` AS ref {where} "
          f"ORDER BY {REFERENCE_ORDER_BY} LIMIT {int(limit)}")


def _panel_n(n: Any) -> int:
  if isinstance(n, bool) or not isinstance(n, int) or n < 1:
    raise ValueError(f"panel n: expected a positive int, got {n!r}")
  return n


def panel_sql(source_read_table: str | SourcePin, n: int) -> str:
  """The first 2n source rows of the generator's order, each with its
  rank `__sdfb_rk` (1-based).

  `source_read_table` is a `project.dataset.table` (e.g. the pinned
  snapshot clone once built) or a `SourcePin`, read through its
  `read_expr`: the state `FOR SYSTEM_TIME AS OF` the job's create time
  when pinned (planning creates no table), the table itself when not.
  Either way `TO_JSON_STRING(ref)` must see exactly the source's columns,
  which a clone and an as-of read both preserve.

  The inner `ORDER BY … LIMIT 2n` is the generator's own query shape (a
  distributed top-k); the outer `ROW_NUMBER` then ranks only those 2n
  rows. Ranking the whole table with an unpartitioned `ROW_NUMBER() OVER
  (ORDER BY …)` would sort every source row on a single worker.

  Raises:
    ValueError: `n` is not a positive int, or the source is neither a
      strict table name nor a `SourcePin`.
  """
  size = 2 * _panel_n(n)
  source = from_item(source_read_table)
  return (f"SELECT ref.*, ROW_NUMBER() OVER (ORDER BY {REFERENCE_ORDER_BY}) "
          f"AS {_RANK} FROM (SELECT * FROM {source} AS ref "
          f"ORDER BY {REFERENCE_ORDER_BY} LIMIT {size}) AS ref "
          f"ORDER BY {_RANK}")


@dataclass(frozen=True)
class Panel:
  """The generator's reference sample R and its holdout H (D3).

  `r_rows` are ranks 1..n and `h_rows` ranks n+1..2n (fewer when the
  source is smaller), as the BigQuery client returns them — the same
  Python types the generator digested. `e_n`/`he_n` count the exposed
  prefixes E (ranks 1..1024) and H_E (ranks n+1..n+1024). `verified` is
  True only when the recomputed `digest` equals the `expected_digest` the
  generator recorded; `reason` says why not.
  """
  r_rows: list[dict] = field(hash=False)
  h_rows: list[dict] = field(hash=False)
  e_n: int
  he_n: int
  digest: str
  verified: bool
  expected_digest: str | None
  reason: str | None = None


def _ranked(rows: Iterable[Mapping[str, Any]],
            limit: int) -> list[tuple[int, dict]]:
  """`(rank, row without the rank)`, ranks checked to run 1..k (k ≤
  limit) with no gap and no repeat."""
  ranked = []
  for row in rows:
    rank = row.get(_RANK)
    if isinstance(rank, bool) or not isinstance(rank, int):
      raise ValueError(f"panel row without an integer {_RANK}: {rank!r}")
    ranked.append((rank, {k: v for k, v in row.items() if k != _RANK}))
  ranked.sort(key=lambda pair: pair[0])
  ranks = [rank for rank, _ in ranked]
  if ranks != list(range(1, len(ranks) + 1)) or len(ranks) > limit:
    raise ValueError(f"panel ranks are not 1..k with k <= {limit}: "
                     f"{ranks[:5]}…{ranks[-5:]}")
  return ranked


def _verdict(digest: str, expected: str | None) -> str | None:
  if expected is None:
    return ("no reference digest recorded for this run "
            "(validation_runs.reference_digest is empty): R cannot be "
            "checked against the sample the generator used")
  if digest != expected:
    return (f"reference digest mismatch: R hashes to {digest[:12]}, the "
            f"generator recorded {expected[:12]} — the source read here no "
            "longer yields the generator's sample (it changed before the "
            "pin, could not be pinned, or a fingerprint collision straddles "
            "rank n)")
  return None


def fetch_panel(bq: Any,
                *,
                source_read_table: str | SourcePin,
                n: int,
                expected_digest: str | None,
                max_bytes: int | None = None) -> Panel:
  """Read the panel through the BigQuery client and verify R.

  Args:
    bq: a `Bq` (or fake) — the client returns the same Python types the
      generator's `load_reference_rows` got, which the digest depends on.
    source_read_table: see `panel_sql`; a `SourcePin`'s own `params`
      are bound with it.
    n: the generator's reference sample size (`--reference_rows_limit`).
    expected_digest: `validation_runs.reference_digest`, or None.
    max_bytes: caps the bytes billed (the query scans the whole source).

  Raises:
    ValueError: a bad `n`/source, or ranks that do not run 1..k.
  """
  size = _panel_n(n)
  rows = bq.query(
      panel_sql(source_read_table, size),
      read_params(source_read_table),
      max_bytes=max_bytes)
  ranked = _ranked(rows, 2 * size)
  r_rows = [row for rank, row in ranked if rank <= size]
  h_rows = [row for rank, row in ranked if rank > size]
  digest = reference_digest(r_rows)
  reason = _verdict(digest, expected_digest)
  return Panel(
      r_rows=r_rows,
      h_rows=h_rows,
      e_n=min(EXPOSURE_ROWS, len(r_rows)),
      he_n=min(EXPOSURE_ROWS, len(h_rows)),
      digest=digest,
      verified=reason is None,
      expected_digest=expected_digest,
      reason=reason)
