"""ADR 0036 preflight: driven children are checked per key, not by the
random-draw model; edge roles are logged; the row count derives.

Every STOP asserted here pins ``on_model_conflict="stop"`` (ADR 0038).
The P4 verdicts are unchanged — the source still disproves the declared
PK in exactly these cases — but the default is now to ADJUST the model
and carry on (`test_model_adjustment.py`). The stop, and every word of
its message, is still what the escape hatch produces.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,use-implicit-booleaness-not-comparison

from __future__ import annotations

import logging

import pytest
from sdfb_beam.cli.preflight import (
    edge_supplied_members,
    pk_cell_columns,
    preflight,
)
from sdfb_core.contracts import TableSchema
from sdfb_core.contracts.relationships import RelationshipRegistry
from sdfb_core.engines.b1_rag.profile import profile_columns

_MODEL = """
model: m
tables:
  parent:
    pk: [PID]
  child:
    pk: [PID, C2, D18]
    fk:
      - cols: [PID]
        ref: parent
        ref_cols: [PID]
"""
_REG = RelationshipRegistry.from_sources([("config/relationships/m.yaml",
                                           _MODEL)])


def _schema(extra: str | None = None) -> TableSchema:
  cols = [
      {
          "name": "PID",
          "type": "STRING",
          "mode": "REQUIRED"
      },
      {
          "name": "C2",
          "type": "STRING",
          "mode": "REQUIRED"
      },
      {
          "name": "D18",
          "type": "STRING",
          "mode": "REQUIRED"
      },
      {
          "name": "AMT",
          "type": "INT64",
          "mode": "REQUIRED"
      },
  ]
  if extra:
    cols.append({"name": "SEQ", "type": extra, "mode": "REQUIRED"})
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.child"
      },
      "schema": cols
  })


def _rows(n: int = 480) -> list[dict]:
  return [{
      "PID": f"C2E3{i:020X}",
      "C2": f"C{i % 4}",
      "D18": f"K{i % 3}",
      "AMT": i,
      "SEQ": i
  } for i in range(n)]


def _fanout(max_k: int) -> dict:
  return {
      "driving_cols": ["PID"],
      "histogram": {
          "0": 10,
          str(max_k): 5
      },
      "cells": {
          "cols": ["C2", "D18"],
          "rows": [[f"C{i % 4}", f"K{i % 3}"] for i in range(12)],
          "counts": [1.0] * 12
      },
      "exact_cells": True
  }


def test_pk_cell_columns_are_the_sampled_members_outside_the_edge():
  profiles = profile_columns(_schema("INT64"), _rows())
  assert pk_cell_columns(("PID", "C2", "D18"), ("PID",),
                         profiles) == (("C2", "D18"), True)
  assert pk_cell_columns(("PID", "C2", "D18", "SEQ"), ("PID",),
                         profiles) == (("C2", "D18"), False)


def test_fanout_within_cells_passes_and_derives_rows(caplog):
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    result = preflight(
        _schema(),
        (),
        (),
        _rows(),
        relations=_REG.relations("child"),
        num_rows=10_000_000,
        fk_parent_rows={"parent": 10_000_000},
        blocker_failure_ratio=0.2,
        fanout=_fanout(12),
        edge_roles=_REG.edge_roles("child"),
    )
  # mean k = (10*0 + 5*12)/15 = 4 -> 40M derived rows
  assert result.derived_rows == 40_000_000
  assert "name=fk_edge_role" in caplog.text and "role=driving" in caplog.text
  assert "name=pk_capacity_tight" not in caplog.text  # the random-draw model is off


def test_fanout_beyond_cells_stops():
  with pytest.raises(
      SystemExit, match=r"preflight P4.*up to 13 times.*12 cells"):
    preflight(
        _schema(),
        (),
        (),
        _rows(),
        relations=_REG.relations("child"),
        num_rows=1_000,
        fk_parent_rows={"parent": 1_000},
        blocker_failure_ratio=0.2,
        fanout=_fanout(13),
        on_model_conflict="stop",
    )


def test_an_unbounded_member_makes_the_check_inexact_and_passes():
  model = _MODEL.replace("pk: [PID, C2, D18]", "pk: [PID, C2, D18, SEQ]")
  reg = RelationshipRegistry.from_sources([("config/relationships/m.yaml",
                                            model)])
  preflight(
      _schema("INT64"),
      (),
      (),
      _rows(),
      relations=reg.relations("child"),
      num_rows=1_000,
      fk_parent_rows={"parent": 1_000},
      blocker_failure_ratio=0.2,
      fanout=_fanout(500),
  )


def test_missing_cell_table_with_exact_members_stops():
  """Fail CLOSED (ADR 0036 review): the PK's completing members are
    categorical, so the per-key draw needs a cell table — and there is
    none. Reading that as "0 cells measured, so 0 fit" would let a launch
    through on a measurement that never happened."""
  fanout = dict(_fanout(3))
  fanout["cells"] = None
  with pytest.raises(
      SystemExit, match=r"preflight P4.*no cell table was measured"):
    preflight(
        _schema(),
        (),
        (),
        _rows(),
        relations=_REG.relations("child"),
        num_rows=1_000,
        fk_parent_rows={"parent": 1_000},
        blocker_failure_ratio=0.2,
        fanout=fanout,
    )


_ONE_TO_ONE = """
model: m2
tables:
  parent:
    pk: [PID]
  child1to1:
    pk: [PID]
    fk:
      - cols: [PID]
        ref: parent
        ref_cols: [PID]
