"""ADR 0036 launcher: roles -> measurement -> derived rows -> config."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,invalid-name,protected-access,redefined-outer-name,unused-argument,unused-variable

from __future__ import annotations

import pytest
from sdfb_beam.cli.run_pipeline import (
    DEFAULT_FK_CANDIDATE_CAP,
    in_set_parent_edges,
    resolve_driven_uniqueness_mode,
    resolve_fanout,
    resolve_table_rows,
)
from sdfb_core.contracts import TableSchema
from sdfb_core.contracts.relationships import RelationshipRegistry

_MODEL = """
model: kw
tables:
  B_TABLE:
    pk: [D_COL_001]
  C_TABLE:
    pk: [D_COL_001, C_COL_002]
    fk:
      - cols: [D_COL_001, D_COL_024]
        ref: B_TABLE
        ref_cols: [D_COL_001, D_COL_024]
  A_TABLE:
    pk: [D_COL_024, X]
    fk:
      - cols: [D_COL_024]
        ref: C_TABLE
        ref_cols: [D_COL_024]
        drives: true
      - cols: [D_COL_024]
        ref: B_TABLE
        ref_cols: [D_COL_024]
"""
_REG = RelationshipRegistry.from_sources([("config/relationships/kw.yaml",
                                           _MODEL)])
_SCHEMA = TableSchema.model_validate({
    "table_info": {
        "table_id": "p.src.C_TABLE"
    },
    "schema": [{
        "name": n,
        "type": "STRING",
        "mode": "REQUIRED"
    } for n in ("D_COL_001", "D_COL_024", "C_COL_002")]
})
_ROWS = [{
    "D_COL_001": f"K{i}",
    "D_COL_024": "A",
    "C_COL_002": "xy"[i % 2]
} for i in range(40)]


@pytest.fixture(autouse=True)
def _stub_pk_measurement(monkeypatch):
  """ADR 0038 fix J added ONE extra BigQuery scan to `resolve_fanout` —
    the declared PK, measured on the source child. Every test here drives
    the launcher with a fake/absent client, so the default is a stub that
    reports a clean key (rows == key tuples); the tests that are ABOUT
    the measurement override it with their own."""
  import sdfb_beam.cli.run_pipeline as rp

  monkeypatch.setattr(
      rp,
      "measure_pk_uniqueness",
      lambda **kw: {
          "cols": list(kw["pk_cols"]),
          "rows": 10,
          "key_tuples": 10,
          "max_rows_per_key": 1
      },
  )


class _Store:

  def __init__(self):
    self.saved = {}

  def get(self, t, cols, sha):
    return self.saved.get((t, cols, sha))

  def put(self, t, cols, sha, payload):
    self.saved[(t, cols, sha)] = payload


def _measure(**kw):
  return {
      "histogram": {
          "0": 1,
          "2": 1
      },
      "cells": {
          "cols": ["C_COL_002"],
          "rows": [["x"], ["y"]],
          "counts": [1.0, 1.0]
      },
      "parents": 2,
      "children": 2
  }


def test_resolve_fanout_measures_the_driving_edge_and_caches(monkeypatch):
  import sdfb_beam.cli.run_pipeline as rp

  monkeypatch.setattr(rp, "measure_fanout", _measure)
  store = _Store()
  payload, roles = resolve_fanout(
      _REG,
      "proj.synthetic_data.C_TABLE",
      "proj.src.C_TABLE",
      in_set_names={"B_TABLE", "C_TABLE", "A_TABLE"},
      reference_rows=_ROWS,
      table_schema=_SCHEMA,
      stats_store=store,
      bq_client=object(),
  )
  assert payload["driving_cols"] == ["D_COL_001", "D_COL_024"]
  assert payload["exact_cells"] is True and payload["cells"]["cols"] == [
      "C_COL_002"
  ]
  assert list(roles.values()) == ["driving"]
  assert store.saved  # cached under the model sha
  # second call hits the cache: measure_fanout must not run
  monkeypatch.setattr(
      rp, "measure_fanout", lambda **kw:
      (_ for _ in ()).throw(AssertionError("measured twice")))
  again, _ = resolve_fanout(
      _REG,
      "proj.synthetic_data.C_TABLE",
      "proj.src.C_TABLE",
      in_set_names={"B_TABLE", "C_TABLE", "A_TABLE"},
      reference_rows=_ROWS,
      table_schema=_SCHEMA,
      stats_store=store,
      bq_client=object(),
  )
  assert again == payload


def test_root_has_no_fanout():
  payload, roles = resolve_fanout(
      _REG,
      "proj.synthetic_data.B_TABLE",
      "proj.src.B_TABLE",
      in_set_names={"B_TABLE", "C_TABLE", "A_TABLE"},
      reference_rows=[],
      table_schema=_SCHEMA,
      stats_store=None,
      bq_client=None,
  )
  assert payload is None and roles == {}


def test_edges_get_their_modes():
  roles = _REG.edge_roles("A_TABLE")
  edges = in_set_parent_edges(
      _REG,
      "proj.synthetic_data.A_TABLE",
      in_set_names={"B_TABLE", "C_TABLE", "A_TABLE"},
      key_sample_caps={},
      edge_roles=roles,
      keys_per_batch=250,
  )
  assert [(e.parent_landing.rsplit(".", 1)[-1], e.mode) for e in edges
         ] == [("C_TABLE", "fanout"), ("B_TABLE", "implied")]
  assert edges[0].keys_per_batch == 250


def test_driven_uniqueness_mode():
  assert resolve_driven_uniqueness_mode(
      "streaming", driven=True, identity_cols=()) == "streaming"
  assert resolve_driven_uniqueness_mode(
      "streaming", driven=True, identity_cols=("X",)) == "exact"
  assert resolve_driven_uniqueness_mode(
      "streaming", driven=False, identity_cols=()) is None


def test_driven_child_without_derived_rows_stops():
  with pytest.raises(SystemExit, match="preflight P4"):
    resolve_table_rows(
        "proj.synthetic_data.C_TABLE",
        driven=True,
        derived_rows=None,
        launch_rows=1000,
    )
  assert resolve_table_rows(
      "proj.synthetic_data.C_TABLE",
      driven=True,
      derived_rows=40,
      launch_rows=1000,
  ) == 40
  assert resolve_table_rows(
      "proj.synthetic_data.C_TABLE",
      driven=False,
      derived_rows=None,
      launch_rows=1000,
  ) == 1000


def test_resolve_fanout_stops_on_unknown_driving_columns(monkeypatch):
  import sdfb_beam.cli.run_pipeline as rp

  monkeypatch.setattr(
      rp,
      "measure_fanout",
      lambda **kw:
      (_ for _ in ()).throw(AssertionError("measure_fanout must not run")),
  )
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.src.C_TABLE"
      },
      "schema": [{
          "name": n,
          "type": "STRING",
          "mode": "REQUIRED"
      } for n in ("D_COL_001", "C_COL_002")]
  }  # D_COL_024 missing
                                     )
  with pytest.raises(SystemExit, match="preflight P2"):
    resolve_fanout(
        _REG,
        "proj.synthetic_data.C_TABLE",
        "proj.src.C_TABLE",
        in_set_names={"B_TABLE", "C_TABLE", "A_TABLE"},
        reference_rows=_ROWS,
        table_schema=schema,
        stats_store=None,
        bq_client=object(),
    )


def test_resolve_fanout_reports_a_measurement_failure_as_a_preflight_stop(
    monkeypatch):
  import sdfb_beam.cli.run_pipeline as rp

  monkeypatch.setattr(
      rp,
      "measure_fanout",
      lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
  )
  with pytest.raises(SystemExit, match="fan-out measurement"):
    resolve_fanout(
        _REG,
        "proj.synthetic_data.C_TABLE",
        "proj.src.C_TABLE",
        in_set_names={"B_TABLE", "C_TABLE", "A_TABLE"},
        reference_rows=_ROWS,
        table_schema=_SCHEMA,
        stats_store=None,
        bq_client=object(),
    )


class _BrokenStore:
  """A cache whose table does not exist: every call raises, as the
    BigQuery client does with NotFound."""

  def get(self, t, cols, sha):
    raise RuntimeError(
        "404 Not found: Table p:synthetic_data_quality.fk_fanout_stats")

  def put(self, t, cols, sha, payload):
    raise RuntimeError(
        "404 Not found: Table p:synthetic_data_quality.fk_fanout_stats")


def test_a_missing_cache_table_is_optional(monkeypatch, caplog):
  """The fk_fanout_stats table is a convenience, not a prerequisite: a
    cache that cannot be read or written warns once and the launch
    measures without it (2026-09-10 operator ask)."""
  import logging

  import sdfb_beam.cli.run_pipeline as rp

  monkeypatch.setattr(rp, "measure_fanout", _measure)
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    payload, _roles = resolve_fanout(
        _REG,
        "proj.synthetic_data.C_TABLE",
        "proj.src.C_TABLE",
        in_set_names={"B_TABLE", "C_TABLE", "A_TABLE"},
        reference_rows=_ROWS,
        table_schema=_SCHEMA,
        stats_store=_BrokenStore(),
        bq_client=object(),
    )
  assert payload is not None and payload["driving_cols"] == [
      "D_COL_001", "D_COL_024"
  ]
  assert "name=fk_fanout_cache_unavailable" in caplog.text
  assert "name=fk_fanout_measured" in caplog.text and "source=measured" in caplog.text


# --- ADR 0037: the flag, the plan payload, the WARNING milestones -----

_DIAMOND = """
model: diamond
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
  BOTTOM_TABLE:
    pk: [T, L, R]
    fk:
      - cols: [T, L]
        ref: LEFT_TABLE
        ref_cols: [T, L]
      - cols: [T, R]
        ref: RIGHT_TABLE
        ref_cols: [T, R]
      - cols: [T]
        ref: ds.EXT_TABLE
        ref_cols: [T]
