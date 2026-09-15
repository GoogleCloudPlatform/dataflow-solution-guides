"""WS2 §4a: the ChunkStore seam. Pure-Python; the BQ implementation lives
in sdfb-beam and is mock-tested there."""

from __future__ import annotations

from sdfb_core.rag.chunking import Chunk
from sdfb_core.rag.store import ChunkStore, InMemoryChunkStore


def _chunk(kind: str,
           digest: str = "d1",
           eid: str = "e",
           ever: str = "v1") -> Chunk:
  return Chunk(
      chunk_id=f"{kind}-{digest}-{eid}-{ever}",
      source_fqn="p.d.t",
      row_digest="r",
      reference_digest=digest,
      chunk_index=0,
      chunk_kind=kind,
      chunk_text="t",
      embedder_id=eid,
      embedder_version=ever,
  )


def test_in_memory_store_satisfies_protocol_and_filters():
  store = InMemoryChunkStore([
      _chunk("row_doc"),
      _chunk("free_text_col"),
      _chunk("row_doc", digest="other"),
      _chunk("row_doc", ever="v2"),
  ])
  assert isinstance(store, ChunkStore)
  hits = store.fetch("d1", "row_doc", "e", "v1")
  assert [c.chunk_kind for c in hits] == ["row_doc"]
  assert store.exists("d1", "e", "v1")
  assert store.exists("other", "e", "v1")
  assert not store.exists("d1", "e", "v3")
  assert store.fetch("d1", "row_doc", "e", "v3") == []


def test_add_appends():
  store = InMemoryChunkStore()
  assert not store.exists("d1", "e", "v1")
  store.add([_chunk("row_doc")])
  assert store.exists("d1", "e", "v1")