"""


def _one_to_one(histogram: dict) -> tuple[TableSchema, dict, dict]:
  reg = RelationshipRegistry.from_sources([("config/relationships/m2.yaml",
                                            _ONE_TO_ONE)])
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.child1to1"
      },
      "schema": [{
          "name": "PID",
          "type": "STRING",
          "mode": "REQUIRED"
      }],
  })
  fanout = {
      "driving_cols": ["PID"],
      "histogram": histogram,
      "cells": None,
      "exact_cells": True
  }
  return schema, reg.relations("child1to1"), fanout


def test_pk_equal_to_driving_edge_needs_no_cell_table():
  """E_TABLE-shape (2026-09-11): PK == driving edge columns exactly, a
    true 1:1 with the parent — `cells` is legitimately empty (nothing to
    measure), so max_k <= 1 passes with no cell table at all."""
  schema, relations, fanout = _one_to_one({"0": 10, "1": 5})
  preflight(
      schema,
      (),
      (),
      [],
      relations=relations,
      num_rows=1_000,
      fk_parent_rows={"parent": 1_000},
      blocker_failure_ratio=0.2,
      fanout=fanout,
  )


def test_pk_equal_to_driving_edge_with_real_fanout_stops():
  """Same shape, but the source has 2 children for one parent key — a
    genuine PK violation, not a missing measurement."""
  schema, relations, fanout = _one_to_one({"0": 10, "2": 5})
  with pytest.raises(
      SystemExit, match=r"preflight P4.*equals the driving edge exactly"):
    preflight(
        schema,
        (),
        (),
        [],
        relations=relations,
        num_rows=1_000,
        fk_parent_rows={"parent": 1_000},
        blocker_failure_ratio=0.2,
        fanout=fanout,
        on_model_conflict="stop",
    )


# --- ADR 0037: independent and conditional members inside the PK ------
#
# A driven child may carry, next to its driving edge, an INDEPENDENT edge
# (a star-schema dimension, drawn whole from a sampled key pool) and a
# CONDITIONAL edge (a diamond branch, one candidate tuple per shared key).
# Both SUPPLY PK members, so P4 must neither measure a cell table over
# them nor ignore the capacity they contribute per parent key (design §6).

_STAR = """
model: star
tables:
  A_TABLE:
    pk: [A_ID]
  B_TABLE:
    pk: [B_ID]
  F_TABLE:
    pk: [A_ID, B_ID, SEQ]
    fk:
      - cols: [A_ID]
        ref: A_TABLE
        ref_cols: [A_ID]
        drives: true
      - cols: [B_ID]
        ref: B_TABLE
        ref_cols: [B_ID]
