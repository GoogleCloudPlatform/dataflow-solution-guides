"""Every relational SHAPE a model file can declare, through the registry
(ADR 0032/0036/0037): what the launcher resolves — waves, driving/
implied/independent/conditional roles — with no `drives:` marker and no
model edit beyond `enabled`.

The 2026-09-11 5-table expansion showed the launch stopping on shapes
the model can legitimately declare. Each case here is one shape,
including a child with two parents that are not on one ancestry line —
the star-schema fact table and the true diamond — resolved by the
`independent` and `conditional` roles (ADR 0037).
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=invalid-name,missing-class-docstring,unused-variable,use-implicit-booleaness-not-comparison

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

import pytest
from sdfb_core.contracts.relationships import RelationshipError, RelationshipRegistry


def _registry(text: str) -> RelationshipRegistry:
  return RelationshipRegistry.from_sources([("config/relationships/shape.yaml",
                                             text)])


def _roles(reg: RelationshipRegistry, table: str) -> dict[str, str]:
  return {
      f"({','.join(e.cols)})->{e.ref}": role
      for e, role in reg.edge_roles(table).items()
  }


def _tables(reg: RelationshipRegistry) -> tuple[str, ...]:
  return tuple(t for m in reg.models for t in m.tables)


class TestShapesTheRegistryResolves:

  def test_star_hub_one_parent_many_children(self):
    reg = _registry("""
model: s
tables:
  hub: {pk: [H]}
  c1: {pk: [H, X], fk: [{cols: [H], ref: hub, ref_cols: [H]}]}
  c2: {pk: [H, Y], fk: [{cols: [H], ref: hub, ref_cols: [H]}]}
  c3: {pk: [H], fk: [{cols: [H], ref: hub, ref_cols: [H]}]}
""")
    assert reg.generation_waves(_tables(reg)) == (("hub",), ("c1", "c2", "c3"))
    for child in ("c1", "c2", "c3"):
      assert _roles(reg, child) == {"(H)->hub": "driving"}

  def test_chain_and_tree(self):
    reg = _registry("""
model: t
tables:
  root: {pk: [R]}
  mid: {pk: [R, M], fk: [{cols: [R], ref: root, ref_cols: [R]}]}
  leaf1: {pk: [R, M, L], fk: [{cols: [R, M], ref: mid, ref_cols: [R, M]}]}
  leaf2: {pk: [R, M, Q], fk: [{cols: [R, M], ref: mid, ref_cols: [R, M]}]}
""")
    assert reg.generation_waves(_tables(reg)) == (("root",), ("mid",),
                                                  ("leaf1", "leaf2"))
    assert _roles(reg, "leaf1") == {"(R,M)->mid": "driving"}

  def test_forest_of_components_with_a_one_to_one_chain(self):
    reg = _registry("""
model: c
tables:
  a: {pk: [A]}
  b: {pk: [A, B], fk: [{cols: [A], ref: a, ref_cols: [A]}]}
  e: {pk: [E]}
  f: {pk: [E], fk: [{cols: [E], ref: e, ref_cols: [E]}]}
""")
    assert reg.component("a") == ("a", "b")
    assert reg.component("e") == ("e", "f")
    assert reg.generation_waves(_tables(reg)) == (("a", "e"), ("b", "f"))
    # f's PK IS its driving edge: a true 1:1 child (E_TABLE, 2026-09-11).
    assert _roles(reg, "f") == {"(E)->e": "driving"}

  def test_grandparent_edge_next_to_the_parent_edge_is_implied(self):
    reg = _registry("""
model: g
tables:
  gp: {pk: [G]}
  p: {pk: [G, P], fk: [{cols: [G], ref: gp, ref_cols: [G]}]}
  c:
    pk: [G, P, C]
    fk:
      - {cols: [G, P], ref: p, ref_cols: [G, P]}
      - {cols: [G], ref: gp, ref_cols: [G]}
""")
    assert _roles(reg, "c") == {"(G,P)->p": "driving", "(G)->gp": "implied"}

  def test_disabling_the_hub_splits_a_star_into_roots(self):
    reg = _registry("""
model: s
tables:
  hub: {pk: [H], enabled: false}
  c1: {pk: [H, X], fk: [{cols: [H], ref: hub, ref_cols: [H]}]}
  c2: {pk: [H, Y], fk: [{cols: [H], ref: hub, ref_cols: [H]}]}
