"""ADR 0039 — the launch says how many rows it will generate BEFORE it
generates any of them, and warns where a measurement makes that number
untrustworthy.

Every figure here is from launch `2026-09-13_06_10_16-12600311608685394436`
(`integration_tests/.../jobs_logs.jsonl`), so the arithmetic in the block
can be checked against the log line for line:

    relational_single_job order=B_TABLE,C_TABLE,E_TABLE,A_TABLE,F_TABLE
      rows_detail=B_TABLE:210958,C_TABLE:846891,E_TABLE:423999,
                  A_TABLE:30937138,F_TABLE:439889

    fk_fanout_measured edge='(D_COL_001)->E_TABLE' ... mean=2.0852
    model_adjustment_descendant_rows table=...F_TABLE parent=E_TABLE
      parent_rows=423999 parent_distinct_keys=210958 mean_fanout=2.0852
      derived_rows=439889

E_TABLE's own driving edge was measured on the SAME sources one launch
earlier (2026-09-12_14_50_30): 1,172,025 child rows over 583,134 distinct
key values, of which the source parent covers 52,545 — the 9% matched
share, the 0.5025 repeat share that ADR 0038 adjusted E_TABLE's `pk:` on,
and the 2.0099 mean that turns B_TABLE's 210,958 rows into E_TABLE's
423,999.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,invalid-name,protected-access,unused-argument,use-implicit-booleaness-not-comparison

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

import pytest
from sdfb_core.contracts.row_projection import (
    MATCHED_SHARE_FLOOR,
    PROJECTION_FACTOR_CEILING,
    ZERO_SHARE_CEILING,
    project_table,
    projected_total,
    projection_banner,
    projection_warnings,
)

# --- the launch, as the launcher saw it -------------------------------

# --num_rows, i.e. the root B_TABLE's count.
_LAUNCH_ROWS = 210_958

# E_TABLE's driving edge (D_COL_001)->B_TABLE, measured on the source:
# 583,134 distinct child key values carrying 1,172,025 rows, of which the
# source parent covers 52,545 (so the zero bucket is empty).
_E_CHILDREN = 1_172_025
_E_KEY_VALUES = 583_134
_E_PARENT_TUPLES = 52_545


def _histogram(key_values: int, children: int, zero: int = 0) -> dict:
  """The smallest histogram with the measured mean: ``key_values`` key
    values carrying ``children`` rows between them, plus ``zero`` parents
    with no child at all.

    Two adjacent buckets (``floor(mean)`` and ``+1``) are not the real
    shape — the real F_TABLE edge runs to max=647 — but only the MEAN
    sizes a table, and this reproduces it exactly.
    """
  base = children // key_values
  rest = children - base * key_values
  return {"0": zero, str(base): key_values - rest, str(base + 1): rest}


def _projections():
  """The five tables, in the launch's generation order."""
  b = project_table("p.d.B_TABLE", launch_rows=_LAUNCH_ROWS)
  c = project_table(
      "p.d.C_TABLE",
      launch_rows=_LAUNCH_ROWS,
      parent="B_TABLE",
      edge="(D_COL_001,D_COL_024,D_COL_025,C_COL_009)->B_TABLE",
      parent_rows=b.rows,
      parent_keys=b.rows,
      # mean 4.0145 -> 846,891
      histogram=_histogram(210_958, 846_891),
  )
  e = project_table(
      "p.d.E_TABLE",
      launch_rows=_LAUNCH_ROWS,
      parent="B_TABLE",
      edge="(D_COL_001)->B_TABLE",
      parent_rows=b.rows,
      parent_keys=b.rows,
      histogram=_histogram(_E_KEY_VALUES, _E_CHILDREN),
      source_parent_tuples=_E_PARENT_TUPLES,
      repeat_share=1 - _E_KEY_VALUES / _E_CHILDREN,
  )
  a = project_table(
      "p.d.A_TABLE",
      launch_rows=_LAUNCH_ROWS,
      parent="C_TABLE",
      edge="(D_COL_024,D_COL_025,C_COL_009)->C_TABLE",
      parent_rows=c.rows,
      parent_keys=c.rows,
      # mean 36.5302 -> 30,937,138
      histogram=_histogram(846_891, 30_937_138),
  )
  f = project_table(
      "p.d.F_TABLE",
      launch_rows=_LAUNCH_ROWS,
      parent="E_TABLE",
      edge="(D_COL_001)->E_TABLE",
      parent_rows=e.rows,
      parent_keys=210_958,
      histogram=_histogram(465_079, 1_215_948, zero=118_055),
  )
  return (b, c, e, a, f)


