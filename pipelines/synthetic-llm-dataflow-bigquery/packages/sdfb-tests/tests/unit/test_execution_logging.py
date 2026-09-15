"""Execution-visibility logging (ADR 0030): every enabled/disabled
config is one human-readable block; in multi-table runs every column
reference is landing-table-qualified so `_full_report.md` +
`worker_logs.jsonl` copy-paste straight into oss/ replacements.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=missing-class-docstring,unused-argument

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

import logging

from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationContext
from sdfb_core.engines.b1_rag import B1RagEngine, HashingEmbedder
from sdfb_core.engines.generation_plan import clear_generation_plan_log
from sdfb_core.observability import log_milestone, milestone_scope

_C2E = ('{"llm_prompt_constraint": {"route": "llm", '
        '"pattern": "^(C2E[13][0-9A-F]{20}|7301[0-9A-F]{20})$"}}')


class TestMilestoneScope:

  def test_scope_tags_every_milestone_with_table(self, caplog):
    with (
        caplog.at_level(logging.INFO, logger="sdfb.milestone"),
        milestone_scope("ORDERS_FLAT"),
    ):
      log_milestone("batch_start", batch_id=1, n=10)
    assert "table=ORDERS_FLAT" in caplog.text
    assert "name=batch_start" in caplog.text

  def test_explicit_table_field_wins_over_scope(self, caplog):
    with (
        caplog.at_level(logging.INFO, logger="sdfb.milestone"),
        milestone_scope("WRONG"),
    ):
      log_milestone("x", table="RIGHT")
    assert "table=RIGHT" in caplog.text
    assert "WRONG" not in caplog.text

  def test_no_scope_no_table_field(self, caplog):
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      log_milestone("plain_event", n=1)
    line = next(r.message for r in caplog.records if "plain_event" in r.message)
    assert "table=" not in line


class TestQualifiedPrettyColumns:

  def _setup(self, caplog, prefix: str):
    clear_generation_plan_log()
    schema = TableSchema.model_validate({
        "table_info": {
            "table_id": "p.src.b_table"
        },
        "schema": [{
            "name": "KEY",
            "type": "STRING",
            "mode": "REQUIRED",
            "description": _C2E
        },],
    })
    rows = [{"KEY": f"C2E3{i:020X}"} for i in range(60)]
    ctx = GenerationContext(
        table_schema=schema,
        reference_rows=rows,
        reference_digest=f"qual-{prefix or 'off'}",
        pipeline_run_id="qual-run",
        landing_table="p.land.B_TABLE",
        log_table_prefix=prefix,
    )
    engine = B1RagEngine(embedder=HashingEmbedder())

    class _C:

      def generate_json(self, **kw):
        return [{"values": []}]

    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      engine.setup(_C(), ctx)
    clear_generation_plan_log()

  def test_multi_table_plan_names_its_table(self, caplog):
    """ADR 0035 rev: the indent-2 pretty plan (which carried
        LANDING.COL keys) is gone; the compact `generation_plan` keeps
        bare column keys and the `table=` field disambiguates a
        multi-table worker log."""
    self._setup(caplog, prefix="B_TABLE")
    plan = [
        r.getMessage()
        for r in caplog.records
        if "name=generation_plan " in r.getMessage()
    ]
    assert len(plan) == 1
    assert "table=p.src.b_table" in plan[0]
    assert '"KEY"' in plan[0]
    assert "name=generation_plan_pretty" not in caplog.text

  def test_single_table_keys_stay_bare(self, caplog):
    self._setup(caplog, prefix="")
    assert '"B_TABLE.KEY"' not in caplog.text
    assert '"KEY"' in caplog.text