"""


def _star_reg(pk: str = "[A_ID, B_ID, SEQ]") -> RelationshipRegistry:
  model = _STAR.replace("pk: [A_ID, B_ID, SEQ]", f"pk: {pk}")
  return RelationshipRegistry.from_sources([("config/relationships/star.yaml",
                                             model)])


def _star_schema() -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.F_TABLE"
      },
      "schema": [
          {
              "name": "A_ID",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "B_ID",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "SEQ",
              "type": "INT64",
              "mode": "REQUIRED"
          },
      ],
  })


def _star_rows(n: int = 480) -> list[dict]:
  return [{
      "A_ID": f"A1B2{i:020X}",
      "B_ID": f"B{i % 6}",
      "SEQ": i
  } for i in range(n)]


def _star_fanout(max_k: int) -> dict:
  return {
      "driving_cols": ["A_ID"],
      "histogram": {
          "0": 3,
          str(max_k): 5
      },
      "cells": None,
      "exact_cells": True
  }


_DIAMOND = """
model: dia
tables:
  TOP_TABLE:
    pk: [T]
  LEFT_TABLE:
    pk: [T, L]
    fk:
      - cols: [T]
        ref: TOP_TABLE
        ref_cols: [T]
  RIGHT_TABLE:
    pk: [T, R]
    fk:
      - cols: [T]
        ref: TOP_TABLE
        ref_cols: [T]
  OTHER_TABLE:
    pk: [T, S]
    fk:
      - cols: [T]
        ref: TOP_TABLE
        ref_cols: [T]
  BOTTOM_TABLE:
    pk: [T, L, R]
    fk:
      - cols: [T, L]
        ref: LEFT_TABLE
        ref_cols: [T, L]
        drives: true
      - cols: [T, R]
        ref: RIGHT_TABLE
        ref_cols: [T, R]
"""
_DIAMOND_REG = RelationshipRegistry.from_sources([
    ("config/relationships/dia.yaml", _DIAMOND)
])
_TWO_CONDITIONAL_REG = RelationshipRegistry.from_sources([
    ("config/relationships/dia.yaml",
     _DIAMOND.replace("pk: [T, L, R]", "pk: [T, L, R, S]") + """
      - cols: [T, S]
        ref: OTHER_TABLE
        ref_cols: [T, S]