"""
_DIAMOND_REG = RelationshipRegistry.from_sources([
    ("config/relationships/diamond.yaml", _DIAMOND)
])
_DIAMOND_IN_SET = frozenset(f"p.land.{t}" for t in ("TOP_TABLE", "LEFT_TABLE",
                                                    "RIGHT_TABLE",
                                                    "BOTTOM_TABLE"))


def _diamond_schema(r_mode: str = "NULLABLE") -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.land.BOTTOM_TABLE"
      },
      "schema": [{
          "name": "T",
          "type": "STRING",
          "mode": "REQUIRED"
      }, {
          "name": "L",
          "type": "STRING",
          "mode": "REQUIRED"
      }, {
          "name": "R",
          "type": "STRING",
          "mode": r_mode
      }]
  })


_DIAMOND_ROWS = [{
    "T": f"t{i % 2}",
    "L": f"l{i // 2}",
    "R": f"r{i % 2}"
} for i in range(40)]


def _measure_one_to_one(**kw):
  """One child per parent, two PK-completing cells."""
  return {
      "histogram": {
          "1": 4
      },
      "parents": 4,
      "children": 4,
      "cells": {
          "cols": ["R"],
          "rows": [["r0"], ["r1"]],
          "counts": [0.5, 0.5]
      }
  }


def _diamond_args(landing: str, **over):
  from sdfb_beam.cli.run_pipeline import parse_args

  args, _ = parse_args([
      "--reference_table",
      "p.src.BOTTOM_TABLE",
      "--landing_table",
      landing,
      "--dlq_table",
      "p.dq.dlq",
      "--num_rows",
      "100",
      "--run_id",
      "adr0037",
      "--source_stats",
      "off",
      "--model_uri",
      "gs://b/models/m/v/",
  ])
  for key, value in over.items():
    setattr(args, key, value)
  return args


def test_fk_candidate_cap_is_a_flag_and_reaches_launch_config():
  from sdfb_beam.cli.run_pipeline import parse_args

  args, _ = parse_args([
      "--reference_table",
      "p.src.T",
      "--landing_table",
      "p.land.T",
      "--dlq_table",
      "p.dq.dlq",
      "--num_rows",
      "10",
      "--run_id",
      "r",
      "--model_uri",
      "gs://b/models/m/v/",
  ])
  assert args.fk_candidate_cap == 64
  # launch_config logs every non-private arg verbatim.
  logged = {
      k: v
      for k, v in sorted(vars(args).items())
      if not k.startswith("_") and k != "model_client"
  }
  assert logged["fk_candidate_cap"] == 64
  over, _ = parse_args([
      "--reference_table",
      "p.src.T",
      "--landing_table",
      "p.land.T",
      "--dlq_table",
      "p.dq.dlq",
      "--num_rows",
      "10",
      "--run_id",
      "r",
      "--model_uri",
      "gs://b/models/m/v/",
      "--fk_candidate_cap",
      "16",
  ])
  assert over.fk_candidate_cap == 16


def test_the_fanout_payload_carries_the_conditional_edges_and_the_cap(
    monkeypatch):
  """Ruling 1: BOTH keys land on the payload the workers read."""
  import sdfb_beam.cli.run_pipeline as rp

  monkeypatch.setattr(rp, "measure_fanout", _measure_one_to_one)
  args = _diamond_args("p.land.BOTTOM_TABLE", fk_candidate_cap=32)
  payload, roles = rp._resolve_table_fanout(
      args,
      _diamond_schema(),
      _DIAMOND_REG,
      {"TOP_TABLE", "LEFT_TABLE", "RIGHT_TABLE", "BOTTOM_TABLE"},
      _DIAMOND_ROWS,
  )
  # G1/G2: `R` completes BOTTOM's declared `pk: [T, L, R]`, so it can
  # never NULL-fill (the record model rejects a NULL there) and it IS
  # the edge that bounds a key's capacity.
  assert payload["conditional"] == [{
      "id": "(T,R)->RIGHT_TABLE",
      "cols": ["R"],
      "nullable": False,
      "pk_member": True
  }]
  # Fix wave F1: `M` is the size of the Top-M candidate SAMPLE shared by
  # every driving key carrying the same join value — NOT a per-key
  # allotment — so the measured max fan-out (1 here) must not clamp it.
  # The payload carries `--fk_candidate_cap` exactly as the operator set it.
  assert payload["candidate_cap"] == 32
  assert sorted(roles.values()) == ["conditional", "driving", "external"]


def test_a_root_table_gets_no_conditional_payload(monkeypatch):
  import sdfb_beam.cli.run_pipeline as rp

  payload, _roles = rp._resolve_table_fanout(
      _diamond_args("p.land.TOP_TABLE"),
      _diamond_schema(),
      _DIAMOND_REG,
      {"TOP_TABLE", "LEFT_TABLE", "RIGHT_TABLE", "BOTTOM_TABLE"},
      _DIAMOND_ROWS,
  )
  assert payload is None


def test_the_launcher_names_a_defaulted_driving_edge_and_an_external_overlap(
    monkeypatch, caplog):
  import logging

  import sdfb_beam.cli.run_pipeline as rp

  monkeypatch.setattr(rp, "load_reference_rows", lambda **kw: _DIAMOND_ROWS)
  monkeypatch.setattr(rp, "measure_fanout", _measure_one_to_one)
  monkeypatch.setattr(
      rp,
      "load_fk_key_pools",
      lambda fks, landing: [{
          "cols": list(fk.cols),
          "keys": [tuple("t0" for _ in fk.cols)]
      } for fk in fks],
  )
  args = _diamond_args("p.land.BOTTOM_TABLE")
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    result = rp._load_reference_and_preflight(
        args,
        _diamond_schema(),
        _DIAMOND_REG,
        in_set_landing=_DIAMOND_IN_SET,
    )
  fanout, edge_roles = result[5], result[6]
  assert fanout["conditional"] == [{
      "id": "(T,R)->RIGHT_TABLE",
      "cols": ["R"],
      "nullable": False,
      "pk_member": True
  }]
  assert fanout["candidate_cap"] == DEFAULT_FK_CANDIDATE_CAP  # the flag
  assert sorted(edge_roles.values()) == ["conditional", "driving", "external"]

  lines = caplog.text.splitlines()
  defaulted = [ln for ln in lines if "name=fk_driving_edge_defaulted" in ln]
  assert len(defaulted) == 1
  assert "edge='(T,L)->LEFT_TABLE'" in defaulted[0]
  assert "mark drives: true to choose" in defaulted[0]
  external = [ln for ln in lines if "name=fk_edge_overlap_external" in ln]
  assert len(external) == 1
  assert "edge='(T)->ds.EXT_TABLE'" in external[0] and "overlap=T" in external[0]
  # Ruling 2: the existing per-edge role milestone gains overlap=.
  conditional = [
      ln for ln in lines
      if "name=fk_edge_role" in ln and "role=conditional" in ln
  ]
  assert len(conditional) == 1 and "overlap=T" in conditional[0]
  assert not any(
      "name=fk_edge_role" in ln and "role=driving" in ln and "overlap=" in ln
      for ln in lines)


def test_a_single_edge_child_is_not_warned_about(monkeypatch, caplog):
  import logging

  import sdfb_beam.cli.run_pipeline as rp

  monkeypatch.setattr(rp, "load_reference_rows", lambda **kw: _DIAMOND_ROWS)
  monkeypatch.setattr(rp, "measure_fanout", _measure_one_to_one)
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.land.LEFT_TABLE"
      },
      "schema": [{
          "name": "T",
          "type": "STRING",
          "mode": "REQUIRED"
      }, {
          "name": "L",
          "type": "STRING",
          "mode": "REQUIRED"
      }]
  })
  args = _diamond_args("p.land.LEFT_TABLE")
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    result = rp._load_reference_and_preflight(
        args,
        schema,
        _DIAMOND_REG,
        in_set_landing=_DIAMOND_IN_SET,
    )
  assert result[5]["conditional"] == []
  assert "name=fk_driving_edge_defaulted" not in caplog.text
  assert "name=fk_edge_overlap_external" not in caplog.text


def test_fk_edge_metadata_names_the_dag_path_of_every_in_set_edge():
  """Task 8 reads `mode`/`overlap` off these dicts in the worker log."""
  import sdfb_beam.cli.run_pipeline as rp

  edges = rp.fk_edge_metadata(
      _DIAMOND_REG,
      "p.land.BOTTOM_TABLE",
      _DIAMOND_REG.relations("BOTTOM_TABLE"),
      "p.land",
      in_set_names={"TOP_TABLE", "LEFT_TABLE", "RIGHT_TABLE", "BOTTOM_TABLE"},
      edge_roles=_DIAMOND_REG.edge_roles("BOTTOM_TABLE"),
  )
  assert [(e["cols"], e.get("mode"), e.get("overlap")) for e in edges] == [
      (["T", "L"], "fanout", None),
      (["T", "R"], "conditional", ["T"]),
      (["T"], None, None),  # external: no mode key, the worker defaults
  ]
  assert edges[0]["parent_landing"] == "p.land.LEFT_TABLE"


def test_fk_edge_metadata_without_roles_is_todays_dict():
  import sdfb_beam.cli.run_pipeline as rp

  edges = rp.fk_edge_metadata(
      _DIAMOND_REG,
      "p.land.BOTTOM_TABLE",
      _DIAMOND_REG.relations("BOTTOM_TABLE"),
      "",
      in_set_names=set(),
      edge_roles={},
  )
  assert [set(e) for e in edges
         ] == [{"cols", "ref", "ref_cols", "enforced", "parent_landing"}] * 3


# --- ADR 0037 §6: PK members another edge supplies ---------------------


def test_resolve_fanout_does_not_measure_cells_over_a_conditional_member(
    monkeypatch,):
  """`R` sits in BOTTOM_TABLE's PK but the conditional edge supplies
    it, so the launcher must not ask BigQuery for a cell table over it —
    the cells are the members nothing else fills."""
  import sdfb_beam.cli.run_pipeline as rp

  seen: dict = {}

  def _spy(**kw):
    seen.update(kw)
    return _measure_one_to_one(**kw)

  monkeypatch.setattr(rp, "measure_fanout", _spy)
  payload, _roles = resolve_fanout(
      _DIAMOND_REG,
      "p.land.BOTTOM_TABLE",
      "p.src.BOTTOM_TABLE",
      in_set_names={"TOP_TABLE", "LEFT_TABLE", "RIGHT_TABLE", "BOTTOM_TABLE"},
      reference_rows=_DIAMOND_ROWS,
      table_schema=_diamond_schema(),
      stats_store=None,
      bq_client=object(),
  )
  assert seen["cell_cols"] == ()
  assert payload["exact_cells"] is True


_STAR37 = """
model: star37
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
_STAR37_REG = RelationshipRegistry.from_sources([
    ("config/relationships/star37.yaml", _STAR37)
])
_STAR37_IN_SET = frozenset(
    f"p.land.{t}" for t in ("A_TABLE", "B_TABLE", "F_TABLE"))
