"""`config/relationships/*.yaml` — the single source of truth (ADR 0032).

PK / identity / FK used to live inside BigQuery table descriptions, so
changing a relationship meant a `bq update` or a `terraform apply` and
the truth was scattered across N tables. It now lives in versioned model
files the repo owns: one file per relational model, readable at a
glance, with a per-table `enabled` flag that DETACHES a subgraph without
deleting anything.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=invalid-name,missing-class-docstring,unbalanced-tuple-unpacking,unused-variable,use-implicit-booleaness-not-comparison

from __future__ import annotations

import pytest
from sdfb_core.contracts.relationships import (
    RelationshipError,
    RelationshipRegistry,
    parse_relationship_model,
)

_RETAIL = """
model: retail
description: orders chain
tables:
  A_TABLE:
    pk: [A_COL_001, A_COL_002]
    identity: [A_COL_009]
  B_TABLE:
    pk: [B_COL_001]
    fk:
      - cols:     [B_COL_006, B_COL_007]
        ref:      A_TABLE
        ref_cols: [A_COL_001, A_COL_002]
  C_TABLE:
    pk: [C_COL_001]
    fk:
      - cols:     [C_COL_004]
        ref:      B_TABLE
        ref_cols: [B_COL_001]
"""

_ISOLATED = """
model: standalone
tables:
  Z_TABLE:
    pk: [Z_COL_001]
