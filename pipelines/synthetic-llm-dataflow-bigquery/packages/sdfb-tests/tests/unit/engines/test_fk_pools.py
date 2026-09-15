"""FK columns sample from parent pools — both engines + loader (Task 14)."""

from unittest.mock import MagicMock

from sdfb_beam.handlers.fake_client import FakeModelClient
from sdfb_beam.io.fk_pools import (
    load_fk_key_pools,
    parent_landing_fqn,
    per_column_view,
)
from sdfb_core.contracts.relationships import parse_relationship_model
from sdfb_core.contracts.schema import TableSchema
from sdfb_core.engines import GenerationConfig, GenerationContext
from sdfb_core.engines.b1_rag import B1RagEngine, HashingEmbedder
from sdfb_core.engines.b2_library import B2LibraryEngine

_PARENT_KEYS = ("CUST001", "CUST002", "CUST003")


def _ctx() -> GenerationContext:
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.child"
      },
      "schema": [
          {
              "name": "CUST_ID",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "AMOUNT",
              "type": "INT64",
              "mode": "REQUIRED"
          },
      ],
  })
  rows = [{"CUST_ID": f"SRC{i:05d}", "AMOUNT": i % 7} for i in range(100)]
  return GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="d1",
      fk_pools={"CUST_ID": _PARENT_KEYS},
      num_rows=500,
  )


def test_b1_fk_column_lands_only_parent_keys():
  ctx = _ctx()
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(FakeModelClient(reference_pool=ctx.reference_rows), ctx)
  out = list(engine.generate_batch(500, GenerationConfig(seed=1)))
  assert out
  assert {r.CUST_ID for r in out} <= set(_PARENT_KEYS)
  assert len({r.CUST_ID for r in out}) == 3  # all keys actually drawn


def test_b2_fk_column_lands_only_parent_keys():
  ctx = _ctx()
  engine = B2LibraryEngine()
  engine.setup(FakeModelClient(reference_pool=ctx.reference_rows), ctx)
  out = list(engine.generate_batch(500, GenerationConfig(seed=2)))
  assert out
  assert {r.CUST_ID for r in out} <= set(_PARENT_KEYS)


def test_parent_landing_fqn():
  assert (parent_landing_fqn(
      "src_ds.customers",
      "proj.synthetic_data") == "proj.synthetic_data.customers")


def test_load_fk_key_pools_keeps_composite_keys_joint():
  """An already-landed parent is read as whole key TUPLES (ADR 0031) —
    the per-column view is a derived convenience, never the draw."""
  relations = parse_relationship_model(
      """
model: m
tables:
  CHILD:
    fk:
      - cols: [A, B]
        ref: ds.parent
        ref_cols: [X, Y]
""",
      source="test.yaml",
  ).tables["CHILD"]
  row1 = {"X": 1, "Y": "a"}
  row2 = {"X": 2, "Y": "b"}
  client = MagicMock()
  client.query.return_value.result.return_value = [row1, row2]
  payloads = load_fk_key_pools(relations.fk, "p.synthetic_data", client=client)
  assert payloads == [{"cols": ["A", "B"], "keys": [(1, "a"), (2, "b")]}]
  assert per_column_view(payloads) == {"A": (1, 2), "B": ("a", "b")}
  sql = client.query.call_args[0][0]
  assert "`p.synthetic_data.parent`" in sql
  assert "SELECT DISTINCT `X`, `Y`" in sql
