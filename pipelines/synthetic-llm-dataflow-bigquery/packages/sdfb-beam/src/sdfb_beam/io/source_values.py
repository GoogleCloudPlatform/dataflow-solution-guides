"""BigQuery-backed `SourceValueStore` + persisted-pool taint preflight.

Implements the seam `sdfb_core.pools.store.SourceValueStore` declares: the
column's FULL distinct values, so the free-text pool ladder rejects
candidates against the whole source domain instead of the profiled sample
(2026-08-05 B_TABLE R1: 33-99% verbatim source values on 10 columns from
exactly that gap). `pool_source_overlap` is the launcher-side complement:
it measures pools already persisted in `freetext_pools` against the live
source so a tainted warm store is rebuilt, never replayed (the 2026-08-07
10M warm run replayed the memorized 2026-08-05 pools wholesale).

Mirrors `sdfb_beam.pools.store.BigQueryFreeTextPoolStore` deliberately —
lazy client, pickle-safe, injectable fake for laptop tests.
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import logging
import re
import threading
from typing import TYPE_CHECKING

from sdfb_core.observability import log_milestone

if TYPE_CHECKING:  # pragma: no cover - typing only
  from collections.abc import Iterable, Mapping

  from sdfb_core.pools import FreeTextPool

# BigQuery column names: letters, digits, underscores — anything else in a
# fetch request is a bug (or an injection attempt), never a real column.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Above this many distinct values the filter set stops being a cheap
# worker-side frozenset; the store reports "unavailable" and the engine
# degrades loudly to sample-only rejection. 1M strings ~= tens of MB on an
# n1-highmem-8 — far below worker RAM, far above every observed E2E column
# (max seen: 146k distinct, B_TABLE COL_038).
_DEFAULT_CAP = 1_000_000

# Process-level fetch cache, keyed (table_fqn, column, cap). B.2 builds its
# pools lazily inside Generate DoFns (no pool branch), so up to 16 sibling
# hook instances per worker process would otherwise each issue the same
# SELECT DISTINCT. The lock is held ACROSS the fetch (single-flight): a
# duplicate multi-second column scan costs more than serializing the few
# cold fetches a process ever makes.
_FETCH_CACHE: dict[tuple[str, str, int], frozenset[str] | None] = {}
_FETCH_CACHE_LOCK = threading.Lock()

# ADR 0034 D4 (amended 2026-09-07): the Storage Read API attempt is a
# per-PROCESS decision. The R7 pair's worker SA had no
# `bigquery.readsessions.create` permission and every one of 107 + 459
# fetches paid a doomed `to_arrow()` before the REST path; once a
# PermissionDenied / Forbidden is seen, the process goes straight to REST.
_STORAGE_API_STATE = {"disabled": False}
_STORAGE_DENIED_ERRORS = frozenset(
    {"PermissionDenied", "Forbidden", "Unauthorized"})


def clear_source_value_cache() -> None:
  """Drop all cached fetches and re-arm the Storage API (tests only)."""
  with _FETCH_CACHE_LOCK:
    _FETCH_CACHE.clear()
    _STORAGE_API_STATE["disabled"] = False


class BigQuerySourceValueStore:
  """`SourceValueStore` over one reference table."""

  def __init__(
      self,
      table_fqn: str,
      *,
      cap: int = _DEFAULT_CAP,
      client: object | None = None,
  ) -> None:
    self.table_fqn = table_fqn
    self.cap = cap
    self._client = client  # injectable for tests; lazy real client

  def __getstate__(self) -> dict:
    # Same rule as BigQueryFreeTextPoolStore: the client is a cache,
    # not state — google.cloud clients refuse to pickle.
    state = self.__dict__.copy()
    state["_client"] = None
    return state

  def _bq(self):
    if self._client is None:
      from google.cloud import bigquery

      self._client = bigquery.Client()
    return self._client

  def _checked(self, column: str) -> str:
    if not _IDENTIFIER.match(column):
      raise ValueError(f"not a BigQuery column name: {column!r}")
    return column

  def fetch_distinct(self, column: str) -> frozenset[str] | None:
    """Every distinct non-NULL value of `column` as strings, or None
        when the column holds more than `cap` distinct values.

        Process-cached per (table, column, cap) — see `_FETCH_CACHE`.
        """
    col = self._checked(column)
    key = (self.table_fqn, col, self.cap)
    with _FETCH_CACHE_LOCK:
      if key in _FETCH_CACHE:
        return _FETCH_CACHE[key]
      # LIMIT cap+1: one extra row is the over-cap signal — cheaper
      # than a COUNT(DISTINCT) pre-query and exact where it matters.
      sql = (f"SELECT DISTINCT CAST(`{col}` AS STRING) AS v "
             f"FROM `{self.table_fqn}` WHERE `{col}` IS NOT NULL "
             f"LIMIT {self.cap + 1}")
      values = _column_values(self._bq().query(sql), "v")
      result = None if len(values) > self.cap else frozenset(values)
      _FETCH_CACHE[key] = result
      return result

  def fetch_frequent(self, column: str,
                     min_count: int) -> frozenset[str] | None:
    """Values shared by at least `min_count` source rows, as strings —
        the k-anonymous enum mass the numeric scrub keeps exact (wave-4
        v2). Same cap/None semantics and process cache as
        `fetch_distinct`; the frequent set is a subset of the distinct set
        so the cap is far harder to hit here."""
    col = self._checked(column)
    key = (self.table_fqn, f"{col}#freq>={int(min_count)}", self.cap)
    with _FETCH_CACHE_LOCK:
      if key in _FETCH_CACHE:
        return _FETCH_CACHE[key]
      sql = (f"SELECT CAST(`{col}` AS STRING) AS v "
             f"FROM `{self.table_fqn}` WHERE `{col}` IS NOT NULL "
             f"GROUP BY v HAVING COUNT(*) >= {int(min_count)} "
             f"LIMIT {self.cap + 1}")
      values = _column_values(self._bq().query(sql), "v")
      result = None if len(values) > self.cap else frozenset(values)
      _FETCH_CACHE[key] = result
      return result

  def count_overlap(self, column: str, values: Iterable[str]) -> int:
    """How many of `values` exist in the column's live source domain."""
    col = self._checked(column)
    from google.cloud import bigquery

    sql = ("SELECT COUNT(DISTINCT v) AS n FROM UNNEST(@vals) AS v "
           f"WHERE v IN (SELECT CAST(`{col}` AS STRING) "
           f"FROM `{self.table_fqn}` WHERE `{col}` IS NOT NULL)")
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("vals", "STRING", list(values))
    ])
    rows = list(self._bq().query(sql, job_config=job_config).result())
    return int(_row_value(rows[0], "n")) if rows else 0