_STAR37_SCHEMA = TableSchema.model_validate({
    "table_info": {
        "table_id": "p.land.F_TABLE"
    },
    "schema": [{
        "name": "A_ID",
        "type": "STRING",
        "mode": "REQUIRED"
    }, {
        "name": "B_ID",
        "type": "STRING",
        "mode": "REQUIRED"
    }, {
        "name": "SEQ",
        "type": "INT64",
        "mode": "REQUIRED"
    }]
})
_STAR37_ROWS = [{
    "A_ID": f"A1B2{i:020X}",
    "B_ID": f"B{i % 6}",
    "SEQ": i
} for i in range(40)]


def test_an_independent_pk_member_gets_a_sized_key_pool(monkeypatch):
  """The cap P4 counted as a per-key factor is the SAME number the
    composer broadcasts — it travels back out on `PreflightResult` and
    into `in_set_parent_edges`, exactly as the random-draw path's caps
    do (ADR 0035)."""
  import sdfb_beam.cli.run_pipeline as rp
  from sdfb_core.engines.pk_capacity import FK_KEY_SAMPLE_FLOOR

  monkeypatch.setattr(rp, "load_reference_rows", lambda **kw: _STAR37_ROWS)
  monkeypatch.setattr(
      rp, "measure_fanout", lambda **kw: {
          "histogram": {
              "2": 4
          },
          "parents": 4,
          "children": 8,
          "cells": None
      })
  args = _diamond_args("p.land.F_TABLE", reference_table="p.src.F_TABLE")
  pf = rp._load_reference_and_preflight(
      args,
      _STAR37_SCHEMA,
      _STAR37_REG,
      in_set_landing=_STAR37_IN_SET,
  )[1]
  # --num_rows 100 is the upper bound on what this child lands (its
  # DERIVED count is the output of this very preflight), and B_TABLE
  # lands at most that, so the sized pool is bounded by the parent.
  assert pf.fk_key_sample_caps == {("B_ID",): 100}
  edges = in_set_parent_edges(
      _STAR37_REG,
      "p.land.F_TABLE",
      in_set_names={"A_TABLE", "B_TABLE", "F_TABLE"},
      key_sample_caps=pf.fk_key_sample_caps,
      edge_roles=_STAR37_REG.edge_roles("F_TABLE"),
  )
  assert [(e.mode, e.key_sample_cap) for e in edges] == [("fanout",
                                                          FK_KEY_SAMPLE_FLOOR),
                                                         ("side_input", 100)]