# --- A. the per-table and total figures -------------------------------


def test_the_five_table_launch_projects_its_own_rows_detail():
  """Every count in `rows_detail`, re-derived from the measurements
    the launcher had in hand."""
  landed = {p.table_name: p.rows for p in _projections()}
  assert landed == {
      "B_TABLE": 210_958,
      "C_TABLE": 846_891,
      "E_TABLE": 423_999,
      "A_TABLE": 30_937_138,
      "F_TABLE": 439_889,
  }


def test_the_total_is_the_number_nothing_in_that_launch_said():
  assert projected_total(_projections()) == 32_858_875


def test_the_driven_children_name_their_parent_and_the_measured_mean():
  _, c, e, a, f = _projections()
  assert (c.parent, round(c.mean_fanout, 4)) == ("B_TABLE", 4.0145)
  assert (e.parent, round(e.mean_fanout, 4)) == ("B_TABLE", 2.0099)
  assert (a.parent, round(a.mean_fanout, 4)) == ("C_TABLE", 36.5302)
  assert (f.parent, round(f.mean_fanout, 4)) == ("E_TABLE", 2.0852)
  # F_TABLE fans out from its parent's DISTINCT keys, not its rows
  # (ADR 0038 fix H3) — the two differ because E_TABLE was adjusted.
  assert (f.parent_rows, f.parent_keys) == (423_999, 210_958)


def test_the_block_makes_the_dominant_table_obvious():
  projections = _projections()
  block = projection_banner(projections, launch_rows=_LAUNCH_ROWS)
  assert "ROW PROJECTION" in block
  assert "30,937,138" in block and "32,858,875" in block
  # Every table, its derivation, and the share that says who owns the run.
  assert "driven by C_TABLE" in block
  assert "94.2%" in block
  # The dominant table's bar is the longest one drawn.
  bars = {
      line.split("|")[0].strip(): line.count("#")
      for line in block.splitlines()
      if "#" in line
  }
  assert max(bars, key=lambda k: bars[k]) == "A_TABLE"


def test_a_root_only_launch_projects_num_rows_and_derives_nothing():
  (root,) = (project_table("p.d.B_TABLE", launch_rows=99),)
  assert (root.rows, root.parent, root.mean_fanout) == (99, "", None)
  assert "--num_rows" in projection_banner([root], launch_rows=99)


def test_an_underivable_projection_says_so_instead_of_printing_a_number():
  """A driven child whose driving edge was never measured has NO row
    count — the block says that, it does not fall back to --num_rows."""
  orphaned = project_table(
      "p.d.X_TABLE",
      launch_rows=100,
      parent="P_TABLE",
      edge="(K)->P_TABLE",
      parent_rows=100,
      parent_keys=100,
      histogram={"0": 10},  # zero bucket only: no mass
  )
  assert orphaned.rows is None
  assert "no mass" in orphaned.undecided
  block = projection_banner([orphaned], launch_rows=100)
  assert "not derivable" in block
  assert "no mass" in block


def test_a_projection_with_an_underivable_table_totals_what_it_knows():
  known = project_table("p.d.B_TABLE", launch_rows=99)
  unknown = project_table(
      "p.d.X_TABLE",
      launch_rows=99,
      parent="B_TABLE",
      parent_rows=99,
      parent_keys=99,
      histogram={},
  )
  assert projected_total([known, unknown]) == 99
  block = projection_banner([known, unknown], launch_rows=99)
  assert "1 table(s) not derivable" in block


def test_the_mean_is_the_histogram_mean_zero_bucket_included():
  """Sanity on the arithmetic every count rests on: the F_TABLE edge's
    measured mean=2.0852 over parents=583,134 (zero_share=0.2024)."""
  p = project_table(
      "p.d.F_TABLE",
      launch_rows=10,
      parent="E_TABLE",
      parent_rows=10,
      parent_keys=210_958,
      histogram=_histogram(465_079, 1_215_948, zero=118_055),
  )
  assert p.mean_fanout == pytest.approx(2.0852, abs=1e-4)
  assert p.zero_share == pytest.approx(0.2024, abs=1e-4)


