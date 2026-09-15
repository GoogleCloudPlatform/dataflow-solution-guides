"""ADR 0038 — a model conflict PROVEN by a full-source measurement
adjusts the effective model and shouts; it no longer stops the launch.

The 2026-09-12 launch (…-6058498192553696658) stopped all five tables
because E_TABLE's declared `pk:` is its own driving FK edge and the
source repeats that value (median 2 rows, up to 13). The source is the
authority for what the data IS, so the PK is dropped from the effective
model, the fan-out histogram is left untouched, and the landing table
reproduces the source's key-repeat distribution by construction.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,protected-access,redefined-outer-name,reimported,unbalanced-tuple-unpacking,unspecified-encoding,unused-argument,use-implicit-booleaness-not-comparison

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sdfb_beam.cli.preflight import preflight
from sdfb_core.contracts import TableSchema
from sdfb_core.contracts.model_adjustment import (
    REPEAT_SHARE_TOLERANCE,
    ModelAdjustment,
    adjusted_model_yaml,
    adjustment_banner,
    landing_repeat_share,
    source_repeat_share,
)
from sdfb_core.contracts.relationships import (
    RelationshipRegistry,
    parse_relationship_model,
)

# E_TABLE's shape: the declared PK IS the driving edge, one column.
_ONE_TO_ONE = """
model: m2
description: the E_TABLE shape
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


def _reg(model: str = _ONE_TO_ONE) -> RelationshipRegistry:
  return RelationshipRegistry.from_sources([("config/relationships/m2.yaml",
                                             model)])


def _schema() -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.child1to1"
      },
      "schema": [{
          "name": "PID",
          "type": "STRING",
          "mode": "REQUIRED"
      }],
  })


def _fanout(histogram: dict) -> dict:
  return {
      "driving_cols": ["PID"],
      "histogram": histogram,
      "cells": None,
      "exact_cells": True
  }


def _preflight(histogram: dict, **kw):
  reg = _reg()
  return preflight(
      _schema(),
      (),
      (),
      [],
      relations=reg.relations("child1to1"),
      num_rows=1_000,
      fk_parent_rows={"parent": 1_000},
      blocker_failure_ratio=0.2,
      fanout=_fanout(histogram),
      edge_roles=reg.edge_roles("child1to1"),
      **kw,
  )


# --- A. the adjustment ------------------------------------------------


def test_one_to_one_conflict_adjusts_instead_of_raising():
  """The declared PK equals the driving edge and the source fans out
    to 2 — that used to be `[preflight P4] … equals the driving edge
    exactly`. Now the PK is dropped and the table still generates."""
  result = _preflight({"0": 10, "2": 5})
  assert result.pk_cols == ()
  (adjustment,) = result.adjustments
  assert adjustment.change == "pk_dropped"
  assert adjustment.declared_pk == ("PID",)
  assert "PID" in adjustment.declared


def test_the_adjusted_table_keeps_its_derived_row_count():
  """The fan-out histogram is UNTOUCHED: dropping the PK must not cap,
    narrow or re-derive the row count (mean k = 10/15 = 0.6667)."""
  result = _preflight({"0": 10, "2": 5})
  assert result.derived_rows == round(1_000 * 10 / 15)


def test_on_model_conflict_stop_restores_the_raise():
  with pytest.raises(
      SystemExit, match=r"preflight P4.*equals the driving edge exactly"):
    _preflight({"0": 10, "2": 5}, on_model_conflict="stop")


def test_a_table_with_no_conflict_is_untouched():
  result = _preflight({"0": 10, "1": 5})
  assert result.pk_cols == ("PID",)
  assert result.adjustments == ()


# --- C. the source repeat share --------------------------------------


def test_source_repeat_share_over_a_known_histogram():
  # 9 key values carrying 24 children -> 15 of 24 rows repeat a key.
  assert source_repeat_share({
      "0": 10,
      "1": 5,
      "2": 3,
      "13": 1
  }) == pytest.approx(1 - 9 / 24)


def test_source_repeat_share_matches_the_e_table_launch():
  """key_values=583,134 over children=1,172,025 (edge (D_COL_001)->
    B_TABLE, launch 2026-09-12_14_50_30) -> 0.5025."""
  assert source_repeat_share({
      "1": 583_134 * 2 - 1_172_025 + 0,
      "2": 1_172_025 - 583_134
  }) == pytest.approx(
      0.5025, abs=1e-4)


def test_source_repeat_share_is_none_without_mass():
  assert source_repeat_share({"0": 10}) is None


def test_the_adjustment_carries_the_source_repeat_share():
  result = _preflight({"0": 10, "2": 5})
  (adjustment,) = result.adjustments
  assert adjustment.source_repeat_share == pytest.approx(0.5)