def pool_source_overlap(
    pool_store,
    value_store,
    reference_digest: str,
    model_uri: str,
) -> dict[str, int]:
  """Per-column count of persisted pool values that exist in the live
    source — nonzero means the warm store is tainted and must be rebuilt,
    not replayed. Columns with zero overlap are omitted."""
  pools: list[FreeTextPool] = pool_store.fetch(reference_digest, model_uri)
  overlap: dict[str, int] = {}
  for pool in pools:
    if not pool.values:
      continue
    n = value_store.count_overlap(pool.column, list(pool.values))
    if n:
      overlap[pool.column] = n
  return overlap


def _row_value(row: Mapping | object, key: str):
  get = row.get if hasattr(
      row, "get") else row.__getitem__  # type: ignore[union-attr]
  return get(key)


def _column_values(query_job, column: str) -> list[str]:
  """One query's ``column`` as a list of non-NULL strings.

    Prefers ``RowIterator.to_arrow()`` (ADR 0034): the BigQuery client
    downloads large results through the Storage Read API when the
    ``google-cloud-bigquery-storage`` client is installed (it is, on the
    worker image) and serves small ones from the cached first page. The
    plain ``RowIterator.__iter__`` path is tabledata.list — the 2026-08-29
    R6 cold run paged a 944,582-value identifier domain through it at
    ~2.9k rows/s (1,078 s inside DoFn.setup()).

    Two rules from the 2026-09-07 R7 pair, where every fetch failed:

    * the REST fallback takes a FRESH ``query_job.result()`` — a
      ``RowIterator`` is one-shot ("Iterator has already started"), and
      the failed Storage attempt had already consumed the first one;
    * a permission failure (the worker SA lacks
      ``bigquery.readsessions.create``) disables the Storage attempt for
      the rest of the process — loudly, once — instead of costing a
      doomed round trip on every column.
    """
  if not _STORAGE_API_STATE["disabled"]:
    result = query_job.result()
    to_arrow = getattr(result, "to_arrow", None)
    if to_arrow is not None:
      try:
        table = to_arrow()
        return [v for v in table.column(column).to_pylist() if v is not None]
      except Exception as exc:  # pylint: disable=broad-exception-caught
        log_milestone(
            "source_values_arrow_fallback",
            level=logging.WARNING,
            error=type(exc).__name__,
        )
        if type(exc).__name__ in _STORAGE_DENIED_ERRORS:
          _STORAGE_API_STATE["disabled"] = True
          log_milestone(
              "source_values_storage_api_disabled",
              level=logging.WARNING,
              error=type(exc).__name__,
              note="grant roles/bigquery.readSessionUser to the "
              "worker SA for Storage Read API domain fetches; "
              "REST paging serves this process",
          )
  rows = query_job.result()
  return [v for v in (_row_value(r, column) for r in rows) if v is not None]


__all__ = [
    "BigQuerySourceValueStore",
    "clear_source_value_cache",
    "pool_source_overlap",
]
