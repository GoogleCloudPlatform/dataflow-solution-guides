"""An all-NULL GEOGRAPHY column generates NULL, never invented WKT.

The Dataflow Solution Guides deployment keeps each `thelook_ecommerce`
table's exact schema in its source snapshot and nulls the GEOGRAPHY column
(`users.user_geom`), because BigQuery rejects free-text values that are not
valid WKT on load (ADR 0040). This pins the behaviour that makes that safe.
"""

from __future__ import annotations

from sdfb_beam.handlers.fake_client import FakeModelClient
from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationConfig, GenerationContext
from sdfb_core.engines.b1_rag import B1RagEngine, HashingEmbedder


def _users_like_ctx() -> GenerationContext:
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "synthetic_source.users"
      },
      "schema": [
          {
              "name": "id",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "city",
              "type": "STRING",
              "mode": "NULLABLE"
          },
          {
              "name": "latitude",
              "type": "FLOAT64",
              "mode": "NULLABLE"
          },
          {
              "name": "user_geom",
              "type": "GEOGRAPHY",
              "mode": "NULLABLE"
          },
      ],
  })
  cities = ["Madrid", "Lyon", "Porto", "Bari"]
  rows = [{
      "id": i,
      "city": cities[i % len(cities)],
      "latitude": 40.0 + i / 100,
      "user_geom": None
  } for i in range(1, 121)]
  return GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="users-geom-null",
      pipeline_run_id="geom-test")


def test_all_null_geography_column_generates_only_nulls():
  ctx = _users_like_ctx()
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(FakeModelClient(reference_pool=ctx.reference_rows), ctx)
  out = list(engine.generate_batch(200, GenerationConfig(seed=7)))
  assert len(out) == 200
  assert all(r.user_geom is None for r in out)
  assert {r.city for r in out} <= {"Madrid", "Lyon", "Porto", "Bari"}
