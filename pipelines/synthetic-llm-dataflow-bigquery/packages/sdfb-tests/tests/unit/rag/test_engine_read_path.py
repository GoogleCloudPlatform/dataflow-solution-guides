"""WS2 §4b.1: read-instead-of-reembed. Self-gating on data: full coverage
in the pinned vector space ⇒ zero embed calls; anything less ⇒ exactly
today's embed path (never a partial mix of vector spaces)."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=unused-argument

from __future__ import annotations

import dataclasses

from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationConfig, GenerationContext
from sdfb_core.engines.b1_rag import B1RagEngine
from sdfb_core.rag import InMemoryChunkStore
from sdfb_core.rag.chunking import chunk_row
from sdfb_core.rag.embedding import HashingEmbedder
from sdfb_core.rag.serialize import serialize_rows


class _CountingEmbedder(HashingEmbedder):

  def __init__(self, dim: int = 384) -> None:
    super().__init__(dim=dim)
    self.embed_calls = 0

  def embed(self, texts):
    self.embed_calls += 1
    return super().embed(texts)


class _NoLLMClient:

  def generate_json(self, prompt, json_schema, **kw):
    return [{"values": []}]


_SCHEMA = TableSchema.model_validate({
    "table_info": {
        "table_id": "p.d.t"
    },
    "schema": [
        {
            "name": "id",
            "type": "INT64",
            "mode": "REQUIRED"
        },
        {
            "name": "status",
            "type": "STRING",
            "mode": "REQUIRED"
        },
    ],
    "primary_keys": ["id"],
})
_ROWS = [{"id": i, "status": ["OPEN", "DONE"][i % 2]} for i in range(20)]
_EID, _EVER = "hashing-384", "v1"


def _populated_store(rows, embedder) -> InMemoryChunkStore:
  """What a --build_rag_layer run would have written for this digest."""
  store = InMemoryChunkStore()
  column_order = ["id", "status"]
  chunks = []
  for row in rows:
    for c in chunk_row(
        row,
        column_order=column_order,
        free_text_columns=[],
        source_fqn="p.d.t",
        reference_digest="dig-1",
        embedder_id=_EID,
        embedder_version=_EVER,
    ):
      chunks.append(c)
  texts = serialize_rows(rows, column_order)
  vectors = embedder.embed(texts)
  store.add([
      dataclasses.replace(c, embedding=vectors[i]) for i, c in enumerate(chunks)
  ])
  return store


def _ctx(store) -> GenerationContext:
  return GenerationContext(
      table_schema=_SCHEMA,
      reference_rows=_ROWS,
      reference_digest="dig-1",
      pipeline_run_id="read-path-test",
      embedder_id=_EID,
      embedder_version=_EVER,
      chunk_store=store,
  )


def test_full_coverage_skips_embedding_and_generates():
  seed_embedder = HashingEmbedder(dim=384)
  store = _populated_store(_ROWS, seed_embedder)
  engine_embedder = _CountingEmbedder()
  engine = B1RagEngine(embedder=engine_embedder)
  engine.setup(_NoLLMClient(), _ctx(store))
  assert engine_embedder.embed_calls == 0  # the whole point
  records = list(
      engine.generate_batch(10, GenerationConfig(seed=1, batch_size=10)))
  assert len(records) == 10
  engine.teardown()


def test_wrong_vector_space_falls_back_to_embed():
  store = _populated_store(_ROWS, HashingEmbedder(dim=384))
  engine_embedder = _CountingEmbedder()
  engine = B1RagEngine(embedder=engine_embedder)
  ctx = _ctx(store).model_copy(update={"embedder_version": "v9"})
  engine.setup(_NoLLMClient(), ctx)
  assert engine_embedder.embed_calls >= 1
  engine.teardown()


def test_partial_coverage_falls_back_to_embed():
  store = _populated_store(_ROWS[:5], HashingEmbedder(dim=384))  # 5 of 20
  engine_embedder = _CountingEmbedder()
  engine = B1RagEngine(embedder=engine_embedder)
  engine.setup(_NoLLMClient(), _ctx(store))
  assert engine_embedder.embed_calls >= 1
  engine.teardown()


def test_no_store_is_todays_path():
  engine_embedder = _CountingEmbedder()
  engine = B1RagEngine(embedder=engine_embedder)
  engine.setup(_NoLLMClient(), _ctx(None))
  assert engine_embedder.embed_calls >= 1
  engine.teardown()