def test_landing_repeat_share_divides_by_the_rows_generated():
  """`valid_count` is the DISTINCT row-digest count in streaming mode,
    so the rows generated are valid + row.duplicate; pk.duplicate over
    that is the landing key-repeat share."""
  assert landing_repeat_share(
      valid_count=80, dlq_by_rule={
          "row.duplicate": 20,
          "pk.duplicate": 50
      }) == pytest.approx(0.5)
  assert landing_repeat_share(valid_count=0, dlq_by_rule={}) is None


def test_the_tolerance_is_stated():
  assert 0 < REPEAT_SHARE_TOLERANCE <= 0.1


# --- D. the banner and the milestone ---------------------------------


def test_the_banner_names_every_field_the_operator_needs():
  adjustments = (ModelAdjustment(
      table="p.d.child1to1",
      change="pk_dropped",
      declared="pk [PID]",
      measured="one key value carries up to 2 rows",
      consequence="the landing table repeats the key",
      declared_pk=("PID",),
      source_repeat_share=0.5,
  ),)
  banner = adjustment_banner(adjustments)
  assert "MODEL ADJUSTED" in banner
  assert "p.d.child1to1" in banner
  assert "pk [PID]" in banner
  assert "up to 2 rows" in banner
  assert "50.00%" in banner


# --- E. the effective model, as YAML ---------------------------------


def test_the_emitted_yaml_parses_and_has_the_pk_removed():
  (model,) = _reg().models
  adjustments = (ModelAdjustment(
      table="p.d.child1to1",
      change="pk_dropped",
      declared="pk [PID]",
      measured="median 2 rows per key value",
      consequence="the landing table repeats the key",
      declared_pk=("PID",),
      source_repeat_share=0.5,
  ),)
  text = adjusted_model_yaml(model, adjustments)
  assert "median 2 rows per key value" in text  # the reason, as a comment
  reparsed = parse_relationship_model(text, source="emitted.yaml")
  assert reparsed.tables["child1to1"].pk == ()
  assert reparsed.tables["parent"].pk == ("PID",)  # untouched
  # The edge survives: FK enforcement is not weakened by the drop.
  assert reparsed.tables["child1to1"].fk[0].ref == "parent"


def test_the_emitted_yaml_is_the_declared_model_when_nothing_adjusted():
  text = adjusted_model_yaml(_reg().models[0], ())
  reparsed = parse_relationship_model(text, source="emitted.yaml")
  assert reparsed.tables["child1to1"].pk == ("PID",)


def test_milestone_fires_once_per_adjustment(caplog):
  from sdfb_beam.cli.run_pipeline import log_model_adjustments

  adjustments = (ModelAdjustment(
      table="p.d.child1to1",
      change="pk_dropped",
      declared="pk [PID]",
      measured="median 2 rows per key value",
      consequence="the landing table repeats the key",
      declared_pk=("PID",),
      source_repeat_share=0.5,
  ),)
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    log_model_adjustments(adjustments)
  assert caplog.text.count("name=model_adjusted ") == 1
  assert "change=pk_dropped" in caplog.text
  assert "name=model_adjustments " in caplog.text  # the banner header
  assert "MODEL ADJUSTED" in caplog.text


# --- F. the launcher, end to end (no BigQuery) ------------------------

_E_SHAPE = """
model: kw
tables:
  B_TABLE:
    pk: [D_COL_001]
  E_TABLE:
    pk: [D_COL_001]
    fk:
      - cols: [D_COL_001]
        ref: B_TABLE
        ref_cols: [D_COL_001]
"""
# The 2026-09-12 measurement, scaled: 3 key values carry 6 rows, one of
# them 3 times -> the source repeats 50% of its rows.
_E_MEASURED = {
    "histogram": {
        "1": 1,
        "2": 1,
        "3": 1
    },
    "cells": None,
    "parents": 3,
    "children": 6
}


def _e_schema() -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.src.E_TABLE"
      },
      "schema": [{
          "name": "D_COL_001",
          "type": "STRING",
          "mode": "REQUIRED"
      }, {
          "name": "E_VAL",
          "type": "INT64",
          "mode": "REQUIRED"
      }],
  })


def _e_args(**over):
  from sdfb_beam.cli.run_pipeline import parse_args

  args, _ = parse_args([
      "--reference_table",
      "p.src.E_TABLE",
      "--landing_table",
      "p.land.E_TABLE",
      "--dlq_table",
      "p.dq.dlq",
      "--num_rows",
      "100",
      "--run_id",
      "adr0038",
      "--source_stats",
      "off",
      "--model_uri",
      "gs://b/models/m/v/",
  ])
  for key, value in over.items():
    setattr(args, key, value)
  return args