# --- ADR 0037 fix round 1: metadata matching, cap validation, nullability ---

# An enforced composite edge and a DOCUMENTED prefix edge to the SAME
# parent: the declared `(T)` must never inherit the enforced `(T,R)`'s mode.
_PREFIX = """
model: prefix
tables:
  P_TABLE:
    pk: [T, R]
  CH_TABLE:
    pk: [T, R, S]
    fk:
      - cols: [T, R]
        ref: P_TABLE
        ref_cols: [T, R]
      - cols: [T]
        ref: P_TABLE
        ref_cols: [T]
        enforced: false
"""
_PREFIX_REG = RelationshipRegistry.from_sources([
    ("config/relationships/prefix.yaml", _PREFIX)
])


def test_a_documented_prefix_edge_never_inherits_the_enforced_edges_mode():
  """`fk_edges` says which DAG path each edge took — a documented edge
    took none, so it must carry no mode at all (it renders as the
    worker's `side_input` default)."""
  import sdfb_beam.cli.run_pipeline as rp

  edges = rp.fk_edge_metadata(
      _PREFIX_REG,
      "p.land.CH_TABLE",
      _PREFIX_REG.relations("CH_TABLE"),
      "p.land",
      in_set_names={"P_TABLE", "CH_TABLE"},
      edge_roles=_PREFIX_REG.edge_roles("CH_TABLE"),
  )
  assert [(e["cols"], e["enforced"], e.get("mode")) for e in edges] == [
      (["T", "R"], True, "fanout"),
      (["T"], False, None),
  ]


def test_fk_candidate_cap_below_one_stops_the_launch():
  """0 divided the keys_per_batch bound; a negative cap emptied every
    candidate list silently."""
  from sdfb_beam.cli.run_pipeline import parse_args

  base = [
      "--reference_table",
      "p.src.T",
      "--landing_table",
      "p.land.T",
      "--dlq_table",
      "p.dq.dlq",
      "--num_rows",
      "10",
      "--run_id",
      "r",
      "--model_uri",
      "gs://b/models/m/v/",
  ]
  for bad in ("0", "-3"):
    with pytest.raises(SystemExit, match=r"--fk_candidate_cap"):
      parse_args([*base, "--fk_candidate_cap", bad])
  ok, _ = parse_args([*base, "--fk_candidate_cap", "1"])
  assert ok.fk_candidate_cap == 1


def _landing_schema(r_mode: str) -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.land.BOTTOM_TABLE"
      },
      "schema": [{
          "name": "T",
          "type": "STRING",
          "mode": "REQUIRED"
      }, {
          "name": "L",
          "type": "STRING",
          "mode": "REQUIRED"
      }, {
          "name": "R",
          "type": "STRING",
          "mode": r_mode
      }]
  })


def test_resolve_schemas_returns_the_landing_schema_alongside(monkeypatch):
  import sdfb_beam.cli.run_pipeline as rp

  source, landing = _diamond_schema("NULLABLE"), _landing_schema("REQUIRED")
  monkeypatch.setattr(
      rp,
      "extract_table_schema",
      lambda fqn: source if fqn == "p.src.B" else landing,
  )
  generation, target = rp.resolve_schemas("", "p.src.B", "p.land.B")
  assert [c.mode for c in generation.columns
         ] == ["REQUIRED", "REQUIRED", "NULLABLE"]
  assert target is landing
  # The one-schema wrapper every other call site uses is unchanged.
  assert rp.resolve_table_schema("", "p.src.B",
                                 "p.land.B").columns == generation.columns


def test_an_unreachable_landing_table_surfaces_no_schema(monkeypatch, caplog):
  import logging

  import sdfb_beam.cli.run_pipeline as rp

  source = _diamond_schema("NULLABLE")

  def _extract(fqn: str):
    if fqn == "p.src.B":
      return source
    raise RuntimeError("landing table not found")

  monkeypatch.setattr(rp, "extract_table_schema", _extract)
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    _generation, target = rp.resolve_schemas("", "p.src.B", "p.land.B")
  assert target is None