""")
])


def _diamond_schema() -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.BOTTOM_TABLE"
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
              "name": "S",
              "type": "STRING",
              "mode": "NULLABLE"
          },
          {
              "name": "C",
              "type": "STRING",
              "mode": "REQUIRED"
          },
      ],
  })


def _diamond_rows(n: int = 480) -> list[dict]:
  return [{
      "T": f"t{i % 3}",
      "L": f"l{i % 5}",
      "R": f"R2F3{i:020X}",
      "S": f"S4E5{i:020X}",
      "C": f"C{i % 4}"
  } for i in range(n)]


def _diamond_fanout(max_k: int) -> dict:
  return {
      "driving_cols": ["T", "L"],
      "histogram": {
          "0": 3,
          str(max_k): 5
      },
      "cells": None,
      "exact_cells": True
  }


def test_known_members_leave_the_cells_to_the_remaining_pk_members():
  """`known` columns are SUPPLIED by another edge: they are neither
    cells (nothing measures a cell table over an FK column) nor free
    members that make the check inexact."""
  profiles = profile_columns(_diamond_schema(), _diamond_rows())
  assert pk_cell_columns(("T", "L", "R", "C"), ("T", "L"),
                         profiles,
                         known=("R",)) == (("C",), True)
  # Without it, R is an identifier the engine does not re-emit from a
  # domain, so the completing members are no longer exact.
  assert pk_cell_columns(("T", "L", "R", "C"), ("T", "L"),
                         profiles) == (("C",), False)


def test_an_inexact_driven_pk_still_returns_the_independent_caps():
  """`SEQ` is unbounded, so P4 stands down (streaming uniqueness
    measures pk.duplicate) — but the independent edge's pool cap must
    still reach the composer, or its side input keeps the floor."""
  reg = _star_reg()
  result = preflight(
      _star_schema(),
      (),
      (),
      _star_rows(),
      relations=reg.relations("F_TABLE"),
      num_rows=1_000,
      fk_parent_rows={
          "A_TABLE": 1_000,
          "B_TABLE": 7
      },
      blocker_failure_ratio=0.2,
      fanout=_star_fanout(500),
      edge_roles=reg.edge_roles("F_TABLE"),
  )
  assert result.fk_key_sample_caps == {("B_ID",): 7}


def test_an_independent_member_of_the_pk_is_not_a_per_key_factor():
  """Fix wave A2 (blocker): an INDEPENDENT edge's pool is drawn PER ROW
    WITH REPLACEMENT (`_draw_fk_columns`), so it adds nothing to per-key
    distinctness. P4 used to multiply it in, declaring a PK impossible to
    duplicate that the engine then duplicated: 5 children per key, a
    5-key pool, and the same pool value drawn twice inside one parent.
    However large the parent, the stop must stand."""
  reg = _star_reg("[A_ID, B_ID]")

  def _run(max_k: int, parent_keys: int):
    return preflight(
        _star_schema(),
        (),
        (),
        _star_rows(),
        relations=reg.relations("F_TABLE"),
        num_rows=1_000,
        fk_parent_rows={
            "A_TABLE": 1_000,
            "B_TABLE": parent_keys
        },
        blocker_failure_ratio=0.2,
        fanout=_star_fanout(max_k),
        edge_roles=reg.edge_roles("F_TABLE"),
        on_model_conflict="stop",
    )

  for parent_keys in (3, 5, 5_000_000):
    with pytest.raises(SystemExit) as exc:
      _run(5, parent_keys)
    assert "drawn per ROW" in str(exc.value)
    # The knob that never worked is no longer advertised.
    assert "a parent that lands more keys" not in str(exc.value)
  # A fan-out of 1 needs no completing member at all.
  assert _run(1, 5).fk_key_sample_caps == {("B_ID",): 5}


def test_a_conditional_member_of_the_pk_is_bounded_by_the_candidate_cap():

  def _run(max_k: int, cap: int = 64):
    return preflight(
        _diamond_schema(),
        (),
        (),
        _diamond_rows(),
        relations=_DIAMOND_REG.relations("BOTTOM_TABLE"),
        num_rows=1_000,
        fk_parent_rows={
            "LEFT_TABLE": 1_000,
            "RIGHT_TABLE": 1_000
        },
        blocker_failure_ratio=0.2,
        fanout=_diamond_fanout(max_k),
        edge_roles=_DIAMOND_REG.edge_roles("BOTTOM_TABLE"),
        conditional_rest={"(T,R)->RIGHT_TABLE": ("R",)},
        candidate_cap=cap,
        on_model_conflict="stop",
    )

  with pytest.raises(SystemExit, match=r"preflight P4.*--fk_candidate_cap"):
    _run(100)
  _run(50)  # 50 <= 64 candidates per shared key


def test_two_conditional_members_multiply():

  def _run(max_k: int):
    fanout = _diamond_fanout(max_k)
    return preflight(
        _diamond_schema(),
        (),
        (),
        _diamond_rows(),
        relations=_TWO_CONDITIONAL_REG.relations("BOTTOM_TABLE"),
        num_rows=1_000,
        fk_parent_rows={
            "LEFT_TABLE": 1_000,
            "RIGHT_TABLE": 1_000,
            "OTHER_TABLE": 1_000
        },
        blocker_failure_ratio=0.2,
        fanout=fanout,
        edge_roles=_TWO_CONDITIONAL_REG.edge_roles("BOTTOM_TABLE"),
        conditional_rest={
            "(T,R)->RIGHT_TABLE": ("R",),
            "(T,S)->OTHER_TABLE": ("S",)
        },
        candidate_cap=8,
        on_model_conflict="stop",
    )

  _run(60)  # 8 x 8 = 64 candidate combinations per driving key
  with pytest.raises(SystemExit, match=r"preflight P4.*--fk_candidate_cap"):
    _run(100)


_MIX = """
model: mix
tables:
  TOP_TABLE:
    pk: [T]
  LEFT_TABLE:
    pk: [T, L]
    fk:
      - cols: [T]
        ref: TOP_TABLE
        ref_cols: [T]
  RIGHT_TABLE:
    pk: [T, R]
    fk:
      - cols: [T]
        ref: TOP_TABLE
        ref_cols: [T]
  DIM_TABLE:
    pk: [D]
  MIX_TABLE:
    pk: [T, L, R, D, C1, C2]
    fk:
      - cols: [T, L]
        ref: LEFT_TABLE
        ref_cols: [T, L]
        drives: true
      - cols: [T, R]
        ref: RIGHT_TABLE
        ref_cols: [T, R]
      - cols: [D]
        ref: DIM_TABLE
        ref_cols: [D]
