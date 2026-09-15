"""BigQuery-backed `FreeTextPoolStore` over `synthetic_rag.freetext_pools`.

Read surface for the generation-time reuse path (WS5 §2) plus the build
branch's `write_rows` (2026-07-29): the branch writes its own rows through
a blocking load job so the DAG gate downstream of it can only open once the
rows are readable. `google.cloud.bigquery` is imported lazily so the module
(and its tests, via an injected fake client) work on the laptop.

Mirrors `sdfb_beam.rag.store.BigQueryChunkStore` deliberately — same
lifecycle, same injection point, same parameter-binding discipline.
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

from typing import Any

from sdfb_core.pools import FreeTextPool


def pool_to_row(pool: FreeTextPool) -> dict[str, Any]:
  """`FreeTextPool` → a `freetext_pools` row (also the branch's output)."""
  return {
      "reference_digest": pool.reference_digest,
      "model_uri": pool.model_uri,
      "column": pool.column,
      "target": pool.target,
      "values": list(pool.values),
      "stagnated": pool.stagnated,
      "attempts": pool.attempts,
  }


def row_to_pool(row: Any) -> FreeTextPool:
  """A `freetext_pools` row → `FreeTextPool`.

    Accepts anything mapping-like: dicts in tests, `google.cloud.bigquery`
    `Row` objects in production.
    """
  get = row.get if hasattr(row, "get") else row.__getitem__
  return FreeTextPool(
      reference_digest=get("reference_digest"),
      model_uri=get("model_uri"),
      column=get("column"),
      target=int(get("target")),
      # BigQuery hands back None (not []) for an empty REPEATED column.
      values=tuple(get("values") or ()),
      stagnated=bool(get("stagnated")),
      attempts=int(get("attempts")),
  )


class BigQueryFreeTextPoolStore:
  """`FreeTextPoolStore` implementation over one `freetext_pools` table."""

  def __init__(self, table_fqn: str, *, client: object | None = None) -> None:
    self.table_fqn = table_fqn
    self._client = client  # injectable for tests; lazy real client

  def __getstate__(self) -> dict:
    # The lazy client is a CACHE, not state: google.cloud clients refuse
    # to pickle, and the 2026-07-29 R1 launch died exactly here — the
    # driver's exists() digest check materialized the client, then the
    # BuildFreeTextPoolsDoFn carrying this store failed graph pickling.
    # Dropped on dump, rebuilt on demand by _bq(). (An injected test
    # client is dropped too — re-inject after unpickling if needed.)
    state = self.__dict__.copy()
    state["_client"] = None
    return state

  def _bq(self):
    if self._client is None:
      from google.cloud import bigquery

      self._client = bigquery.Client()
    return self._client

  def _query(self, sql: str, params: dict[str, str]):
    from google.cloud import bigquery

    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter(name, "STRING", value)
        for name, value in params.items()
    ])
    return self._bq().query(sql, job_config=job_config).result()

  def fetch(self, reference_digest: str, model_uri: str) -> list[FreeTextPool]:
    """Every pool for this digest + LLM. One query per worker setup —
        that is the whole point of WS5."""
    # `values` is a BigQuery RESERVED keyword and `column` is close
    # enough to one to be worth quoting too — unquoted, this is a
    # syntax error against the real service. Every identifier is
    # backticked so the schema can never trip this again.
    sql = ("SELECT `reference_digest`, `model_uri`, `column`, `target`, "
           f"`values`, `stagnated`, `attempts` FROM `{self.table_fqn}` "
           "WHERE `reference_digest` = @reference_digest "
           "AND `model_uri` = @model_uri")
    rows = self._query(sql, {
        "reference_digest": reference_digest,
        "model_uri": model_uri
    })
    return [row_to_pool(r) for r in rows]

  def exists(self, reference_digest: str, model_uri: str) -> bool:
    sql = (f"SELECT 1 AS n FROM `{self.table_fqn}` "
           "WHERE `reference_digest` = @reference_digest "
           "AND `model_uri` = @model_uri LIMIT 1")
    rows = list(
        self._query(sql, {
            "reference_digest": reference_digest,
            "model_uri": model_uri
        }))
    return bool(rows)

  def write_rows(self, rows: list[dict[str, Any]]) -> None:
    """Append pool rows via a LOAD job, blocking until it lands.

        A load job — never `insert_rows_json` — because streamed rows sit in
        the streaming buffer where the digest DELETE between seeding arms
        (RUN_PLAYBOOK §6c) would fail for up to ~90 minutes. The schema comes
        from the existing table (deployment_prerequisites step 10 owns it).
        """
    from google.cloud import bigquery

    job_config = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")
    self._bq().load_table_from_json(
        rows, self.table_fqn, job_config=job_config).result()

  def delete(self, reference_digest: str, model_uri: str) -> None:
    """Drop every pool row for this digest + LLM (blocking DML).

        The taint-preflight path (2026-08-07: the 10M warm run replayed
        memorized 2026-08-05 pools): `fetch` has no per-column dedup, so a
        rebuild must clear the stale rows before the branch re-appends.
        """
    sql = (f"DELETE FROM `{self.table_fqn}` "
           "WHERE `reference_digest` = @reference_digest "
           "AND `model_uri` = @model_uri")
    self._query(sql, {
        "reference_digest": reference_digest,
        "model_uri": model_uri
    })


__all__ = ["BigQueryFreeTextPoolStore", "pool_to_row", "row_to_pool"]
