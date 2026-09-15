"""Identity synthesis must not clobber a constraint-driven column.

2026-08-25 run …-11759075672032343276: `B_COL_008` declared
`pattern=^(C2E[13][0-9A-F]{20}|7301[0-9A-F]{20})$`, the Tier-P sampler
routed it (`constraint_sampler_active route=pattern capacity=3.63e+24`)
— and every landed value was a UUIDv4, because the column is also the
table's `identity` and the DoFn overwrites every identity column with a
UUID after generation.

Identity synthesis exists for a privacy reason (2026-07: identity
columns copied verbatim, copy_ratio 1.0). A routed sampler satisfies
that reason BETTER: it draws from the clause's value space and rejects
every source value (`_routed_forbidden`, ADR 0023/0028). So when a
column has a constraint-driven generator, that generator owns it — and
the identity contract is kept by giving it the same per-run uniqueness
tracking a PK column gets.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=missing-class-docstring,superfluous-parens

from __future__ import annotations

import logging
import re

from sdfb_beam.dofns.generate import GenerateRecordsDoFn
from sdfb_beam.handlers.fake_client import FakeModelClient
from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationContext

_PATTERN = r"^(C2E[13][0-9A-F]{20}|7301[0-9A-F]{20})$"
_CLAUSE = ('{"llm_prompt_constraint": {"route": "llm", "pattern": '
           '"^(C2E[13][0-9A-F]{20}|7301[0-9A-F]{20})$", "prefix": "C2E"}}')


def _schema(constrained: bool = True) -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.C_TABLE"
      },
      "schema": [
          {
              "name": "B_COL_008",
              "type": "STRING",
              "mode": "REQUIRED",
              "description": _CLAUSE if constrained else "",
          },
          {
              "name": "PLAIN_ID",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "AMOUNT",
              "type": "INT64",
              "mode": "REQUIRED"
          },
      ],
  })


def _rows(n: int = 80) -> list[dict]:
  return [{
      "B_COL_008": f"C2E3{i:020X}",
      "PLAIN_ID": f"SRC{i:05d}",
      "AMOUNT": i % 13,
  } for i in range(n)]


def _ctx(constrained: bool = True) -> GenerationContext:
  return GenerationContext(
      table_schema=_schema(constrained),
      reference_rows=_rows(),
      reference_digest="identity-clause",
      pipeline_run_id="run-1",
      identity_columns=["B_COL_008", "PLAIN_ID"],
      num_rows=200,
  )


def _generate(ctx, n: int = 120) -> list[dict]:
  dofn = GenerateRecordsDoFn(
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=ctx.reference_rows),
      ctx=ctx,
      seed=7,
  )
  dofn.setup()
  return list(dofn.process({"batch_id": 0, "n": n}))


class TestConstraintOwnsTheColumn:

  def test_a_routed_identity_column_keeps_its_declared_shape(self):
    rows = _generate(_ctx())
    assert rows
    values = [r["B_COL_008"] for r in rows]
    offenders = [v for v in values if not re.match(_PATTERN, str(v))]
    assert not offenders, f"identity synthesis clobbered: {offenders[:3]}"

  def test_it_stays_unique_because_identity_still_means_unique(self):
    rows = _generate(_ctx())
    values = [r["B_COL_008"] for r in rows]
    assert len(set(values)) == len(values)

  def test_it_never_emits_a_source_value(self):
    """The privacy property identity synthesis was introduced for —
        the routed sampler rejects the source domain outright."""
    ctx = _ctx()
    source = {r["B_COL_008"] for r in ctx.reference_rows}
    values = {r["B_COL_008"] for r in _generate(ctx)}
    assert not (values & source)

  def test_an_unconstrained_identity_column_still_gets_a_uuid(self):
    rows = _generate(_ctx())
    uuids = [r["PLAIN_ID"] for r in rows]
    assert all(len(str(v)) == 36 and str(v).count("-") == 4 for v in uuids)
    assert len(set(uuids)) == len(uuids)

  def test_the_handover_is_logged(self, caplog):
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      _generate(_ctx())
    assert "name=identity_constraint_owned" in caplog.text
    assert "B_COL_008" in caplog.text

  def test_without_a_clause_nothing_changes(self):
    rows = _generate(_ctx(constrained=False))
    values = [str(r["B_COL_008"]) for r in rows]
    assert all(len(v) == 36 for v in values)  # UUIDs, as before
