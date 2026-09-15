"""Relational preflight P2-P5 (Task 13), model-driven (ADR 0032).

The relational input is the table's entry in `config/relationships/`;
the table description is never read for it.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel

import pytest
from sdfb_beam.cli.preflight import preflight
from sdfb_core.contracts import TableSchema
from sdfb_core.contracts.relationships import parse_relationship_model

_MODEL = """
model: sales
tables:
  t:
    pk: [ID]
    identity: [ID]
    fk:
      - cols: [CUST_ID]
        ref: ds.customers
        ref_cols: [ID]
"""


def _relations(text: str = _MODEL, table: str = "t"):
  return parse_relationship_model(text, source="test.yaml").tables[table]


def _schema(desc: str = "", cols=("ID", "CUST_ID", "NOTES")) -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t",
          "description": desc
      },
      "schema": [{
          "name": c,
          "type": "STRING",
          "mode": "REQUIRED"
      } for c in cols],
  })


def _rows(n: int = 10) -> list[dict]:
  return [{"ID": f"id{i}", "CUST_ID": "c1", "NOTES": "x"} for i in range(n)]


def test_p2_unknown_column_stops():
  """A typo in the model file stops the launch here, naming it."""
  bad = _relations("model: m\ntables:\n  t:\n    pk: [NOPE]\n")
  with pytest.raises(SystemExit, match=r"preflight P2.*NOPE"):
    preflight(_schema(), (), (), _rows(), relations=bad)


def test_p2_ignores_documented_edge_columns():
  """A documented edge's join key need not exist in the DDL — that is
    the whole reason `enforced: false` exists."""
  relations = _relations("model: m\ntables:\n  t:\n    pk: [ID]\n    fk:\n"
                         "      - cols: [NOT_A_COLUMN]\n        ref: ds.other\n"
                         "        ref_cols: [X]\n        enforced: false\n")
  assert preflight(
      _schema(), (), (), _rows(), relations=relations).pk_cols == ("ID",)


def test_p3_unresolved_fk_parent_stops():
  with pytest.raises(SystemExit, match=r"preflight P3.*customers"):
    preflight(
        _schema(),
        (),
        (),
        _rows(),
        relations=_relations(),
        fk_parents_resolved={"ds.customers": False},
    )


def test_p3_resolved_parent_passes():
  result = preflight(
      _schema(),
      (),
      (),
      _rows(),
      relations=_relations(),
      fk_parents_resolved={"ds.customers": True},
  )
  assert result.pk_cols == ("ID",)


def test_the_model_is_the_source_of_truth_over_cli_flags():
  result = preflight(_schema(), (), (), _rows(), relations=_relations())
  assert result.pk_cols == ("ID",)
  assert result.identity_cols == ("ID",)

  conflicting = preflight(
      _schema(), ("CUST_ID",), (), _rows(), relations=_relations())
  assert conflicting.pk_cols == ("ID",)  # the model wins
  assert any("IGNORED" in w for w in conflicting.warnings)


def test_a_table_outside_every_model_falls_back_to_cli_flags():
  result = preflight(_schema(), ("ID",), ("CUST_ID",), _rows())
  assert result.relations is None
  assert result.pk_cols == ("ID",)
  assert result.identity_cols == ("CUST_ID",)


def test_p5_duplicate_pk_in_sample_warns_not_stops():
  rows = [*_rows(), {"ID": "id0", "CUST_ID": "c1", "NOTES": "dup"}]
  result = preflight(_schema(), (), (), rows, relations=_relations())
  assert any("not unique in the reference sample" in w for w in result.warnings)


def test_no_model_entry_passthrough():
  result = preflight(_schema("just prose"), ("A",), (), [])
  assert result.relations is None
  assert result.pk_cols == ("A",)


# --------------------------------------------------------------------------- #
# prompt-constraint discovery (C5 — refinement, never a requirement)
# --------------------------------------------------------------------------- #
def _capture_milestones(monkeypatch):
  calls: list = []
  monkeypatch.setattr(
      "sdfb_beam.cli.preflight.log_milestone",
      lambda name, **kw: calls.append((name, kw)),
  )
  return calls


def test_constraints_on_but_none_found_is_a_logged_noop(monkeypatch):
  calls = _capture_milestones(monkeypatch)
  result = preflight(_schema("just prose"), (), (), _rows())
  assert result.relations is None  # generation proceeds unchanged
  names = [n for n, _ in calls]
  assert "prompt_constraints_none" in names
  (_, kw) = next(c for c in calls if c[0] == "prompt_constraints_none")
  assert "no-op" in kw["note"]


def test_constraints_found_are_listed(monkeypatch):
  calls = _capture_milestones(monkeypatch)
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t"
      },
      "schema": [
          {
              "name": "ID",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "NOTES",
              "type": "STRING",
              "mode": "NULLABLE",
              "description": 'x {"llm_prompt_constraint": "SWIFT refs"}',
          },
      ],
  })
  preflight(schema, (), (), [])
  (_, kw) = next(c for c in calls if c[0] == "prompt_constraints_found")
  assert kw["columns"] == "NOTES"
  assert kw["count"] == 1
  # The milestone shows WHAT was fetched, not just where: the rendered
  # clause plus its sha12, so a Terraform edit is verifiable from logs.
  import json as _json

  detail = _json.loads(kw["detail"])
  assert detail["NOTES"]["clause"] == "SWIFT refs"
  assert len(detail["NOTES"]["clause_sha12"]) == 12
  assert detail["NOTES"]["chars"] == len("SWIFT refs")


def test_constraints_disabled_and_none_found_stays_silent(monkeypatch):
  calls = _capture_milestones(monkeypatch)
  preflight(
      _schema("just prose"), (), (), _rows(), prompt_constraints_enabled=False)
  assert "prompt_constraints_none" not in [n for n, _ in calls]


def test_malformed_column_constraint_stops_loudly():
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t"
      },
      "schema": [{
          "name": "NOTES",
          "type": "STRING",
          "mode": "NULLABLE",
          "description": '{"llm_prompt_constraint": BROKEN}',
      }],
  })
  with pytest.raises(SystemExit, match="NOTES"):
    preflight(schema, (), (), [])
