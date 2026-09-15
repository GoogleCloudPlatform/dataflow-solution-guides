"""WS2 §4b: context fields that thread the RAG layer into the engine.

The BQ-backed ChunkStore cannot ride the pickled Beam graph — the context
is built driver-side with chunk_store=None + string config, and the
worker DoFn attaches the live store via model_copy (the embedder_uri
localization pattern)."""

from __future__ import annotations

from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationContext
from sdfb_core.rag import InMemoryChunkStore
from sdfb_core.rag.embedding import embedder_identity

_SCHEMA = TableSchema.model_validate({
    "table_info": {
        "table_id": "d.t"
    },
    "schema": [{
        "name": "a",
        "type": "STRING",
        "mode": "REQUIRED"
    }],
    "primary_keys": None,
})


def test_context_defaults_are_off():
  ctx = GenerationContext(table_schema=_SCHEMA)
  assert ctx.num_rows == 0
  assert ctx.embedder_id == ""
  assert ctx.embedder_version == ""
  assert ctx.rag_chunks_table == ""
  assert ctx.chunk_store is None


def test_worker_attaches_store_via_model_copy():
  ctx = GenerationContext(
      table_schema=_SCHEMA, rag_chunks_table="p.synthetic_rag.rag_chunks")
  store = InMemoryChunkStore()
  updated = ctx.model_copy(update={"chunk_store": store})
  assert updated.chunk_store is store
  assert ctx.chunk_store is None  # frozen original untouched


def test_embedder_identity_parses_model_layout_uri():
  uri = "gs://bkt/synthetic/models/embedders/bge-small-en-v1.5/v1/"
  assert embedder_identity(uri) == ("bge-small-en-v1.5", "v1")
  assert embedder_identity("/local-ssd/embedders/bge-small-en-v1.5/v2") == (
      "bge-small-en-v1.5",
      "v2",
  )
  assert embedder_identity("") == ("hashing-384", "v1")
  assert embedder_identity("solo") == ("solo", "v1")
