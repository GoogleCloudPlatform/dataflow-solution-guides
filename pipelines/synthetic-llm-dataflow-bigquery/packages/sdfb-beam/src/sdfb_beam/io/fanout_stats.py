"""SOURCE fan-out statistics for a driven child (design 2026-09-10, ADR 0036).

Per driving edge, measured driver-side on the SOURCE child table: the
histogram of children per parent tuple (zero bucket from the source
parent's distinct tuple count) and the joint PK-completing cells. One
scan of the FK + cell columns; cached in ``synthetic_data_quality
.fk_fanout_stats`` by (source child, edge cols, model sha) so a
re-launch pays nothing. A 5k-row sample cannot measure this: it almost
never holds two rows of one parent (design §10).
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from sdfb_core.contracts.model_adjustment import pk_repeat_share
from sdfb_core.observability import log_milestone

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Rarely-typed control char used as a tuple-key separator in the joint
# COUNT(DISTINCT CONCAT(...)) below — chosen because it cannot appear in a
# CAST(... AS STRING) value from any column BigQuery lets us read here.
# The SQL literal carries the ESCAPE (`'\\x1f'`, four printable chars), not
# a raw 0x1F byte: the query text is logged, copied into the BigQuery
# console and diffed by humans, and an invisible control byte in it
# survives none of that intact. BigQuery parses `\\x1f` in a string literal
# to the same character.
_TUPLE_SEP = "\\x1f"

# Percentile cut points for the fan-out milestone below.
_P50 = 0.5
_P95 = 0.95


def _validated(cols: tuple[str, ...]) -> tuple[str, ...]:
  for c in cols:
    if not _IDENTIFIER.match(c):
      raise ValueError(f"not a BigQuery column name: {c!r}")
  return cols


def _cols(cols: tuple[str, ...]) -> str:
  return ", ".join(f"`{c}`" for c in _validated(cols))


def _distinct_key_sql(cols: tuple[str, ...]) -> str:
  """``COUNT(DISTINCT ...)`` over one or several columns as a tuple key.

    A single column skips the CONCAT entirely; two or more are joined by a
    single-quoted separator literal with no trailing separator, e.g.
    ``COUNT(DISTINCT CONCAT(CAST(`c1` AS STRING), '\\x1f', CAST(`c2` AS
    STRING)))``. Callers pass already-``_validated`` columns — this does
    not re-validate.
    """
  casts = [f"CAST(`{c}` AS STRING)" for c in cols]
  if len(casts) == 1:
    return f"COUNT(DISTINCT {casts[0]})"
  joined = f", '{_TUPLE_SEP}', ".join(casts)
  return f"COUNT(DISTINCT CONCAT({joined}))"


def _rows(job: Any) -> list[dict]:
  return [dict(r) if not isinstance(r, dict) else r for r in job.result()]


def measure_fanout(
    *,
    source_child: str,
    child_cols: tuple[str, ...],
    source_parent: str,
    ref_cols: tuple[str, ...],
    cell_cols: tuple[str, ...],
    client: Any = None,
    edge: str = "",
) -> dict:
  """``{"histogram", "cells", "parents", "children"}`` from three
    GROUP BY queries; ``cells`` is None without cell columns.

    ``edge`` labels the ``fk_fanout_source_orphans`` warning below and is
    otherwise unused.
    """
  if client is None:  # pragma: no cover - GCP-only path
    from google.cloud import bigquery

    client = bigquery.Client()
  child_sql = (
      f"SELECT n AS k, COUNT(*) AS parents FROM (SELECT {_cols(child_cols)}, "
      f"COUNT(*) AS n FROM `{source_child}` GROUP BY {_cols(child_cols)}) GROUP BY n"
  )
  histogram = {
      int(r["k"]): int(r["parents"]) for r in _rows(client.query(child_sql))
  }
  ref = _validated(ref_cols)
  parent_sql = (
      f"SELECT {_distinct_key_sql(ref)} AS parents FROM `{source_parent}` WHERE "
      + " AND ".join(f"`{c}` IS NOT NULL" for c in ref))
  (parent_row,) = _rows(client.query(parent_sql))
  parents = int(parent_row["parents"])
  with_children = sum(histogram.values())
  if with_children > parents:
    # The SOURCE child holds tuples its own parent does not (the
    # source's FK is not enforced, or the two tables were snapshotted
    # at different times). The zero bucket is then unmeasurable — it
    # is clamped to 0, not negative — and `children / parents` is an
    # UPPER bound on the true mean, which sizes the whole driven
    # child. Announced, never inferred from a suspicious ratio.
    log_milestone(
        "fk_fanout_source_orphans",
        level=logging.WARNING,
        edge=edge,
        child_tuples=with_children,
        parent_tuples=parents,
        orphan_keys=with_children - parents,
        matched_share=round(parents / max(1, with_children), 4),
        note=("child tuples without a parent in the source; the zero "
              "bucket is unmeasurable, and this child generates from "
              "the matched share of its key space only"),
    )
  # The child GROUP BY above can never emit k=0 (COUNT(*) over an actual
  # group is always >= 1), so histogram.get(0, 0) is always 0 here — this
  # line's only job is filling the zero bucket from the parent count.
  histogram[0] = max(0, parents - with_children) + histogram.get(0, 0)
  children = sum(k * n for k, n in histogram.items())
  cells = None
  if cell_cols:
    cell_sql = (
        f"SELECT {_cols(cell_cols)}, COUNT(*) AS n FROM `{source_child}` "
        f"GROUP BY {_cols(cell_cols)}")
    rows = _rows(client.query(cell_sql))
    cells = {
        "cols": list(cell_cols),
        "rows": [[r[c] for c in cell_cols] for r in rows],
        "counts": [float(r["n"]) for r in rows],
    }
  return {
      "histogram": {
          str(k): n for k, n in sorted(histogram.items())
      },
      "cells": cells,
      "parents": parents,
      "children": children,
  }


def measure_pk_uniqueness(
    *,
    source_child: str,
    pk_cols: tuple[str, ...],
    client: Any = None,
) -> dict:
  """``{"cols", "rows", "key_tuples", "max_rows_per_key"}`` — the
    DECLARED PK, measured on the SOURCE child (ADR 0038 fix J).

    The fan-out histogram describes the DRIVING EDGE. A PK with a member
    outside that edge is a different column set, so nothing in the
    histogram says whether it is a key of the data. This does, in one
    GROUP BY over the key tuple:

    ``SELECT COUNT(*) AS key_tuples, SUM(n) AS row_count, MAX(n) AS
    max_rows_per_key FROM (SELECT <pk>, COUNT(*) AS n FROM <child> GROUP
    BY <pk>)``

    — the same inner shape ``measure_fanout`` groups the child by, so a
    COMPOSITE key counts as one tuple and NULL handling matches: BigQuery
    groups NULLs together, i.e. a NULL member is a VALUE of the key.
    ``_distinct_key_sql``'s ``COUNT(DISTINCT CONCAT(...))`` is
    deliberately NOT used here — CONCAT returns NULL if any member is
    NULL, so every NULL-bearing tuple would leave the key count while
    staying in the row count, understating the repeat share on exactly
    the sparse columns that motivate this check.

    One extra scan of the PK columns per driven child, cached beside the
    fan-out payload. Skipped entirely when the declared PK IS the driving
    edge — `pk_measurement_from_histogram` reads the same three numbers
    off the histogram already in hand.
    """
  if client is None:  # pragma: no cover - GCP-only path
    from google.cloud import bigquery

    client = bigquery.Client()
  cols = _cols(_validated(pk_cols))
  sql = (f"SELECT COUNT(*) AS key_tuples, SUM(n) AS row_count, "
         f"MAX(n) AS max_rows_per_key FROM (SELECT {cols}, COUNT(*) AS n "
         f"FROM `{source_child}` GROUP BY {cols})")
  (row,) = _rows(client.query(sql))
  return {
      "cols": list(pk_cols),
      "rows": int(row["row_count"] or 0),
      "key_tuples": int(row["key_tuples"] or 0),
      "max_rows_per_key": int(row["max_rows_per_key"] or 0),
  }


def log_pk_measured(table: str, measurement: dict, *, source: str) -> None:
  """One ``source_pk_measured`` milestone — the evidence the P4 verdict
    rests on (ADR 0038 fix J). ``source`` is ``measured``, ``cache`` or
    ``histogram`` (the declared PK IS the driving edge, so the fan-out
    already measured it and no second scan was paid for)."""
  log_milestone(
      "source_pk_measured",
      table=table,
      pk=",".join(measurement.get("cols") or ()),
      rows=measurement.get("rows", 0),
      key_tuples=measurement.get("key_tuples", 0),
      max_rows_per_key=measurement.get("max_rows_per_key", 0),
      repeat_share=round(pk_repeat_share(measurement) or 0.0, 4),
      source=source,
  )


def fanout_payload(measured: dict, driving_cols: tuple[str, ...],
                   exact_cells: bool) -> dict:
  """The ``FanoutPlan`` payload shape (Task 1), plus the DECLARED PK's
    own source measurement (``pk_source``, ADR 0038 fix J) and the source
    PARENT's distinct tuple count (``parents``, ADR 0039).

    ``FanoutPlan.from_payload`` ignores both extra keys — they are
    preflight's evidence, not the engine's recipe — and they ride here so
    they are cached and carried by exactly the same plumbing as the
    histogram. ``parents`` is what makes the launch's row projection able
    to restate `fk_fanout_source_orphans`'s ``matched_share`` with no
    second BigQuery call: an orphan-heavy source clamps the histogram's
    zero bucket to 0, so the parent count cannot be read back off the
    histogram alone. A payload cached before ADR 0039 has no such key and
    the projection then claims no matched share at all.
    """
  return {
      "driving_cols": list(driving_cols),
      "histogram": dict(measured["histogram"]),
      "cells": measured.get("cells"),
      "exact_cells": bool(exact_cells),
      "pk_source": measured.get("pk"),
      "parents": measured.get("parents"),
  }


def log_fanout_measured(edge: str, measured: dict, *, source: str) -> None:
  """One ``fk_fanout_measured`` milestone.

    Every figure is over the histogram, which counts rows per CHILD KEY
    VALUE: p50, p95, max and the mean all describe the same distribution,
    so the mean can never exceed the max. It used to divide the total
    child rows by the PARENT key count, which on an orphan-heavy source
    reported 22.3 beside a max of 13 (launch 2026-09-12_14_50_30) — the
    quantity an operator needs there is ``key_values`` and
    ``orphan_keys``, logged beside it, which say how much of the child's
    key space the parent actually covers."""
  hist = {int(k): n for k, n in measured["histogram"].items()}
  total = sum(hist.values()) or 1
  order = sorted(hist)
  cum = 0
  p50 = p95 = order[-1]
  for k in order:
    cum += hist[k]
    if cum / total >= _P50 and p50 == order[-1]:
      p50 = k
    if cum / total >= _P95:
      p95 = k
      break
  key_values = sum(n for k, n in hist.items() if k > 0)
  log_milestone(
      "fk_fanout_measured",
      edge=edge,
      parents=measured["parents"],
      children=measured["children"],
      key_values=key_values,
      orphan_keys=max(0, key_values - int(measured["parents"])),
      mean=round(measured["children"] / total, 4),
      p50=p50,
      p95=p95,
      max=order[-1],
      zero_share=round(hist.get(0, 0) / total, 4),
      source=source,
  )


class BigQueryFanoutStatsStore:
  """Cache of measured fan-out payloads in ``fk_fanout_stats``, keyed by
    (source child table, edge cols, model sha). Mirrors
    ``BigQuerySourceStatsStore`` — lazy client, pickle-safe."""

  def __init__(self, table_fqn: str, *, client: Any = None) -> None:
    self.table_fqn = table_fqn
    self._client = client  # injectable for tests; lazy real client

  def __getstate__(self) -> dict:
    # The lazy client is a CACHE, not state — google.cloud clients
    # refuse to pickle (see io/stats_store.py, io/source_values.py).
    state = self.__dict__.copy()
    state["_client"] = None
    return state

  def _bq(self):
    if self._client is None:  # pragma: no cover - GCP-only path
      from google.cloud import bigquery

      self._client = bigquery.Client()
    return self._client

  def get(self, source_table: str, edge_cols: tuple[str, ...],
          model_sha: str) -> dict | None:
    """Most recent cached payload for this (table, edge, model), or
        None on a cache miss."""
    from google.cloud import bigquery

    sql = (
        f"SELECT payload FROM `{self.table_fqn}` WHERE source_table = @t "
        f"AND edge_cols = @e AND model_sha = @s ORDER BY measured_at DESC LIMIT 1"
    )
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("t", "STRING", source_table),
        bigquery.ScalarQueryParameter("e", "STRING", ",".join(edge_cols)),
        bigquery.ScalarQueryParameter("s", "STRING", model_sha),
    ])
    rows = _rows(self._bq().query(sql, job_config=job_config))
    return json.loads(rows[0]["payload"]) if rows else None

  def put(self, source_table: str, edge_cols: tuple[str, ...], model_sha: str,
          payload: dict) -> None:
    """Append one measured payload via a LOAD job (never streaming —
        the CLAUDE.md batch rule)."""
    from google.cloud import bigquery

    row = {
        "source_table": source_table,
        "edge_cols": ",".join(edge_cols),
        "model_sha": model_sha,
        "measured_at": datetime.now(UTC).isoformat(),
        "payload": json.dumps(payload, separators=(",", ":"), default=str),
    }
    job_config = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")
    self._bq().load_table_from_json([row],
                                    self.table_fqn,
                                    job_config=job_config).result()


__all__ = [
    "BigQueryFanoutStatsStore",
    "fanout_payload",
    "log_fanout_measured",
    "log_pk_measured",
    "measure_fanout",
    "measure_pk_uniqueness",
]