# The 2026-09-12 sample, in miniature: 1,000 reference rows with 5
# duplicate keys (0.5%) — P5 warns and adjusts nothing, exactly as the
# real launch's "40 duplicates of 10,000" did. The FULL-source fan-out
# is the only thing that can see the repeat.
_MILD_ROWS = [{
    "D_COL_001": f"K{min(i, 995):04d}",
    "E_VAL": i
} for i in range(1_000)]
# The same table with a HOTTER key: ~300 distinct values in 1,000 sample
# rows (70% duplicates), which is over P5's 50% bar.
_HOT_ROWS = [{"D_COL_001": f"K{i % 300:04d}", "E_VAL": i} for i in range(1_000)]


def _run_launcher(monkeypatch, rows=None, distinct_keys=None, **over):
  import sdfb_beam.cli.run_pipeline as rp

  reg = RelationshipRegistry.from_sources([("config/relationships/kw.yaml",
                                            _E_SHAPE)])
  sample = _MILD_ROWS if rows is None else rows
  monkeypatch.setattr(rp, "load_reference_rows", lambda **kw: sample)
  monkeypatch.setattr(rp, "measure_fanout", lambda **kw: _E_MEASURED)
  args = _e_args(**over)
  args._rows_by_landing = {"p.land.B_TABLE": 1_000}
  if distinct_keys is not None:
    args._distinct_keys_by_landing = distinct_keys
  result = rp._load_reference_and_preflight(
      args,
      _e_schema(),
      reg,
      in_set_landing=frozenset({"p.land.B_TABLE", "p.land.E_TABLE"}),
  )
  return reg, result[1], result[5]  # registry, PreflightResult, fanout


def test_the_launcher_adjusts_the_e_table_shape_and_keeps_the_fanout(
    monkeypatch,):
  """The whole 2026-09-12 stop, on the laptop: the declared PK IS the
    driving edge and the source repeats it, so the launch adjusts."""
  _, pf, fanout = _run_launcher(monkeypatch)
  (adjustment,) = pf.adjustments
  assert pf.pk_cols == ()
  assert adjustment.declared_pk == ("D_COL_001",)
  assert adjustment.source_repeat_share == pytest.approx(0.5)
  # The histogram is untouched and the cells stop keying the child.
  assert fanout["histogram"] == _E_MEASURED["histogram"]
  assert fanout["exact_cells"] is False
  # mean k = 6/3 = 2 -> 1000 parent rows x 2
  assert pf.derived_rows == 2_000


def test_the_launcher_stop_mode_refuses_the_same_launch(monkeypatch):
  with pytest.raises(SystemExit, match=r"preflight P4"):
    _run_launcher(monkeypatch, on_model_conflict="stop")


def test_on_model_conflict_defaults_to_adjust_and_reaches_launch_config():
  args = _e_args()
  assert args.on_model_conflict == "adjust"
  logged = {
      k: v
      for k, v in sorted(vars(args).items())
      if not k.startswith("_") and k != "model_client"
  }
  assert logged["on_model_conflict"] == "adjust"


def test_the_effective_model_is_written_beside_the_staged_artifacts(
    monkeypatch, tmp_path, caplog):
  from apache_beam.options.pipeline_options import PipelineOptions
  from sdfb_beam.cli.run_pipeline import emit_effective_model

  reg, pf, _ = _run_launcher(monkeypatch)
  options = PipelineOptions([
      f"--staging_location={tmp_path}",
      "--runner=DirectRunner",
  ])
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    (written,) = emit_effective_model(reg, pf.adjustments, options, "adr0038")
  assert written.startswith(f"{tmp_path}/model_adjustments/")
  text = Path(written).read_text()
  reparsed = parse_relationship_model(text, source=written)
  assert reparsed.tables["E_TABLE"].pk == ()
  assert reparsed.tables["B_TABLE"].pk == ("D_COL_001",)
  assert "name=model_adjustment_model " in caplog.text
  assert written in caplog.text


def test_the_reference_sample_warns_but_never_adjusts(monkeypatch, caplog):
  """Boundary (ADR 0038 §6): the 10,000-row sample is far too weak to
    drop a key on. It warns — and on the SAME rows, with no full-source
    fan-out to read, the declared PK stays exactly where the model put
    it. Only the measurement adjusts."""
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    _, pf, _ = _run_launcher(monkeypatch)
  assert "name=preflight_pk_not_unique_in_sample" in caplog.text
  assert pf.adjustments  # ...driven by the fan-out, not by the sample

  reg = RelationshipRegistry.from_sources([("config/relationships/kw.yaml",
                                            _E_SHAPE)])
  rows = [{
      "D_COL_001": f"K{min(i, 995):04d}",
      "E_VAL": i
  } for i in range(1_000)]
  undriven = preflight(
      _e_schema(),
      (),
      (),
      rows,
      relations=reg.relations("E_TABLE"),
      num_rows=100,
  )
  assert undriven.pk_cols == ("D_COL_001",)
  assert undriven.adjustments == ()