"""
_MIX_REG = RelationshipRegistry.from_sources([("config/relationships/mix.yaml",
                                               _MIX)])


def _mix_schema() -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.MIX_TABLE"
      },
      "schema": [{
          "name": n,
          "type": "STRING",
          "mode": "REQUIRED"
      } for n in ("T", "L", "R", "D", "C1", "C2")],
  })


def _mix_rows(n: int = 480) -> list[dict]:
  return [{
      "T": f"t{i % 3}",
      "L": f"l{i % 5}",
      "R": f"R2F3{i:020X}",
      "D": f"D6A7{i:020X}",
      "C1": f"C{i % 2}",
      "C2": f"E{i % 3}"
  } for i in range(n)]


def test_cells_and_conditional_factors_multiply_but_an_independent_pool_does_not(
):
  """Design §6's factors, corrected by fix wave A2: 6 measured joint
    cells x a 4-candidate conditional edge = 24 children per key. The
    3-key independent pool is NOT a third factor — it is drawn per row,
    with replacement."""

  def _run(max_k: int):
    fanout = {
        "driving_cols": ["T", "L"],
        "histogram": {
            "0": 3,
            str(max_k): 5
        },
        "cells": {
            "cols": ["C1", "C2"],
            "rows": [[f"C{i // 3}", f"E{i % 3}"] for i in range(6)],
            "counts": [1.0] * 6
        },
        "exact_cells": True,
    }
    return preflight(
        _mix_schema(),
        (),
        (),
        _mix_rows(),
        relations=_MIX_REG.relations("MIX_TABLE"),
        num_rows=1_000,
        fk_parent_rows={
            "LEFT_TABLE": 1_000,
            "RIGHT_TABLE": 1_000,
            "DIM_TABLE": 3
        },
        blocker_failure_ratio=0.2,
        fanout=fanout,
        edge_roles=_MIX_REG.edge_roles("MIX_TABLE"),
        conditional_rest={"(T,R)->RIGHT_TABLE": ("R",)},
        candidate_cap=4,
        on_model_conflict="stop",
    )

  _run(24)  # 6 cells x 4 candidates
  with pytest.raises(SystemExit, match=r"preflight P4.*up to 25 times"):
    _run(25)
  # The conditional factor is an UPPER bound: a key whose co-parent
  # offers fewer candidates emits fewer children (fix wave A1 counts
  # them), so the stop must not sell the cap as a guarantee.
  with pytest.raises(SystemExit) as exc:
    _run(25)
  assert "upper bound" in str(exc.value)


# --- Fix round 1: rulings 12 (one supply rule) and 13 (caps inside) ----

_PARTIAL = """
model: partial
tables:
  A_TABLE:
    pk: [A_ID]
  B_TABLE:
    pk: [B_ID, X]
  F_TABLE:
    pk: [A_ID, B_ID]
    fk:
      - cols: [A_ID]
        ref: A_TABLE
        ref_cols: [A_ID]
        drives: true
      - cols: [B_ID, X]
        ref: B_TABLE
        ref_cols: [B_ID, X]
