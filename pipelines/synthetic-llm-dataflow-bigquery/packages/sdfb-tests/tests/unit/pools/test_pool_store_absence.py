"""A run that pays the pool ladder again must SAY so (WS6 W1).

2026-07-26_17_10_37 generated 1M rows in 53 minutes and 26 of those minutes
were workers rebuilding free-text pools — because the WS5 pool store was
simply never switched on. Nothing in the logs said so; the absence was only
discoverable by noticing that freetext_pool_store_* milestones were
missing, which is exactly the kind of silence a milestone exists to break.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=missing-class-docstring,unused-argument

from __future__ import annotations

import pytest
from sdfb_core.contracts import TableSchema
from sdfb_core.engines.b1_rag.engine import B1RagEngine, clear_free_text_pool_cache
from sdfb_core.engines.base import GenerationContext
from sdfb_core.observability import parse_milestone
from sdfb_core.pools import InMemoryFreeTextPoolStore

_COLS = ["col_a"]


@pytest.fixture(autouse=True)
def _fresh_cache():
  clear_free_text_pool_cache()
  yield
  clear_free_text_pool_cache()


def _ctx(**overrides) -> GenerationContext:
  defaults = dict(
      table_schema=TableSchema.model_validate({
          "table_info": {
              "table_id": "demo.t"
          },
          "schema": [{
              "name": c,
              "type": "STRING",
              "mode": "REQUIRED"
          } for c in _COLS],
      }),
      reference_rows=[{
          c: f"{c} reference prose value number {i}" for c in _COLS
      } for i in range(60)],
      reference_digest="digest-w1",
      model_uri="gs://m/v1",
      num_rows=1_000_000,
  )
  defaults.update(overrides)
  return GenerationContext(**defaults)


class _StubClient:

  def generate_json(self,
                    prompt,
                    json_schema,
                    *,
                    max_tokens=2048,
                    temperature=0.7,
                    n=1,
                    seed=None,
                    top_p=None,
                    top_k=None):
    return [{"values": [f"gen-{c}-{i}" for i in range(32)]} for c in range(n)]


def _names(caplog):
  return [
      m["name"] for m in (parse_milestone(r.getMessage())
                          for r in caplog.records) if m
  ]


def test_absent_pool_store_is_announced(caplog):
  with caplog.at_level("WARNING"):
    B1RagEngine().setup(_StubClient(), _ctx())
  assert "freetext_pool_store_absent" in _names(caplog)


def test_the_warning_carries_the_row_count_that_makes_it_matter(caplog):
  with caplog.at_level("WARNING"):
    B1RagEngine().setup(_StubClient(), _ctx())
  line = next(r.getMessage()
              for r in caplog.records
              if "freetext_pool_store_absent" in r.getMessage())
  assert "num_rows=1000000" in line


def test_no_warning_when_a_store_is_attached(caplog):
  with caplog.at_level("WARNING"):
    B1RagEngine().setup(_StubClient(),
                        _ctx(pool_store=InMemoryFreeTextPoolStore()))
  assert "freetext_pool_store_absent" not in _names(caplog)


def test_no_warning_when_there_is_no_free_text_column_to_build(caplog):
  """Nothing to rebuild ⇒ nothing to warn about."""
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.t"
      },
      "schema": [{
          "name": "n",
          "type": "INT64",
          "mode": "REQUIRED"
      }],
  })
  with caplog.at_level("WARNING"):
    B1RagEngine().setup(
        _StubClient(),
        _ctx(table_schema=schema, reference_rows=[{
            "n": i
        } for i in range(60)]),
    )
  assert "freetext_pool_store_absent" not in _names(caplog)
