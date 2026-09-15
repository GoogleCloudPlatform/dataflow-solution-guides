"""Preflight P4 (PK generation capacity) + FK activation — ADR 0028.

The 2026-08-21 run discovered its PK/pool conflict 37 minutes and 1 586
GPU-s after launch (999 488 pk.duplicate), and its declared FK was
silently inactive. Both become launcher-side stops. The PK itself now
comes from `config/relationships/` (ADR 0032).
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,missing-class-docstring,protected-access,superfluous-parens,unused-argument,use-implicit-booleaness-not-comparison

from __future__ import annotations

import pytest
from sdfb_beam.cli.preflight import preflight
from sdfb_core.contracts import TableSchema


def _relations(text: str, table: str = "t"):
  from sdfb_core.contracts.relationships import parse_relationship_model

  return parse_relationship_model(text, source="test.yaml").tables[table]


_PK_RELATIONS = _relations("model: m\ntables:\n  t:\n    pk: [ID]\n")
_FK_RELATIONS = _relations(
    "model: m\ntables:\n  t:\n    pk: [ID]\n    fk:\n"
    "      - cols: [CUST_ID]\n        ref: ds.customers\n"
    "        ref_cols: [ID]\n")
_C2E = ('{"llm_prompt_constraint": {"route": "llm", '
        '"pattern": "^(C2E[13][0-9A-F]{20}|7301[0-9A-F]{20})$"}}')
_TINY = '{"llm_prompt_constraint": {"route": "llm", "pattern": "^[0-9]{3}$"}}'
_PROSE = '{"llm_prompt_constraint": {"route": "llm", "format": "opaque key"}}'


def _schema(table_desc: str = "", id_desc: str = "") -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t",
          "description": ""
      },
      "schema": [
          {
              "name": "ID",
              "type": "STRING",
              "mode": "REQUIRED",
              "description": id_desc,
          },
          {
              "name": "CUST_ID",
              "type": "STRING",
              "mode": "REQUIRED"
          },
      ],
  })


def _rows(n: int = 10) -> list[dict]:
  return [{"ID": f"C2E3{i:020X}", "CUST_ID": "c1"} for i in range(n)]


class TestP4PkCapacity:

  def test_constrained_pk_without_pattern_stops_at_scale(self):
    with pytest.raises(SystemExit, match="preflight P4"):
      preflight(
          _schema("", _PROSE),
          (),
          (),
          _rows(),
          relations=_PK_RELATIONS,
          num_rows=1_000_000,
      )

  def test_constrained_pk_without_pattern_ok_below_pool_cap(self):
    preflight(
        _schema("", _PROSE), (), (),
        _rows(),
        relations=_PK_RELATIONS,
        num_rows=500)

  def test_pattern_pk_with_ample_capacity_passes(self):
    preflight(
        _schema("", _C2E), (), (),
        _rows(),
        relations=_PK_RELATIONS,
        num_rows=1_000_000)

  def test_pattern_pk_with_small_capacity_stops(self):
    with pytest.raises(SystemExit, match="preflight P4"):
      preflight(
          _schema("", _TINY),
          (),
          (),
          _rows(),
          relations=_PK_RELATIONS,
          num_rows=1_000_000,
      )

  def test_unconstrained_pk_is_untouched(self):
    preflight(
        _schema(), (), (), _rows(), relations=_PK_RELATIONS, num_rows=1_000_000)

  def test_num_rows_zero_disables_the_check(self):
    preflight(_schema("", _PROSE), (), (), _rows(), relations=_PK_RELATIONS)


class TestFkActivationIsDerived:
  """ADR 0029 rev B: fk_parent_landing derives from --landing_table,
    so preflight no longer refuses a declared FK — activation is checked
    where pools LOAD (loud empty-parent stop in run_pipeline)."""

  def test_declared_fk_passes_preflight_without_any_flag(self):
    preflight(_schema(), (), (), _rows(), relations=_FK_RELATIONS)

  def test_empty_parent_pool_stops_loudly(self):
    from sdfb_beam.cli.run_pipeline import assert_fk_pools_nonempty
    with pytest.raises(SystemExit, match=r"not landed"):
      assert_fk_pools_nonempty(_FK_RELATIONS.fk, {}, "p.landing")

  def test_populated_parent_pool_passes(self):
    from sdfb_beam.cli.run_pipeline import assert_fk_pools_nonempty
    assert_fk_pools_nonempty(_FK_RELATIONS.fk, {"CUST_ID": ("K1", "K2")},
                             "p.landing")


class TestP4CompositePk:
  """The 2026-08-22 first single-job launch: A_TABLE's 5-column
    composite PK failed P4 because one member (A_COL_002, a NUMERIC
    branch code with a cosmetic examples-only clause) was judged ALONE
    against 1M rows. P4 must bound the TUPLE (product of factors), and a
    column whose type never routes through the capped pool contributes
    unbounded capacity (ADR 0024: non-STRING keeps its typed route)."""

  def _relations(self, cols):
    names = ", ".join(c[0] for c in cols)
    return _relations(f"model: m\ntables:\n  t:\n    pk: [{names}]\n")

  def _schema(self, cols):
    return TableSchema.model_validate({
        "table_info": {
            "table_id": "p.d.t",
            "description": ""
        },
        "schema": [{
            "name": n,
            "type": t,
            "mode": "REQUIRED",
            "description": d
        } for n, t, d in cols],
    })

  def test_a_table_shape_passes(self):
    # numeric member w/ examples-only clause + unconstrained members:
    # tuple capacity is unbounded — must NOT stop the launch.
    examples_only = ('{"llm_prompt_constraint": {"examples": ["20"]}}')
    cols = [
        ("A_COL_001", "INT64", ""),
        ("A_COL_002", "INT64", examples_only),
        ("A_COL_003", "INT64", ""),
    ]
    schema = self._schema(cols)
    rows = [{
        "A_COL_001": i,
        "A_COL_002": 20,
        "A_COL_003": i
    } for i in range(10)]
    preflight(
        schema, (), (),
        rows,
        relations=self._relations(cols),
        num_rows=1_000_000)

  def test_all_members_capped_below_num_rows_stops(self):
    cols = [
        ("A", "STRING", _PROSE),
        ("B", "STRING", _PROSE),
    ]
    schema = self._schema(cols)
    rows = [{"A": f"a{i}", "B": f"b{i}"} for i in range(10)]
    # 512 * 512 = 262 144 < 1M -> tuple genuinely cannot be unique.
    with pytest.raises(SystemExit, match="preflight P4"):
      preflight(
          schema, (), (),
          rows,
          relations=self._relations(cols),
          num_rows=1_000_000)
    # ...but covers 200k rows fine.
    preflight(
        schema, (), (), rows, relations=self._relations(cols), num_rows=200_000)

  def test_enum_values_clause_counts_its_domain(self):
    enum = '{"llm_prompt_constraint": {"values": ["I", "O"]}}'
    cols = [
        ("DIRECTION", "STRING", enum),
        ("KEY", "STRING", _PROSE),
    ]
    schema = self._schema(cols)
    rows = [{"DIRECTION": "I", "KEY": f"k{i}"} for i in range(10)]
    # 2 * 512 = 1024 -> stops at 1M, passes at 1000.
    with pytest.raises(SystemExit, match="preflight P4"):
      preflight(
          schema, (), (),
          rows,
          relations=self._relations(cols),
          num_rows=1_000_000)
    preflight(
        schema, (), (), rows, relations=self._relations(cols), num_rows=1_000)

  def test_single_numeric_pk_with_clause_is_untouched(self):
    examples_only = ('{"llm_prompt_constraint": {"examples": ["7"]}}')
    cols = [("ACCT", "INT64", examples_only)]
    schema = self._schema(cols)
    rows = [{"ACCT": i} for i in range(10)]
    preflight(
        schema, (), (),
        rows,
        relations=self._relations(cols),
        num_rows=1_000_000)


class TestP5PkIsActuallyAKey:
  """2026-08-25 run …-11759075672032343276: the model declared a
    3-column PK whose tuple repeats on 99.4% of the source sample. The
    launch ran anyway and landed **74 rows of 1,000,000** — every other
    row was `pk.duplicate` — then tripped the BLOCKER gate 11 minutes
    and one GPU later. The sample already knew; preflight must say so.
    """

  @staticmethod
  def _rows(distinct: int, total: int = 10_000) -> list[dict]:
    return [{
        "ID": f"k{i % distinct}",
        "CUST_ID": "c",
        "NOTES": "x"
    } for i in range(total)]

  def test_a_pk_that_is_not_a_key_stops_before_the_gpu(self):
    with pytest.raises(SystemExit, match=r"preflight P5") as exc:
      preflight(
          _schema(),
          (),
          (),
          self._rows(60),
          relations=_PK_RELATIONS,
          num_rows=1_000_000,
      )
    message = str(exc.value)
    assert "ID" in message  # names the columns
    assert "60" in message  # the distinct tuples measured
    assert "1,000,000" in message or "1000000" in message

  def test_a_real_key_passes(self):
    preflight(
        _schema(),
        (),
        (),
        self._rows(10_000),
        relations=_PK_RELATIONS,
        num_rows=1_000_000,
    )

  def test_mild_source_duplication_still_only_warns(self):
    """Source data may legitimately dent an undeclared PK; the
        generator recombines values, so this is not a launch stop."""
    result = preflight(
        _schema(),
        (),
        (),
        self._rows(9_000),
        relations=_PK_RELATIONS,
        num_rows=1_000_000,
    )
    assert any("not unique" in w for w in result.warnings)

  def test_a_small_run_within_the_key_space_is_fine(self):
    preflight(
        _schema(),
        (),
        (),
        self._rows(60),
        relations=_PK_RELATIONS,
        num_rows=50,
    )

  def test_num_rows_zero_never_stops(self):
    preflight(_schema(), (), (), self._rows(60), relations=_PK_RELATIONS)


class TestP4FkBoundAndCategoricalMembers:
  """2026-09-09 job …-16364509521974163594 (ADR 0035): C_TABLE's PK is
    (D_COL_001 -> B_TABLE, C_COL_002, D_COL_018). With B_TABLE in the
    launch, D_COL_001 became an FK member drawn from the 100k side-input
    sample; the two categoricals multiply that by ~12. Ten million draws
    into ~1.2M tuples: 87.9% pk.duplicate, gate tripped after 3h16m.
    P4 must bound FK-bound and categorical members — and size the FK
    key sample the DAG will broadcast."""

  _MODEL = (
      "model: m\ntables:\n"
      "  parent:\n    pk: [PID]\n"
      "  child:\n    pk: [D1, C2, D18]\n    fk:\n"
      "      - cols: [D1]\n        ref: parent\n        ref_cols: [PID]\n")

  def _relations(self):
    return _relations(self._MODEL, table="child")

  @staticmethod
  def _schema(extra_pk_type: str | None = None) -> TableSchema:
    cols = [
        {
            "name": "D1",
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
    if extra_pk_type:
      cols.append({"name": "SEQ", "type": extra_pk_type, "mode": "REQUIRED"})
    return TableSchema.model_validate({
        "table_info": {
            "table_id": "p.d.child",
            "description": ""
        },
        "schema": cols
    })

  @staticmethod
  def _rows(n: int = 480) -> list[dict]:
    # C2 has 4 values, D18 has 3 -> the categoricals contribute x12
    # (480 rows: every (C2, D18) cell holds exactly 40, no skew).
    return [{
        "D1": f"C2E3{i:020X}",
        "C2": f"C{i % 4}",
        "D18": f"K{i % 3}",
        "AMT": i * 7,
        "SEQ": i,
    } for i in range(n)]

  def test_the_2026_09_09_launch_stops_at_second_zero(self):
    with pytest.raises(SystemExit, match=r"preflight P4") as exc:
      preflight(
          self._schema(),
          (),
          (),
          self._rows(),
          relations=self._relations(),
          num_rows=10_000_000,
          fk_parent_rows={"parent": 10_000_000},
          blocker_failure_ratio=0.2,
      )
    message = str(exc.value)
    assert "pk.duplicate" in message
    assert "D1" in message and "C2" in message and "D18" in message
    assert "%" in message  # the expected duplicate share, not just a product

  def test_one_million_rows_pass_and_size_the_fk_sample(self):
    from sdfb_core.engines.pk_capacity import fk_key_sample_cap

    result = preflight(
        self._schema(),
        (),
        (),
        self._rows(),
        relations=self._relations(),
        num_rows=1_000_000,
        fk_parent_rows={"parent": 10_000_000},
        blocker_failure_ratio=0.2,
    )
    assert result.fk_key_sample_caps == {
        ("D1",): fk_key_sample_cap(1_000_000, 12)
    }

  def test_a_small_parent_bounds_the_fk_member(self):
    # 500 parent keys x 12 = 6 000 tuples for 5 000 rows -> ~32%
    # expected duplicates, over the 20% gate.
    with pytest.raises(SystemExit, match=r"preflight P4"):
      preflight(
          self._schema(),
          (),
          (),
          self._rows(),
          relations=self._relations(),
          num_rows=5_000,
          fk_parent_rows={"parent": 500},
          blocker_failure_ratio=0.2,
      )

  def test_an_unbounded_sibling_member_covers_the_tuple(self):
    model = self._MODEL.replace("pk: [D1, C2, D18]", "pk: [D1, C2, D18, SEQ]")
    result = preflight(
        self._schema("INT64"),
        (),
        (),
        self._rows(),
        relations=_relations(model, table="child"),
        num_rows=10_000_000,
        fk_parent_rows={"parent": 10_000_000},
        blocker_failure_ratio=0.2,
    )
    from sdfb_core.engines.pk_capacity import FK_KEY_SAMPLE_FLOOR

    assert result.fk_key_sample_caps == {("D1",): FK_KEY_SAMPLE_FLOOR}

  def test_categorical_only_pk_stops_on_the_product_rule(self):
    # No gate given: the hard product rule alone (12 tuples < 1 000).
    # 12 sample rows = 12 distinct (C2, D18) tuples, so P5 stays quiet.
    model = "model: m\ntables:\n  child:\n    pk: [C2, D18]\n"
    with pytest.raises(SystemExit, match=r"preflight P4"):
      preflight(
          self._schema(),
          (),
          (),
          self._rows(12),
          relations=_relations(model, table="child"),
          num_rows=1_000,
      )

  def test_without_a_gate_only_the_product_rule_applies(self):
    # 100k (floor, parent unknown) x 12 = 1.2M >= 1M passes without a
    # gate — the birthday stop needs the gate to compare against.
    result = preflight(
        self._schema(),
        (),
        (),
        self._rows(),
        relations=self._relations(),
        num_rows=1_000_000,
    )
    assert ("D1",) in result.fk_key_sample_caps


class TestP4JointSkewedCategoricalMembers:
  """2026-09-09_16_44_42 job …-563627394951127087 (ADR 0035 rev): the
    sized 1M-key sample reached the DAG and C_TABLE still lost 56.5% of
    10M rows. P4 had multiplied the two categoricals' DISTINCT counts
    (uniform cells); the sample knew the truth — the pair is skewed and
    jointly covers fewer cells. P4 must weigh the joint cells."""

  _MODEL = TestP4FkBoundAndCategoricalMembers._MODEL
  _schema = staticmethod(TestP4FkBoundAndCategoricalMembers._schema)

  @staticmethod
  def _rows(n: int = 1_000) -> list[dict]:
    # Per-column distinct: C2 has 6 values, D18 has 4 -> product 24.
    # Joint: only 12 (C2, D18) pairs ever occur, and they are skewed
    # (the first pair carries 40% of rows).
    pairs = [
        ("C0", "K0"),
        ("C1", "K1"),
        ("C2", "K2"),
        ("C3", "K3"),
        ("C4", "K0"),
        ("C5", "K1"),
        ("C0", "K2"),
        ("C1", "K3"),
        ("C2", "K0"),
        ("C3", "K1"),
        ("C4", "K2"),
        ("C5", "K3"),
    ]
    weights = [40, 20, 12, 8, 6, 4, 3, 2, 2, 1, 1, 1]
    rows = []
    i = 0
    for (c2, d18), w in zip(pairs, weights, strict=True):
      for _ in range(w * n // 100):
        rows.append({
            "D1": f"C2E3{i:020X}",
            "C2": c2,
            "D18": d18,
            "AMT": i * 7,
            "SEQ": i
        })
        i += 1
    return rows

  def test_the_2026_09_09_16_44_launch_stops_at_second_zero(self):
    # Uniform x24 over 1M keys would pass (~18% expected); the joint
    # skewed cells put the run over the 20% gate by a wide margin.
    with pytest.raises(SystemExit, match=r"preflight P4") as exc:
      preflight(
          self._schema(),
          (),
          (),
          self._rows(),
          relations=_relations(self._MODEL, table="child"),
          num_rows=10_000_000,
          fk_parent_rows={"parent": 10_000_000},
          blocker_failure_ratio=0.2,
      )
    message = str(exc.value)
    assert "cells=12" in message  # joint cells, not 6 x 4
    assert "effective" in message  # the skew is named
    assert "pk.duplicate" in message

  def test_fk_sample_is_sized_from_effective_cells(self):
    from sdfb_core.engines.pk_capacity import (
        effective_cells,
        fk_key_sample_cap,
    )

    result = preflight(
        self._schema(),
        (),
        (),
        self._rows(),
        relations=_relations(self._MODEL, table="child"),
        num_rows=200_000,
        fk_parent_rows={"parent": 10_000_000},
        blocker_failure_ratio=0.2,
    )
    n_eff = effective_cells([40, 20, 12, 8, 6, 4, 3, 2, 2, 1, 1, 1])
    assert result.fk_key_sample_caps == {
        ("D1",): fk_key_sample_cap(200_000, n_eff)
    }


class TestP4EdgeToDisabledParent:
  """2026-09-10 launch …-4049668522929163666: B_TABLE flipped to
    ``enabled: false`` so C_TABLE became a root, its card said
    ``parent DISABLED — not drawn``, yet P4 still counted the FK member
    at the 1M key-sample ceiling and stopped a 10M run at 56.5 %. An
    edge the launch never draws must not bound the PK."""

  _MODEL = (
      "model: m\ntables:\n"
      "  parent:\n    pk: [PID]\n    enabled: false\n"
      "  t:\n    pk: [ID]\n    fk:\n"
      "      - cols: [ID]\n        ref: parent\n        ref_cols: [PID]\n")

  def _registry(self):
    from sdfb_core.contracts.relationships import RelationshipRegistry

    return RelationshipRegistry.from_sources([("test.yaml", self._MODEL)])

  def test_an_undrawn_edge_leaves_the_pk_member_to_its_own_route(self):
    registry = self._registry()
    assert registry.enforced_edges("t") == ()  # the launcher draws nothing
    result = preflight(
        _schema("", _C2E),
        (),
        (),
        _rows(),
        relations=registry.relations("t"),
        enforced_fk=registry.enforced_edges("t"),
        num_rows=2_000_000,
        blocker_failure_ratio=0.2,
    )
    assert result.fk_key_sample_caps == {}

  def test_the_same_edge_to_an_enabled_parent_still_bounds_the_pk(self):
    from sdfb_core.contracts.relationships import RelationshipRegistry

    registry = RelationshipRegistry.from_sources([
        ("test.yaml", self._MODEL.replace("    enabled: false\n", ""))
    ])
    with pytest.raises(SystemExit, match="preflight P4"):
      preflight(
          _schema("", _C2E),
          (),
          (),
          _rows(),
          relations=registry.relations("t"),
          enforced_fk=registry.enforced_edges("t"),
          num_rows=2_000_000,
          blocker_failure_ratio=0.2,
      )