"""


def _registry(*texts: str) -> RelationshipRegistry:
  return RelationshipRegistry.from_sources([
      (f"config/relationships/m{i}.yaml", t) for i, t in enumerate(texts)
  ])


class TestParsing:

  def test_reads_pk_identity_and_edges(self):
    model = parse_relationship_model(_RETAIL, source="retail.yaml")
    assert model.model == "retail"
    assert model.tables["A_TABLE"].pk == ("A_COL_001", "A_COL_002")
    assert model.tables["A_TABLE"].identity == ("A_COL_009",)
    edge = model.tables["B_TABLE"].fk[0]
    assert edge.cols == ("B_COL_006", "B_COL_007")
    assert edge.ref == "A_TABLE"
    assert edge.ref_cols == ("A_COL_001", "A_COL_002")

  def test_defaults_are_enabled_and_enforced(self):
    model = parse_relationship_model(_RETAIL, source="retail.yaml")
    assert model.tables["B_TABLE"].enabled is True
    assert model.tables["B_TABLE"].fk[0].enforced is True

  def test_source_is_kept_for_provenance(self):
    model = parse_relationship_model(_RETAIL, source="retail.yaml")
    assert model.source == "retail.yaml"

  @pytest.mark.parametrize(
      "bad,match",
      [
          ("model: m\ntables:\n  T:\n    fk:\n      - cols: [A]\n"
           "        ref: MISSING\n        ref_cols: [A]\n",
           "does not name a table in model"),
          ("model: m\ntables:\n  T:\n    fk:\n      - cols: [A, B]\n"
           "        ref: ds.other\n        ref_cols: [A]\n", "arity"),
          ("model: m\ntables:\n  T:\n    fk:\n      - cols: [A]\n"
           "        ref: T\n        ref_cols: [A]\n", "references itself"),
          ("model: m\ntables:\n  T:\n    fk:\n      - cols: [A]\n"
           "        ref: ds.other\n        ref_cols: [A]\n"
           "      - cols: [A]\n        ref: ds.other\n        ref_cols: [A]\n",
           r"declared twice"),
          ("tables:\n  T:\n    pk: [A]\n", "model"),
      ],
  )
  def test_bad_models_fail_loudly(self, bad, match):
    with pytest.raises(RelationshipError, match=match):
      parse_relationship_model(bad, source="bad.yaml")

  def test_external_parent_needs_no_entry(self):
    """A parent that already landed elsewhere is referenced
        dataset-qualified and is NOT part of this model."""
    model = parse_relationship_model(
        "model: m\ntables:\n  T:\n    pk: [A]\n    fk:\n"
        "      - cols: [A]\n        ref: other_ds.PARENT\n"
        "        ref_cols: [A]\n",
        source="m.yaml",
    )
    assert model.tables["T"].fk[0].ref == "other_ds.PARENT"


class TestRegistryLookup:

  def test_finds_a_table_by_bare_name_and_by_fqn(self):
    reg = _registry(_RETAIL)
    assert reg.relations("B_TABLE") is not None
    assert reg.relations("proj.landing.B_TABLE") is not None
    assert reg.relations("proj.landing.UNKNOWN") is None

  def test_model_name_and_source_are_reachable(self):
    reg = _registry(_RETAIL)
    assert reg.model_for("proj.landing.B_TABLE").model == "retail"

  def test_a_table_declared_twice_is_a_loud_error(self):
    with pytest.raises(RelationshipError, match="declared in 2 models"):
      _registry(_RETAIL, _RETAIL)

  def test_independent_models_coexist(self):
    reg = _registry(_RETAIL, _ISOLATED)
    assert reg.component("Z_TABLE") == ("Z_TABLE",)
    assert len(reg.component("A_TABLE")) == 3


class TestComponentAndDetaching:

  def test_related_tables_travel_together(self):
    assert set(_registry(_RETAIL).component("A_TABLE")) == {
        "A_TABLE", "B_TABLE", "C_TABLE"
    }

  def test_disabling_a_middle_table_detaches_everything_behind_it(self):
    """The worked case: C reaches A only through B. Disable B and a
        launch on A generates A alone — no deletion, one flag."""
    reg = _registry(
        _RETAIL.replace(
            "  B_TABLE:\n    pk: [B_COL_001]",
            "  B_TABLE:\n    enabled: false\n    pk: [B_COL_001]",
        ))
    assert reg.component("A_TABLE") == ("A_TABLE",)
    assert reg.enabled("B_TABLE") is False

  def test_a_disabled_target_still_generates_alone(self):
    """`enabled: false` detaches from the model; it never blocks an
        explicit request to generate that table."""
    reg = _registry(
        _RETAIL.replace(
            "  B_TABLE:\n    pk: [B_COL_001]",
            "  B_TABLE:\n    enabled: false\n    pk: [B_COL_001]",
        ))
    assert reg.component("B_TABLE") == ("B_TABLE",)
    assert reg.relations("B_TABLE").pk == ("B_COL_001",)

  def test_a_documented_edge_still_groups_but_never_enforces(self):
    reg = _registry(
        _RETAIL.replace(
            "        ref_cols: [B_COL_001]",
            "        ref_cols: [B_COL_001]\n        enforced: false",
        ))
    assert set(reg.component("A_TABLE")) == {"A_TABLE", "B_TABLE", "C_TABLE"}
    assert reg.enforced_edges("C_TABLE") == ()

  def test_edges_of_a_disabled_parent_are_not_enforced(self):
    reg = _registry(
        _RETAIL.replace("  A_TABLE:\n    pk:",
                        "  A_TABLE:\n    enabled: false\n    pk:"))
    assert reg.enforced_edges("B_TABLE") == ()


class TestOrdering:

  def test_parents_generate_first(self):
    reg = _registry(_RETAIL)
    order = reg.generation_order(reg.component("C_TABLE"))
    assert order.index("A_TABLE") < order.index("B_TABLE")
    assert order.index("B_TABLE") < order.index("C_TABLE")

  def test_enforced_cycles_are_rejected_at_load(self):
    cyclic = ("model: m\ntables:\n"
              "  T1:\n    fk:\n      - cols: [A]\n        ref: T2\n"
              "        ref_cols: [A]\n"
              "  T2:\n    fk:\n      - cols: [A]\n        ref: T1\n"
              "        ref_cols: [A]\n")
    with pytest.raises(RelationshipError, match="cycle"):
      _registry(cyclic)


class TestVisualCard:

  def test_card_shows_everything_at_a_glance(self):
    card = _registry(_RETAIL).card("A_TABLE")
    assert "retail" in card
    assert "config/relationships/m0.yaml" in card  # provenance
    assert "pk(A_COL_001,A_COL_002)" in card
    assert "identity(A_COL_009)" in card
    assert "-->" in card  # enforced edge arrow
    assert "B_COL_006,B_COL_007" in card
    assert "wave 0" in card and "wave 1" in card

  def test_card_marks_disabled_and_documented(self):
    reg = _registry(
        _RETAIL.replace(
            "  C_TABLE:\n    pk: [C_COL_001]",
            "  C_TABLE:\n    enabled: false\n    pk: [C_COL_001]",
        ).replace(
            "        ref_cols: [A_COL_001, A_COL_002]",
            "        ref_cols: [A_COL_001, A_COL_002]\n"
            "        enforced: false",
        ))
    card = reg.card("A_TABLE")
    assert "DISABLED" in card
    assert "detached" in card
    assert "..>" in card  # documented edge arrow
    assert "documented" in card


class TestDiagram:

  def test_mermaid_marks_enforced_documented_and_disabled(self):
    reg = _registry(
        _RETAIL.replace(
            "  C_TABLE:\n    pk: [C_COL_001]",
            "  C_TABLE:\n    enabled: false\n    pk: [C_COL_001]",
        ))
    src = reg.mermaid("A_TABLE")
    assert src.startswith("flowchart BT")
    assert "🗄️ A_TABLE" in src
    assert "🚫 C_TABLE (disabled)" in src
    assert "-->" in src
    assert "classDef store" in src

  def test_log_body_carries_card_then_fenced_mermaid(self):
    body = _registry(_RETAIL).log_body("A_TABLE")
    assert body.index("RELATIONSHIP MODEL") < body.index("```mermaid")
    assert body.rstrip().endswith("```")

  def test_a_table_outside_every_model_still_renders_a_card(self):
    reg = _registry(_RETAIL)
    assert "not in any model" in reg.log_body("proj.d.UNRELATED")


class TestWaves:

  def test_independent_tables_share_a_wave(self):
    reg = _registry(_RETAIL, _ISOLATED)
    waves = reg.generation_waves(("A_TABLE", "B_TABLE", "Z_TABLE"))
    assert waves[0] == ("A_TABLE", "Z_TABLE")  # both parentless
    assert waves[1] == ("B_TABLE",)

  def test_order_is_the_flattened_waves(self):
    reg = _registry(_RETAIL)
    component = reg.component("A_TABLE")
    flat = [t for w in reg.generation_waves(component) for t in w]
    assert tuple(flat) == reg.generation_order(component)


class TestEdgeRoles:
  """Design 2026-09-10 (ADR 0036): one DRIVING edge per child — the
    parent whose keys the child is generated from; every other enforced
    in-model edge must be IMPLIED (its columns are carried by the driving
    parent from that other parent), else the launch stops. Today the
    engine writes each edge's columns in turn and the last one wins."""

  _THREE = """
model: kw
tables:
  B_TABLE:
    pk: [D_COL_001]
  C_TABLE:
    pk: [D_COL_001, C_COL_002, D_COL_018]
    fk:
      - cols: [D_COL_001, D_COL_024, D_COL_025, C_COL_009]
        ref: B_TABLE
        ref_cols: [D_COL_001, D_COL_024, D_COL_025, C_COL_009]
  A_TABLE:
    pk: [D_COL_024, D_COL_025, C_COL_009, C_COL_045, A_COL_005]
    fk:
      - cols: [D_COL_024, D_COL_025, C_COL_009]
        ref: C_TABLE
        ref_cols: [D_COL_024, D_COL_025, C_COL_009]
        drives: true
      - cols: [D_COL_024, D_COL_025, C_COL_009]
        ref: B_TABLE
        ref_cols: [D_COL_024, D_COL_025, C_COL_009]
"""

  def _registry(self, text: str) -> RelationshipRegistry:
    return RelationshipRegistry.from_sources([("config/relationships/kw.yaml",
                                               text)])

  def test_single_edge_drives_by_itself(self):
    reg = self._registry(self._THREE)
    (edge,) = reg.enforced_edges("C_TABLE")
    assert reg.edge_roles("C_TABLE") == {edge: "driving"}
    assert reg.driving_edge("C_TABLE") == edge

  def test_marked_edge_drives_and_the_other_is_implied(self):
    reg = self._registry(self._THREE)
    to_c, to_b = reg.enforced_edges("A_TABLE")
    assert reg.edge_roles("A_TABLE") == {to_c: "driving", to_b: "implied"}

  def test_root_has_no_driving_edge(self):
    reg = self._registry(self._THREE)
    assert reg.driving_edge("B_TABLE") is None
    assert reg.edge_roles("B_TABLE") == {}

  def test_two_unmarked_edges_resolve_when_one_parent_descends_from_the_other(
      self):
    # ADR 0036 rev 2: no `drives:` needed — C_TABLE descends from
    # B_TABLE, so C_TABLE's edge drives and B_TABLE's is implied.
    text = self._THREE.replace("        drives: true\n", "")
    reg = self._registry(text)
    to_c, to_b = reg.enforced_edges("A_TABLE")
    assert reg.edge_roles("A_TABLE") == {to_c: "driving", to_b: "implied"}

  def test_a_narrow_parent_edge_is_widened_from_the_childs_pins(self):
    # C_TABLE's declared edge carries only D_COL_001; A_TABLE references
    # (D_COL_024, D_COL_025, C_COL_009) in BOTH C_TABLE and B_TABLE, so
    # the registry widens C_TABLE's edge with those pairs (rev 2).
    text = self._THREE.replace(
        "      - cols: [D_COL_001, D_COL_024, D_COL_025, C_COL_009]\n"
        "        ref: B_TABLE\n"
        "        ref_cols: [D_COL_001, D_COL_024, D_COL_025, C_COL_009]\n",
        "      - cols: [D_COL_001]\n        ref: B_TABLE\n        ref_cols: [D_COL_001]\n",
    )
    reg = self._registry(text)
    (edge,) = reg.enforced_edges("C_TABLE")
    assert edge.cols == ("D_COL_001", "D_COL_024", "D_COL_025", "C_COL_009")
    to_c, to_b = reg.enforced_edges("A_TABLE")
    assert reg.edge_roles("A_TABLE") == {to_c: "driving", to_b: "implied"}

  def test_external_edges_are_external(self):
    text = """
model: m
tables:
  T:
    pk: [ID]
    fk:
      - cols: [X]
        ref: ds.other
        ref_cols: [X]
"""
    reg = self._registry(text)
    (edge,) = reg.enforced_edges("T")
    assert reg.edge_roles("T") == {edge: "external"}
    assert reg.driving_edge("T") is None

  def test_external_edge_next_to_a_driving_edge_keeps_its_role_and_reports_overlap(
      self):
    # A table can legally have both an internal driving edge and an
    # external one (a parent landed outside this model) that shares
    # a column with it. The registry never routes the external edge
    # (the launcher only logs the overlap, Task 6).
    text = """
model: m
tables:
  P:
    pk: [ID]
  T:
    pk: [ID, X]
    fk:
      - cols: [ID]
        ref: P
        ref_cols: [ID]
      - cols: [ID, X]
        ref: ds.other
        ref_cols: [ID, X]
"""
    reg = self._registry(text)
    to_p, to_ext = reg.enforced_edges("T")
    assert reg.edge_roles("T") == {to_p: "driving", to_ext: "external"}
    assert reg.edge_overlap("T", to_ext) == ("ID",)

  def test_card_names_the_roles(self):
    card = self._registry(self._THREE).card("A_TABLE")
    assert "[enforced, DRIVES]" in card
    assert "[enforced, implied via C_TABLE]" in card

  def test_diamond_ancestry_is_implied_through_the_reaching_branch(self):
    # ADR 0036 fix: _carries must track (table, cols) not just table,
    # so a table visited with one column set that fails doesn't poison
    # a later branch with different column names that succeeds.
    text = """
model: diamond
tables:
  TARGET:
    pk: [K, Z]
  SH:
    pk: [K, Z]
    fk:
      - cols: [K, Z]
        ref: TARGET
        ref_cols: [K, Z]
  P:
    pk: [K, Z]
    fk:
      - cols: [K, Z]
        ref: SH
        ref_cols: [A, B]
  Q:
    pk: [K, Z]
    fk:
      - cols: [K, Z]
        ref: SH
        ref_cols: [K, Z]
  M:
    pk: [K, Z]
    fk:
      - cols: [K, Z]
        ref: P
        ref_cols: [K, Z]
      - cols: [K, Z]
        ref: Q
        ref_cols: [K, Z]
  CHILD:
    pk: [K, Z, C]
    fk:
      - cols: [K, Z]
        ref: M
        ref_cols: [K, Z]
        drives: true
      - cols: [K, Z]
        ref: TARGET
        ref_cols: [K, Z]
"""
    reg = self._registry(text)
    to_m, to_target = reg.enforced_edges("CHILD")
    assert reg.edge_roles("CHILD") == {to_m: "driving", to_target: "implied"}


