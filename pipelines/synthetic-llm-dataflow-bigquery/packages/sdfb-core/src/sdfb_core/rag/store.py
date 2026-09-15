"""The `ChunkStore` seam (WS2 §4a).

Analogous to `Embedder` and `ModelClient`: engines and the population
stage depend on this Protocol; the BigQuery-backed implementation lives
in `sdfb_beam.rag.store` so `sdfb-core` stays GCP-free. Tests (and
laptop DirectRunner runs) inject `InMemoryChunkStore`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
  from collections.abc import Iterable

  from sdfb_core.rag.chunking import Chunk


@runtime_checkable
class ChunkStore(Protocol):
  """Read surface over `synthetic_rag.rag_chunks` for one vector space."""

  def fetch(
      self,
      reference_digest: str,
      chunk_kind: str,
      embedder_id: str,
      embedder_version: str,
  ) -> list[Chunk]:
    """All chunks for the digest+kind in one pinned vector space."""

  def exists(self, reference_digest: str, embedder_id: str,
             embedder_version: str) -> bool:
    """True when ANY chunk exists for the digest in this vector space
        (the population stage's idempotency check)."""


class InMemoryChunkStore:
  """List-backed `ChunkStore` for tests and laptop runs."""

  def __init__(self, chunks: Iterable[Chunk] = ()) -> None:
    self._chunks: list[Chunk] = list(chunks)

  def add(self, chunks: Iterable[Chunk]) -> None:
    self._chunks.extend(chunks)

  def fetch(
      self,
      reference_digest: str,
      chunk_kind: str,
      embedder_id: str,
      embedder_version: str,
  ) -> list[Chunk]:
    return [
        c for c in self._chunks if c.reference_digest == reference_digest and
        c.chunk_kind == chunk_kind and c.embedder_id == embedder_id and
        c.embedder_version == embedder_version
    ]

  def exists(self, reference_digest: str, embedder_id: str,
             embedder_version: str) -> bool:
    return any(
        c.reference_digest == reference_digest and
        c.embedder_id == embedder_id and c.embedder_version == embedder_version
        for c in self._chunks)


__all__ = ["ChunkStore", "InMemoryChunkStore"]