# --- H2. the sample-based P5 stop defers to the measurement -----------
#
# 2026-09-13 verification, finding H2: `_check_pk_is_a_key` ran BEFORE
# P4, so a driven child whose 10k SAMPLE showed >=50% duplicate PK
# tuples exited the whole launch at `[preflight P5]` — zero rows for
# every planned table, the very outcome ADR 0038 exists to end — and
# `--on_model_conflict` never reached it. Where a full-source
# measurement is in scope, the measurement decides.


def test_a_hot_sample_defers_p5_to_the_full_source_measurement(
    monkeypatch, caplog):
  """300 distinct key values in 1,000 sample rows (70% duplicates) is
    over P5's bar — and irrelevant: this table's PK is drawn from its
    parent's keys times the measured fan-out, not from marginals."""
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    _, pf, _ = _run_launcher(monkeypatch, rows=_HOT_ROWS, num_rows=1_000)
  (adjustment,) = pf.adjustments
  assert pf.pk_cols == ()
  assert adjustment.change == "pk_dropped"
  # ...and the table still generates: mean k = 6/3 = 2 over 1,000
  # parent rows.
  assert pf.derived_rows == 2_000
  assert "name=preflight_pk_sample_stop_deferred" in caplog.text


def test_the_same_hot_sample_refuses_at_p4_under_stop(monkeypatch):
  """The escape hatch the operator actually has: P4's message, which
    names the measurement — never P5's sample."""
  with pytest.raises(SystemExit, match=r"preflight P4") as exc:
    _run_launcher(
        monkeypatch,
        rows=_HOT_ROWS,
        num_rows=1_000,
        on_model_conflict="stop",
    )
  assert "preflight P5" not in str(exc.value)


def test_an_undriven_table_with_the_same_sample_still_stops_at_p5():
  """No measurement, no deference: the sample is all there is."""
  reg = RelationshipRegistry.from_sources([("config/relationships/kw.yaml",
                                            _E_SHAPE)])
  with pytest.raises(SystemExit, match=r"preflight P5") as exc:
    preflight(
        _e_schema(),
        (),
        (),
        _HOT_ROWS,
        relations=reg.relations("E_TABLE"),
        num_rows=1_000,
    )
  assert "marginals" in str(exc.value)


def test_a_driven_but_unmeasured_table_stops_with_the_driven_message():
  """P5 can still be reached on a table that DECLARES a driving edge
    whose parent is not in the launch — it then generates from marginals
    at --num_rows like a root, so the stop stands, but its message must
    not claim a fan-out nobody measured."""
  reg = RelationshipRegistry.from_sources([("config/relationships/kw.yaml",
                                            _E_SHAPE)])
  with pytest.raises(SystemExit, match=r"preflight P5") as exc:
    preflight(
        _e_schema(),
        (),
        (),
        _HOT_ROWS,
        relations=reg.relations("E_TABLE"),
        num_rows=1_000,
        edge_roles=reg.edge_roles("E_TABLE"),
    )
  message = str(exc.value)
  assert "no full-source fan-out was measured" in message
  assert "ADR 0038" in message


# --- H3. a descendant is sized off the parent's DISTINCT keys ---------
#
# Finding H3: the derived row count multiplied the parent's ROWS by the
# mean fan-out, but the composer fans a child out from the parent's
# DISTINCT keys (`parent_pk=()` arms `FanoutDistinct`). On the
# motivating launch F_TABLE would have been asked for 220,215 rows and
# could only produce ~109,556 — a 50.25% shortfall written to
# `validation_runs` as a missed request, with no milestone naming why.

_CHAIN = """
model: kw3
tables:
  B_TABLE:
    pk: [D_COL_001]
  E_TABLE:
    pk: [D_COL_001]
    fk:
      - cols: [D_COL_001]
        ref: B_TABLE
        ref_cols: [D_COL_001]
  F_TABLE:
    fk:
      - cols: [D_COL_001]
        ref: E_TABLE
        ref_cols: [D_COL_001]
        drives: true
"""
# Every E_TABLE key carries exactly 2 F_TABLE rows.
_F_MEASURED = {
    "histogram": {
        "2": 1
    },
    "cells": None,
    "parents": 1,
    "children": 2
}


def _f_schema() -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.src.F_TABLE"
      },
      "schema": [{
          "name": "D_COL_001",
          "type": "STRING",
          "mode": "REQUIRED"
      }, {
          "name": "F_VAL",
          "type": "INT64",
          "mode": "REQUIRED"
      }],
  })


