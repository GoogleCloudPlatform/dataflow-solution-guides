"""B.1 parity with B.2's relaxed-shape fallback (2026-07-24 16:35 E2E).

That run: COL_053 parroted one exemplar on every escalation level
(distinct=1, novel=0) -> FreeTextEmptyYieldError out of DoFn.setup() ->
Dataflow silently retried the whole setup twice (~17 min of rework).
A relaxed per-position template generates verified-novel in-format values
without the LLM, exactly as b2_library/freetext.py already does.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=protected-access,unused-argument

from __future__ import annotations

import pytest
from sdfb_core.contracts import TableSchema
from sdfb_core.engines.b1_rag.engine import B1RagEngine
from sdfb_core.engines.base import FreeTextEmptyYieldError, GenerationContext
from sdfb_core.observability import parse_milestone


def _schema(col: str) -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.t"
      },
      "schema": [{
          "name": col,
          "type": "STRING",
          "mode": "REQUIRED"
      }],
  })


# Mixed lengths defeat detect_identifier_shape (strict), no whitespace so
# build_relaxed_shapes CAN template them -> FREE_TEXT with
# identifier_shape=None, exactly the COL_053 class.
_ID_VALUES = [f"USR{i:04d}X" for i in range(30)
             ] + [f"USR{i:05d}XX" for i in range(30)]
# Whitespace -> build_relaxed_shapes returns None (prose stays prose).
_PROSE_VALUES = [f"support ticket about outage number {i}" for i in range(60)]


class _ParrotClient:
  """Always echoes shown observed values — parsed>0, novel=0."""

  def __init__(self, echoes: list[str]):
    self._echoes = echoes
    self.call_count = 0

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
    self.call_count += 1
    return [{"values": list(self._echoes[:8])} for _ in range(n)]


def _ctx(col: str, values: list[str], **overrides) -> GenerationContext:
  defaults = dict(
      table_schema=_schema(col),
      reference_rows=[{
          col: v
      } for v in values],
      pipeline_run_id="run-shape-1",
      strict_freetext=True,
      num_rows=40,
      # Ladder-mechanics tests: expansion off forces the pool path.
      freetext_expansion="off",
  )
  defaults.update(overrides)
  return GenerationContext(**defaults)


def _milestone_names(caplog) -> list[str]:
  return [
      m["name"] for m in (parse_milestone(r.getMessage())
                          for r in caplog.records) if m
  ]


def test_copy_saturated_id_column_falls_back_to_shapes(caplog):
  engine = B1RagEngine()
  with caplog.at_level("WARNING"):
    engine.setup(_ParrotClient(_ID_VALUES), _ctx("carr_id", _ID_VALUES))
  pool = engine._free_text_pools["carr_id"]
  assert pool, "shape fallback must produce a pool instead of raising"
  observed = set(_ID_VALUES)
  assert all(v not in observed for v in pool), "fallback values must be novel"
  assert "freetext_pool_shape_fallback" in _milestone_names(caplog)


def test_copy_saturated_prose_column_still_raises_strict():
  engine = B1RagEngine()
  with pytest.raises(FreeTextEmptyYieldError):
    engine.setup(_ParrotClient(_PROSE_VALUES), _ctx("notes", _PROSE_VALUES))


class _FewNovelThenParrotClient(_ParrotClient):
  """First call yields 6 IN-FORMAT novel values (matching the observed
    USR…X 8-char bucket/charset — the format gate rejects out-of-format
    candidates before they can count as novel), then parrots ->
    undersized pool."""

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
    self.call_count += 1
    if self.call_count == 1:
      return [{"values": [f"USR9{i:03d}X" for i in range(6)]}]
    return [{"values": list(self._echoes[:8])} for _ in range(n)]


def test_undersized_pool_topped_up_with_shape_values(caplog):
  engine = B1RagEngine()
  with caplog.at_level("WARNING"):
    engine.setup(
        _FewNovelThenParrotClient(_ID_VALUES), _ctx("carr_id", _ID_VALUES))
  pool = engine._free_text_pools["carr_id"]
  # target = min(num_rows=40, distinct=60, 512) = 40; the LLM delivered 6.
  assert len(pool) == 40
  assert "freetext_pool_shape_topup" in _milestone_names(caplog)
  observed = set(_ID_VALUES)
  assert all(v not in observed for v in pool[6:]), "top-up values must be novel"