""")
    assert reg.enforced_edges("c1") == ()
    assert reg.generation_waves(("c1", "c2")) == (("c1", "c2"),)

  def test_star_fact_dimensions_are_independent(self):
    reg = _registry(_STAR_FACT)
    _to_a, to_b = reg.enforced_edges("fact")
    assert reg.edge_overlap("fact", to_b) == ()
    assert reg.edge_rest("fact", to_b) == ("B_ID",)
    assert reg.driving_choice("fact") == "first_declared"
    assert reg.driving_choice("dim_a") is None

  def test_diamond_branch_overlap_and_rest(self):
    reg = _registry(_DIAMOND)
    _to_left, to_right = reg.enforced_edges("bottom")
    assert reg.edge_overlap("bottom", to_right) == ("T",)
    assert reg.edge_rest("bottom", to_right) == ("R",)
    assert "conditional on (T)" in reg.card("bottom")

  def test_two_marked_edges_still_stop(self):
    text = _STAR_FACT.replace(
        "ref: dim_a, ref_cols: [A_ID]}",
        "ref: dim_a, ref_cols: [A_ID], drives: true}",
    ).replace(
        "ref: dim_b, ref_cols: [B_ID]}",
        "ref: dim_b, ref_cols: [B_ID], drives: true}",
    )
    with pytest.raises(RelationshipError, match="2 marked"):
      _registry(text).edge_roles("fact")


_STAR_FACT = """
model: s
tables:
  dim_a: {pk: [A_ID]}
  dim_b: {pk: [B_ID]}
  fact:
    pk: [A_ID, B_ID, SEQ]
    fk:
      - {cols: [A_ID], ref: dim_a, ref_cols: [A_ID]}
      - {cols: [B_ID], ref: dim_b, ref_cols: [B_ID]}
"""

_DIAMOND = """
model: d
tables:
  top: {pk: [T]}
  left: {pk: [T, L], fk: [{cols: [T], ref: top, ref_cols: [T]}]}
  right: {pk: [T, R], fk: [{cols: [T], ref: top, ref_cols: [T]}]}
  bottom:
    pk: [T, L, R, S]
    fk:
      - {cols: [T, L], ref: left, ref_cols: [T, L]}
      - {cols: [T, R], ref: right, ref_cols: [T, R]}
"""


class TestShapesStillPending:
  """A child with two in-set parents that are not on one ancestry line —
    the star-schema fact table and the true diamond. Resolved by the
    `independent` and `conditional` roles (ADR 0037)."""

  def test_star_schema_fact_with_two_independent_dimensions(self):
    reg = _registry(_STAR_FACT)
    roles = _roles(reg, "fact")
    assert roles["(A_ID)->dim_a"] == "driving"
    assert roles["(B_ID)->dim_b"] == "independent"

  def test_true_diamond_rejoining_at_the_bottom(self):
    reg = _registry(_DIAMOND)
    roles = _roles(reg, "bottom")
    assert roles["(T,L)->left"] == "driving"
    assert roles["(T,R)->right"] == "conditional"


# Two INDEPENDENT edges that both claim the child column `X`: neither
# shares a column with the driving edge, so each is handed the ADR
# 0030/0031 side-input pool and each writes its WHOLE tuple into the row
# (`_draw_fk_columns` in B.1, the pool loop in B.2). The second write
# lands on top of the first's `X`, so the first edge's tuple is destroyed
# and `EnforceFkIntegrityDoFn` diverts nearly every row as `fk.orphan` —
# after the GPU has already generated it.
_TWO_INDEPENDENT = """
model: ti
tables:
  drv: {pk: [D]}
  pa: {pk: [X, Y]}
  pb: {pk: [X, Z]}
  child:
    pk: [D, X, Y, Z]
    fk:
      - {cols: [D], ref: drv, ref_cols: [D]}
      - {cols: [X, Y], ref: pa, ref_cols: [X, Y]}
      - {cols: [X, Z], ref: pb, ref_cols: [X, Z]}
