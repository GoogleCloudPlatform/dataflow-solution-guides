"""B.1 setup must stay O(bounded) in the reference-sample size.

Production defect (2026-07-16 corp run, job `..._13_23_14-11053114042412770609`):
embedding all 10,000 reference rows took 26-92 minutes PER Dataflow bundle
attempt on a CPU thrashed by sibling SDK processes — and the index those
embeddings feed is used ONLY to pick the top-k centroid exemplars for the
free-text pool prompt (M1 samples marginals; there is no per-batch
retrieval). Four bundle retries x ~1.5 h setup = a 5.4 h failed job.

The fix: embed a bounded prefix of the reference sample. The reference
SELECT is already `ORDER BY FARM_FINGERPRINT(...)` (deterministic spread),
so a prefix is a representative sample.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=protected-access,redefined-outer-name,unused-argument

from __future__ import annotations

import logging

import pytest
from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationContext
from sdfb_core.engines.b1_rag import B1RagEngine, HashingEmbedder
from sdfb_core.engines.b1_rag.engine import _MAX_EMBED_ROWS


class _NovelClient:
  """A `ModelClient` returning novel free-text values (keeps setup happy)."""

  def generate_json(self, *a, **k):
    return [{"bio": f"synthetic bio {i}"} for i in range(4)]


class _CountingEmbedder(HashingEmbedder):

  def __init__(self, dim: int = 32):
    super().__init__(dim=dim)
    self.embedded_counts: list[int] = []

  def embed(self, texts):
    self.embedded_counts.append(len(texts))
    return super().embed(texts)


@pytest.fixture
def big_ctx() -> GenerationContext:
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.profiles"
      },
      "schema": [
          {
              "name": "user_id",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "bio",
              "type": "STRING",
              "mode": "NULLABLE"
          },
      ],
      "primary_keys": ["user_id"],
  })
  n = _MAX_EMBED_ROWS + 76
  rows = [{
      "user_id": i,
      "bio": f"User number {i} writes long descriptive prose."
  } for i in range(n)]
  return GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="big-digest",
      pipeline_run_id="b1-big",
      # Embed-cap test guards the pool build's seed-example embed;
      # expansion off keeps that path live (wave 4 skips ladders for
      # expandable columns).
      freetext_expansion="off",
  )


def test_setup_embeds_at_most_the_row_cap(big_ctx):
  # WS2 §4b.3: `_column_seed_examples` adds a second, independently
  # bounded embed() call per free-text column (locally-embedded column
  # values, no chunk_store here) alongside the row-doc embed — both stay
  # capped at _MAX_EMBED_ROWS, which is the invariant this test guards.
  embedder = _CountingEmbedder()
  engine = B1RagEngine(embedder=embedder)
  engine.setup(_NovelClient(), big_ctx)
  assert embedder.embedded_counts == [_MAX_EMBED_ROWS, _MAX_EMBED_ROWS]
  assert len(engine._ref_vectors) == _MAX_EMBED_ROWS


def test_embed_milestone_reports_capped_and_total_rows(caplog, big_ctx):
  engine = B1RagEngine(embedder=_CountingEmbedder())
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    engine.setup(_NovelClient(), big_ctx)
  text = "\n".join(r.message for r in caplog.records)
  assert f"rows={_MAX_EMBED_ROWS} " in text
  assert f"rows_total={len(big_ctx.reference_rows)}" in text


def test_small_reference_is_embedded_in_full(caplog):
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.profiles"
      },
      "schema": [
          {
              "name": "user_id",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "bio",
              "type": "STRING",
              "mode": "NULLABLE"
          },
      ],
      "primary_keys": ["user_id"],
  })
  rows = [{
      "user_id": i,
      "bio": f"User number {i} writes long descriptive prose."
  } for i in range(12)]
  ctx = GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="small-digest",
      pipeline_run_id="b1-small",
  )
  embedder = _CountingEmbedder()
  engine = B1RagEngine(embedder=embedder)
  engine.setup(_NovelClient(), ctx)
  # See test_setup_embeds_at_most_the_row_cap: two bounded embed() calls
  # (row-doc + per-free-text-column local values) now, both == len(rows).
  assert embedder.embedded_counts == [12, 12]
