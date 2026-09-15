"""--generate_fk_relationships + the launcher's relationship card.

The flag is the user-facing switch between relational and isolated
generation; its state must be one greppable milestone, and the model the
run resolved must be readable at a glance in Cloud Logging — tables,
PK, identity, every edge with its state (ADR 0029 flag, ADR 0032 card).
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=missing-class-docstring

from __future__ import annotations

import logging

from sdfb_beam.cli.run_pipeline import (
    log_relationship_model,
    resolve_fk_mode,
)
from sdfb_core.contracts.relationships import RelationshipRegistry
from sdfb_core.observability import log_milestone_text

_MODEL = """
model: sales
description: orders and their parties
tables:
  orders:
    pk: [ID]
    identity: [ORDER_UUID]
    fk:
      - cols: [CUST_ID]
        ref: customers
        ref_cols: [ID]
      - cols: [JOIN_KEY]
        ref: parties
        ref_cols: [JOIN_KEY]
        enforced: false
  customers:
    pk: [ID]
  parties:
    pk: [JOIN_KEY]
"""


def _registry(text: str = _MODEL) -> RelationshipRegistry:
  return RelationshipRegistry.from_sources([("config/relationships/sales.yaml",
                                             text)])


class TestResolveFkMode:

  def test_enabled_passes_parent_landing_through(self):
    landing, mode = resolve_fk_mode(True, "p.landing")
    assert (landing, mode) == ("p.landing", "relational")

  def test_disabled_clears_landing_and_is_isolated(self):
    landing, mode = resolve_fk_mode(False, "p.landing")
    assert (landing, mode) == ("", "isolated")

  def test_disabled_with_empty_flag_still_isolated(self):
    landing, mode = resolve_fk_mode(False, "")
    assert (landing, mode) == ("", "isolated")


class TestLogMilestoneText:

  def test_header_line_plus_raw_body(self, caplog):
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      log_milestone_text(
          "relationship_model", "flowchart BT\n  a --> b", table="p.d.t")
    assert "name=relationship_model" in caplog.text
    assert "flowchart BT\n  a --> b" in caplog.text


class TestRelationshipCard:

  def test_card_carries_keys_edges_and_diagram(self, caplog):
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      log_relationship_model("p.landing.orders", _registry(), mode="relational")
    text = caplog.text
    assert "name=relationship_model" in text
    assert "RELATIONSHIP MODEL sales" in text
    assert "config/relationships/sales.yaml" in text  # provenance
    assert "pk(ID)" in text and "identity(ORDER_UUID)" in text
    assert "-->" in text and "..>" in text  # enforced + documented
    # Pipes and arrows only (2026-09-10 operator ask): no mermaid in
    # any log; `scripts/relationships/card.py --mermaid` renders it.
    assert "mermaid" not in text and "flowchart" not in text
    assert "enforced=1" in text and "documented=1" in text

  def test_a_table_no_model_declares_says_so(self, caplog):
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      log_relationship_model("p.src.unknown", _registry(), mode="relational")
    assert "not in any model" in caplog.text
    assert "model=none" in caplog.text

  def test_mode_milestone_always_present(self, caplog):
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      log_relationship_model("p.landing.orders", _registry(), mode="isolated")
    assert "name=fk_generation_mode" in caplog.text
    assert "mode=isolated" in caplog.text

  def test_documented_only_relational_launch_warns(self, caplog):
    """A relational launch that can enforce NOTHING is the 2026-08-23
        failure mode: it costs a full parent generation and delivers no
        integrity. It must be impossible to miss."""
    only_documented = _MODEL.replace(
        "      - cols: [CUST_ID]\n        ref: customers\n"
        "        ref_cols: [ID]\n",
        "      - cols: [CUST_ID]\n        ref: customers\n"
        "        ref_cols: [ID]\n        enforced: false\n",
    )
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      log_relationship_model(
          "p.landing.orders",
          _registry(only_documented),
          mode="relational",
      )
    assert "WARNING" in caplog.text
    assert "enforced=0" in caplog.text

  def test_disabled_table_is_marked_in_the_card(self, caplog):
    disabled = _MODEL.replace(
        "  customers:\n    pk: [ID]",
        "  customers:\n    enabled: false\n    pk: [ID]",
    )
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      log_relationship_model(
          "p.landing.orders", _registry(disabled), mode="relational")
    assert "DISABLED" in caplog.text
    assert "parent DISABLED — not drawn" in caplog.text