"""

# Two CONDITIONAL edges whose `rest`s overlap on `L`. `mid` is NOT under
# `left`, so the candidate `mid` supplies for `L` need not exist in
# `left` for the same `T` — and `apply_conditional_overrides` writes the
# edges in plan order, so the surviving `(T, L)` is whatever the last
# edge said. Conditional edges are not gated at all (`_fk_integrity_stage`
# only sees side-input pools), so those rows land in BigQuery silently.
_TWO_CONDITIONAL = """
model: tc
tables:
  top: {pk: [T]}
  left:
    pk: [T, L]
    fk: [{cols: [T], ref: top, ref_cols: [T]}]
  mid:
    pk: [T, L, M]
    fk: [{cols: [T], ref: top, ref_cols: [T]}]
  bottom:
    pk: [T, L, M, S]
    fk:
      - {cols: [T], ref: top, ref_cols: [T]}
      - {cols: [T, L], ref: left, ref_cols: [T, L]}
      - {cols: [T, L, M], ref: mid, ref_cols: [T, L, M]}
"""


class TestCrossEdgeColumnOwnership:
  """Two NON-DRIVING edges may not own the same child column.

    `edge_roles` gives each non-driving edge a role from its overlap with
    the DRIVING edge alone, so until ADR 0037's final review nothing
    compared the non-driving edges with EACH OTHER. Both shapes below
    resolved to a clean role assignment and then corrupted the data
    downstream — silently, in the conditional case. Both raised a
    `RelationshipError` before this branch (ADR 0036 D4's blanket stop);
    they must raise again, by name.

    The columns an edge WRITES: `driving` its `cols`, `implied` nothing,
    `independent` all its `cols`, `conditional` its `rest`, `external`
    its `cols`. The DRIVING edge is deliberately outside the check —
    `implied`/`independent`/`conditional` are disjoint from it by
    construction, and an `external` edge overlapping it is design §9's
    named limitation (logged as `fk_edge_overlap_external`, resolved by
    enabling the parent), not a stop.
    """

  def test_two_independent_edges_cannot_share_a_column(self):
    reg = _registry(_TWO_INDEPENDENT)
    with pytest.raises(RelationshipError) as err:
      reg.edge_roles("child")
    message = str(err.value)
    assert "child" in message
    assert "(X,Y)->pa" in message and "(X,Z)->pb" in message
    assert "X" in message
    assert "drives: true" in message

  def test_two_conditional_edges_cannot_share_a_rest_column(self):
    reg = _registry(_TWO_CONDITIONAL)
    with pytest.raises(RelationshipError) as err:
      reg.edge_roles("bottom")
    message = str(err.value)
    assert "bottom" in message
    assert "(T,L)->left" in message and "(T,L,M)->mid" in message
    assert "L" in message

  def test_the_stop_names_the_role_of_each_clashing_edge(self):
    # The operator has to know WHICH mechanism claimed the column to
    # pick a fix, so the roles ride in the message.
    with pytest.raises(RelationshipError, match="independent"):
      _registry(_TWO_INDEPENDENT).edge_roles("child")
    with pytest.raises(RelationshipError, match="conditional"):
      _registry(_TWO_CONDITIONAL).edge_roles("bottom")

  def test_the_card_still_renders_a_clashing_model(self):
    # `card`/`mermaid` swallow a RelationshipError so an operator can
    # still SEE the model that stopped the launch.
    card = _registry(_TWO_INDEPENDENT).card("child")
    assert "(X,Y) --> pa" in card

  def test_shapes_with_disjoint_written_columns_still_resolve(self):
    # Every shape ADR 0037 shipped: the star's dimensions write
    # disjoint columns, the diamond's conditional branch writes only
    # its `rest`, and an implied edge writes nothing at all.
    assert _roles(_registry(_STAR_FACT), "fact") == {
        "(A_ID)->dim_a": "driving",
        "(B_ID)->dim_b": "independent",
    }
    assert _roles(_registry(_DIAMOND), "bottom") == {
        "(T,L)->left": "driving",
        "(T,R)->right": "conditional",
    }

  def test_two_conditional_edges_with_empty_rests_are_pure_filters(self):
    # `(K)->P` drives; `(K)->Q` and `(K)->R` are existence filters —
    # `rest` is empty on both, so neither WRITES anything and three
    # edges on one column are legitimate (the cli `_TWO_PARENTS`
    # shape).
    reg = _registry("""
model: f
tables:
  P: {pk: [K]}
  Q: {pk: [K]}
  R: {pk: [K]}
  CH:
    pk: [K, S]
    fk:
      - {cols: [K], ref: P, ref_cols: [K]}
      - {cols: [K], ref: Q, ref_cols: [K]}
      - {cols: [K], ref: R, ref_cols: [K]}
""")
    assert _roles(reg, "CH") == {
        "(K)->P": "driving",
        "(K)->Q": "conditional",
        "(K)->R": "conditional",
    }


# An external parent is one the launch never generates: its rows are
# already landed, so nothing the launch does can change what it holds.
_DENORM_EXTERNAL = """
model: dx
tables:
  CH:
    pk: [A_KEY, B_KEY]
    fk:
      - {cols: [A_KEY], ref: ds.A_TABLE, ref_cols: [A_KEY]}
      - {cols: [A_KEY, B_KEY], ref: ds.B_TABLE, ref_cols: [A_KEY, B_KEY]}
"""

# One in-model driving edge, one in-model conditional edge (rest = `X`)
# and an external edge that covers BOTH columns.
_EXTERNAL_OVER_IN_MODEL = """
model: xm
tables:
  drv: {pk: [K]}
  pa: {pk: [K, X]}
  CH:
    pk: [K, X]
    fk:
      - {cols: [K], ref: drv, ref_cols: [K]}
      - {cols: [K, X], ref: pa, ref_cols: [K, X]}
      - {cols: [K, X], ref: ds.EXT_TABLE, ref_cols: [K, X]}
"""


def _overlap_labels(reg: RelationshipRegistry, table: str):
  return [(f"({','.join(a.cols)})->{a.ref}", f"({','.join(b.cols)})->{b.ref}",
           cols) for a, b, cols in reg.external_overlaps(table)]


class TestExternalEdgesWarnRatherThanStop:
  """`external` edges are outside the ownership STOP (fix wave F3).

    Commit 76cafa5 credited every edge with WRITING its columns, external
    ones included, and `edge_roles` assigns `external` BEFORE any
    implied/subset analysis — so the classic denormalised child (every
    parent external, all on one ancestry line) hard-stopped a launch that
    ran fine at 2728203, and none of the three remedies in the message
    could be applied: an external edge can never become `implied` (the
    role is assigned first), `drives: true` is inert for it
    (`_pick_driving` only considers in-model edges), and an external
    parent has no `tables:` entry to disable. The identical shape with
    IN-MODEL parents is legal and ships today (the narrower edge becomes
    `implied`), so the external variant must resolve too — the risk is
    REPORTED as an overlap, never fatal.
    """

  def test_two_external_parents_on_one_ancestry_line_resolve(self):
    assert _roles(_registry(_DENORM_EXTERNAL), "CH") == {
        "(A_KEY)->ds.A_TABLE": "external",
        "(A_KEY,B_KEY)->ds.B_TABLE": "external",
    }

  def test_the_external_pair_is_reported_with_both_edges_and_the_columns(self):
    assert _overlap_labels(_registry(_DENORM_EXTERNAL), "CH") == [
        ("(A_KEY)->ds.A_TABLE", "(A_KEY,B_KEY)->ds.B_TABLE", ("A_KEY",)),
    ]

  def test_an_external_edge_over_in_model_edges_resolves_and_reports(self):
    # The external edge overlaps BOTH the driving edge (on `K`) and
    # the conditional edge's `rest` (on `X`); the second pair is the
    # one 76cafa5 turned into a stop.
    reg = _registry(_EXTERNAL_OVER_IN_MODEL)
    assert _roles(reg, "CH") == {
        "(K)->drv": "driving",
        "(K,X)->pa": "conditional",
        "(K,X)->ds.EXT_TABLE": "external",
    }
    assert _overlap_labels(reg, "CH") == [
        ("(K,X)->ds.EXT_TABLE", "(K)->drv", ("K",)),
        ("(K,X)->ds.EXT_TABLE", "(K,X)->pa", ("X",)),
    ]

  def test_an_in_model_clash_still_raises(self):
    # The stop is unchanged for a pair of IN-MODEL non-driving edges:
    # both remedies it names can actually be applied there.
    for text, table in ((_TWO_INDEPENDENT, "child"), (_TWO_CONDITIONAL,
                                                      "bottom")):
      with pytest.raises(RelationshipError):
        _registry(text).edge_roles(table)

  def test_the_stop_names_documenting_an_edge_as_a_way_out(self):
    # `enforced: false` keeps the relationship in the card and takes
    # the edge out of every draw — a legitimate resolution the
    # message omitted.
    with pytest.raises(RelationshipError) as err:
      _registry(_TWO_INDEPENDENT).edge_roles("child")
    assert "enforced: false" in str(err.value)
