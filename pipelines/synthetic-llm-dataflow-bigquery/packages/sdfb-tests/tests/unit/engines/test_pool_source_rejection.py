"""Full-source-domain rejection for free-text pools (2026-08-05 B_TABLE R1).

The R1 cold baseline landed 33-99% verbatim source values on 10 free-text
columns: the pool ladder rejects candidates only against the PROFILED
sample (`prof.observed_values`, ≤10k rows), so in-format generations
collide freely with the unobserved rest of a 2.5k-146k-value source
domain — and the 10M warm run then replayed those tainted pools wholesale.

`GenerationContext.source_value_store` closes the gap: an optional
worker-side store (Protocol: `fetch_distinct(column)`) holding each
column's FULL distinct values. When present, both the LLM-novelty check
and the shape fallback reject against it, so a persisted pool cannot
contain a source value by construction.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=protected-access,redefined-outer-name,unused-argument

from __future__ import annotations

import logging

import pytest
from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationContext
from sdfb_core.engines.b1_rag import B1RagEngine, HashingEmbedder
from sdfb_core.engines.b1_rag.profile import ColumnKind, ColumnProfile
from sdfb_core.engines.text_shapes import build_shape_mix

_OBSERVED_BIOS = [
    f"User number {i} enjoys long-form descriptive prose and writes a lot."
    for i in range(1, 33)
]
# The full source domain: the observed sample plus values the profiler
# never saw — exactly the collision surface of the 2026-08-05 finding.
_UNOBSERVED_BIOS = [
    f"User number {i} enjoys long-form descriptive prose and writes a lot."
    for i in range(33, 200)
]
_FULL_DOMAIN = frozenset(_OBSERVED_BIOS) | frozenset(_UNOBSERVED_BIOS)


class _FakeSourceValueStore:

  def __init__(self, values_by_column: dict[str, frozenset[str] | None]):
    self.values_by_column = values_by_column
    self.calls: list[str] = []

  def fetch_distinct(self, column: str) -> frozenset[str] | None:
    self.calls.append(column)
    return self.values_by_column.get(column)


class _BoomSourceValueStore:

  def fetch_distinct(self, column: str) -> frozenset[str] | None:
    raise RuntimeError("bq unavailable")


class _CollidingClient:
  """Yields unobserved-but-real source values mixed with novel ones.

    Models the production failure: the LLM lands on values inside the
    full source domain that the profiled sample never showed.
    """

  def generate_json(self, *, n=1, **kwargs):
    batch = _UNOBSERVED_BIOS[:24] + [
        f"Synthetic person {i} curates miniature bonsai gardens weekly."
        for i in range(40)
    ]
    return [{"values": batch} for _ in range(n)]


@pytest.fixture
def bio_ctx() -> GenerationContext:
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
      "bio": bio
  } for i, bio in enumerate(_OBSERVED_BIOS, start=1)]
  return GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="src-reject-digest",
      pipeline_run_id="src-reject-run",
  )


def test_pool_rejects_full_source_domain(bio_ctx, caplog) -> None:
  store = _FakeSourceValueStore({"bio": _FULL_DOMAIN})
  ctx = bio_ctx.model_copy(update={"source_value_store": store})
  engine = B1RagEngine(embedder=HashingEmbedder())
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    engine.setup(_CollidingClient(), ctx)
  pool = engine._free_text_pools["bio"]
  assert pool, "pool must still be built from the novel values"
  assert not set(pool) & _FULL_DOMAIN, "no pool value may exist in source"
  assert "bio" in store.calls
  text = "\n".join(r.message for r in caplog.records)
  assert "name=freetext_pool_source_filter" in text
  engine.teardown()


def test_without_store_behavior_is_unchanged(bio_ctx) -> None:
  engine = B1RagEngine(embedder=HashingEmbedder())
  engine.setup(_CollidingClient(), bio_ctx)
  pool = engine._free_text_pools["bio"]
  # Unobserved source values pass the (sample-only) novelty filter —
  # today's behavior, kept bit-for-bit when no store is attached.
  assert set(pool) & frozenset(_UNOBSERVED_BIOS)
  engine.teardown()


def test_store_error_degrades_loudly_not_fatally(bio_ctx, caplog) -> None:
  ctx = bio_ctx.model_copy(
      update={"source_value_store": _BoomSourceValueStore()})
  engine = B1RagEngine(embedder=HashingEmbedder())
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    engine.setup(_CollidingClient(), ctx)
  assert engine._free_text_pools["bio"]
  text = "\n".join(r.message for r in caplog.records)
  assert "name=freetext_pool_source_filter_error" in text
  engine.teardown()


def test_shape_fallback_rejects_source_values() -> None:
  # Three varying digit positions (template space REF0000-REF0999) so
  # the mix is templateable (>= 2 class positions).
  observed = [" " * 4 + f"REF{i:04d}" for i in (101, 222, 333, 404, 515)]
  # Every code from 0000-0499 exists in the full source domain: the
  # fallback must template around it, not into it.
  source = frozenset(" " * 4 + f"REF{i:04d}" for i in range(500))
  prof = ColumnProfile(
      name="ref_code",
      bq_type="STRING",
      kind=ColumnKind.FREE_TEXT,
      nullable=True,
      null_fraction=0.0,
      observed_values=tuple(observed),
      shape_mix=build_shape_mix(observed),
      is_unique_valued=True,
  )
  engine = B1RagEngine()
  pool = engine._shape_fallback_pool(
      prof, 50, exclude=set(), source_values=source)
  assert len(pool) == 50
  assert not set(pool) & source