"""
_PARTIAL_REG = RelationshipRegistry.from_sources([
    ("config/relationships/partial.yaml", _PARTIAL)
])


def _partial_schema() -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.F_TABLE"
      },
      "schema": [{
          "name": n,
          "type": "STRING",
          "mode": "REQUIRED"
      } for n in ("A_ID", "B_ID", "X")],
  })


def _partial_rows(n: int = 480) -> list[dict]:
  return [{
      "A_ID": f"A1B2{i:020X}",
      "B_ID": f"B{i % 6}",
      "X": f"X{i % 4}"
  } for i in range(n)]


def test_one_supply_rule_covers_partial_and_disjoint_edges():
  """Ruling 12: an edge SUPPLIES its columns whether or not the PK
    contains them (the engine overwrites them from the pool either way),
    and it COUNTS as a factor as soon as one of them is in the PK."""
  roles = _PARTIAL_REG.edge_roles("F_TABLE")
  # `B_ID` is in the PK, `X` is not — one factor, both columns known.
  partial = edge_supplied_members(("A_ID", "B_ID"), roles, {})
  assert partial.known == ("B_ID", "X")
  assert [e.ref for e in partial.independent] == ["B_TABLE"]
  # Neither column in the PK — still supplied, but no factor.
  disjoint = edge_supplied_members(("A_ID",), roles, {})
  assert disjoint.known == ("B_ID", "X") and disjoint.independent == ()


def test_a_partially_contained_edge_does_not_false_stop():
  """The review's B-1 scenario: the measurement was told to build NO
    cell table (its `known` covers `B_ID`), so the check must reach the
    same conclusion — anything else stops on a cell table that can never
    be measured, with `clear the cache` as an impossible remedy."""

  def _run(max_k: int, parent_keys: int):
    return preflight(
        _partial_schema(),
        (),
        (),
        _partial_rows(),
        relations=_PARTIAL_REG.relations("F_TABLE"),
        num_rows=1_000,
        fk_parent_rows={
            "A_TABLE": 1_000,
            "B_TABLE": parent_keys
        },
        blocker_failure_ratio=0.2,
        fanout={
            "driving_cols": ["A_ID"],
            "histogram": {
                "0": 3,
                str(max_k): 5
            },
            "cells": None,
            "exact_cells": True
        },
        edge_roles=_PARTIAL_REG.edge_roles("F_TABLE"),
        on_model_conflict="stop",
    )

  # The caps still travel out (the composer sizes its side input from
  # them) — but they bound no PK, so a fan-out of 1 is what passes.
  assert _run(1, 5).fk_key_sample_caps == {("B_ID", "X"): 5}
  with pytest.raises(SystemExit) as exc:
    _run(5, 3)
  assert "no cell table was measured" not in str(exc.value)
  assert "drawn per ROW" in str(exc.value)


def test_independent_pools_are_sized_from_the_derived_rows():
  """Ruling 13 / finding B-2: a driven child lands
    `parent_rows x mean_fanout`, not `--num_rows`, so the pool that both
    P4 and the composer use must be sized from the DERIVED count."""
  from sdfb_core.engines.pk_capacity import fk_key_sample_cap

  reg = _star_reg()
  result = preflight(
      _star_schema(),
      (),
      (),
      _star_rows(),
      relations=reg.relations("F_TABLE"),
      num_rows=10_000,
      fk_parent_rows={
          "A_TABLE": 10_000,
          "B_TABLE": 5_000_000
      },
      blocker_failure_ratio=0.2,
      fanout={
          "driving_cols": ["A_ID"],
          "histogram": {
              "30": 4
          },
          "cells": None,
          "exact_cells": True
      },
      edge_roles=reg.edge_roles("F_TABLE"),
  )
  assert result.derived_rows == 300_000
  assert result.fk_key_sample_caps == {("B_ID",): fk_key_sample_cap(300_000, 1)}
  # The launch-wide count would have sized it an order of magnitude low.
  assert fk_key_sample_cap(10_000, 1) != fk_key_sample_cap(300_000, 1)


def test_a_none_candidate_cap_falls_back_to_the_default():
  """A programmatic caller that leaves the flag unset must not get a
    TypeError out of the capacity product."""
  preflight(
      _diamond_schema(),
      (),
      (),
      _diamond_rows(),
      relations=_DIAMOND_REG.relations("BOTTOM_TABLE"),
      num_rows=1_000,
      fk_parent_rows={
          "LEFT_TABLE": 1_000,
          "RIGHT_TABLE": 1_000
      },
      blocker_failure_ratio=0.2,
      fanout=_diamond_fanout(50),
      edge_roles=_DIAMOND_REG.edge_roles("BOTTOM_TABLE"),
      conditional_rest={"(T,R)->RIGHT_TABLE": ("R",)},
      candidate_cap=None,
  )
  with pytest.raises(SystemExit, match=r"preflight P4.*--fk_candidate_cap"):
    preflight(
        _diamond_schema(),
        (),
        (),
        _diamond_rows(),
        relations=_DIAMOND_REG.relations("BOTTOM_TABLE"),
        num_rows=1_000,
        fk_parent_rows={
            "LEFT_TABLE": 1_000,
            "RIGHT_TABLE": 1_000
        },
        blocker_failure_ratio=0.2,
        fanout=_diamond_fanout(100),
        edge_roles=_DIAMOND_REG.edge_roles("BOTTOM_TABLE"),
        conditional_rest={"(T,R)->RIGHT_TABLE": ("R",)},
        candidate_cap=None,
        on_model_conflict="stop",
    )


def test_the_stop_names_a_sufficient_candidate_cap_not_just_the_floor():
  """"raise it above 64" is not actionable when 65 still fails: with
    one conditional edge and no other factor, 100 children per key need
    a cap of 100."""
  with pytest.raises(SystemExit) as exc:
    preflight(
        _diamond_schema(),
        (),
        (),
        _diamond_rows(),
        relations=_DIAMOND_REG.relations("BOTTOM_TABLE"),
        num_rows=1_000,
        fk_parent_rows={
            "LEFT_TABLE": 1_000,
            "RIGHT_TABLE": 1_000
        },
        blocker_failure_ratio=0.2,
        fanout=_diamond_fanout(100),
        edge_roles=_DIAMOND_REG.edge_roles("BOTTOM_TABLE"),
        conditional_rest={"(T,R)->RIGHT_TABLE": ("R",)},
        candidate_cap=64,
        on_model_conflict="stop",
    )
  assert "--fk_candidate_cap to at least 100" in str(exc.value)
  # Two edges share the shortfall: 8 x 8 = 64 < 100, and 10 x 10 >= 100.
  with pytest.raises(SystemExit) as exc2:
    preflight(
        _diamond_schema(),
        (),
        (),
        _diamond_rows(),
        relations=_TWO_CONDITIONAL_REG.relations("BOTTOM_TABLE"),
        num_rows=1_000,
        fk_parent_rows={
            "LEFT_TABLE": 1_000,
            "RIGHT_TABLE": 1_000,
            "OTHER_TABLE": 1_000
        },
        blocker_failure_ratio=0.2,
        fanout=_diamond_fanout(100),
        edge_roles=_TWO_CONDITIONAL_REG.edge_roles("BOTTOM_TABLE"),
        conditional_rest={
            "(T,R)->RIGHT_TABLE": ("R",),
            "(T,S)->OTHER_TABLE": ("S",)
        },
        candidate_cap=8,
        on_model_conflict="stop",
    )
  assert "--fk_candidate_cap to at least 10" in str(exc2.value)