class TestDerivedRolesFromToggledTables:
  """2026-09-10 launch …-8177138577202163642: the operator flipped
    B_TABLE to `enabled: true` in the real model and preflight stopped
    A_TABLE for two enforced edges with no `drives:`. Toggling tables is
    the model's whole point (ADR 0032), so the registry must resolve
    what the DAG already says: the DRIVING parent is the candidate that
    itself descends from every other candidate (C_TABLE -> B_TABLE), and
    the other edge is implied once the driving parent's edge to that
    parent is WIDENED with the column pairs the child's two edges pin
    (C.(C_COL_006,C_COL_007,C_COL_009) == B.(B_COL_023,B_COL_011,B_COL_013))."""

  _KW = """
model: kw
tables:
  B_TABLE:
    pk: [B_COL_008]
  C_TABLE:
    pk: [C_COL_001, C_COL_002, C_COL_004]
    fk:
      - cols: [C_COL_001]
        ref: B_TABLE
        ref_cols: [B_COL_008]
  A_TABLE:
    pk: [A_COL_001, A_COL_002, A_COL_003, A_COL_004, A_COL_005]
    fk:
      - cols: [A_COL_001, A_COL_002, A_COL_003]
        ref: C_TABLE
        ref_cols: [C_COL_006, C_COL_007, C_COL_009]
      - cols: [A_COL_001, A_COL_002, A_COL_003]
        ref: B_TABLE
        ref_cols: [B_COL_023, B_COL_011, B_COL_013]
"""

  def _registry(self, text: str) -> RelationshipRegistry:
    return RelationshipRegistry.from_sources([("config/relationships/kw.yaml",
                                               text)])

  def test_the_toggled_model_resolves_without_drives(self):
    reg = self._registry(self._KW)
    to_c, to_b = reg.enforced_edges("A_TABLE")
    assert reg.edge_roles("A_TABLE") == {to_c: "driving", to_b: "implied"}

  def test_the_driving_parents_edge_is_widened_with_the_inherited_columns(self):
    reg = self._registry(self._KW)
    (edge,) = reg.enforced_edges("C_TABLE")
    assert edge.cols == ("C_COL_001", "C_COL_006", "C_COL_007", "C_COL_009")
    assert edge.ref_cols == ("B_COL_008", "B_COL_023", "B_COL_011", "B_COL_013")
    (rec,) = reg.derived_widenings()
    assert rec["table"] == "C_TABLE" and rec["ref"] == "B_TABLE"
    assert rec["via"] == "A_TABLE"
    assert rec["added"] == [("C_COL_006", "B_COL_023"),
                            ("C_COL_007", "B_COL_011"),
                            ("C_COL_009", "B_COL_013")]

  def test_edge_overlap_and_rest_recognize_a_widened_driving_edge_by_value(
      self):
    # C_TABLE's driving edge is WIDENED (via A_TABLE's pins, previous
    # test) into a fresh FkEdge object on every enforced_edges() /
    # edge_roles() call. edge_overlap/edge_rest must recognize "this
    # IS the driving edge" by value, not by Python object identity,
    # or a widened driving edge is silently misreported as
    # conditional on all of its own columns (review round 1).
    reg = self._registry(self._KW)
    roles = reg.edge_roles("C_TABLE")
    for edge, role in roles.items():
      assert role == "driving"
      assert reg.edge_overlap("C_TABLE", edge) == ()
      assert reg.edge_rest("C_TABLE", edge) == ()

    # Regression check for the fix: A_TABLE's genuinely IMPLIED edge
    # (to_b) must still overlap the driving edge on its whole column
    # tuple (a subset by construction) with an empty rest — it must
    # not itself be mistaken for the driving edge once the check
    # switches from `is` to `==`.
    _to_c, to_b = reg.enforced_edges("A_TABLE")
    assert reg.edge_roles("A_TABLE")[to_b] == "implied"
    assert reg.edge_overlap("A_TABLE", to_b) == to_b.cols
    assert reg.edge_rest("A_TABLE", to_b) == ()

  def test_widened_resolves_a_declaration_to_the_edge_the_launch_draws(self):
    # The launcher (`fk_edge_metadata`) has to name the WIDENED edge
    # a declaration became to look its role up — that must go through
    # a PUBLIC method, not a private one across the package boundary
    # (ADR 0037 review, ruling 11).
    reg = self._registry(self._KW)
    (declared,) = reg.relations("C_TABLE").fk
    (enforced,) = reg.enforced_edges("C_TABLE")
    assert declared != enforced  # this one IS widened, via A_TABLE
    assert reg.widened("C_TABLE", declared) == enforced

    # Nothing widens A_TABLE's own edges: each comes back equal to
    # itself, so the launcher can call `widened` on every edge.
    for edge in reg.relations("A_TABLE").fk:
      assert reg.widened("A_TABLE", edge) == edge

  def test_widened_leaves_a_documented_edge_alone(self):
    # A documented edge next to the widened enforced one: the launch
    # never draws it, so it is never widened either (its declared
    # columns are what the card and the metadata show).
    text = self._KW.replace(
        """      - cols: [C_COL_001]
        ref: B_TABLE
        ref_cols: [B_COL_008]
""",
        """      - cols: [C_COL_001]
        ref: B_TABLE
        ref_cols: [B_COL_008]
      - cols: [C_COL_003]
        ref: B_TABLE
        ref_cols: [B_COL_008]
        enforced: false
""",
    )
    reg = self._registry(text)
    documented = next(e for e in reg.relations("C_TABLE").fk if not e.enforced)
    assert reg.widened("C_TABLE", documented) == documented

  def test_the_declared_model_is_untouched_and_the_sha_is_stable(self):
    reg = self._registry(self._KW)
    declared = reg.relations("C_TABLE").fk
    assert declared[0].cols == ("C_COL_001",
                               )  # widening is derived, not written back
    assert reg.sha12() == self._registry(self._KW).sha12()

  def test_card_shows_the_widening(self):
    card = self._registry(self._KW).card("A_TABLE")
    assert "DRIVES" in card
    assert "widened via A_TABLE" in card

  def test_unrelated_parents_default_to_the_first_declared_edge(self):
    text = """
model: m
tables:
  P:
    pk: [K]
  Q:
    pk: [K]
  CHILD:
    pk: [K, X]
    fk:
      - cols: [K]
        ref: P
        ref_cols: [K]
      - cols: [K]
        ref: Q
        ref_cols: [K]
"""
    reg = self._registry(text)
    to_p, to_q = reg.enforced_edges("CHILD")
    assert reg.edge_roles("CHILD") == {to_p: "driving", to_q: "conditional"}
    assert reg.driving_choice("CHILD") == "first_declared"
    assert reg.edge_overlap("CHILD", to_q) == ("K",)
    assert reg.edge_rest("CHILD", to_q) == ()
    assert "DRIVES (first declared" in reg.card("CHILD")
    assert "conditional on (K)" in reg.card("CHILD")

  def test_no_edge_between_the_parents_cannot_be_widened(self):
    # C_TABLE has NO edge to B_TABLE: nothing to widen, and neither
    # parent descends from the other, so ruling A applies (ADR 0037) —
    # the first declared edge (to C_TABLE) drives and the other is
    # conditional on the columns they share.
    text = self._KW.replace(
        "    fk:\n      - cols: [C_COL_001]\n        ref: B_TABLE\n        ref_cols: [B_COL_008]\n",
        "")
    reg = self._registry(text)
    to_c, to_b = reg.enforced_edges("A_TABLE")
    assert reg.edge_roles("A_TABLE") == {to_c: "driving", to_b: "conditional"}
    assert reg.driving_choice("A_TABLE") == "first_declared"