def test_nullable_needs_both_schemas_to_agree(monkeypatch, caplog):
  """Fix wave A4 (blocker): the LANDING schema is the sink, but the
    engines validate every generated row against a record model derived
    from the GENERATION schema, whose modes mirror the SOURCE table. When
    landing said NULLABLE and generation said REQUIRED, the DoFn kept the
    unmatched key, the engine wrote None, `model_validate` rejected it
    and the engine's `except Exception: continue` discarded every one of
    that key's rows — no envelope, no counter, no milestone. Nullable now
    needs BOTH; a disagreement drops the key visibly as `fk.unmatched`
    and says so once.

    Read on `_NO_PK_DIAMOND_REG`: this is the MODE rule in isolation, and
    on the canonical diamond `R` sits in the declared `pk:`, which
    refuses the NULL whatever the modes say (fix wave G2, covered by
    `test_the_model_declared_pk_blocks_the_null_fill`)."""
  import logging

  import sdfb_beam.cli.run_pipeline as rp

  monkeypatch.setattr(rp, "measure_fanout", _measure_one_to_one)
  names = {"TOP_TABLE", "LEFT_TABLE", "RIGHT_TABLE", "BOTTOM_TABLE"}
  args = _diamond_args("p.land.BOTTOM_TABLE")

  def _resolve(generation: str, landing: str):
    return rp._resolve_table_fanout(
        args,
        _diamond_schema(generation),
        _NO_PK_DIAMOND_REG,
        names,
        _DIAMOND_ROWS,
        landing_schema=_landing_schema(landing),
    )[0]

  # generation NULLABLE, landing REQUIRED -> the sink refuses the NULL.
  assert _resolve("NULLABLE", "REQUIRED")["conditional"] == [{
      "id": "(T,R)->RIGHT_TABLE",
      "cols": ["R"],
      "nullable": False,
      "pk_member": False
  }]
  # generation REQUIRED, landing NULLABLE -> the RECORD MODEL refuses it.
  # (The case above disagrees too, and warned — clear it so this
  # assertion counts ONE edge's milestone, not the file's history.)
  caplog.clear()
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    payload = _resolve("REQUIRED", "NULLABLE")
  assert payload["conditional"] == [{
      "id": "(T,R)->RIGHT_TABLE",
      "cols": ["R"],
      "nullable": False,
      "pk_member": False
  }]
  mismatch = [
      ln for ln in caplog.text.splitlines()
      if "name=fk_nullable_schema_mismatch" in ln
  ]
  assert len(mismatch) == 1
  assert "edge='(T,R)->RIGHT_TABLE'" in mismatch[0]
  assert "landing=NULLABLE" in mismatch[
      0] and "generation=REQUIRED" in mismatch[0]
  # Both NULLABLE -> the ADR 0037 NULL-fill branch, as before.
  assert _resolve("NULLABLE", "NULLABLE")["conditional"] == [{
      "id": "(T,R)->RIGHT_TABLE",
      "cols": ["R"],
      "nullable": True,
      "pk_member": False
  }]


def test_a_rest_column_in_the_declared_pk_is_never_nullable(
    monkeypatch, caplog):
  """Fix wave F2: a column MODE is not the whole nullability contract.

    `derive_record_model` builds a PK base that rejects `None` on every
    column of the GENERATION schema's declared `primary_keys`, REGARDLESS
    of mode (`_make_pk_base`), and both engines swallow the resulting
    ValidationError with `except Exception: continue` — so a NULL-filled
    row on a declared-PK rest column disappears with no envelope, counter
    or milestone: exactly the silent loss fix wave A4 set out to close.
    On ADR 0037's own diamond the child PK's last member IS the
    co-parent's column, so this is the default shape, not a corner.
    """
  import logging

  import sdfb_beam.cli.run_pipeline as rp

  monkeypatch.setattr(rp, "measure_fanout", _measure_one_to_one)
  generation = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.land.BOTTOM_TABLE"
      },
      "schema": [{
          "name": "T",
          "type": "STRING",
          "mode": "REQUIRED"
      }, {
          "name": "L",
          "type": "STRING",
          "mode": "REQUIRED"
      }, {
          "name": "R",
          "type": "STRING",
          "mode": "NULLABLE"
      }],
      "primary_keys": ["T", "L", "R"]
  })
  args = _diamond_args("p.land.BOTTOM_TABLE")
  caplog.clear()
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    payload, _roles = rp._resolve_table_fanout(
        args,
        generation,
        _DIAMOND_REG,
        {"TOP_TABLE", "LEFT_TABLE", "RIGHT_TABLE", "BOTTOM_TABLE"},
        _DIAMOND_ROWS,
        landing_schema=_landing_schema("NULLABLE"),
    )
  # BOTH schemas say NULLABLE — and the record model still refuses it.
  assert payload["conditional"] == [{
      "id": "(T,R)->RIGHT_TABLE",
      "cols": ["R"],
      "nullable": False,
      "pk_member": True
  }]
  mismatch = [
      ln for ln in caplog.text.splitlines()
      if "name=fk_nullable_schema_mismatch" in ln
  ]
  assert len(mismatch) == 1
  assert "edge='(T,R)->RIGHT_TABLE'" in mismatch[0]
  # The operator has to see WHY the edge lost its NULL branch.
  assert "reason=declared_pk" in mismatch[0] and "pk=R" in mismatch[0]
  # The composer spec agrees with the plan entry.
  edges = in_set_parent_edges(
      _DIAMOND_REG,
      "p.land.BOTTOM_TABLE",
      in_set_names={"TOP_TABLE", "LEFT_TABLE", "RIGHT_TABLE", "BOTTOM_TABLE"},
      key_sample_caps={},
      edge_roles=_DIAMOND_REG.edge_roles("BOTTOM_TABLE"),
      table_schema=_landing_schema("NULLABLE"),
      generation_schema=generation,
      effective_pk=("T", "L", "R"),
  )
  assert [e.nullable for e in edges if e.mode == "conditional"] == [False]


def test_a_declared_pk_that_misses_the_rest_columns_keeps_the_null_branch(
    monkeypatch,):
  """The PK rule is per-column: a declared PK that does not name the
    edge's `rest` leaves ADR 0037's NULL-fill branch exactly as it was.

    On `_NO_PK_DIAMOND_REG`, so the DDL constraint `[T, L]` IS the
    enforced PK (fix wave G2's fallback) — the model declares none."""
  import sdfb_beam.cli.run_pipeline as rp

  monkeypatch.setattr(rp, "measure_fanout", _measure_one_to_one)
  generation = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.land.BOTTOM_TABLE"
      },
      "schema": [{
          "name": "T",
          "type": "STRING",
          "mode": "REQUIRED"
      }, {
          "name": "L",
          "type": "STRING",
          "mode": "REQUIRED"
      }, {
          "name": "R",
          "type": "STRING",
          "mode": "NULLABLE"
      }],
      "primary_keys": ["T", "L"]
  })
  payload, _roles = rp._resolve_table_fanout(
      _diamond_args("p.land.BOTTOM_TABLE"),
      generation,
      _NO_PK_DIAMOND_REG,
      {"TOP_TABLE", "LEFT_TABLE", "RIGHT_TABLE", "BOTTOM_TABLE"},
      _DIAMOND_ROWS,
      landing_schema=_landing_schema("NULLABLE"),
  )
  assert payload["conditional"] == [{
      "id": "(T,R)->RIGHT_TABLE",
      "cols": ["R"],
      "nullable": True,
      "pk_member": False
  }]


