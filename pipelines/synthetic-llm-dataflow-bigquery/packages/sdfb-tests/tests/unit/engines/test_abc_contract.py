"""GenerationEngine ABC contract tests.

Every `GenerationEngine` implementation MUST pass these five tests. Add
new engines (B1RagEngine, B2LibraryEngine, …) to the `engine_class`
fixture's `params` list as they land in the codebase — that's how the
contract gate is enforced for all engines.

Contract (from `.claude/skills/engine-contract.md`):

  1. `test_setup_idempotent`        — `setup()` may be called twice safely.
  2. `test_batch_size_respected`    — `generate_batch(n)` yields ≤ n.
  3. `test_schema_conformance`      — yielded records are `GeneratedRecord`s.
  4. `test_seed_reproducibility`    — same `seed` + same `ctx` ⇒ same output.
  5. `test_teardown_releases_state` — `generate_batch` after `teardown()` raises.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,redefined-outer-name,reimported

from __future__ import annotations

import pytest
from sdfb_core.contracts import GeneratedRecord, TableSchema
from sdfb_core.engines import GenerationConfig, GenerationContext, ModelClient
from sdfb_core.engines.b1_rag import B1RagEngine
from sdfb_core.engines.b2_library import B2LibraryEngine
from sdfb_tests.fakes import FakeModelClient, MinimalEngine


# Each param is a zero-arg factory that builds a fresh engine. Classes are
# valid factories (``MinimalEngine()``); engines needing constructor args use
# a lambda. B.2 pins the deterministic NumPy backend (``use_sdgx=False``) for
# the reproducibility contract — sdgx/CTGAN is not bit-for-bit reproducible
# across machines (design §2).
@pytest.fixture(
    params=[
        pytest.param(MinimalEngine, id="MinimalEngine"),
        pytest.param(B1RagEngine, id="B1RagEngine"),
        pytest.param(
            lambda: B2LibraryEngine(use_sdgx=False), id="B2LibraryEngine"),
    ],)
def engine_class(request):
  """A zero-arg factory for the engine under test.

    Extend `params` when new engines land:
        pytest.param(lambda: B1RagEngine(...), id="B1RagEngine")
    """
  return request.param


@pytest.fixture
def ctx(narrow_ddl_dict):
  schema = TableSchema.model_validate(narrow_ddl_dict)
  return GenerationContext(
      table_schema=schema,
      reference_rows=[
          {
              "customer_id": 1,
              "email": "a@example.com",
              "signup_at": "2026-01-01T00:00:00Z",
              "lifetime_value": "10.00",
          },
          {
              "customer_id": 2,
              "email": "b@example.com",
              "signup_at": "2026-01-02T00:00:00Z",
              "lifetime_value": "20.50",
          },
      ],
      reference_digest="abc123",
      pipeline_run_id="test-run-1",
  )


@pytest.fixture
def model_client(ctx):
  """FakeModelClient pre-loaded with the reference rows so any engine
    that 'regenerates' by echoing produces schema-valid output."""
  return FakeModelClient(responses=ctx.reference_rows)


# ---------------------------------------------------------------------------
# Sanity check on the test driver itself.
# ---------------------------------------------------------------------------


def test_fake_model_client_satisfies_protocol(model_client):
  """FakeModelClient is a structural ModelClient (runtime_checkable)."""
  assert isinstance(model_client, ModelClient)


# ---------------------------------------------------------------------------
# The 5 contract tests.
# ---------------------------------------------------------------------------


def test_setup_idempotent(engine_class, model_client, ctx):
  """Calling setup() twice must not raise or invalidate engine state."""
  engine = engine_class()
  engine.setup(model_client, ctx)
  engine.setup(model_client, ctx)  # second call — must be a no-op
  out = list(engine.generate_batch(2, GenerationConfig(seed=42)))
  assert isinstance(out, list)


def test_batch_size_respected(engine_class, model_client, ctx):
  """generate_batch(n) yields at most n records."""
  engine = engine_class()
  engine.setup(model_client, ctx)
  out = list(engine.generate_batch(5, GenerationConfig(seed=42)))
  assert len(out) <= 5


def test_schema_conformance(engine_class, model_client, ctx):
  """Yielded records are GeneratedRecord subclass instances with the
    schema's columns."""
  engine = engine_class()
  engine.setup(model_client, ctx)
  out = list(engine.generate_batch(3, GenerationConfig(seed=42)))
  assert len(out) > 0, "engine yielded nothing — test fixture mismatch?"
  for record in out:
    assert isinstance(record, GeneratedRecord)
    for col in ctx.table_schema.columns:
      assert hasattr(record,
                     col.name), (f"record missing schema column {col.name!r}")


def test_seed_reproducibility(engine_class, ctx):
  """Same seed + same ctx + fresh client ⇒ identical output stream."""
  # Fresh clients so client state doesn't leak between engines.
  e1 = engine_class()
  e1.setup(FakeModelClient(responses=ctx.reference_rows), ctx)
  out_a = [
      r.model_dump() for r in e1.generate_batch(3, GenerationConfig(seed=42))
  ]

  e2 = engine_class()
  e2.setup(FakeModelClient(responses=ctx.reference_rows), ctx)
  out_b = [
      r.model_dump() for r in e2.generate_batch(3, GenerationConfig(seed=42))
  ]

  assert out_a == out_b
  assert len(out_a) > 0, "engine yielded nothing — test fixture mismatch?"


def test_teardown_releases_state(engine_class, model_client, ctx):
  """generate_batch after teardown raises RuntimeError."""
  engine = engine_class()
  engine.setup(model_client, ctx)
  list(engine.generate_batch(1, GenerationConfig(seed=42)))
  engine.teardown()
  with pytest.raises(RuntimeError):
    list(engine.generate_batch(1, GenerationConfig(seed=42)))


def test_context_carries_the_fanout_payload():
  """ADR 0036: a driven child is generated from parent keys; the
    GenerationContext carries the FanoutPlan.to_payload() dict."""
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t"
      },
      "schema": [{
          "name": "ID",
          "type": "STRING",
          "mode": "REQUIRED"
      }]
  })
  ctx = GenerationContext(
      table_schema=schema,
      reference_rows=[{
          "ID": "a"
      }],
      reference_digest="d",
      pipeline_run_id="r",
      fanout={
          "driving_cols": ["ID"],
          "histogram": {
              "1": 1
          },
          "cells": None,
          "exact_cells": False
      },
  )
  # `fanout` is a pydantic field (default None) narrowed by the `is not None`.
  assert ctx.fanout is not None and ctx.fanout["driving_cols"] == ["ID"]  # pylint: disable=unsubscriptable-object


def test_both_engines_implement_generate_for_keys():
  """ADR 0036: a driven child is generated from parent keys; both
    engines must own the entry point (the base refuses)."""
  from sdfb_core.engines import GenerationEngine, get_engine
  from sdfb_core.engines.b1_rag import B1RagEngine
  from sdfb_core.engines.b2_library import B2LibraryEngine

  assert B1RagEngine.generate_for_keys is not GenerationEngine.generate_for_keys
  assert B2LibraryEngine.generate_for_keys is not GenerationEngine.generate_for_keys
  assert get_engine("b1_rag") is B1RagEngine