# --- B. the warnings --------------------------------------------------


def _warnings(projections, launch_rows=_LAUNCH_ROWS):
  return {
      w.code: w
      for w in projection_warnings(projections, launch_rows=launch_rows)
  }


def test_a_clean_launch_warns_about_nothing():
  """The bar every threshold is set against: a root plus an ordinary
    2x child must print the projection and NOTHING else."""
  root = project_table("p.d.B_TABLE", launch_rows=1_000)
  child = project_table(
      "p.d.C_TABLE",
      launch_rows=1_000,
      parent="B_TABLE",
      edge="(K)->B_TABLE",
      parent_rows=1_000,
      parent_keys=1_000,
      histogram=_histogram(1_000, 2_000, zero=200),
      source_parent_tuples=1_200,
  )
  assert projection_warnings([root, child], launch_rows=1_000) == ()
  assert "WARNINGS" not in projection_banner(
      [root, child],
      launch_rows=1_000,
      warnings=projection_warnings([root, child], launch_rows=1_000),
  )


def test_the_orphan_edge_names_the_edge_the_share_and_the_consequence():
  """E_TABLE's driving edge: the source parent covers 52,545 of the
    583,134 distinct key values the source child holds — 9%."""
  _, _, e, _, _ = _projections()
  warning = _warnings(_projections())["fk_source_orphans"]
  assert warning.table_name == "E_TABLE"
  assert e.matched_share == pytest.approx(0.0901, abs=1e-4)
  assert "(D_COL_001)->B_TABLE" in warning.measurement
  assert "9.0%" in warning.measurement
  assert "fk_fanout_source_orphans" in warning.measurement
  # ...and what it means for the LANDED table.
  assert "key space" in warning.consequence
  assert "UPPER bound" in warning.consequence


def test_an_edge_whose_parent_covers_its_child_raises_no_orphan_warning():
  covered = project_table(
      "p.d.C_TABLE",
      launch_rows=1_000,
      parent="B_TABLE",
      edge="(K)->B_TABLE",
      parent_rows=1_000,
      parent_keys=1_000,
      histogram=_histogram(900, 1_800, zero=100),
      source_parent_tuples=1_000,  # >= the child's 900 key values
  )
  assert covered.matched_share is None
  assert "fk_source_orphans" not in _warnings([covered], 1_000)


def test_the_explosion_names_the_multiplier_and_the_chain():
  warning = _warnings(_projections())["projection_explodes"]
  assert warning.table_name == "A_TABLE"
  assert "146.7x" in warning.measurement
  assert "--num_rows=210,958" in warning.measurement
  assert "B_TABLE -> C_TABLE -> A_TABLE" in warning.measurement
  assert "94.2%" in warning.consequence


def test_a_child_inside_the_factor_ceiling_is_silent():
  """C_TABLE is 4.0x --num_rows on the same launch — the shape working
    exactly as declared, and not a word about it."""
  projections = _projections()
  exploding = {
      w.table_name
      for w in projection_warnings(projections, launch_rows=_LAUNCH_ROWS)
      if w.code == "projection_explodes"
  }
  assert exploding == {"A_TABLE"}


def test_the_zero_share_warning_fires_only_over_the_ceiling():
  """More than half the source parents have no child at all."""
  empty = project_table(
      "p.d.C_TABLE",
      launch_rows=1_000,
      parent="B_TABLE",
      edge="(K)->B_TABLE",
      parent_rows=1_000,
      parent_keys=1_000,
      histogram=_histogram(300, 600, zero=700),
  )
  warning = _warnings([empty], 1_000)["fanout_zero_share"]
  assert "70.0%" in warning.measurement
  assert "30.0%" in warning.consequence  # what the landed child covers
  # The real F_TABLE edge measured 0.2024 and says nothing.
  assert "fanout_zero_share" not in _warnings(_projections())


def test_the_adjusted_table_warns_that_its_repeats_must_reproduce():
  warning = _warnings(_projections())["adjusted_key_projection"]
  assert warning.table_name == "E_TABLE"
  assert "50.25%" in warning.measurement
  assert "ADR 0038" in warning.measurement
  # It names the descendant sized from its DISTINCT keys, and the
  # end-of-run check that proves the assumption held.
  assert "210,958 DISTINCT key values" in warning.consequence
  assert "F_TABLE" in warning.consequence
  assert "model_adjustment_repeat_share" in warning.consequence