def test_the_candidate_cap_is_the_operators_flag_not_the_measured_fanout(
    monkeypatch, caplog):
  """Fix wave F1 (reverting A3): `M` bounds the Top-M candidate SAMPLE
    the composer keeps per JOIN VALUE — ONE sample shared by every driving
    key that carries that value — not a per-key allotment.

    Clamping it to the measured max fan-out collapses a 1:1 driving edge to
    `M = 1`, so `Top.SmallestPerKey` keeps exactly one of the co-parent's
    rows for that value and every child across every driving key lands the
    identical candidate: a point mass per shared value, with the co-parent's
    other rows never referenced. The payload and the composer spec carry
    `--fk_candidate_cap` verbatim; the payload-size argument A3 was reaching
    for is already served by the `keys_per_batch` bound.
    """
  import logging

  import sdfb_beam.cli.run_pipeline as rp

  # The clamp helper is gone: nothing derives a cap from a histogram.
  assert not hasattr(rp, "effective_candidate_cap")

  monkeypatch.setattr(
      rp,
      "measure_fanout",
      lambda **kw: {
          "histogram": {
              "1": 2,
              "9": 2
          },
          "parents": 4,
          "children": 20,
          "cells": {
              "cols": ["R"],
              "rows": [["r0"], ["r1"]],
              "counts": [0.5, 0.5]
          }
      },
  )
  args = _diamond_args("p.land.BOTTOM_TABLE", fk_candidate_cap=32)
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    payload, _roles = rp._resolve_table_fanout(
        args,
        _diamond_schema(),
        _DIAMOND_REG,
        {"TOP_TABLE", "LEFT_TABLE", "RIGHT_TABLE", "BOTTOM_TABLE"},
        _DIAMOND_ROWS,
    )
  assert payload["candidate_cap"] == 32  # max_k=9 does NOT clamp it
  assert "name=fk_candidate_cap_effective" not in caplog.text
  # The composer spec carries the SAME number, so the Top-M combine and
  # the per-request payload cannot drift apart.
  edges = in_set_parent_edges(
      _DIAMOND_REG,
      "p.land.BOTTOM_TABLE",
      in_set_names={"TOP_TABLE", "LEFT_TABLE", "RIGHT_TABLE", "BOTTOM_TABLE"},
      key_sample_caps={},
      edge_roles=_DIAMOND_REG.edge_roles("BOTTOM_TABLE"),
      table_schema=_landing_schema("NULLABLE"),
      candidate_cap=payload["candidate_cap"],
  )
  conditional = [e for e in edges if e.mode == "conditional"]
  assert conditional and all(e.candidate_cap == 32 for e in conditional)


# Fix wave F3: the classic DENORMALISED child — every parent EXTERNAL and
# on one ancestry line, the narrower edge's column contained in the wider
# one. This launched fine at 2728203; the cross-edge ownership stop must
# not fire on it, because none of the stop's remedies can be applied to an
# external edge.
_DENORM = """
model: denorm
tables:
  CH_TABLE:
    pk: [A_KEY, B_KEY]
    fk:
      - cols: [A_KEY]
        ref: ds.A_TABLE
        ref_cols: [A_KEY]
      - cols: [A_KEY, B_KEY]
        ref: ds.B_TABLE
        ref_cols: [A_KEY, B_KEY]
"""
_DENORM_REG = RelationshipRegistry.from_sources([
    ("config/relationships/denorm.yaml", _DENORM)
])


def test_two_external_parents_warn_instead_of_stopping_the_launch(caplog):
  """Fix wave F3: both parents are outside the launch, so the launcher
    NAMES the overlap once and runs — a warning, not a stop."""
  import logging

  import sdfb_beam.cli.run_pipeline as rp

  roles = _DENORM_REG.edge_roles("p.land.CH_TABLE")
  assert sorted(roles.values()) == ["external", "external"]
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    rp._log_edge_role_warnings("p.land.CH_TABLE", _DENORM_REG, roles)
  external = [
      ln for ln in caplog.text.splitlines()
      if "name=fk_edge_overlap_external" in ln
  ]
  assert len(external) == 1
  assert "edge='(A_KEY)->ds.A_TABLE'" in external[0]
  assert "other='(A_KEY,B_KEY)->ds.B_TABLE'" in external[0]
  assert "overlap=A_KEY" in external[0]


# G2: the PK the nullability guard must read is the run's EFFECTIVE one —
# the relationship model's `pk:` (ADR 0032), not `TableSchema.primary_keys`
# (the BigQuery table constraint the extractor copies into `_ddl.json` and
# its own docstring calls "useful context, never the source of truth").
_NO_PK_DIAMOND = _DIAMOND.replace("    pk: [T, L, R]\n", "")
_NO_PK_DIAMOND_REG = RelationshipRegistry.from_sources([
    ("config/relationships/diamond.yaml", _NO_PK_DIAMOND)
])


def test_the_model_declared_pk_blocks_the_null_fill(monkeypatch, caplog):
  """G2 (blocker): on the CANONICAL ADR 0032 setup the PK of record is
    the relationship model's `pk:` and `TableSchema.primary_keys` is
    None, so the fix-wave-F2 guard never fired. ADR 0037's own diamond
    (BOTTOM declares `pk: [T, L, R]`, `R` NULLABLE in both schemas) then
    NULL-filled a declared key member: the rows land and their repeats
    divert as `pk.duplicate`."""
  import logging

  import sdfb_beam.cli.run_pipeline as rp

  monkeypatch.setattr(rp, "measure_fanout", _measure_one_to_one)
  caplog.clear()
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    payload, _roles = rp._resolve_table_fanout(
        _diamond_args("p.land.BOTTOM_TABLE"),
        _diamond_schema("NULLABLE"),  # NO `primary_keys` at all
        _DIAMOND_REG,
        {"TOP_TABLE", "LEFT_TABLE", "RIGHT_TABLE", "BOTTOM_TABLE"},
        _DIAMOND_ROWS,
        landing_schema=_landing_schema("NULLABLE"),
    )
  assert payload["conditional"] == [{
      "id": "(T,R)->RIGHT_TABLE",
      "cols": ["R"],
      "nullable": False,
      "pk_member": True
  }]
  mismatch = [
      ln for ln in caplog.text.splitlines()
      if "name=fk_nullable_schema_mismatch" in ln
  ]
  assert len(mismatch) == 1
  assert "reason=declared_pk" in mismatch[0] and "pk=R" in mismatch[0]


def test_the_ddl_constraint_stands_in_when_the_model_declares_no_pk(
    monkeypatch, caplog):
  """The fallback half of G2: with no `pk:` in the model the guard
    reads the DDL constraint, exactly as fix wave F2 did."""
  import logging

  import sdfb_beam.cli.run_pipeline as rp

  monkeypatch.setattr(rp, "measure_fanout", _measure_one_to_one)
  generation = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.land.BOTTOM_TABLE"
      },
      "schema": [{
          "name": "T",
          "type": "STRING",
          "mode": "REQUIRED"
      }, {
          "name": "L",
          "type": "STRING",
          "mode": "REQUIRED"
      }, {
          "name": "R",
          "type": "STRING",
          "mode": "NULLABLE"
      }],
      "primary_keys": ["T", "L", "R"]
  })
  caplog.clear()
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    payload, _roles = rp._resolve_table_fanout(
        _diamond_args("p.land.BOTTOM_TABLE"),
        generation,
        _NO_PK_DIAMOND_REG,
        {"TOP_TABLE", "LEFT_TABLE", "RIGHT_TABLE", "BOTTOM_TABLE"},
        _DIAMOND_ROWS,
        landing_schema=_landing_schema("NULLABLE"),
    )
  # No model `pk:`, so nothing bounds the key — but the DDL constraint
  # still refuses the NULL the record model would reject.
  assert payload["conditional"] == [{
      "id": "(T,R)->RIGHT_TABLE",
      "cols": ["R"],
      "nullable": False,
      "pk_member": False
  }]
  assert "reason=declared_pk" in caplog.text


