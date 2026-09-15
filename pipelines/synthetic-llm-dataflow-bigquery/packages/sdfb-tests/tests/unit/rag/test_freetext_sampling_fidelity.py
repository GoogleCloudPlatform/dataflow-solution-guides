"""B.1 free-text sampling: empty mask + shape-mix expansion (Task 6)."""

import re

import pytest
from sdfb_beam.handlers.fake_client import FakeModelClient
from sdfb_core.contracts.schema import TableSchema
from sdfb_core.engines import GenerationConfig, GenerationContext
from sdfb_core.engines.b1_rag import B1RagEngine, HashingEmbedder

# Digits spread across every position (i*17041 walks all six), so the
# observed shape mix justifies a ~10^6 sample space for the expander.
_OBSERVED = {f"U{(123456 + i * 17041) % 1000000:06d}" for i in range(55)}


def _ctx(expansion: str) -> GenerationContext:
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t"
      },
      "schema": [{
          "name": "REF_CODE",
          "type": "STRING",
          "mode": "NULLABLE"
      }],
  })
  # 55 distinct codes (> free-text category cap) + 45 empties: FREE_TEXT
  # route with empty_fraction 0.45 and an identifier-like shape mix.
  rows = [{"REF_CODE": v} for v in sorted(_OBSERVED)]
  rows += [{"REF_CODE": ""} for _ in range(45)]
  return GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="d1",
      freetext_expansion=expansion,
      num_rows=4000,
  )


def _generate(expansion: str, n: int = 4000) -> list:
  ctx = _ctx(expansion)
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(FakeModelClient(reference_pool=ctx.reference_rows), ctx)
  out = list(engine.generate_batch(n, GenerationConfig(seed=7)))
  return [r.REF_CODE for r in out]


def test_empty_fraction_reproduced():
  values = _generate("off")
  empty = sum(1 for v in values if v == "")
  assert empty / len(values) == pytest.approx(0.45, abs=0.04)


def test_expansion_breaks_pool_ceiling_and_stays_in_shape():
  values = [v for v in _generate("identifiers") if v]
  distinct = set(values)
  assert len(distinct) > 1200  # far beyond the 55-value observed pool
  pat = re.compile(r"^U\d{6}$")
  assert all(pat.match(v) for v in distinct)


def test_expansion_novelty_guard():
  values = [v for v in _generate("identifiers") if v]
  collisions = sum(1 for v in values if v in _OBSERVED)
  # A 3-retry guard cannot be absolute, but collisions must be rare.
  assert collisions / len(values) < 0.02


def test_expansion_off_keeps_pool_bounded():
  on = {v for v in _generate("identifiers") if v}
  off = {v for v in _generate("off") if v}
  assert len(off) < len(on) / 2  # the ceiling is the pool without expansion
