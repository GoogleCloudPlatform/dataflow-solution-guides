"""B.1 profiler: empty_fraction + empties leave the pools (Task 5)."""

import pytest
from sdfb_core.contracts.schema import TableSchema
from sdfb_core.engines.b1_rag.profile import ColumnKind, profile_columns


def _schema(cols: list[dict]) -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t"
      },
      "schema": cols
  })


def _string_col(name: str = "NOTES") -> list[dict]:
  return [{"name": name, "type": "STRING", "mode": "NULLABLE"}]


def test_mostly_empty_freetext_column():
  rows = [{
      "NOTES": ""
  } for _ in range(90)] + [{
      "NOTES": f"REF {i:04d} SETTLED PAYMENT ORDER"
  } for i in range(60)]
  prof = profile_columns(_schema(_string_col()), rows)["NOTES"]
  assert prof.kind is ColumnKind.FREE_TEXT
  assert prof.empty_fraction == pytest.approx(90 / 150)
  assert "" not in prof.text_examples
  assert all(v.strip() for v in prof.observed_values)
  assert prof.shape_mix is not None


def test_null_and_empty_are_separate_fractions():
  rows = ([{
      "NOTES": None
  } for _ in range(20)] + [{
      "NOTES": ""
  } for _ in range(30)] + [{
      "NOTES": f"LONG DESCRIPTION VALUE NUMBER {i:03d}"
  } for i in range(55)])
  prof = profile_columns(_schema(_string_col()), rows)["NOTES"]
  assert prof.null_fraction == pytest.approx(20 / 105)
  assert prof.empty_fraction == pytest.approx(30 / 105)


def test_all_empty_column_routes_constant_empty():
  rows = [{"NOTES": ""} for _ in range(40)]
  prof = profile_columns(_schema(_string_col()), rows)["NOTES"]
  assert prof.kind is ColumnKind.CONSTANT
  assert prof.constant_value == ""


def test_categorical_keeps_empties_in_categories():
  rows = ([{
      "CODE": ""
  } for _ in range(50)] + [{
      "CODE": "A"
  } for _ in range(25)] + [{
      "CODE": "B"
  } for _ in range(25)])
  prof = profile_columns(_schema(_string_col("CODE")), rows)["CODE"]
  assert prof.kind is ColumnKind.CATEGORICAL
  assert "" in prof.categories  # frequency table carries the parity here
  assert prof.empty_fraction == 0.0  # never double-counted with categories
