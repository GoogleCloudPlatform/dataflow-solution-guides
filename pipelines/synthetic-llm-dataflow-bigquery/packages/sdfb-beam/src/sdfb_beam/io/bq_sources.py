"""Driver-side BigQuery sources for reference data.

For M1, reference rows are pulled **eagerly** in the pipeline driver via the
BigQuery Python client, not via `beam.io.ReadFromBigQuery` — three reasons:

  1. `pipeline.build_pipeline()` computes `reference_digest` eagerly, before
     graph construction, so the rows must exist in driver memory anyway.
  2. The reference cardinality is bounded by `--reference_rows` (default
     10k), trivially driver-loadable.
  3. Keeping the BQ read out of the graph means the digest can be the
     `pipeline_run_id` provenance key, written to `validation_runs` before
     workers even spin up.

`beam.io.ReadFromBigQuery` may still be appropriate for a *streaming* mode
later (M2+), but for M1's batch path the driver-side pull is simpler.

REFs:
  - .claude/skills/reference-data.md
  - https://cloud.google.com/python/docs/reference/bigquery/latest
"""

from __future__ import annotations

import logging
from typing import Any

from google.cloud import bigquery

logger = logging.getLogger(__name__)


def load_reference_rows(
    *,
    table: str,
    limit: int = 10_000,
    project: str | None = None,
    client: bigquery.Client | None = None,
    extra_filters: str | None = None,
) -> list[dict[str, Any]]:
  """Pull up to `limit` rows from a BigQuery table as plain dicts.

    Parameters
    ----------
    table : str
        Fully-qualified `project.dataset.table` ID.
    limit : int
        `LIMIT` clause cardinality. Default 10k — matches M1 brief.
    project : str | None
        Billing project for the query. Defaults to ADC project.
    client : bigquery.Client | None
        Injectable for testing with a mock; in production this is created
        from ADC.
    extra_filters : str | None
        Optional `WHERE` predicate appended to the query. Use cautiously
        — the digest is the only provenance for "what rows did we see",
        and filters change the population without changing the predicate
        being applied.

    Returns
    -------
    list[dict]
        Each row's `name` → `value` mapping, with native Python types as
        produced by the BQ client (datetime, Decimal, etc.).
    """
  if client is None:
    client = bigquery.Client(project=project)

  where = f"WHERE {extra_filters}" if extra_filters else ""
  # A bare `LIMIT n` returns a storage-contiguous slice — in the
  # 2026-07-15 E2E run 51 % of the sample came from a single load batch,
  # which skewed column profiling (897-distinct columns classified as
  # ≤9-distinct categoricals) and fidelity. `FARM_FINGERPRINT` of the
  # whole row spreads the sample across the table *deterministically*:
  # the same table contents always yield the same sample, keeping
  # `reference_digest` stable across retriggers (unlike `RAND()`).
  query = (f"SELECT * FROM `{table}` AS ref {where} "
           f"ORDER BY FARM_FINGERPRINT(TO_JSON_STRING(ref)) LIMIT {int(limit)}")

  logger.info("Loading reference rows from %s (limit=%d)", table, limit)
  job = client.query(query)
  rows: list[dict[str, Any]] = [dict(row.items()) for row in job.result()]
  logger.info("Loaded %d reference rows", len(rows))
  return rows