def _run_chain(monkeypatch, *, parent_rows: int, parent_distinct=None):
  import sdfb_beam.cli.run_pipeline as rp

  reg = RelationshipRegistry.from_sources([("config/relationships/kw3.yaml",
                                            _CHAIN)])
  rows = [{"D_COL_001": f"K{i % 300:04d}", "F_VAL": i} for i in range(1_000)]
  monkeypatch.setattr(rp, "load_reference_rows", lambda **kw: rows)
  monkeypatch.setattr(rp, "measure_fanout", lambda **kw: _F_MEASURED)
  args = _e_args(
      reference_table="p.src.F_TABLE",
      landing_table="p.land.F_TABLE",
  )
  args._rows_by_landing = {"p.land.E_TABLE": parent_rows}
  args._distinct_keys_by_landing = ({} if parent_distinct is None else {
      "p.land.E_TABLE": parent_distinct
  })
  _, pf, *_ = rp._load_reference_and_preflight(
      args,
      _f_schema(),
      reg,
      in_set_landing=frozenset({"p.land.E_TABLE", "p.land.F_TABLE"}),
  )
  return pf


def test_a_child_of_an_adjusted_parent_is_sized_off_its_distinct_keys(
    monkeypatch, caplog):
  """E_TABLE lands 105,609 rows at a 0.5025 repeat share, so it holds
    52,540 DISTINCT keys — and F_TABLE fans out from THOSE."""
  from sdfb_core.contracts.model_adjustment import landed_distinct_keys

  distinct = landed_distinct_keys(105_609, 0.5025)
  assert distinct == round(105_609 * (1 - 0.5025))
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    pf = _run_chain(
        monkeypatch,
        parent_rows=105_609,
        parent_distinct=distinct,
    )
  assert pf.derived_rows == distinct * 2
  assert pf.derived_rows < 105_609 * 2  # the shortfall that was written
  assert "name=model_adjustment_descendant_rows" in caplog.text
  assert "parent_distinct_keys=52540" in caplog.text


def test_a_child_of_an_enforced_pk_parent_is_unchanged(monkeypatch, caplog):
  """A parent whose PK is ENFORCED lands one row per key, so rows and
    distinct keys coincide and nothing moves — no milestone either."""
  from sdfb_core.contracts.model_adjustment import landed_distinct_keys

  assert landed_distinct_keys(105_609, None) == 105_609
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    pf = _run_chain(
        monkeypatch,
        parent_rows=105_609,
        parent_distinct=105_609,
    )
  assert pf.derived_rows == 105_609 * 2
  assert "name=model_adjustment_descendant_rows" not in caplog.text
  # ...and with no distinct-key figure at all (a launch that never
  # measured one), the parent's rows stand exactly as before.
  assert _run_chain(
      monkeypatch, parent_rows=105_609).derived_rows == 105_609 * 2


def test_the_distinct_key_count_is_carried_from_the_adjustment():
  """The launcher's carry-forward: an adjusted spec hands its
    descendants rows x (1 - source_repeat_share), an unadjusted one its
    rows."""
  from sdfb_beam.cli.run_pipeline import distinct_keys_landed
  from sdfb_core.contracts.model_adjustment import landed_distinct_keys

  assert landed_distinct_keys(1_000, 0.5) == 500
  assert landed_distinct_keys(1_000, None) == 1_000
  assert landed_distinct_keys(0, 0.5) == 0
  adjusted = (ModelAdjustment(
      table="p.land.E_TABLE",
      change="pk_dropped",
      declared="pk [D_COL_001]",
      measured="the source repeats it",
      consequence="repeats land",
      declared_pk=("D_COL_001",),
      source_repeat_share=0.5,
  ),)
  assert distinct_keys_landed(1_000, adjusted) == 500
  assert distinct_keys_landed(1_000, ()) == 1_000


# --- H4/J. the faithfulness verdict compares like with like -----------
#
# Finding H4: `source_repeat_share` was read off the DRIVING-EDGE
# histogram while `landing_repeat_share` is `pk.duplicate` over the FULL
# declared PK. At the second conflict site — a PK with completing
# members outside the driving edge — those are different column sets, so
# a perfectly faithful run was handed `within_tolerance=false` and a
# WARNING saying the copy was broken. Fix J removes the mismatch at its
# root: the source is measured over the DECLARED PK (section J below),
# so a share, when there is one, is always comparable — and a launch
# that measured none carries none.

_CELL_SHAPE = """
model: kw5
tables:
  parent:
    pk: [PID]
  child:
    pk: [PID, CAT]
    fk:
      - cols: [PID]
        ref: parent
        ref_cols: [PID]
"""


