"""BigQuery store for `source_table_stats` rows (2026-08-05 spec, WS-B).

One row per (table_fqn, reference_digest, column), computed driver-side by
``sdfb_core.stats.profile_source_table`` from the eager reference read the
run already pays for. Append-only; a digest that already has rows is
skipped (the pool-store `exists()` idiom).

Copies `BigQueryFreeTextPoolStore`'s structure verbatim — including the
0dfb1a3 pickle fix: the lazy client is a cache, never pickled state.
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

from typing import Any


class BigQuerySourceStatsStore:
  """Writer/existence-check over one `source_table_stats` table."""

  def __init__(self, table_fqn: str, *, client: object | None = None) -> None:
    self.table_fqn = table_fqn
    self._client = client  # injectable for tests; lazy real client

  def __getstate__(self) -> dict:
    # The lazy client is a CACHE, not state (see pools/store.py — the
    # 2026-07-29 R1 launch died on exactly this pickle).
    state = self.__dict__.copy()
    state["_client"] = None
    return state

  def _bq(self):
    if self._client is None:  # pragma: no cover - GCP-only path
      from google.cloud import bigquery

      self._client = bigquery.Client()
    return self._client

  def exists(
      self,
      table_fqn: str,
      reference_digest: str,
      *,
      profiler_version: str | None = None,
      stats_tier: str | None = None,
  ) -> bool:
    """True when stats rows already cover this (table, digest) — and,
        when given, this profiler version / tier. The digest hashes reference
        ROWS only, so callers must pass ``profiler_version`` or upgraded
        profilers will skip forever; ``stats_tier`` keeps a Tier-1 row from
        blocking the later exact pass (ADR 0022)."""
    from google.cloud import bigquery

    sql = (f"SELECT 1 AS n FROM `{self.table_fqn}` "
           "WHERE `table_fqn` = @table_fqn "
           "AND `reference_digest` = @reference_digest")
    params = [
        bigquery.ScalarQueryParameter("table_fqn", "STRING", table_fqn),
        bigquery.ScalarQueryParameter("reference_digest", "STRING",
                                      reference_digest),
    ]
    if profiler_version is not None:
      sql += " AND `profiler_version` = @profiler_version"
      params.append(
          bigquery.ScalarQueryParameter("profiler_version", "STRING",
                                        profiler_version))
    if stats_tier is not None:
      sql += " AND `stats_tier` = @stats_tier"
      params.append(
          bigquery.ScalarQueryParameter("stats_tier", "STRING", stats_tier))
    sql += " LIMIT 1"
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    return bool(list(self._bq().query(sql, job_config=job_config).result()))

  def write_rows(self, rows: list[dict[str, Any]]) -> None:
    """Append stats rows via a LOAD job, blocking until it lands
        (never streaming inserts — the CLAUDE.md batch rule)."""
    from google.cloud import bigquery

    job_config = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")
    self._bq().load_table_from_json(
        rows, self.table_fqn, job_config=job_config).result()


__all__ = ["BigQuerySourceStatsStore"]
