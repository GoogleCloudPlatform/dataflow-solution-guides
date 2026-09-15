"""--pool_seed_strategy: three arms off ONE build (WS5 T9).

Retrieval runs 3x per worker setup and its whole job is picking 8 prompt
seeds, so this is the cheapest available lever on novel-yield-per-call.
Making it a flag rather than two branches means the three E2E runs differ
in exactly one variable.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,missing-class-docstring,unused-argument

from __future__ import annotations

import pytest
from sdfb_beam.cli.run_pipeline import POOL_SEED_STRATEGIES, validate_seed_strategy
from sdfb_core.contracts import TableSchema
from sdfb_core.engines.base import GenerationContext
from sdfb_core.rag.retrieval import select_seed_examples


def _ctx(**overrides) -> GenerationContext:
  defaults = dict(
      table_schema=TableSchema.model_validate({
          "table_info": {
              "table_id": "demo.t"
          },
          "schema": [{
              "name": "c",
              "type": "STRING",
              "mode": "NULLABLE"
          }],
      }),)
  defaults.update(overrides)
  return GenerationContext(**defaults)


def _three_modes():
  vectors = ([[1.0, 0.0, 0.0] for _ in range(20)] +
             [[0.0, 1.0, 0.0] for _ in range(3)] +
             [[0.0, 0.0, 1.0] for _ in range(2)])
  texts = ([f"dense{i}" for i in range(20)] + [f"a{i}" for i in range(3)] +
           [f"b{i}" for i in range(2)])
  return vectors, texts


def test_default_is_the_control_arm():
  assert _ctx().pool_seed_strategy == "centroid"


def test_every_arm_is_accepted():
  for arm in POOL_SEED_STRATEGIES:
    assert validate_seed_strategy(arm) == arm


def test_unknown_arm_fails_loudly_at_launch():
  """A typo must not silently degrade to the control arm — that would
    corrupt the very comparison this flag exists for."""
  with pytest.raises(ValueError, match="pool_seed_strategy"):
    validate_seed_strategy("kcentre")


def test_centroid_selection_stays_in_the_dense_mode():
  vectors, texts = _three_modes()
  got = select_seed_examples(vectors, texts, 4, strategy="centroid")
  assert all(g.startswith("dense") for g in got)


def test_kcenter_selection_spans_the_modes():
  vectors, texts = _three_modes()
  got = select_seed_examples(vectors, texts, 4, strategy="kcenter")
  assert len({g[0] for g in got}) >= 2


def test_rotate_varies_seeds_across_attempts():
  vectors, texts = _three_modes()
  first = select_seed_examples(
      vectors, texts, 4, strategy="kcenter_rotate", attempt=0)
  second = select_seed_examples(
      vectors, texts, 4, strategy="kcenter_rotate", attempt=1)
  assert first != second


def test_non_rotate_strategies_ignore_attempt():
  """centroid and kcenter must keep a byte-identical prompt prefix so
    vLLM prefix caching still applies (ADR 0018)."""
  vectors, texts = _three_modes()
  for strategy in ("centroid", "kcenter"):
    a = select_seed_examples(vectors, texts, 4, strategy=strategy, attempt=0)
    b = select_seed_examples(vectors, texts, 4, strategy=strategy, attempt=5)
    assert a == b


def test_short_populations_return_everything_under_every_strategy():
  vectors = [[1.0, 0.0], [0.0, 1.0]]
  texts = ["x", "y"]
  for strategy in POOL_SEED_STRATEGIES:
    assert sorted(select_seed_examples(vectors, texts, 8,
                                       strategy=strategy)) == [
                                           "x",
                                           "y",
                                       ]


def test_unknown_strategy_at_selection_time_falls_back_to_centroid():
  """Defence in depth: the CLI validates, but a stale ctx must not crash
    a worker mid-run."""
  vectors, texts = _three_modes()
  got = select_seed_examples(vectors, texts, 4, strategy="nonsense")
  assert all(g.startswith("dense") for g in got)


class _PromptRecordingClient:

  def __init__(self):
    self.prompts: list[str] = []

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
    self.prompts.append(prompt)
    # Never satisfy the target, so the ladder runs many attempts.
    return [{"values": [f"v-{len(self.prompts)}-{i}" for i in range(2)]}]


def _freetext_ctx(strategy: str) -> GenerationContext:
  return GenerationContext(
      table_schema=TableSchema.model_validate({
          "table_info": {
              "table_id": "demo.t"
          },
          "schema": [{
              "name": "notes",
              "type": "STRING",
              "mode": "REQUIRED"
          }],
      }),
      reference_rows=[{
          "notes": f"reference prose value number {i}"
      } for i in range(60)],
      reference_digest="",  # disable the process cache for this test
      model_uri="gs://m/v1",
      num_rows=200,
      pool_seed_strategy=strategy,
  )


@pytest.mark.parametrize("strategy", ["centroid", "kcenter"])
def test_stable_strategies_keep_a_byte_identical_prompt(strategy):
  """vLLM prefix caching depends on this (ADR 0018)."""
  from sdfb_core.engines.b1_rag.engine import B1RagEngine

  client = _PromptRecordingClient()
  B1RagEngine().setup(client, _freetext_ctx(strategy))
  assert len(client.prompts) > 1, "ladder should have run several attempts"
  assert len(set(client.prompts)) == 1


def test_rotate_actually_varies_the_prompt_sent_to_the_client():
  from sdfb_core.engines.b1_rag.engine import B1RagEngine

  client = _PromptRecordingClient()
  B1RagEngine().setup(client, _freetext_ctx("kcenter_rotate"))
  assert len(client.prompts) > 1
  assert len(set(client.prompts)) > 1, "rotate must re-seed across attempts"