def _cell_schema() -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.child"
      },
      "schema": [{
          "name": "PID",
          "type": "STRING",
          "mode": "REQUIRED"
      }, {
          "name": "CAT",
          "type": "STRING",
          "mode": "REQUIRED"
      }, {
          "name": "AMT",
          "type": "INT64",
          "mode": "REQUIRED"
      }],
  })


def _cell_preflight(**kw):
  reg = RelationshipRegistry.from_sources([("config/relationships/kw5.yaml",
                                            _CELL_SHAPE)])
  rows = [{
      "PID": f"C2E3{i:020X}",
      "CAT": "abc"[i % 3],
      "AMT": i
  } for i in range(300)]
  # 18 key values carry 81 rows -> the source repeats 7/9 of them.
  fanout = {
      "driving_cols": ["PID"],
      "histogram": {
          "1": 11,
          "10": 7
      },
      "cells": {
          "cols": ["CAT"],
          "rows": [["a"], ["b"], ["c"]],
          "counts": [1.0, 1.0, 1.0]
      },
      "exact_cells": True,
  }
  return preflight(
      _cell_schema(),
      (),
      (),
      rows,
      relations=reg.relations("child"),
      num_rows=1_000,
      fk_parent_rows={"parent": 1_000},
      blocker_failure_ratio=0.2,
      fanout=fanout,
      edge_roles=reg.edge_roles("child"),
      **kw,
  )


def test_a_wide_pk_without_its_own_measurement_claims_no_share():
  """Fix J: a share measured over the DRIVING EDGE is not the declared
    PK's, so it is no longer carried at all. This shape's conflict is
    proven by the cell capacity, not by a measurement of (PID, CAT) — so
    the adjustment states the conflict and claims no share, instead of
    reporting one the landing table is not measured against."""
  result = _cell_preflight()
  (adjustment,) = result.adjustments
  assert adjustment.declared_pk == ("PID", "CAT")
  assert adjustment.source_repeat_share is None


def test_the_one_to_one_case_carries_the_declared_pk_share():
  """There the declared PK IS the driving edge, so the histogram
    measures that very key tuple and the ±0.05 verdict stands."""
  result = _preflight({"0": 10, "2": 5})
  (adjustment,) = result.adjustments
  assert adjustment.declared_pk == ("PID",)
  assert adjustment.source_repeat_share == pytest.approx(0.5)


def test_the_banner_says_when_no_share_was_measured():
  (adjustment,) = _cell_preflight().adjustments
  banner = adjustment_banner([adjustment])
  assert "not measured over ['PID', 'CAT']" in banner
  assert "no ±5% verdict is written" in banner


def test_the_summary_row_withholds_the_verdict_when_no_share_was_measured():
  """The wiring through `pipeline._build_validation_run_row`: the
    landing share is still MEASURED on the full declared PK (reporting),
    and with no source share there is no delta and no verdict — never a
    verdict against a share nobody measured."""
  from sdfb_beam.pipeline import _build_validation_run_row
  from sdfb_core.validation import Thresholds

  (adjustment,) = _cell_preflight().adjustments
  row = _build_validation_run_row(
      None,
      valid_count=10_000,
      dlq_by_rule={"pk.duplicate": 5_702},
      thresholds=Thresholds(env="dev", blocker_failure_ratio=0.2),
      run_id="r",
      reference_digest="d",
      num_rows=10_000,
      reference_table="p.src.child",
      landing_table="p.land.child",
      engine="b1_rag",
      model_uri="gs://b/m/v/",
      excluded_blocker_rules=("pk.duplicate",),
      source_repeat_share=adjustment.source_repeat_share,
  )
  assert row["source_repeat_share"] is None
  assert row["landing_repeat_share"] is None
  assert row["repeat_share_delta"] is None
  assert row["repeat_share_within_tolerance"] is None


# --- J. the DECLARED PK is measured on the source, and decides --------
#
# Launch 2026-09-13 …-12600311608685394436: fix H's adjustment worked —
# E_TABLE's key was dropped, F_TABLE was sized off the parent's distinct
# keys, three tables generated. Then F_TABLE tripped the BLOCKER gate at
# `blocker_count=179853 observed=0.2908 > gate=0.2`. Its
# `pk: [D_COL_001, CONTINUOUS_NR]` is driven by `(D_COL_001)`, so its key
# has a member OUTSIDE the driving edge and `_check_driven_pk` returned
# early on it: the histogram describes the EDGE and says nothing about
# that key. The declared PK's OWN source measurement now decides, and it
# decides against the run's BLOCKER gate — not against any repetition.