def test_an_unadjusted_table_says_nothing_about_repeats():
  root = project_table("p.d.B_TABLE", launch_rows=1_000)
  assert "adjusted_key_projection" not in _warnings([root], 1_000)


def test_the_thresholds_are_stated_and_loose():
  assert PROJECTION_FACTOR_CEILING >= 5
  assert 0 < MATCHED_SHARE_FLOOR <= 0.5
  assert 0.5 <= ZERO_SHARE_CEILING < 1


# --- C. the launcher: the block and the milestones, before the graph ---


def _spec(name,
          rows,
          *,
          parent="",
          child_cols=("D_COL_001",),
          histogram=None,
          parents_measured=None,
          adjustment=None):
  """One `TableSpec` as the relational launcher builds it."""
  from sdfb_beam.pipeline import FkEdgeSpec, PipelineConfig, TableSpec
  from sdfb_core.contracts import TableSchema
  from sdfb_tests.fakes import FakeModelClient

  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": f"p.src.{name}"
      },
      "schema": [{
          "name": c,
          "type": "STRING",
          "mode": "REQUIRED"
      } for c in child_cols],
  })
  fanout = None
  edges: tuple = ()
  if parent:
    fanout = {
        "driving_cols": list(child_cols),
        "histogram": histogram or {},
        "cells": None,
        "exact_cells": False,
        "parents": parents_measured,
    }
    edges = (FkEdgeSpec(
        child_cols=tuple(child_cols),
        ref_cols=tuple(child_cols),
        parent_landing=f"p.d.{parent}",
        parent_table=parent,
        mode="fanout",
    ),)
  config = PipelineConfig(
      table_schema=schema,
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=[{}]),
      num_rows=rows,
      landing_table=f"p.d.{name}",
      fanout=fanout,
  )
  return TableSpec(
      config=config,
      reference_rows=[],
      landing_sink=None,
      dlq_sink=None,  # type: ignore[arg-type]
      parent_edges=edges,
      adjustments=(adjustment,) if adjustment else (),
  )


def _launch_specs():
  """The 2026-09-13 five-table launch, as TableSpecs."""
  from sdfb_core.contracts.model_adjustment import ModelAdjustment

  return [
      _spec("B_TABLE", 210_958),
      _spec(
          "C_TABLE",
          846_891,
          parent="B_TABLE",
          histogram=_histogram(210_958, 846_891)),
      _spec(
          "E_TABLE",
          423_999,
          parent="B_TABLE",
          histogram=_histogram(_E_KEY_VALUES, _E_CHILDREN),
          parents_measured=_E_PARENT_TUPLES,
          adjustment=ModelAdjustment(
              table="p.d.E_TABLE",
              change="pk_dropped",
              declared="pk ['D_COL_001']",
              measured="the full source repeats it",
              consequence="`pk:` DROPPED",
              declared_pk=("D_COL_001",),
              source_repeat_share=1 - _E_KEY_VALUES / _E_CHILDREN,
          )),
      _spec(
          "A_TABLE",
          30_937_138,
          parent="C_TABLE",
          histogram=_histogram(846_891, 30_937_138)),
      _spec(
          "F_TABLE",
          439_889,
          parent="E_TABLE",
          histogram=_histogram(465_079, 1_215_948, zero=118_055)),
  ]


def _rows_and_keys():
  rows = {
      "p.d.B_TABLE": 210_958,
      "p.d.C_TABLE": 846_891,
      "p.d.E_TABLE": 423_999,
      "p.d.A_TABLE": 30_937_138,
      "p.d.F_TABLE": 439_889
  }
  keys = dict(rows, **{"p.d.E_TABLE": 210_958})  # E_TABLE was adjusted
  return rows, keys


def test_the_launcher_projects_the_whole_launch_from_its_specs(caplog):
  """`report_row_projection` turns the resolved specs into the block —
    every count matching that launch's `rows_detail`."""
  import logging

  from sdfb_beam.cli.run_pipeline import report_row_projection

  rows, keys = _rows_and_keys()
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    projections = report_row_projection(
        _launch_specs(),
        launch_rows=_LAUNCH_ROWS,
        rows_by_landing=rows,
        keys_by_landing=keys,
    )
  assert [p.rows for p in projections
         ] == [210_958, 846_891, 423_999, 30_937_138, 439_889]
  assert "ROW PROJECTION" in caplog.text
  assert "name=row_projection " in caplog.text