def test_an_external_overlap_is_named_in_a_single_table_launch(caplog):
  """G3: `_log_edge_role_warnings` returned early on empty roles and
    `_resolve_table_fanout` returns `{}` for a non-relational launch, so
    a denormalised child whose parents are BOTH external launched with no
    stop AND no signal — at run time the second pool overwrites the shared
    column and the rows divert as `fk.orphan`, with nothing in the launch
    log naming the overlap."""
  import logging

  import sdfb_beam.cli.run_pipeline as rp

  caplog.clear()
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    rp._log_edge_role_warnings("p.land.CH_TABLE", _DENORM_REG, {})
  external = [
      ln for ln in caplog.text.splitlines()
      if "name=fk_edge_overlap_external" in ln
  ]
  assert len(external) == 1
  assert "edge='(A_KEY)->ds.A_TABLE'" in external[0]
  assert "other='(A_KEY,B_KEY)->ds.B_TABLE'" in external[0]
  assert "overlap=A_KEY" in external[0]


def test_no_landing_schema_falls_back_to_the_source_and_says_so(
    monkeypatch, caplog):
  import logging

  import sdfb_beam.cli.run_pipeline as rp

  monkeypatch.setattr(rp, "measure_fanout", _measure_one_to_one)
  args = _diamond_args("p.land.BOTTOM_TABLE")
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    payload, _roles = rp._resolve_table_fanout(
        args,
        _diamond_schema("NULLABLE"),
        _NO_PK_DIAMOND_REG,
        {"TOP_TABLE", "LEFT_TABLE", "RIGHT_TABLE", "BOTTOM_TABLE"},
        _DIAMOND_ROWS,
        landing_schema=None,
    )
  assert payload["conditional"] == [
      # The SOURCE's mode — and no model `pk:` to override it.
      {
          "id": "(T,R)->RIGHT_TABLE",
          "cols": ["R"],
          "nullable": True,
          "pk_member": False
      }
  ]
  fallback = [
      ln for ln in caplog.text.splitlines()
      if "name=fk_nullable_from_source" in ln
  ]
  assert len(fallback) == 1
  assert "edge='(T,R)->RIGHT_TABLE'" in fallback[0]
  assert "table=p.land.BOTTOM_TABLE" in fallback[0]


def test_nullability_schema_prefers_the_landing_one():
  import sdfb_beam.cli.run_pipeline as rp

  source, landing = _diamond_schema("NULLABLE"), _landing_schema("REQUIRED")
  assert rp.nullability_schema(landing, source) is landing
  assert rp.nullability_schema(None, source) is source


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
_PARTIAL_SCHEMA = TableSchema.model_validate({
    "table_info": {
        "table_id": "p.land.F_TABLE"
    },
    "schema": [{
        "name": n,
        "type": "STRING",
        "mode": "REQUIRED"
    } for n in ("A_ID", "B_ID", "X")]
})
_PARTIAL_ROWS = [{
    "A_ID": f"A1B2{i:020X}",
    "B_ID": f"B{i % 6}",
    "X": f"X{i % 4}"
} for i in range(40)]


def test_measure_and_check_agree_on_a_partially_contained_edge(monkeypatch):
  """Ruling 12: `resolve_fanout`'s `known` and `_check_driven_pk`'s are
    the SAME set — the launcher asks for no cell table over `B_ID`, and
    P4 must not then demand one (review finding B-1)."""
  import sdfb_beam.cli.run_pipeline as rp
  from sdfb_beam.cli.preflight import edge_supplied_members

  seen: dict = {}

  def _spy(**kw):
    seen.update(kw)
    return {"histogram": {"1": 4}, "parents": 4, "children": 4, "cells": None}

  monkeypatch.setattr(rp, "measure_fanout", _spy)
  payload, roles = resolve_fanout(
      _PARTIAL_REG,
      "p.land.F_TABLE",
      "p.src.F_TABLE",
      in_set_names={"A_TABLE", "B_TABLE", "F_TABLE"},
      reference_rows=_PARTIAL_ROWS,
      table_schema=_PARTIAL_SCHEMA,
      stats_store=None,
      bq_client=object(),
  )
  assert seen["cell_cols"] == ()
  assert payload["exact_cells"] is True
  # The very set P4 will use, from the one shared helper.
  assert edge_supplied_members(
      ("A_ID", "B_ID"),
      roles,
      rp.conditional_rest_of(_PARTIAL_REG, "p.land.F_TABLE", roles),
  ).known == ("B_ID", "X")


def test_a_high_fanout_child_gets_a_pool_sized_for_its_derived_rows(
    monkeypatch,):
  """Finding B-2 end to end: --num_rows 100 with a mean fan-out of 30
    lands 3,000 rows, and the broadcast pool is sized for those."""
  import sdfb_beam.cli.run_pipeline as rp
  from sdfb_core.engines.pk_capacity import fk_key_sample_cap

  monkeypatch.setattr(rp, "load_reference_rows", lambda **kw: _STAR37_ROWS)
  monkeypatch.setattr(
      rp, "measure_fanout", lambda **kw: {
          "histogram": {
              "30": 4
          },
          "parents": 4,
          "children": 120,
          "cells": None
      })
  args = _diamond_args("p.land.F_TABLE", reference_table="p.src.F_TABLE")
  # The relational runner carries the already-resolved parent counts.
  args._rows_by_landing = {
      "p.land.A_TABLE": 10_000,
      "p.land.B_TABLE": 5_000_000
  }
  pf = rp._load_reference_and_preflight(
      args,
      _STAR37_SCHEMA,
      _STAR37_REG,
      in_set_landing=_STAR37_IN_SET,
  )[1]
  assert pf.derived_rows == 300_000  # 10k parents x mean 30
  assert pf.fk_key_sample_caps == {("B_ID",): fk_key_sample_cap(300_000, 1)}
  # --num_rows 100 would have sized the same pool at the ADR 0035 floor.
  assert fk_key_sample_cap(args.num_rows, 1) != fk_key_sample_cap(300_000, 1)


# G2 round 2 — the two PK sources are a UNION, never a replacement: the
# model's `pk:` is what the run ENFORCES, the DDL constraint is what the
# record model validates against, and a NULL in either is fatal.
_NARROW_PK_DIAMOND_REG = RelationshipRegistry.from_sources([(
    "config/relationships/diamond.yaml",
    _DIAMOND.replace("    pk: [T, L, R]\n", "    pk: [T, L]\n"),
)])