_J_SHAPE = """
model: kw6
tables:
  parent:
    pk: [PID]
  child:
    pk: [PID, NR]
    fk:
      - cols: [PID]
        ref: parent
        ref_cols: [PID]
"""

# The same six tables, but the child is keyed only by `--pk_cols`.
_J_SHAPE_NO_PK = _J_SHAPE.replace("    pk: [PID, NR]\n", "")


def _measure_one_to_one_j(**kwargs):
  """A fan-out measurement stub: one child per parent key, no cells."""
  return {"histogram": {1: 4}, "cells": None, "parents": 4, "children": 4}


def _j_schema() -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.childj"
      },
      "schema": [{
          "name": "PID",
          "type": "STRING",
          "mode": "REQUIRED"
      }, {
          "name": "NR",
          "type": "INT64",
          "mode": "REQUIRED"
      }, {
          "name": "AMT",
          "type": "INT64",
          "mode": "REQUIRED"
      }],
  })


# The F_TABLE measurement, in miniature: 18 key values carry 81 rows.
_J_HISTOGRAM = {"1": 11, "10": 7}
# The declared PK, measured on the source: 0.2908 of the rows repeat a
# (PID, NR) tuple — the share the landing table actually reached.
_J_ABOVE_GATE = {
    "cols": ["PID", "NR"],
    "rows": 1_215_948,
    "key_tuples": 862_401,
    "max_rows_per_key": 4
}
# The same shape on a source that is merely DIRTY: 0.4% repeats.
_J_BELOW_GATE = {
    "cols": ["PID", "NR"],
    "rows": 10_000,
    "key_tuples": 9_960,
    "max_rows_per_key": 2
}


def _j_preflight(pk_source: dict | None, **kw):
  reg = RelationshipRegistry.from_sources([("config/relationships/kw6.yaml",
                                            _J_SHAPE)])
  rows = [{"PID": f"C2E3{i:020X}", "NR": i, "AMT": i} for i in range(300)]
  # `NR` is an unbounded INT64, so the cell check is INEXACT — the
  # shape that used to return early, before anything measured the key.
  fanout = {
      "driving_cols": ["PID"],
      "histogram": _J_HISTOGRAM,
      "cells": None,
      "exact_cells": False,
      "pk_source": pk_source
  }
  return preflight(
      _j_schema(),
      (),
      (),
      rows,
      relations=reg.relations("child"),
      num_rows=1_000,
      fk_parent_rows={"parent": 1_000},
      blocker_failure_ratio=0.2,
      fanout=fanout,
      edge_roles=reg.edge_roles("child"),
      **kw,
  )


def test_a_pk_with_a_completing_member_above_the_gate_adjusts():
  """F_TABLE's shape. `CONTINUOUS_NR` is unbounded, so the cell check
    returns early and nothing looked at the key — until now."""
  from sdfb_core.contracts.model_adjustment import pk_repeat_share

  result = _j_preflight(_J_ABOVE_GATE)
  assert result.pk_cols == ()
  (adjustment,) = result.adjustments
  assert adjustment.change == "pk_dropped"
  assert adjustment.declared_pk == ("PID", "NR")
  assert adjustment.source_repeat_share == pytest.approx(
      pk_repeat_share(_J_ABOVE_GATE))
  assert adjustment.source_repeat_share == pytest.approx(0.2908, abs=1e-4)
  # ...and the run proceeds: the fan-out is untouched, mean k = 81/18.
  assert result.derived_rows == round(1_000 * 81 / 18)


def test_the_same_shape_below_the_gate_keeps_its_key_and_warns(caplog):
  """A source that is 0.4% dirty must NOT lose its key — today's
    machinery diverts those few rows as pk.duplicate."""
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    result = _j_preflight(_J_BELOW_GATE)
  assert result.pk_cols == ("PID", "NR")
  assert result.adjustments == ()
  assert "name=preflight_pk_source_repeats" in caplog.text
  assert "repeat_share=0.004" in caplog.text
  assert "gate=0.2" in caplog.text


def test_the_measured_pk_conflict_still_refuses_under_stop():
  with pytest.raises(SystemExit, match=r"preflight P4") as exc:
    _j_preflight(_J_ABOVE_GATE, on_model_conflict="stop")
  message = str(exc.value)
  assert "['PID', 'NR']" in message
  assert "29.1%" in message or "0.2908" in message


