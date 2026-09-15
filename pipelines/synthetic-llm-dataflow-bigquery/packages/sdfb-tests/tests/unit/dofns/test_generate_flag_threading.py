"""--freetext_expansion / --prompt_constraints reach both engines (Task 8)."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,protected-access,unused-argument

from sdfb_core.contracts.schema import TableSchema
from sdfb_core.engines.base import GenerationContext


def _ctx(**kwargs) -> GenerationContext:
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t"
      },
      "schema": [{
          "name": "A",
          "type": "STRING",
          "mode": "NULLABLE"
      }],
  })
  return GenerationContext(table_schema=schema, **kwargs)


def test_context_defaults():
  ctx = _ctx()
  assert ctx.freetext_expansion == "identifiers"
  assert ctx.prompt_constraints is True


def test_generate_dofn_mirrors_ctx_into_engine_specific():
  from sdfb_beam.dofns.generate import GenerateRecordsDoFn

  captured: list = []

  class _StubEngine:
    name = "stub"

    def generate_batch(self, n, cfg):
      captured.append(cfg)
      return iter(())

  dofn = GenerateRecordsDoFn(
      engine_name="b1_rag",
      model_client=object(),
      ctx=_ctx(freetext_expansion="all", prompt_constraints=False),
  )
  dofn._engine = _StubEngine()
  dofn._column_types = {}
  dofn._column_max_lengths = {}
  list(dofn.process({"batch_id": 0, "n": 4}))
  assert captured, "stub engine was not called"
  assert captured[0].engine_specific["freetext_expansion"] == "all"
  assert captured[0].engine_specific["prompt_constraints"] is False


def test_generate_dofn_attaches_source_value_store():
  """B.2 builds pools lazily in Generate workers (no pool branch), so the
    ADR 0023 rejection set must ride the generate path too: a configured
    `source_values_table` becomes a worker-side BigQuerySourceValueStore,
    mirroring chunk_store / pool_store attachment."""
  from sdfb_beam.dofns.generate import GenerateRecordsDoFn
  from sdfb_beam.io.source_values import BigQuerySourceValueStore
  from sdfb_tests.fakes import FakeModelClient

  dofn = GenerateRecordsDoFn(
      engine_name="minimal",
      model_client=FakeModelClient(reference_pool=[{}]),
      ctx=_ctx(source_values_table="p.d.src"),
  )
  dofn.setup()
  store = dofn.ctx.source_value_store
  assert isinstance(store, BigQuerySourceValueStore)
  assert store.table_fqn == "p.d.src"
