"""`fk.orphan` — the in-DAG referential-integrity gate (ADR 0031).

The 2026-08-23 run reported `status=PASSED` on a table where 81.8% of
rows referenced a parent key that does not exist: no rule anywhere
scored referential integrity, so the gate could not see it. The engine
now draws whole parent key tuples, which makes orphans impossible — and
this gate is the independent check that says so, per run, instead of
trusting the generator (three lines of defense).
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=missing-class-docstring

from __future__ import annotations

from pathlib import Path

import apache_beam as beam
import pytest
import yaml
from sdfb_beam.dofns.fk_integrity import EnforceFkIntegrityDoFn

_EDGES = [{"cols": ["CC", "BR"], "keys": [("ES", 10), ("FR", 30)]}]


def _run(dofn, records, side=None):
  out = list(dofn.process(records[0], **({"fk_side": side} if side else {})))
  for r in records[1:]:
    out.extend(dofn.process(r, **({"fk_side": side} if side else {})))
  return out


def _split(results):
  valid = [r for r in results if not isinstance(r, beam.pvalue.TaggedOutput)]
  invalid = [
      r.value for r in results if isinstance(r, beam.pvalue.TaggedOutput)
  ]
  return valid, invalid


class TestOrphanDetection:

  def test_a_real_parent_tuple_passes(self):
    dofn = EnforceFkIntegrityDoFn(fk_key_pools=_EDGES)
    dofn.setup()
    valid, invalid = _split(_run(dofn, [{"CC": "ES", "BR": 10, "X": 1}]))
    assert len(valid) == 1 and not invalid

  def test_a_combination_the_parent_never_held_is_diverted(self):
    """Both values exist in the parent — the COMBINATION does not.
        Exactly the failure per-column pools produce."""
    dofn = EnforceFkIntegrityDoFn(fk_key_pools=_EDGES)
    dofn.setup()
    valid, invalid = _split(_run(dofn, [{"CC": "ES", "BR": 30}]))
    assert not valid
    assert invalid[0]["rule_id"] == "fk.orphan"
    assert invalid[0]["error_type"] == "referential_integrity"
    assert "CC,BR" in invalid[0]["error_detail"]

  def test_null_tuple_is_not_an_orphan(self):
    """SQL MATCH SIMPLE: a NULL FK means "no parent", not a broken
        reference. Diverting these would fail every optional relation."""
    dofn = EnforceFkIntegrityDoFn(fk_key_pools=_EDGES)
    dofn.setup()
    valid, invalid = _split(_run(dofn, [{"CC": None, "BR": None}]))
    assert len(valid) == 1 and not invalid

  def test_side_input_pools_are_accepted(self):
    """A same-job parent delivers its keys as a side input, exactly
        as the Generate DoFn receives them."""
    dofn = EnforceFkIntegrityDoFn(fk_key_pools=[])
    dofn.setup()
    valid, invalid = _split(
        _run(
            dofn, [{
                "CC": "FR",
                "BR": 30
            }, {
                "CC": "FR",
                "BR": 10
            }], side=_EDGES))
    assert len(valid) == 1
    assert len(invalid) == 1

  def test_side_input_pools_survive_a_reentered_setup(self):
    dofn = EnforceFkIntegrityDoFn(fk_key_pools=[])
    dofn.setup()
    _run(dofn, [{"CC": "FR", "BR": 30}], side=_EDGES)
    dofn.setup()  # re-entered lifecycle: must still bind and check
    valid, invalid = _split(_run(dofn, [{"CC": "ES", "BR": 30}], side=_EDGES))
    assert not valid and invalid[0]["rule_id"] == "fk.orphan"

  def test_no_edges_passes_everything_through(self):
    dofn = EnforceFkIntegrityDoFn(fk_key_pools=[])
    dofn.setup()
    valid, invalid = _split(_run(dofn, [{"CC": "ZZ", "BR": 99}]))
    assert len(valid) == 1 and not invalid


def test_fk_orphan_is_a_blocker_rule_in_the_catalog():
  """The gate is only as strong as its threshold: an orphan must fail
    the run, not merely count."""
  catalog = yaml.safe_load(
      Path("config/thresholds.yml").read_text(encoding="utf-8"))
  rule = catalog["rules"]["fk.orphan"]
  assert rule["severity"] == "BLOCKER"
  assert rule["threshold"] == 0
  assert rule["dimension"] == "consistency"


@pytest.mark.parametrize("missing", ["CC", "BR"])
def test_a_missing_fk_column_is_an_orphan_not_a_crash(missing):
  dofn = EnforceFkIntegrityDoFn(fk_key_pools=_EDGES)
  dofn.setup()
  record = {"CC": "ES", "BR": 10}
  record.pop(missing)
  valid, invalid = _split(_run(dofn, [record]))
  assert not valid
  assert invalid[0]["rule_id"] == "fk.orphan"
