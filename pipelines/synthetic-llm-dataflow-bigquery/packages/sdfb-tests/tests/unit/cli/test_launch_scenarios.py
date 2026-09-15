"""The three launch scenarios — minimal inputs, model-driven (ADR 0032).

Users give landing_table (one FQN or a CSV list) + the flag; everything
else — which tables travel together, the order, fk_parent_landing, the
sibling source tables — comes from `config/relationships/`.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,missing-class-docstring,unused-argument,use-implicit-booleaness-not-comparison

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

from sdfb_beam.cli.run_pipeline import (
    derive_fk_parent_landing,
    derive_source_fqn,
    parse_landing_tables,
    plan_launch,
)
from sdfb_core.contracts.relationships import RelationshipRegistry

_SRC = "proj.src_ds.A_TABLE"
_LAND = "proj.synthetic_data"

_MODEL = """
model: retail
tables:
  A_TABLE:
    pk: [A_COL_001]
  B_TABLE:
    fk:
      - cols: [B_COL_006]
        ref: A_TABLE
        ref_cols: [A_COL_001]
  C_TABLE:
    fk:
      - cols: [PK_X]
        ref: A_TABLE
        ref_cols: [PK_X]
        enforced: false
"""


def _registry(text: str = _MODEL) -> RelationshipRegistry:
  return RelationshipRegistry.from_sources([("config/relationships/retail.yaml",
                                             text)])


def _write_model(tmp_path, text: str = _MODEL):
  (tmp_path / "retail.yaml").write_text(text)
  return tmp_path


class TestDerivations:

  def test_fk_parent_landing_is_the_landing_dataset(self):
    assert derive_fk_parent_landing(
        "proj.synthetic_data.B_TABLE") == "proj.synthetic_data"

  def test_sibling_source_shares_the_reference_dataset(self):
    assert derive_source_fqn(f"{_LAND}.B_TABLE", _SRC) == "proj.src_ds.B_TABLE"

  def test_landing_tables_csv(self):
    assert parse_landing_tables(" a.b.t1, a.b.t2 ") == ["a.b.t1", "a.b.t2"]


class TestScenario1SingleIsolated:

  def test_single_table_flag_false_plans_itself_only(self):
    plan = plan_launch([f"{_LAND}.B_TABLE"], _SRC, False, _registry(), "r1")
    assert plan.scenario == "isolated"
    (run,) = plan.runs
    assert run.landing_table == f"{_LAND}.B_TABLE"
    assert run.source_table == "proj.src_ds.B_TABLE"
    assert run.fk_parent_landing == ""
    assert run.run_id == "r1"  # single run: untouched (goldens)
    # the ignored enforced edge is called out
    assert any("B_TABLE" in w for w in plan.warnings)


class TestScenario2RelationalClosure:

  def test_related_table_expands_to_the_component(self):
    plan = plan_launch([f"{_LAND}.B_TABLE"], _SRC, True, _registry(), "r1")
    assert plan.scenario == "relational_closure"
    order = [r.landing_table for r in plan.runs]
    # parents-first: A before B; C grouped in via its documented edge
    assert order.index(f"{_LAND}.A_TABLE") < order.index(f"{_LAND}.B_TABLE")
    assert f"{_LAND}.C_TABLE" in order
    assert f"{_LAND}.LONER" not in order

  def test_only_enforced_children_get_parent_landing(self):
    plan = plan_launch([f"{_LAND}.B_TABLE"], _SRC, True, _registry(), "r1")
    by_table = {r.landing_table: r for r in plan.runs}
    assert by_table[f"{_LAND}.B_TABLE"].fk_parent_landing == _LAND
    assert by_table[f"{_LAND}.A_TABLE"].fk_parent_landing == ""
    assert by_table[f"{_LAND}.C_TABLE"].fk_parent_landing == ""

  def test_multi_run_ids_are_suffixed(self):
    plan = plan_launch([f"{_LAND}.B_TABLE"], _SRC, True, _registry(), "r1")
    assert [r.run_id for r in plan.runs] == [
        f"r1-{i:02d}-{r.landing_table.rsplit('.', 1)[-1]}"
        for i, r in enumerate(plan.runs)
    ]

  def test_a_table_no_model_declares_stays_single(self):
    plan = plan_launch([f"{_LAND}.LONER"], _SRC, True, _registry(), "r1")
    assert plan.scenario == "isolated"
    (run,) = plan.runs
    assert run.run_id == "r1"
    assert plan.warnings == ()

  def test_disabling_a_table_detaches_its_branch(self):
    """ADR 0032: one flag prunes the launch. B disabled ⇒ a launch on
        A generates A alone, and C (which reached A only through its
        documented edge) is judged on its own edges."""
    disabled = _MODEL.replace("  B_TABLE:\n    fk:",
                              "  B_TABLE:\n    enabled: false\n    fk:")
    plan = plan_launch([f"{_LAND}.A_TABLE"], _SRC, True, _registry(disabled),
                       "r1")
    assert [r.landing_table for r in plan.runs
           ] == [f"{_LAND}.A_TABLE", f"{_LAND}.C_TABLE"]

  def test_disabling_the_only_link_isolates_the_target(self):
    detached = _MODEL.replace(
        "  C_TABLE:\n    fk:",
        "  C_TABLE:\n    enabled: false\n    fk:").replace(
            "  B_TABLE:\n    fk:", "  B_TABLE:\n    enabled: false\n    fk:")
    plan = plan_launch([f"{_LAND}.A_TABLE"], _SRC, True, _registry(detached),
                       "r1")
    assert [r.landing_table for r in plan.runs] == [f"{_LAND}.A_TABLE"]
    assert plan.scenario == "isolated"


class TestScenario3Multi:

  def test_many_tables_flag_false_run_in_given_order(self):
    plan = plan_launch(
        [f"{_LAND}.LONER", f"{_LAND}.A_TABLE"],
        _SRC,
        False,
        _registry(),
        "r1",
    )
    assert plan.scenario == "multi_isolated"
    assert [r.landing_table for r in plan.runs
           ] == [f"{_LAND}.LONER", f"{_LAND}.A_TABLE"]
    assert all(r.fk_parent_landing == "" for r in plan.runs)
    assert [r.run_id for r in plan.runs] == ["r1-00-LONER", "r1-01-A_TABLE"]

  def test_many_tables_flag_true_takes_the_union_and_says_it_is_big(self):
    plan = plan_launch(
        [f"{_LAND}.LONER", f"{_LAND}.B_TABLE"],
        _SRC,
        True,
        _registry(),
        "r1",
    )
    order = [r.landing_table for r in plan.runs]
    assert f"{_LAND}.LONER" in order
    assert order.index(f"{_LAND}.A_TABLE") < order.index(f"{_LAND}.B_TABLE")
    assert len(order) == len(set(order))  # deduped
    assert any("independent concurrent" in w for w in plan.warnings)


class TestMainPlansAndLoops:

  @staticmethod
  def _argv(tmp_path, *extra: str) -> list[str]:
    return [
        "--reference_table=proj.src_ds.B_TABLE",
        f"--landing_table={_LAND}.B_TABLE",
        "--dlq_table=proj.q.dlq",
        "--num_rows=10",
        "--model_uri=gs://m/x",
        "--run_id=r9",
        f"--relationships_uri={tmp_path}",
        *extra,
    ]

  def test_scenario2_runs_component_in_order(self, tmp_path, monkeypatch):
    from sdfb_beam.cli import run_pipeline as rp

    _write_model(tmp_path)
    seen = []
    monkeypatch.setattr(
        rp,
        "_run_one_table",
        lambda a, beam_argv, registry=None: (seen.append(
            (a.landing_table, a.reference_table, a.run_id, a.fk_parent_landing))
                                             or 0),
    )
    rc = rp.main(self._argv(tmp_path, "--multi_table_mode=sequential_jobs"))
    assert rc == 0
    ran = [t for t, *_ in seen]
    # A before B (enforced edge); C rides along on its documented one
    # and has no enforced parent, so its position is free.
    assert ran.index(f"{_LAND}.A_TABLE") < ran.index(f"{_LAND}.B_TABLE")
    assert set(ran) == {
        f"{_LAND}.A_TABLE", f"{_LAND}.B_TABLE", f"{_LAND}.C_TABLE"
    }
    by_table = {t: rest for t, *rest in seen}
    a_run = [f"{_LAND}.A_TABLE", *by_table[f"{_LAND}.A_TABLE"]]
    b_run = [f"{_LAND}.B_TABLE", *by_table[f"{_LAND}.B_TABLE"]]
    assert a_run[1] == "proj.src_ds.A_TABLE"  # sibling source derived
    assert a_run[3] == ""  # root: no parent landing
    assert b_run[3] == _LAND  # child: derived
    assert a_run[2] == "r9-00-A_TABLE"
    assert b_run[2].startswith("r9-") and b_run[2].endswith("-B_TABLE")

  def test_failure_aborts_remaining_tables(self, tmp_path, monkeypatch):
    from sdfb_beam.cli import run_pipeline as rp

    _write_model(tmp_path)
    seen = []
    monkeypatch.setattr(
        rp,
        "_run_one_table",
        lambda a, beam_argv, registry=None: (seen.append(a.landing_table) or 7),
    )
    rc = rp.main(self._argv(tmp_path, "--multi_table_mode=sequential_jobs"))
    assert rc == 7
    assert seen == [f"{_LAND}.A_TABLE"]  # B never ran

  def test_single_job_default_routes_to_relational_runner(
      self, tmp_path, monkeypatch):
    from sdfb_beam.cli import run_pipeline as rp

    _write_model(tmp_path)
    captured = {}
    monkeypatch.setattr(
        rp,
        "_run_relational_job",
        lambda plan, a, beam_argv, registry=None:
        (captured.update(plan=plan) or 0),
    )
    rc = rp.main(self._argv(tmp_path))
    assert rc == 0
    planned = [r.landing_table for r in captured["plan"].runs]
    assert planned.index(f"{_LAND}.A_TABLE") < planned.index(f"{_LAND}.B_TABLE")

  def test_the_model_card_is_logged_before_anything_runs(
      self, tmp_path, monkeypatch):
    """One glance in Cloud Logging answers "what will this run
        generate, and which relationships are live" (ADR 0032). The
        launcher reconfigures root logging, so assert on the CALL."""
    from sdfb_beam.cli import run_pipeline as rp

    _write_model(tmp_path)
    cards = []
    monkeypatch.setattr(
        rp,
        "log_relationship_model",
        lambda table, registry, mode: cards.append(
            (table, mode, registry.card(table))),
    )
    monkeypatch.setattr(
        rp,
        "_run_relational_job",
        lambda plan, a, beam_argv, registry=None: 0,
    )
    assert rp.main(self._argv(tmp_path)) == 0
    assert cards, "no relationship card logged"
    table, mode, card = cards[0]
    assert table == f"{_LAND}.B_TABLE" and mode == "relational"
    assert "RELATIONSHIP MODEL retail" in card
    assert "wave 0 | A_TABLE" in card
    assert "documented, never drawn" in card

  def test_single_job_prep_collects_all_failures(self, tmp_path, monkeypatch,
                                                 caplog):
    """2026-08-22 launch: table 1's P4 stop hid tables 2-4 entirely.
        Driver-side prep must keep going, surface EVERY table's report,
        then abort once with all blockers named."""
    import logging as _logging

    from sdfb_beam.cli import run_pipeline as rp

    _write_model(tmp_path)
    prepped = []

    def _fake_prep(a, client, in_set_landing=frozenset(), registry=None):
      prepped.append(a.landing_table)
      raise SystemExit(
          f"[preflight P4] boom on {a.landing_table.rsplit('.', 1)[-1]}")

    monkeypatch.setattr(rp, "_prepare_table_spec", _fake_prep)
    monkeypatch.setattr(rp, "build_model_client", lambda *a, **k: object())
    import pytest as _pytest

    with (
        caplog.at_level(_logging.ERROR),
        _pytest.raises(SystemExit) as exc,
    ):
      rp.main(self._argv(tmp_path))
    # EVERY table was prepped despite the first failure...
    assert set(prepped) == {
        f"{_LAND}.A_TABLE", f"{_LAND}.B_TABLE", f"{_LAND}.C_TABLE"
    }
    # ...and the single abort names them.
    assert "A_TABLE" in str(exc.value) and "B_TABLE" in str(exc.value)