class TestDrivingChoiceLabels:
  """Rule 3 ("the most-derived parent drives") needs a real CHOICE.

    With two enforced edges to the SAME parent there is one candidate
    parent, so `all(self._descends(...))` runs over an EMPTY set and is
    vacuously true for every edge: rule 3 fired, returned the first
    declared edge, and labelled it `"derived"` — though nothing was
    derived and nothing was widened. The operator then got neither the
    `fk_driving_edge_defaulted` WARNING nor the card's
    `DRIVES (first declared …)` tag that rule 4 promises, and had no
    hint that `drives: true` was theirs to set (ADR 0037 final review).
    """

  _SAME_PARENT = """
model: sp
tables:
  P:
    pk: [K1, K2]
  CHILD:
    pk: [A, B]
    fk:
      - cols: [A]
        ref: P
        ref_cols: [K1]
      - cols: [B]
        ref: P
        ref_cols: [K2]
"""

  # Two DISTINCT parents with ancestry between them: C_TABLE holds an
  # edge to B_TABLE, so C_TABLE is the most-derived parent and rule 3
  # genuinely decides.
  _ANCESTRY = """
model: an
tables:
  B_TABLE:
    pk: [B_COL_008]
  C_TABLE:
    pk: [C_COL_001]
    fk:
      - cols: [C_COL_001]
        ref: B_TABLE
        ref_cols: [B_COL_008]
  A_TABLE:
    pk: [A_COL_001, A_COL_002]
    fk:
      - cols: [A_COL_001]
        ref: B_TABLE
        ref_cols: [B_COL_008]
      - cols: [A_COL_001]
        ref: C_TABLE
        ref_cols: [C_COL_001]
"""

  def _registry(self, text: str) -> RelationshipRegistry:
    return RelationshipRegistry.from_sources([
        ("config/relationships/labels.yaml", text)
    ])

  def test_two_edges_to_one_parent_default_instead_of_deriving(self):
    reg = self._registry(self._SAME_PARENT)
    to_a, _to_b = reg.enforced_edges("CHILD")
    assert reg.driving_choice("CHILD") == "first_declared"
    assert reg.edge_roles("CHILD")[to_a] == "driving"
    # …and the operator is told, on the card, that the launch chose
    # for them and how to choose themselves.
    assert "DRIVES (first declared" in reg.card("CHILD")

  def test_two_distinct_parents_with_ancestry_still_derive(self):
    reg = self._registry(self._ANCESTRY)
    _to_b, to_c = reg.enforced_edges("A_TABLE")
    assert reg.driving_choice("A_TABLE") == "derived"
    # The most-derived parent drives even though it is declared
    # SECOND — that is the whole point of rule 3.
    assert reg.edge_roles("A_TABLE")[to_c] == "driving"
    assert "DRIVES (first declared" not in reg.card("A_TABLE")