def test_one_milestone_per_table_and_one_for_the_total(caplog):
  import logging

  from sdfb_beam.cli.run_pipeline import report_row_projection

  rows, keys = _rows_and_keys()
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    report_row_projection(
        _launch_specs(),
        launch_rows=_LAUNCH_ROWS,
        rows_by_landing=rows,
        keys_by_landing=keys,
    )
  assert caplog.text.count("name=row_projection_table ") == 5
  assert caplog.text.count("name=row_projection_total ") == 1
  assert "rows=30937138" in caplog.text
  assert "rows=32858875" in caplog.text
  assert "basis=driven" in caplog.text and "basis=root" in caplog.text


def test_every_warning_is_a_greppable_milestone_too(caplog):
  import logging

  from sdfb_beam.cli.run_pipeline import report_row_projection

  rows, keys = _rows_and_keys()
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    report_row_projection(
        _launch_specs(),
        launch_rows=_LAUNCH_ROWS,
        rows_by_landing=rows,
        keys_by_landing=keys,
    )
  assert caplog.text.count("name=row_projection_warning ") == 3
  for code in ("fk_source_orphans", "projection_explodes",
               "adjusted_key_projection"):
    assert f"code={code}" in caplog.text


def test_a_clean_launch_logs_the_block_and_no_warning_milestone(caplog):
  import logging

  from sdfb_beam.cli.run_pipeline import report_row_projection

  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    report_row_projection(
        [_spec("B_TABLE", 1_000)],
        launch_rows=1_000,
        rows_by_landing={"p.d.B_TABLE": 1_000},
        keys_by_landing={"p.d.B_TABLE": 1_000},
    )
  assert "name=row_projection " in caplog.text
  assert "row_projection_warning" not in caplog.text
  assert "WARNINGS" not in caplog.text


def test_the_projection_is_logged_before_the_graph_is_built(
    monkeypatch, caplog):
  """Ordering is the whole point: an operator sizing a run must read
    the projection BEFORE the DAG exists, not in the job's aftermath."""
  import logging

  import sdfb_beam.cli.run_pipeline as rp

  seen: dict[str, str] = {}

  def _fake_build(p, specs):
    seen["at_build"] = caplog.text
    return []

  monkeypatch.setattr(rp, "build_relational_pipeline", _fake_build)
  monkeypatch.setattr(
      rp,
      "_prepare_table_spec",
      lambda table_args, *a, **kw: _by_landing[table_args.landing_table],
  )
  specs = _launch_specs()
  _by_landing = {s.config.landing_table: s for s in specs}
  plan = rp.LaunchPlan(
      scenario="relational",
      runs=tuple(
          rp.TableRun(
              landing_table=s.config.landing_table,
              source_table=f"p.src.{s.config.landing_table.rsplit('.', 1)[-1]}",
              run_id="proj",
              fk_parent_landing="p.d",
          ) for s in specs),
  )
  args, _ = rp.parse_args([
      "--reference_table",
      "p.src.B_TABLE",
      "--landing_table",
      "p.d.B_TABLE",
      "--dlq_table",
      "p.dq.dlq",
      "--num_rows",
      str(_LAUNCH_ROWS),
      "--run_id",
      "proj",
      "--client_type",
      "fake",
      "--model_uri",
      "gs://b/models/m/v/",
  ])
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    assert rp._run_relational_job(plan, args, ["--runner=DirectRunner"]) == 0
  assert "ROW PROJECTION" in seen["at_build"]
  assert "name=row_projection_total " in seen["at_build"]


def test_the_fanout_payload_carries_the_source_parent_count():
  """`matched_share` is derivable with no extra BigQuery work — the
    measurement already counted the source parent's distinct tuples, so
    the cached payload carries it (launch 2026-09-12: 52,545 parents
    against 583,134 child key values)."""
  from sdfb_beam.io.fanout_stats import fanout_payload

  measured = {
      "histogram": {
          "1": 100,
          "2": 50
      },
      "cells": None,
      "parents": 52_545,
      "children": 200,
  }
  assert fanout_payload(measured, ("D_COL_001",), False)["parents"] == 52_545
