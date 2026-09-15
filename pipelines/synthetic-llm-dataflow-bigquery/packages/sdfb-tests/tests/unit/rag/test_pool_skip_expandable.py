"""Expandable columns skip the LLM pool ladder (wave 4).

2026-08-20 B_TABLE R1: PoolTrigger ran 28.7 min (53% of wall time), yet
every column whose draw path is shape-mix expansion never reads its pool —
`_sample_free_text` draws from the observed shape mix directly. The ladder
work (LLM calls, stagnation waits, store writes) for those columns was
dead cost. The pool build now skips them, EXCEPT when the column carries an
`llm_prompt_constraint`: an explicit user constraint means the pool prompt
(and its guided `pattern`) is the enforcement vehicle, so constraint
columns keep their pool AND their draw path stops bypassing it — a silent
`format` clause on an expandable column was previously decorative.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,protected-access,unused-argument,use-implicit-booleaness-not-comparison

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

import logging

from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationContext
from sdfb_core.engines.b1_rag import B1RagEngine, HashingEmbedder
from sdfb_core.engines.base import GenerationConfig


class _CountingClient:
  """Yields fresh in-format values; records which columns asked."""

  def __init__(self) -> None:
    self.prompts: list[str] = []
    self._counter = 7000

  def generate_json(self, *, prompt: str, n: int = 1, **kwargs):
    self.prompts.append(prompt)
    out = []
    for _ in range(n):
      values = []
      for _ in range(32):
        self._counter += 1
        values.append(f"{self._counter % 10000:04d}K{self._counter % 10}")
      out.append({"values": values})
    return out


def _ctx(**update) -> GenerationContext:
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.codes"
      },
      "schema": [
          {
              "name": "code_plain",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name":
                  "code_hint",
              "type":
                  "STRING",
              "mode":
                  "REQUIRED",
              "description": ('{"llm_prompt_constraint": '
                              '{"format": "4 digits, K, then 1 digit"}}'),
          },
      ],
  })
  rows = [{
      "code_plain": f"{i:04d}A{i % 10}",
      "code_hint": f"{i:04d}K{i % 10}"
  } for i in range(100)]
  return GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="codes-digest",
      pipeline_run_id="codes-run",
      **update,
  )


def test_expandable_column_skips_the_ladder_and_still_draws(caplog) -> None:
  client = _CountingClient()
  engine = B1RagEngine(embedder=HashingEmbedder())
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    engine.setup(client, _ctx())
  text = "\n".join(r.message for r in caplog.records)
  assert "name=freetext_pool_skipped_expandable" in text
  assert "column=code_plain" in text
  assert "code_plain" not in engine._free_text_pools
  # Draws still work — from the shape mix, in-format.
  out = engine._sample_free_text(200, GenerationConfig(seed=5),
                                 0.5)["code_plain"]
  vals = [v for v in out if v]
  assert vals
  assert all(len(v) == 6 and v[4] == "A" for v in vals)
  engine.teardown()


def test_constraint_column_keeps_pool_and_draws_from_it() -> None:
  client = _CountingClient()
  engine = B1RagEngine(embedder=HashingEmbedder())
  engine.setup(client, _ctx())
  pool = engine._free_text_pools.get("code_hint")
  assert pool  # ladder ran for the constraint column
  out = engine._sample_free_text(300, GenerationConfig(seed=9),
                                 0.5)["code_hint"]
  vals = [v for v in out if v]
  assert vals
  # The constraint column must NOT bypass its pool via expansion: every
  # substantive draw is a pool value (the LLM/pattern enforcement path).
  assert set(vals) <= set(pool)
  engine.teardown()


def test_expansion_off_builds_the_pool_again() -> None:
  client = _CountingClient()
  engine = B1RagEngine(embedder=HashingEmbedder())
  engine.setup(client, _ctx(freetext_expansion="off"))
  assert "code_plain" in engine._free_text_pools
  engine.teardown()


def test_zero_llm_columns_warns_gpu_idle(caplog) -> None:
  # 2026-08-21 four-run cycle: a run where EVERY free-text column
  # resolved expandable never ignited vLLM, yet billed ~28 GPU-minutes
  # on an idle T4. The engine now says so the moment the plan is known.
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.codes"
      },
      "schema": [{
          "name": "code_plain",
          "type": "STRING",
          "mode": "REQUIRED"
      },],
  })
  ctx = GenerationContext(
      table_schema=schema,
      reference_rows=[{
          "code_plain": f"{i:04d}A{i % 10}"
      } for i in range(100)],
      reference_digest="codes-digest-idle",
      pipeline_run_id="codes-run-idle",
  )
  engine = B1RagEngine(embedder=HashingEmbedder())
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    engine.setup(_CountingClient(), ctx)
  text = "\n".join(r.message for r in caplog.records)
  assert "name=llm_route_unused" in text
  engine.teardown()


def test_llm_run_does_not_warn_gpu_idle(caplog) -> None:
  engine = B1RagEngine(embedder=HashingEmbedder())
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    engine.setup(_CountingClient(), _ctx())  # code_hint runs the ladder
  text = "\n".join(r.message for r in caplog.records)
  assert "name=llm_route_unused" not in text
  engine.teardown()


def test_binary_class_column_skips_the_llm_ladder(caplog) -> None:
  # COL_048-class (2026-08-21 cycle): a binary/control-char column spent
  # 8.6 min (the ENTIRE cold pool phase, 41% of wall time) in an LLM
  # ladder whose candidates were format-rejected en masse — an LLM
  # cannot usefully emit control bytes. Such columns now go straight to
  # the shape-fallback template pool.
  values = [f"{'X' * (i % 5)}\x03{i:03x}\x0b{'Q' * (i % 3)}" for i in range(80)]
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.bin"
      },
      "schema": [{
          "name": "bin_col",
          "type": "STRING",
          "mode": "REQUIRED"
      }],
  })
  ctx = GenerationContext(
      table_schema=schema,
      reference_rows=[{
          "bin_col": v
      } for v in values],
      reference_digest="bin-digest",
      pipeline_run_id="bin-run",
  )
  client = _CountingClient()
  engine = B1RagEngine(embedder=HashingEmbedder())
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    engine.setup(client, ctx)
  text = "\n".join(r.message for r in caplog.records)
  assert "name=freetext_pool_binary_fallback" in text
  assert client.prompts == []  # the LLM was never consulted
  assert engine._pool_sources.get("bin_col") == "binary_fallback"
  engine.teardown()


def test_store_warm_setup_reports_pools_warm_not_gpu_idle(caplog) -> None:
  # 2026-08-26 R6 10M run: 64 `llm_route_unused` WARNINGs — one per
  # generate-DoFn instance — inside a job whose pool branch had just
  # spent 17 GPU-minutes building those very pools. Store-warm is the
  # designed steady state (ADR 0020), not idle hardware: say THAT.
  from types import SimpleNamespace

  class _WarmStore:

    def fetch(self, digest, model_uri):
      return [
          SimpleNamespace(
              column="code_hint",
              values=[f"{i:04d}W{i % 10}" for i in range(64)],
          )
      ]

  client = _CountingClient()
  engine = B1RagEngine(embedder=HashingEmbedder())
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    engine.setup(client, _ctx(pool_store=_WarmStore()))
  text = "\n".join(r.message for r in caplog.records)
  assert "name=freetext_pool_store_hit" in text
  assert "name=llm_route_unused" not in text
  assert "name=freetext_pools_warm" in text
  assert client.prompts == []  # no LLM call in a store-warm setup
  engine.teardown()
