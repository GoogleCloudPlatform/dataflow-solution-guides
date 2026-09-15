"""WS2 §4b.3: free-text exemplars are COLUMN-relevant. The pool prompt for
column C seeds from C's own values (store chunks when present, locally
embedded values otherwise) — not from whole-row sentences that dilute C."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=unused-argument

from __future__ import annotations

from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationContext
from sdfb_core.engines.b1_rag import B1RagEngine
from sdfb_core.rag import InMemoryChunkStore
from sdfb_core.rag.chunking import Chunk, compute_chunk_id
from sdfb_core.rag.embedding import HashingEmbedder

_SCHEMA = TableSchema.model_validate({
    "table_info": {
        "table_id": "p.d.tickets"
    },
    "schema": [
        {
            "name": "id",
            "type": "INT64",
            "mode": "REQUIRED"
        },
        {
            "name": "notes",
            "type": "STRING",
            "mode": "REQUIRED"
        },
    ],
    "primary_keys": ["id"],
})
_ROWS = [{
    "id": i,
    "notes": f"shipping delay complaint number {i} from customer"
} for i in range(40)]
_EID, _EVER = "hashing-384", "v1"


class _RecordingClient:
  """Captures the prompts so seed provenance is assertable."""

  def __init__(self) -> None:
    self.prompts: list[str] = []

  def generate_json(self, prompt, json_schema, **kw):
    self.prompts.append(prompt)
    return [{
        "values": [
            f"synthetic note {len(self.prompts)}-{i}" for i in range(32)
        ]
    }]


def _ctx(store=None) -> GenerationContext:
  return GenerationContext(
      table_schema=_SCHEMA,
      reference_rows=_ROWS,
      reference_digest="dig-1",
      pipeline_run_id="per-col",
      num_rows=40,
      embedder_id=_EID,
      embedder_version=_EVER,
      chunk_store=store,
  )


def test_seeds_come_from_column_values_locally():
  client = _RecordingClient()
  engine = B1RagEngine(embedder=HashingEmbedder(dim=32))
  engine.setup(client, _ctx())
  prompt = next(p for p in client.prompts if "'notes'" in p)
  # Seeds are raw column values, not GReaT row sentences ("id is ...").
  assert "complaint number" in prompt
  assert "id is" not in prompt
  engine.teardown()


def test_seeds_prefer_store_free_text_chunks():
  emb = HashingEmbedder(dim=384)
  marker = "STORE-ONLY exemplar text"
  values = [f"{marker} {i}" for i in range(10)]
  vectors = emb.embed(values)
  store = InMemoryChunkStore([
      Chunk(
          chunk_id=compute_chunk_id("p.d.tickets", f"r{i}", 1),
          source_fqn="p.d.tickets",
          row_digest=f"r{i}",
          reference_digest="dig-1",
          chunk_index=1,
          chunk_kind="free_text_col",
          chunk_text=values[i],
          embedder_id=_EID,
          embedder_version=_EVER,
          embedding=vectors[i],
          metadata={"column": "notes"},
      ) for i in range(10)
  ])
  client = _RecordingClient()
  engine = B1RagEngine(embedder=HashingEmbedder(dim=384))
  engine.setup(client, _ctx(store))
  prompt = next(p for p in client.prompts if "'notes'" in p)
  assert marker in prompt
  engine.teardown()
