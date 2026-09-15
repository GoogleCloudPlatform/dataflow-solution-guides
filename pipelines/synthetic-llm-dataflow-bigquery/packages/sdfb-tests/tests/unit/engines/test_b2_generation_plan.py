"""B.2 mirror of the `generation_plan` milestone (2026-07-29).

Same one-line-per-run contract as b1_rag's: every column mapped to its
generation strategy. B.2 differences: the bulk sampler is the fitted
statistical backend (reported via `backend=`), and free-text pools build
lazily per batch, so there is no `pool_sources` field at setup time.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,missing-class-docstring,protected-access,redefined-outer-name,reimported,unused-argument

from __future__ import annotations

import json
import logging
from datetime import date
from typing import ClassVar

import pytest
from sdfb_core.contracts import TableSchema
from sdfb_core.engines import get_engine
from sdfb_core.engines.b2_library import engine as b2_engine_mod
from sdfb_core.engines.base import GenerationConfig, GenerationContext
from sdfb_core.engines.generation_plan import clear_generation_plan_log

_DIGEST = "digest-b2-plan"
_MODEL = "gs://m/qwen3/v1"


@pytest.fixture(autouse=True)
def _fresh_state():
  clear_generation_plan_log()
  yield
  clear_generation_plan_log()


_SCHEMA = TableSchema.model_validate({
    "table_info": {
        "table_id": "demo.b2_plan_t"
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
  return [{
      "konst": "FIXED",
      "code": ["A", "B", "C"][i % 3],
      "amount": i * 7,
      "event_dt": date(2024, i % 12 + 1, i % 28 + 1),
      "ident": f"ID-{i:05d}",
      "notes": f"reference prose value number {i} long enough to be text",
  } for i in range(n)]


def _ctx() -> GenerationContext:
  return GenerationContext(
      table_schema=_SCHEMA,
      reference_rows=_rows(),
      reference_digest=_DIGEST,
      model_uri=_MODEL,
      pipeline_run_id="run-b2-plan",
      num_rows=40,
  )


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
    return [{"values": [f"gen-{i}" for i in range(32)]}]


def _capture_plans(monkeypatch):
  captured: list[dict] = []
  real = b2_engine_mod.log_milestone

  def _spy(name, **kwargs):
    if name == "generation_plan":
      captured.append(kwargs)
    return real(name, **kwargs)

  monkeypatch.setattr(b2_engine_mod, "log_milestone", _spy)
  return captured


def test_b2_setup_logs_one_generation_plan(monkeypatch):
  plans = _capture_plans(monkeypatch)
  engine = get_engine("b2_library")(use_sdgx=False)
  engine.setup(_StubClient(), _ctx())

  assert len(plans) == 1
  kwargs = plans[0]
  assert kwargs["engine"] == "b2_library"
  assert kwargs["table"] == _SCHEMA.fqn
  assert kwargs["backend"] == "empirical"
  plan = json.loads(kwargs["plan"])
  assert plan == {
      "categorical": ["code"],
      "constant": ["konst"],
      "freetext_llm_pool": ["notes"],
      "numeric": ["amount"],
      "shaped_identifier": ["ident"],
      "temporal": ["event_dt"],
  }


def test_b2_plan_logged_once_per_digest(monkeypatch):
  plans = _capture_plans(monkeypatch)
  get_engine("b2_library")(use_sdgx=False).setup(_StubClient(), _ctx())
  get_engine("b2_library")(use_sdgx=False).setup(_StubClient(), _ctx())
  assert len(plans) == 1


def test_b1_and_b2_plans_do_not_dedupe_each_other(monkeypatch):
  """Same digest + table through BOTH engines must yield BOTH plans —
    the once-guard is keyed per engine."""
  from sdfb_core.engines.b1_rag import engine as b1_engine_mod
  from sdfb_core.engines.b1_rag.engine import clear_free_text_pool_cache

  clear_free_text_pool_cache()
  b2_plans = _capture_plans(monkeypatch)
  b1_plans: list[dict] = []
  real_b1 = b1_engine_mod.log_milestone

  def _b1_spy(name, **kwargs):
    if name == "generation_plan":
      b1_plans.append(kwargs)
    return real_b1(name, **kwargs)

  monkeypatch.setattr(b1_engine_mod, "log_milestone", _b1_spy)

  get_engine("b2_library")(use_sdgx=False).setup(_StubClient(), _ctx())
  get_engine("b1_rag")().setup(_StubClient(), _ctx())
  clear_free_text_pool_cache()
  assert len(b2_plans) == 1
  assert len(b1_plans) == 1


def test_b2_generate_for_keys_matches_the_b1_contract():
  """ADR 0036: both engines are driven the same way."""
  from sdfb_core.contracts import TableSchema
  from sdfb_core.engines import GenerationConfig, GenerationContext, get_engine

  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.src.child_t"
      },
      "schema": [
          {
              "name": "PID",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "CAT",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "AMT",
              "type": "INT64",
              "mode": "REQUIRED"
          },
      ]
  })
  rows = [{
      "PID": f"P{i:04d}",
      "CAT": "abc"[i % 3],
      "AMT": i
  } for i in range(60)]
  ctx = GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="b2-fanout",
      pipeline_run_id="b2-run",
      pk_columns=["PID", "CAT"],
      fanout={
          "driving_cols": ["PID"],
          "histogram": {
              "1": 1,
              "3": 1
          },
          "cells": {
              "cols": ["CAT"],
              "rows": [["a"], ["b"], ["c"]],
              "counts": [1, 1, 1]
          },
          "exact_cells": True
      },
  )

  class _Client:

    def generate_json(self, *, prompt, n=1, **kw):
      return [{"values": []}]

  engine = get_engine("b2_library")(use_sdgx=False)
  engine.setup(_Client(), ctx)
  keys = [(f"K{i}",) for i in range(30)]
  out = [
      r.model_dump() for r in engine.generate_for_keys(
          keys, GenerationConfig(seed=2, batch_size=8))
  ]
  assert out and all((r["PID"],) in keys for r in out)
  per_key: dict = {}
  for r in out:
    per_key.setdefault(r["PID"], []).append(r["CAT"])
  assert all(len(v) == len(set(v)) for v in per_key.values())
  assert {len(v) for v in per_key.values()} <= {1, 3}


def test_b2_fanout_bound_logs_zero_conditional_and_omits_candidate_cap(caplog):
  """ADR 0037 (design §8): `fanout_bound conditional=<n> candidate_cap=`
    — a plan with no conditional edges logs `conditional=0` and omits
    `candidate_cap` entirely. B.2 mirror of the B.1 coverage. Also pins
    the four pre-existing fields (name + value) — review round 1
    should-fix: `fanout_bound` had no prior regression coverage anywhere
    in the suite."""
  from sdfb_core.contracts import TableSchema
  from sdfb_core.engines import GenerationContext, get_engine

  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.src.child_t"
      },
      "schema": [
          {
              "name": "PID",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "CAT",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "AMT",
              "type": "INT64",
              "mode": "REQUIRED"
          },
      ]
  })
  rows = [{
      "PID": f"P{i:04d}",
      "CAT": "abc"[i % 3],
      "AMT": i
  } for i in range(60)]
  ctx = GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="b2-fanout",
      pipeline_run_id="b2-run",
      pk_columns=["PID", "CAT"],
      fanout={
          "driving_cols": ["PID"],
          "histogram": {
              "1": 1,
              "3": 1
          },
          "cells": {
              "cols": ["CAT"],
              "rows": [["a"], ["b"], ["c"]],
              "counts": [1, 1, 1]
          },
          "exact_cells": True
      },
  )

  class _Client:

    def generate_json(self, *, prompt, n=1, **kw):
      return [{"values": []}]

  engine = get_engine("b2_library")(use_sdgx=False)
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    engine.setup(_Client(), ctx)
  assert "name=fanout_bound" in caplog.text
  assert "driving_cols=PID" in caplog.text
  assert "cells=3" in caplog.text
  assert "exact_cells=True" in caplog.text
  assert "mean_fanout=2.0" in caplog.text
  assert "conditional=0" in caplog.text
  assert "candidate_cap=" not in caplog.text


def test_b2_rest_columns_do_not_repeat_across_chunks():
  """ADR 0036: the per-chunk `rest` sampling must consume ONE rng across
    the whole call, not replay a fresh stream per chunk."""
  from sdfb_core.contracts import TableSchema
  from sdfb_core.engines import GenerationConfig, GenerationContext, get_engine

  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.src.child_t"
      },
      "schema": [
          {
              "name": "PID",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "CAT",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "AMT",
              "type": "INT64",
              "mode": "REQUIRED"
          },
      ]
  })
  rows = [{
      "PID": f"P{i:04d}",
      "CAT": "abc"[i % 3],
      "AMT": i
  } for i in range(60)]
  ctx = GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="b2-fanout",
      pipeline_run_id="b2-run",
      pk_columns=["PID", "CAT"],
      fanout={
          "driving_cols": ["PID"],
          "histogram": {
              "3": 1
          },
          "cells": {
              "cols": ["CAT"],
              "rows": [["a"], ["b"], ["c"]],
              "counts": [1, 1, 1]
          },
          "exact_cells": True
      },
  )

  class _Client:

    def generate_json(self, *, prompt, n=1, **kw):
      return [{"values": []}]

  engine = get_engine("b2_library")(use_sdgx=False)
  engine.setup(_Client(), ctx)
  keys = [(f"K{i}",) for i in range(20)]
  out = [
      r.model_dump() for r in engine.generate_for_keys(
          keys, GenerationConfig(seed=2, batch_size=6))
  ]
  amts = [r["AMT"] for r in out]
  assert len(amts) == 60
  assert amts[:6] != amts[6:12]


def test_b2_generate_for_keys_keeps_external_key_pool_tuples():
  """ADR 0036 review I3: a driven child may ALSO carry an enforced
    EXTERNAL edge (a side-input / BQ key pool). `generate_batch` step 2b
    draws those as whole tuples (ADR 0031); `generate_for_keys` skipped the
    block entirely, so those columns fell back to per-column marginals and
    the edge lost referential integrity the moment a table was driven."""
  from sdfb_core.contracts import TableSchema
  from sdfb_core.engines import GenerationConfig, GenerationContext, get_engine

  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.src.child_t"
      },
      "schema": [
          {
              "name": "PID",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "CAT",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "CC",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "BR",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "AMT",
              "type": "INT64",
              "mode": "REQUIRED"
          },
      ]
  })
  # The reference sample's own CC/BR values are NOT in the parent pool —
  # only the tuple draw can put a pool tuple on the row.
  rows = [{
      "PID": f"P{i:04d}",
      "CAT": "abc"[i % 3],
      "CC": "XX",
      "BR": 900 + i % 5,
      "AMT": i
  } for i in range(60)]
  pool_keys = [("ES", 10), ("ES", 11), ("PT", 12)]
  ctx = GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="b2-fanout-fk",
      pipeline_run_id="b2-run",
      pk_columns=["PID", "CAT"],
      fanout={
          "driving_cols": ["PID"],
          "histogram": {
              "3": 1
          },
          "cells": {
              "cols": ["CAT"],
              "rows": [["a"], ["b"], ["c"]],
              "counts": [1, 1, 1]
          },
          "exact_cells": True
      },
      fk_key_pools=[{
          "cols": ["CC", "BR"],
          "keys": [list(k) for k in pool_keys]
      }],
  )

  class _Client:

    def generate_json(self, *, prompt, n=1, **kw):
      return [{"values": []}]

  engine = get_engine("b2_library")(use_sdgx=False)
  engine.setup(_Client(), ctx)
  keys = [(f"K{i}",) for i in range(20)]
  out = [
      r.model_dump() for r in engine.generate_for_keys(
          keys, GenerationConfig(seed=2, batch_size=6))
  ]
  assert len(out) == 60
  assert {(r["CC"], r["BR"]) for r in out} <= set(pool_keys)
  # The driving edge still wins its own columns.
  assert all((r["PID"],) in keys for r in out)


class TestGenerateForKeysConditional:
  """ADR 0037 (design 2026-09-11 §4): B.2 twin of the B.1 conditional-edge
    coverage in `test_b1_rag.py::TestGenerateForKeysConditional` — a
    non-driving FK edge resolved per key from a co-parent's matches."""

  _SCHEMA = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.src.multi_parent_t"
      },
      "schema": [
          {
              "name": "T",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "L",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "R",
              "type": "STRING",
              "mode": "NULLABLE"
          },
          {
              "name": "X",
              "type": "INT64",
              "mode": "REQUIRED"
          },
      ],
  })

  @staticmethod
  def _rows():
    return [{
        "T": f"t{i % 2}",
        "L": f"l{i % 2}",
        "R": f"r{i % 3}",
        "X": i
    } for i in range(30)]

  def _ctx(self, *, nullable: bool) -> GenerationContext:
    return GenerationContext(
        table_schema=self._SCHEMA,
        reference_rows=self._rows(),
        reference_digest="b2-conditional-digest",
        pipeline_run_id="b2-conditional-run",
        fanout={
            "driving_cols": ["T", "L"],
            "histogram": {
                "2": 1
            },
            "conditional": [{
                "id": "(T,R)->right",
                "cols": ["R"],
                "nullable": nullable
            }],
        },
    )

  class _Client:

    def generate_json(self, *, prompt, n=1, **kw):
      return [{"values": [f"gen-{i}" for i in range(32)]}]

  _KEYS: ClassVar[list[tuple]] = [("t1", "l1"), ("t2", "l2")]
  _MATCHES: ClassVar[dict] = {"(T,R)->right": [[("r1",), ("r2",)], []]}

  def test_matched_key_gets_each_candidate_once_unmatched_key_dropped(self):
    engine = get_engine("b2_library")(use_sdgx=False)
    engine.setup(self._Client(), self._ctx(nullable=False))
    cfg = GenerationConfig(seed=1, batch_size=1_000)
    rows = [
        r.model_dump() for r in engine.generate_for_keys(
            self._KEYS, cfg, matches=self._MATCHES)
    ]
    t1_rows = [r for r in rows if r["T"] == "t1"]
    t2_rows = [r for r in rows if r["T"] == "t2"]
    assert len(t1_rows) == 2
    assert {r["R"] for r in t1_rows} == {"r1", "r2"}
    assert not t2_rows

  def test_unmatched_key_on_a_nullable_edge_gets_null(self):
    engine = get_engine("b2_library")(use_sdgx=False)
    engine.setup(self._Client(), self._ctx(nullable=True))
    cfg = GenerationConfig(seed=1, batch_size=1_000)
    rows = [
        r.model_dump() for r in engine.generate_for_keys(
            self._KEYS, cfg, matches=self._MATCHES)
    ]
    t2_rows = [r for r in rows if r["T"] == "t2"]
    assert len(t2_rows) == 2
    assert all(r["R"] is None for r in t2_rows)

  def _capping_ctx(self, landing_table: str) -> GenerationContext:
    """A plan whose per-key capacity (2 cells x 2 candidates) falls
        short of its fan-out (5), so EVERY key caps."""
    return GenerationContext(
        table_schema=self._SCHEMA,
        reference_rows=self._rows(),
        reference_digest="capping-digest",
        pipeline_run_id="capping-run",
        landing_table=landing_table,
        fanout={
            "driving_cols": ["T", "L"],
            "histogram": {
                "5": 1
            },
            "cells": {
                "cols": ["X"],
                "rows": [[1], [2]],
                "counts": [1, 1]
            },
            "exact_cells":
                True,
            "conditional": [{
                "id": "(T,R)->right",
                "cols": ["R"],
                "nullable": False,
                "pk_member": True
            }],
        },
    )

  def test_every_driven_table_reports_its_own_capping(self, caplog):
    """G4: the ``table=`` argument this engine hands
        `conditional_draws` is what scopes the once-per-table
        `fanout_rows_capped` guard (fix wave E3). Nothing drove that
        argument from an ENGINE, so deleting it at this call site
        restored the process-global bucket — every driven table after the
        first capping in silence in a single-job relational run (ADR
        0030) — with the whole suite green.
        """
    from sdfb_core.engines import base as base_mod

    base_mod._reset_rows_capped_log()
    matches = {"(T,R)->right": [[("r1",), ("r2",)], [("r1",), ("r2",)]]}
    cfg = GenerationConfig(seed=1, batch_size=1_000)
    with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
      for table in ("p.land.child_a", "p.land.child_b"):
        engine = get_engine("b2_library")(use_sdgx=False)
        engine.setup(self._Client(), self._capping_ctx(table))
        rows = list(engine.generate_for_keys(self._KEYS, cfg, matches=matches))
        assert len(rows) == 8  # 2 keys x min(5, 2 cells x 2 cands)
    capped = [
        ln for ln in caplog.text.splitlines() if "name=fanout_rows_capped" in ln
    ]
    assert len(capped) == 2  # one per driven table, not one per process

  def test_fanout_bound_logs_conditional_count(self, caplog) -> None:
    engine = get_engine("b2_library")(use_sdgx=False)
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      engine.setup(self._Client(), self._ctx(nullable=False))
    assert "name=fanout_bound" in caplog.text
    assert "conditional=1" in caplog.text

  def test_fanout_bound_logs_candidate_cap_when_present(self, caplog) -> None:
    # `candidate_cap` rides next to `conditional` in the plan payload
    # (Task 6, absent until the launcher writes it).
    engine = get_engine("b2_library")(use_sdgx=False)
    ctx = self._ctx(nullable=False)
    assert ctx.fanout is not None
    ctx = ctx.model_copy(update={"fanout": {**ctx.fanout, "candidate_cap": 64}})
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      engine.setup(self._Client(), ctx)
    assert "candidate_cap=64" in caplog.text
