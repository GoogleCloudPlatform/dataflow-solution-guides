"""One `generation_plan` milestone per run (2026-07-29 request).

Answers, in ONE greppable log line, the question every postmortem re-derives
by hand: which fields are LLM free-text pools (and via which RAG seeding
method), which are shaped identifiers routed off the LLM, which are
constants / categoricals / numeric / temporal samplers.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,missing-class-docstring,redefined-outer-name,reimported,unused-argument

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

import json

import pytest
from sdfb_core.contracts import TableSchema
from sdfb_core.engines import get_engine
from sdfb_core.engines.b1_rag import engine as engine_mod
from sdfb_core.engines.b1_rag.engine import clear_free_text_pool_cache
from sdfb_core.engines.base import GenerationContext
from sdfb_core.engines.generation_plan import clear_generation_plan_log

_DIGEST = "digest-plan"
_MODEL = "gs://m/qwen3/v1"


@pytest.fixture(autouse=True)
def _fresh_state():
  clear_free_text_pool_cache()
  clear_generation_plan_log()
  yield
  clear_free_text_pool_cache()
  clear_generation_plan_log()


_SCHEMA = TableSchema.model_validate({
    "table_info": {
        "table_id": "demo.plan_t"
    },
    "schema": [
        {
            "name": "konst",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "code",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "amount",
            "type": "INTEGER",
            "mode": "REQUIRED"
        },
        {
            "name": "event_dt",
            "type": "DATE",
            "mode": "REQUIRED"
        },
        {
            "name": "ident",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "notes",
            "type": "STRING",
            "mode": "REQUIRED"
        },
    ],
})


def _rows(n: int = 60) -> list[dict]:
  # event_dt is a real `date` (as BQ reads deliver) — a string here would
  # fall back to categorical profiling. ident is length 8: shape detection
  # requires >= 8 fixed-length characters.
  from datetime import date

  return [{
      "konst": "FIXED",
      "code": ["A", "B", "C"][i % 3],
      "amount": i * 7,
      "event_dt": date(2024, i % 12 + 1, i % 28 + 1),
      "ident": f"ID-{i:05d}",
      "notes": f"reference prose value number {i} long enough to be text",
  } for i in range(n)]


def _ctx(**overrides) -> GenerationContext:
  defaults = dict(
      table_schema=_SCHEMA,
      reference_rows=_rows(),
      reference_digest=_DIGEST,
      model_uri=_MODEL,
      pipeline_run_id="run-plan",
      num_rows=40,
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
    return [{
        "values": [f"gen-{choice}-{i}" for i in range(32)]
    } for choice in range(n)]


def _capture_plans(monkeypatch):
  captured: list[dict] = []
  real = engine_mod.log_milestone

  def _spy(name, **kwargs):
    if name == "generation_plan":
      captured.append(kwargs)
    return real(name, **kwargs)

  monkeypatch.setattr(engine_mod, "log_milestone", _spy)
  return captured


def test_setup_logs_one_generation_plan_covering_every_column(monkeypatch):
  plans = _capture_plans(monkeypatch)
  engine = get_engine("b1_rag")()
  engine.setup(_StubClient(), _ctx())

  assert len(plans) == 1, "exactly one generation_plan per setup"
  kwargs = plans[0]
  plan = json.loads(kwargs["plan"])
  assert plan == {
      "constant": ["konst"],
      "categorical": ["code"],
      "numeric": ["amount"],
      "temporal": ["event_dt"],
      "shaped_identifier": ["ident"],
      "freetext_llm_pool": ["notes"],
  }
  assert kwargs["table"] == _SCHEMA.fqn
  assert kwargs["engine"] == "b1_rag"
  assert kwargs["seed_strategy"] == "centroid"
  assert kwargs["top_k"] >= 1
  assert json.loads(kwargs["pool_sources"]) == {"notes": "llm_ladder"}


def test_plan_is_logged_once_per_digest_not_once_per_setup(monkeypatch):
  """8 worker threads each run setup(); the log must not repeat 8x."""
  plans = _capture_plans(monkeypatch)
  get_engine("b1_rag")().setup(_StubClient(), _ctx())
  get_engine("b1_rag")().setup(_StubClient(), _ctx())
  assert len(plans) == 1

  clear_generation_plan_log()
  get_engine("b1_rag")().setup(_StubClient(), _ctx())
  assert len(plans) == 2


def test_pool_sources_distinguish_store_hits_from_fresh_builds(monkeypatch):
  from sdfb_core.pools import FreeTextPool, InMemoryFreeTextPoolStore

  plans = _capture_plans(monkeypatch)
  store = InMemoryFreeTextPoolStore([
      FreeTextPool(
          reference_digest=_DIGEST,
          model_uri=_MODEL,
          column="notes",
          target=40,
          values=tuple(f"stored-{i}" for i in range(40)),
      )
  ])
  engine = get_engine("b1_rag")()
  engine.setup(_StubClient(), _ctx(pool_store=store))
  assert json.loads(plans[0]["pool_sources"]) == {"notes": "store"}


def test_worker_logs_carry_the_fetched_constraint_clause(caplog):
  # 2026-08-20 follow-up: the launcher preflight named constrained
  # COLUMNS, the worker plan said `constraint: true` — neither showed
  # WHAT was fetched from the BigQuery DDL metadata. The engine now
  # emits `prompt_constraints_found` (same greppable name as preflight)
  # with the rendered clause + clause_sha12 per column.
  import logging

  from sdfb_core.contracts import TableSchema
  from sdfb_core.engines.b1_rag import B1RagEngine, HashingEmbedder

  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.constrained_t"
      },
      "schema": [{
          "name":
              "REF",
          "type":
              "STRING",
          "mode":
              "REQUIRED",
          "description": ('ops ref {"llm_prompt_constraint":'
                          ' {"format": "4 letters then 3 digits"}}'),
      },],
  })
  rows = [{"REF": f"{'ABCD'[i % 4] * 4}{i:03d}"} for i in range(60)]
  ctx = GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="constraint-log-digest",
      pipeline_run_id="constraint-log-run",
  )

  class _NoLLM:

    def generate_json(self, *, n=1, **kw):
      return [{"values": [f"ZZZZ{i:03d}" for i in range(40)]} for _ in range(n)]

  engine = B1RagEngine(embedder=HashingEmbedder())
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    engine.setup(_NoLLM(), ctx)
  text = "\n".join(r.message for r in caplog.records)
  assert "name=prompt_constraints_found" in text
  assert "4 letters then 3 digits" in text
  assert "clause_sha12" in text
  assert "engine=b1_rag" in text
  engine.teardown()
