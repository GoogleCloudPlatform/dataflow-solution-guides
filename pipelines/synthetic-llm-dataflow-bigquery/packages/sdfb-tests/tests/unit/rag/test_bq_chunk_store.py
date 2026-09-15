"""WS2 §4b.1: the BQ-backed ChunkStore, mock-tested (laptop has no GCP).
The DoFn attaches it worker-side — the pickled graph never carries a
live client (same lifecycle rule as the vLLM subprocess)."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,missing-class-docstring,redefined-outer-name,reimported,unused-argument

from __future__ import annotations

from types import SimpleNamespace

from sdfb_beam.rag.store import BigQueryChunkStore
from sdfb_core.rag.store import ChunkStore

_TABLE = "proj.synthetic_rag.rag_chunks"


class _FakeBqClient:

  def __init__(self, rows: list[dict]) -> None:
    self.rows = rows
    self.queries: list[tuple[str, dict]] = []

  def query(self, sql, job_config=None):
    params = {
        p.name: p.value
        for p in (job_config.query_parameters if job_config else [])
    }
    self.queries.append((sql, params))
    rows = self.rows
    return SimpleNamespace(result=lambda: iter(rows))


def _chunk_row(i: int) -> dict:
  return {
      "chunk_id": f"c{i}",
      "source_fqn": "p.d.t",
      "source_pk": f'{{"id": {i}}}',
      "row_digest": f"r{i}",
      "reference_digest": "dig",
      "chunk_index": 0,
      "chunk_kind": "row_doc",
      "chunk_text": f"t{i}",
      "embedder_id": "e",
      "embedder_version": "v1",
      "embedding": [0.1, 0.2],
      "metadata": '{"column": "notes"}',
  }


def test_satisfies_protocol_without_gcp_import():
  store = BigQueryChunkStore(_TABLE, client=_FakeBqClient([]))
  assert isinstance(store, ChunkStore)


def test_exists_true_and_false():
  assert BigQueryChunkStore(
      _TABLE, client=_FakeBqClient([{
          "n": 1
      }])).exists("dig", "e", "v1")
  assert not BigQueryChunkStore(
      _TABLE, client=_FakeBqClient([])).exists("dig", "e", "v1")


def test_fetch_maps_rows_to_chunks_with_parameters():
  client = _FakeBqClient([_chunk_row(0), _chunk_row(1)])
  store = BigQueryChunkStore(_TABLE, client=client)
  chunks = store.fetch("dig", "row_doc", "e", "v1")
  assert len(chunks) == 2
  c = chunks[0]
  assert c.chunk_text == "t0"
  assert c.embedding == [0.1, 0.2]
  assert c.source_pk == {"id": 0}
  assert c.metadata == {"column": "notes"}
  sql, params = client.queries[-1]
  assert _TABLE in sql
  assert params == {
      "reference_digest": "dig",
      "chunk_kind": "row_doc",
      "embedder_id": "e",
      "embedder_version": "v1",
  }


def test_dofn_attaches_store_from_ctx(monkeypatch):
  from types import SimpleNamespace

  from sdfb_beam.dofns import generate as generate_mod
  from sdfb_beam.dofns.generate import GenerateRecordsDoFn
  from sdfb_core.contracts import TableSchema
  from sdfb_core.engines import GenerationContext

  captured: dict = {}

  class _Engine:

    def setup(self, model_client, ctx):
      captured["ctx"] = ctx

    def generate_batch(self, n, cfg):
      return iter(())

    def teardown(self):
      pass

  monkeypatch.setattr(generate_mod, "get_engine", lambda name: _Engine)
  schema = TableSchema.model_validate({
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
  ctx = GenerationContext(
      table_schema=schema, rag_chunks_table="proj.synthetic_rag.rag_chunks")
  dofn = GenerateRecordsDoFn(
      engine_name="stub",
      model_client=SimpleNamespace(generate_json=lambda **k: [{}]),
      ctx=ctx)
  monkeypatch.setattr(
      "sdfb_beam.rag.store.BigQueryChunkStore",
      lambda table_fqn, **kw: f"store-for-{table_fqn}",
  )
  dofn.setup()
  assert captured[
      "ctx"].chunk_store == "store-for-proj.synthetic_rag.rag_chunks"
  dofn.teardown()
