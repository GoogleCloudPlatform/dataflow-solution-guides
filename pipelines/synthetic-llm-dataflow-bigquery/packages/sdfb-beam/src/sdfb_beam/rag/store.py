"""BigQuery-backed `ChunkStore` over `synthetic_rag.rag_chunks`.

Read-only surface for the generation-time reuse path (WS2 §4b.1). The
population stage writes through `WriteToBigQuery`, never through this
class. `google.cloud.bigquery` is imported lazily so the module (and its
tests, via an injected fake client) work on the laptop.
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sdfb_core.rag.chunking import Chunk

if TYPE_CHECKING:  # pragma: no cover - typing only
  pass


class BigQueryChunkStore:
  """`ChunkStore` implementation over one `rag_chunks` table."""

  def __init__(self, table_fqn: str, *, client: object | None = None) -> None:
    self.table_fqn = table_fqn
    self._client = client  # injectable for tests; lazy real client

  def _bq(self):
    if self._client is None:
      from google.cloud import bigquery

      self._client = bigquery.Client()
    return self._client

  def _query(self, sql: str, params: dict[str, str]):
    from google.cloud import bigquery  # local import; fake clients skip it

    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter(name, "STRING", value)
        for name, value in params.items()
    ])
    return self._bq().query(sql, job_config=job_config).result()

  def exists(self, reference_digest: str, embedder_id: str,
             embedder_version: str) -> bool:
    sql = (f"SELECT 1 AS n FROM `{self.table_fqn}` "
           "WHERE reference_digest = @reference_digest "
           "AND embedder_id = @embedder_id "
           "AND embedder_version = @embedder_version LIMIT 1")
    rows = list(
        self._query(
            sql,
            {
                "reference_digest": reference_digest,
                "embedder_id": embedder_id,
                "embedder_version": embedder_version,
            },
        ))
    return len(rows) > 0

  def fetch(
      self,
      reference_digest: str,
      chunk_kind: str,
      embedder_id: str,
      embedder_version: str,
  ) -> list[Chunk]:
    sql = ("SELECT chunk_id, source_fqn, source_pk, row_digest, "
           "reference_digest, chunk_index, chunk_kind, chunk_text, "
           "embedder_id, embedder_version, embedding, metadata "
           f"FROM `{self.table_fqn}` "
           "WHERE reference_digest = @reference_digest "
           "AND chunk_kind = @chunk_kind "
           "AND embedder_id = @embedder_id "
           "AND embedder_version = @embedder_version")
    rows = list(
        self._query(
            sql,
            {
                "reference_digest": reference_digest,
                "chunk_kind": chunk_kind,
                "embedder_id": embedder_id,
                "embedder_version": embedder_version,
            },
        ))
    return [self._to_chunk(r) for r in rows]

  @staticmethod
  def _to_chunk(row) -> Chunk:
    get = row.get if isinstance(row,
                                dict) else lambda k, d=None: getattr(row, k, d)
    return Chunk(
        chunk_id=get("chunk_id"),
        source_fqn=get("source_fqn"),
        row_digest=get("row_digest"),
        reference_digest=get("reference_digest"),
        chunk_index=int(get("chunk_index")),
        chunk_kind=get("chunk_kind"),
        chunk_text=get("chunk_text"),
        embedder_id=get("embedder_id"),
        embedder_version=get("embedder_version"),
        source_pk=_parse_json(get("source_pk")),
        embedding=list(get("embedding")) if get("embedding") else None,
        metadata=_parse_json(get("metadata")) or {},
    )


def _parse_json(value) -> dict | None:
  if value in (None, ""):
    return None
  if isinstance(value, dict):
    return value
  return json.loads(value)


__all__ = ["BigQueryChunkStore"]