def test_the_faithfulness_verdict_is_now_comparable_for_a_wide_pk():
  """Fix H4 wrote "not comparable" here: the source share came off the
    DRIVING EDGE while `pk.duplicate` is measured on the declared PK.
    Both now describe the declared PK, so the ±0.05 verdict stands."""
  from sdfb_beam.pipeline import _build_validation_run_row
  from sdfb_core.validation import Thresholds

  (adjustment,) = _j_preflight(_J_ABOVE_GATE).adjustments
  row = _build_validation_run_row(
      None,
      valid_count=10_000,
      dlq_by_rule={"pk.duplicate": 2_908},
      thresholds=Thresholds(env="dev", blocker_failure_ratio=0.2),
      run_id="r",
      reference_digest="d",
      num_rows=10_000,
      reference_table="p.src.childj",
      landing_table="p.land.childj",
      engine="b1_rag",
      model_uri="gs://b/m/v/",
      excluded_blocker_rules=("pk.duplicate",),
      source_repeat_share=adjustment.source_repeat_share,
  )
  assert row["source_repeat_share"] == pytest.approx(0.2908, abs=1e-4)
  assert row["landing_repeat_share"] == pytest.approx(0.2908, abs=1e-4)
  assert row["repeat_share_delta"] == pytest.approx(0.0, abs=1e-3)
  assert row["repeat_share_within_tolerance"] is True


def test_the_one_to_one_case_reads_the_same_measurement_off_the_histogram():
  """ONE decision path: a declared PK that IS the driving edge is the
    special case where the histogram already measures the key tuple — so
    it costs no second scan and takes the same branch."""
  from sdfb_core.contracts.model_adjustment import (
      pk_measurement_from_histogram,
      pk_repeat_share,
      source_repeat_share,
  )

  measurement = pk_measurement_from_histogram({"0": 10, "2": 5}, ["PID"])
  assert measurement == {
      "cols": ["PID"],
      "rows": 10,
      "key_tuples": 5,
      "max_rows_per_key": 2
  }
  assert pk_repeat_share(measurement) == source_repeat_share({"0": 10, "2": 5})
  assert pk_measurement_from_histogram({"0": 10}, ["PID"]) is None


# 2026-09-13 — the gate divides the diverted rows by the rows that REACH
# it, so a source share `s` lands `s / (1 + s)`. Comparing `s` against the
# gate misjudged every source in (gate, gate/(1-gate)]: at 0.2 that is the
# whole 20-25% band, refused or stripped of its key on a run the gate
# would have PASSED with it enforced.
_J_IN_BAND = {
    "cols": ["PID", "NR"],
    "rows": 1_000_000,
    "key_tuples": 780_000,
    "max_rows_per_key": 3
}  # share 0.22
_J_ABOVE_BAND = {
    "cols": ["PID", "NR"],
    "rows": 1_000_000,
    "key_tuples": 740_000,
    "max_rows_per_key": 3
}  # share 0.26


def test_a_share_the_gate_would_pass_keeps_its_key(caplog):
  """0.22 of the source repeats, gate 0.2. A faithful landing puts
    220,000 duplicates beside 1,000,000 rows, so the gate computes
    0.1803 and PASSES — the key must survive."""
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    result = _j_preflight(_J_IN_BAND)
  assert result.pk_cols == ("PID", "NR")
  assert result.adjustments == ()
  assert "predicted_observed=0.1803" in caplog.text


def test_a_share_the_gate_would_fail_still_adjusts():
  """0.26 repeats lands 0.2063 at the gate, over 0.2, so the key goes."""
  result = _j_preflight(_J_ABOVE_BAND)
  assert result.pk_cols == ()
  (adjustment,) = result.adjustments
  assert adjustment.change == "pk_dropped"


def test_the_real_f_table_share_is_still_above_the_boundary():
  """Regression guard for the launch this came from: 0.2908 lands
    0.2253, still over the 0.2 gate, so F_TABLE keeps adjusting."""
  result = _j_preflight(_J_ABOVE_GATE)
  assert result.pk_cols == ()


def test_the_measurement_reads_the_key_the_run_enforces(monkeypatch):
  """A table no model keys takes its PK from --pk_cols. Reading
    `relations.pk` left it with NO measurement at all, so the sample stop
    stood down for evidence that did not exist and the driven check fell
    through — worse than before ADR 0038."""
  import sdfb_beam.cli.run_pipeline as rp

  seen = {}

  def _capture(measured, pk, driving_cols, **kw):
    seen["pk"] = pk
    return False

  monkeypatch.setattr(rp, "resolve_pk_measurement", _capture)
  monkeypatch.setattr(rp, "measure_fanout", _measure_one_to_one_j)
  reg = RelationshipRegistry.from_sources([("config/relationships/kw6.yaml",
                                            _J_SHAPE_NO_PK)])
  rp.resolve_fanout(
      reg,
      "p.land.child",
      "p.src.child",
      in_set_names={"parent", "child"},
      reference_rows=[],
      table_schema=_j_schema(),
      stats_store=None,
      bq_client=None,
      effective_pk=("PID", "NR"),
  )
  assert seen["pk"] == ("PID", "NR")
