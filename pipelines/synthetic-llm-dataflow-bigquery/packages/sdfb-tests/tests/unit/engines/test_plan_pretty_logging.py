"""Compact relational log entries (ADR 0028 follow-up, ADR 0035 rev).

Operators inspecting a Dataflow run read the `generation_plan` milestone
as one 8k-char single-line JSON blob, and nothing in the worker log ties
the clauses + landing table + FK parents together. The first answer was
two once-per-plan PRETTY entries (indent-2 JSON + a fenced mermaid card).
Eight engine instances per table echoed them, and on the 2026-09-09
three-table runs they were 40% of the worker log by bytes and buried
every other line. They are gone: one single-line `relational_e2e`, one
single-line `relational_fk_edge` per edge, and the pipe/arrow card
without the mermaid fence.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=missing-class-docstring,unused-argument

from __future__ import annotations

import logging
import re

import pytest
from sdfb_core.contracts import TableSchema
from sdfb_core.contracts.relationships import RelationshipRegistry
from sdfb_core.engines import GenerationContext, get_engine
from sdfb_core.engines.b1_rag import B1RagEngine, HashingEmbedder
from sdfb_core.engines.generation_plan import clear_generation_plan_log

_C2E = ('{"llm_prompt_constraint": {"route": "llm", '
        '"pattern": "^(C2E[13][0-9A-F]{20}|7301[0-9A-F]{20})$"}}')


@pytest.fixture(autouse=True)
def _fresh_state():
  clear_generation_plan_log()
  yield
  clear_generation_plan_log()


_SCHEMA = TableSchema.model_validate({
    "table_info": {
        "table_id": "p.src.child_t"
    },
    "schema": [
        {
            "name": "KEY",
            "type": "STRING",
            "mode": "REQUIRED",
            "description": _C2E,
        },
        {
            "name": "PARENT_ID",
            "type": "STRING",
            "mode": "REQUIRED"
        },
    ],
})


def _rows(n: int = 60) -> list[dict]:
  return [{"KEY": f"C2E3{i:020X}", "PARENT_ID": f"P{i % 7}"} for i in range(n)]


_REGISTRY = RelationshipRegistry.from_sources([(
    "config/relationships/retail.yaml",
    """
model: retail
tables:
  parent_t:
    pk: [PARENT_ID]
  child_t:
    pk: [KEY]
    fk:
      - cols: [PARENT_ID]
        ref: parent_t
        ref_cols: [PARENT_ID]