def test_the_two_pk_sources_are_unioned_not_replaced(monkeypatch, caplog):
  """A rest column the DDL declares a key member but the model's `pk:`
    omits must still refuse the NULL fill. `_enforced_pk` read the model
    PK as a REPLACEMENT, so this window reopened the fix-wave-F2 blocker:
    the edge came out nullable, the engine NULL-filled `R`, and the record
    model (built from `TableSchema.primary_keys`) rejected every one of
    that key's rows inside `except Exception: continue` — no envelope, no
    counter, no milestone.

    `pk_member` stays False: capacity counts only what distinguishes the
    ENFORCED key, and `R` is outside the model's `pk:`."""
  import logging

  import sdfb_beam.cli.run_pipeline as rp

  monkeypatch.setattr(rp, "measure_fanout", _measure_one_to_one)
  generation = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.land.BOTTOM_TABLE"
      },
      "schema": [{
          "name": "T",
          "type": "STRING",
          "mode": "REQUIRED"
      }, {
          "name": "L",
          "type": "STRING",
          "mode": "REQUIRED"
      }, {
          "name": "R",
          "type": "STRING",
          "mode": "NULLABLE"
      }],
      "primary_keys": ["T", "L", "R"]
  })
  caplog.clear()
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    payload, _roles = rp._resolve_table_fanout(
        _diamond_args("p.land.BOTTOM_TABLE"),
        generation,
        _NARROW_PK_DIAMOND_REG,
        {"TOP_TABLE", "LEFT_TABLE", "RIGHT_TABLE", "BOTTOM_TABLE"},
        _DIAMOND_ROWS,
        landing_schema=_landing_schema("NULLABLE"),
    )
  assert payload["conditional"] == [{
      "id": "(T,R)->RIGHT_TABLE",
      "cols": ["R"],
      "nullable": False,
      "pk_member": False
  }]
  assert "reason=declared_pk" in caplog.text


# --- ADR 0038 fix J: the DECLARED PK is measured on the source --------
#
# The fan-out histogram describes the DRIVING EDGE. A declared PK with a
# member outside that edge (F_TABLE, launch …-12600311608685394436) had
# no measured evidence at all, and the BLOCKER gate found out after the
# GPU hours were spent. One extra GROUP BY, cached in the same payload.

_PK_MEASURED = {
    "cols": ["D_COL_001", "C_COL_002"],
    "rows": 12,
    "key_tuples": 9,
    "max_rows_per_key": 3
}


def _pk_measure_spy(calls):

  def _measure(**kw):
    calls.append(kw)
    return dict(_PK_MEASURED, cols=list(kw["pk_cols"]))

  return _measure


def test_resolve_fanout_measures_the_declared_pk_and_caches_it(monkeypatch):
  import sdfb_beam.cli.run_pipeline as rp

  calls: list[dict] = []
  monkeypatch.setattr(rp, "measure_fanout", _measure)
  monkeypatch.setattr(rp, "measure_pk_uniqueness", _pk_measure_spy(calls))
  store = _Store()
  payload, _roles = resolve_fanout(
      _REG,
      "proj.synthetic_data.C_TABLE",
      "proj.src.C_TABLE",
      in_set_names={"B_TABLE", "C_TABLE", "A_TABLE"},
      reference_rows=_ROWS,
      table_schema=_SCHEMA,
      stats_store=store,
      bq_client=object(),
  )
  assert payload["pk_source"] == _PK_MEASURED
  assert [c["pk_cols"] for c in calls] == [("D_COL_001", "C_COL_002")]
  assert [c["source_child"] for c in calls] == ["proj.src.C_TABLE"]
  # The cached payload carries it, so a re-launch re-scans NEITHER.
  monkeypatch.setattr(
      rp, "measure_fanout", lambda **kw:
      (_ for _ in ()).throw(AssertionError("fan-out measured twice")))
  monkeypatch.setattr(
      rp, "measure_pk_uniqueness", lambda **kw:
      (_ for _ in ()).throw(AssertionError("pk measured twice")))
  again, _ = resolve_fanout(
      _REG,
      "proj.synthetic_data.C_TABLE",
      "proj.src.C_TABLE",
      in_set_names={"B_TABLE", "C_TABLE", "A_TABLE"},
      reference_rows=_ROWS,
      table_schema=_SCHEMA,
      stats_store=store,
      bq_client=object(),
  )
  assert again == payload


def test_a_pk_equal_to_the_driving_edge_costs_no_second_scan(monkeypatch):
  """The 1:1 case is the SAME decision, read off the histogram already
    in hand: 2 parents, one with 2 children -> 2 rows over 1 key tuple."""
  import sdfb_beam.cli.run_pipeline as rp

  model = _MODEL.replace(
      "  C_TABLE:\n    pk: [D_COL_001, C_COL_002]",
      "  C_TABLE:\n    pk: [D_COL_001, D_COL_024]",
  )
  reg = RelationshipRegistry.from_sources([("config/relationships/kw.yaml",
                                            model)])
  monkeypatch.setattr(rp, "measure_fanout", _measure)
  monkeypatch.setattr(
      rp, "measure_pk_uniqueness", lambda **kw:
      (_ for _ in ()).throw(AssertionError("must not scan")))
  payload, _roles = resolve_fanout(
      reg,
      "proj.synthetic_data.C_TABLE",
      "proj.src.C_TABLE",
      in_set_names={"B_TABLE", "C_TABLE", "A_TABLE"},
      reference_rows=_ROWS,
      table_schema=_SCHEMA,
      stats_store=None,
      bq_client=object(),
  )
  assert payload["pk_source"] == {
      "cols": ["D_COL_001", "D_COL_024"],
      "rows": 2,
      "key_tuples": 1,
      "max_rows_per_key": 2,
  }


def test_a_cache_entry_from_before_fix_j_measures_only_the_pk(monkeypatch):
  """An `fk_fanout_stats` row written by an older launch has no `pk`
    key. The fan-out is NOT re-scanned; the PK alone is, and the merged
    payload is written back."""
  import sdfb_beam.cli.run_pipeline as rp

  store = _Store()
  store.saved[("proj.src.C_TABLE", ("D_COL_001", "D_COL_024"),
               _REG.sha12())] = _measure()
  calls: list[dict] = []
  monkeypatch.setattr(
      rp, "measure_fanout", lambda **kw:
      (_ for _ in ()).throw(AssertionError("fan-out re-measured")))
  monkeypatch.setattr(rp, "measure_pk_uniqueness", _pk_measure_spy(calls))
  payload, _roles = resolve_fanout(
      _REG,
      "proj.synthetic_data.C_TABLE",
      "proj.src.C_TABLE",
      in_set_names={"B_TABLE", "C_TABLE", "A_TABLE"},
      reference_rows=_ROWS,
      table_schema=_SCHEMA,
      stats_store=store,
      bq_client=object(),
  )
  assert payload["pk_source"] == _PK_MEASURED
  assert len(calls) == 1
  assert store.saved[("proj.src.C_TABLE", ("D_COL_001", "D_COL_024"),
                      _REG.sha12())]["pk"] == _PK_MEASURED


def test_a_failed_pk_measurement_is_a_preflight_stop(monkeypatch):
  """Same class of scan as the fan-out, same handling: a loud stop, not
    a silent fall back to the unmeasured ladder."""
  import sdfb_beam.cli.run_pipeline as rp

  monkeypatch.setattr(rp, "measure_fanout", _measure)
  monkeypatch.setattr(rp, "measure_pk_uniqueness", lambda **kw:
                      (_ for _ in ()).throw(RuntimeError("boom")))
  with pytest.raises(SystemExit, match="declared PK measurement"):
    resolve_fanout(
        _REG,
        "proj.synthetic_data.C_TABLE",
        "proj.src.C_TABLE",
        in_set_names={"B_TABLE", "C_TABLE", "A_TABLE"},
        reference_rows=_ROWS,
        table_schema=_SCHEMA,
        stats_store=None,
        bq_client=object(),
    )