""",
)])
# What the launcher used to hand the worker: card + fenced mermaid.
_CARD_WITH_MERMAID = _REGISTRY.log_body("child_t")


class _StubClient:

  def generate_json(self, *, prompt: str, n: int = 1, **kwargs):
    return [{"values": [f"gen-{i}" for i in range(32)]}]


def _ctx() -> GenerationContext:
  return GenerationContext(
      table_schema=_SCHEMA,
      reference_rows=_rows(),
      reference_digest="pretty-digest",
      pipeline_run_id="pretty-run",
      pk_columns=["KEY"],
      landing_table="p.landing.child_t",
      fk_edges=[{
          "cols": ["PARENT_ID"],
          "ref": "src.parent_t",
          "parent_landing": "p.landing.parent_t",
      }],
      fk_pools={"PARENT_ID": tuple(f"P{i}" for i in range(7))},
      relationship_card=_CARD_WITH_MERMAID,
  )


def _b1_setup(caplog) -> None:
  engine = B1RagEngine(embedder=HashingEmbedder())
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    engine.setup(_StubClient(), _ctx())


def _entries(caplog, name: str) -> list[str]:
  """The full message of every milestone entry with this name."""
  return [
      r.getMessage()
      for r in caplog.records
      if f"name={name} " in r.getMessage() or
      r.getMessage().endswith(f"name={name}")
  ]


class TestCompactRelationalEntries:

  def test_no_pretty_json_entries(self, caplog) -> None:
    _b1_setup(caplog)
    assert "name=generation_plan_pretty" not in caplog.text
    # The compact plan milestone is still the plan's point of truth.
    assert "name=generation_plan " in caplog.text

  def test_relational_e2e_is_one_line(self, caplog) -> None:
    _b1_setup(caplog)
    (entry,) = _entries(caplog, "relational_e2e")
    assert "\n" not in entry
    assert "landing=p.landing.child_t" in entry
    assert "pk=KEY" in entry
    assert "fk_edges=1" in entry

  def test_one_line_per_fk_edge(self, caplog) -> None:
    _b1_setup(caplog)
    (edge,) = _entries(caplog, "relational_fk_edge")
    assert "\n" not in edge
    assert "cols=PARENT_ID" in edge
    assert "parent_landing=p.landing.parent_t" in edge
    assert "pool_size=7" in edge
    assert "active=True" in edge

  def test_entries_log_once_per_plan(self, caplog) -> None:
    _b1_setup(caplog)
    _b1_setup(caplog)  # same digest+table: the once-guard holds
    assert len(_entries(caplog, "relational_e2e")) == 1
    assert len(_entries(caplog, "relational_fk_edge")) == 1


class TestEdgeModeObservability:
  """ADR 0037 (design §8): `relational_fk_edge mode=side_input|conditional
    overlap=` names the DAG path each edge took — the driving/implied
    modes already exist on `FkEdgeSpec.mode` and pass through unchanged;
    this covers the two new roles plus the pre-ADR-0037 default."""

  @staticmethod
  def _ctx_for_edge(edge: dict) -> GenerationContext:
    return GenerationContext(
        table_schema=_SCHEMA,
        reference_rows=_rows(),
        reference_digest="edge-mode-digest",
        pipeline_run_id="edge-mode-run",
        pk_columns=["KEY"],
        landing_table="p.landing.child_t",
        fk_edges=[edge],
        fk_pools={"PARENT_ID": tuple(f"P{i}" for i in range(7))},
    )

  def test_conditional_edge_logs_mode_and_overlap(self, caplog) -> None:
    engine = B1RagEngine(embedder=HashingEmbedder())
    edge = {
        "cols": ["PARENT_ID"],
        "ref": "src.parent_t",
        "parent_landing": "p.landing.parent_t",
        "mode": "conditional",
        "overlap": ["T_COL"],
    }
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      engine.setup(_StubClient(), self._ctx_for_edge(edge))
    (entry,) = _entries(caplog, "relational_fk_edge")
    assert "mode=conditional" in entry
    assert "overlap=T_COL" in entry

  def test_independent_edge_logs_mode_side_input_without_overlap(
      self, caplog) -> None:
    engine = B1RagEngine(embedder=HashingEmbedder())
    edge = {
        "cols": ["PARENT_ID"],
        "ref": "src.parent_t",
        "parent_landing": "p.landing.parent_t",
        "mode": "side_input",
    }
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      engine.setup(_StubClient(), self._ctx_for_edge(edge))
    (entry,) = _entries(caplog, "relational_fk_edge")
    assert "mode=side_input" in entry
    assert "overlap=" not in entry

  def test_edge_without_mode_key_defaults_to_side_input(self, caplog) -> None:
    # Legacy metadata (pre-ADR-0037, or a launcher not yet on Task 6):
    # an edge dict with no "mode" key must not crash and must default
    # to the pre-existing side-input path.
    engine = B1RagEngine(embedder=HashingEmbedder())
    edge = {
        "cols": ["PARENT_ID"],
        "ref": "src.parent_t",
        "parent_landing": "p.landing.parent_t",
    }
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      engine.setup(_StubClient(), self._ctx_for_edge(edge))
    (entry,) = _entries(caplog, "relational_fk_edge")
    assert "mode=side_input" in entry
    assert "overlap=" not in entry


class TestWorkerRelationshipCard:
  """ADR 0032 — the worker echoes the card the LAUNCHER rendered from
    `config/relationships/`. Pipes and arrows are enough at 3am; the
    mermaid fence stays in the launcher entry for the report tooling."""

  def test_card_is_echoed_without_mermaid(self, caplog) -> None:
    _b1_setup(caplog)
    (entry,) = _entries(caplog, "relationship_model")
    assert "RELATIONSHIP MODEL retail" in entry
    assert "wave 0 | parent_t" in entry
    assert "-->" in entry
    assert "mermaid" not in entry
    assert "flowchart" not in entry

  def test_card_logs_once_per_plan(self, caplog) -> None:
    _b1_setup(caplog)
    _b1_setup(caplog)
    assert len(_entries(caplog, "relationship_model")) == 1

  def test_no_card_means_no_entry(self, caplog) -> None:
    """A table outside every model carries no card — the worker log
        stays quiet instead of printing an empty block."""
    engine = B1RagEngine(embedder=HashingEmbedder())
    ctx = _ctx().model_copy(update={"relationship_card": ""})
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      engine.setup(_StubClient(), ctx)
    assert "name=relationship_model" not in caplog.text


def test_b2_engine_emits_the_same_compact_entries(caplog) -> None:
  engine = get_engine("b2_library")(use_sdgx=False)
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    engine.setup(_StubClient(), _ctx())
  assert "name=generation_plan_pretty" not in caplog.text
  assert len(_entries(caplog, "relational_e2e")) == 1
  assert len(_entries(caplog, "relational_fk_edge")) == 1


class TestJointKeyPoolsInThePlan:
  """ADR 0031 — `relational_fk_edge` is the point of truth for what an
    edge is drawing from. A per-column `pool_size` misreports a
    composite edge badly: the 2026-08-23 edge's first column is
    CONSTANT, so it would have read `pool_size: 1` for a pool of a
    million key tuples."""

  @staticmethod
  def _ctx_with_joint_edge():
    schema = TableSchema.model_validate({
        "table_info": {
            "table_id": "p.src.child_t"
        },
        "schema": [
            {
                "name": "KEY",
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
        ],
    })
    rows = [{
        "KEY": f"K{i:04d}",
        "CC": "ES",
        "BR": 10 + (i % 2)
    } for i in range(40)]
    return GenerationContext(
        table_schema=schema,
        reference_rows=rows,
        reference_digest="joint-digest",
        pipeline_run_id="joint-run",
        pk_columns=["KEY"],
        landing_table="p.landing.child_t",
        fk_edges=[{
            "cols": ["CC", "BR"],
            "ref": "landing.parent_t",
            "ref_cols": ["CC", "BR"],
            "parent_landing": "p.landing.parent_t",
        }],
        fk_key_pools=[{
            "cols": ["CC", "BR"],
            # CC is constant — a per-column view reads "1".
            "keys": [("ES", 10), ("ES", 11), ("ES", 12)],
        }],
    )

  def test_edge_reports_key_tuples_not_first_column_distinct(self, caplog):
    engine = B1RagEngine(embedder=HashingEmbedder())
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      engine.setup(_StubClient(), self._ctx_with_joint_edge())
    (edge,) = _entries(caplog, "relational_fk_edge")
    assert re.search(r"\bcols=CC,BR\b", edge)
    assert "key_tuples=3" in edge
    assert "joint=True" in edge
    assert "active=True" in edge
