"""B.2 profiler: empty_fraction + empties leave the text_pool (Task 5)."""

import pytest
from sdfb_core.contracts.schema import FieldSchema
from sdfb_core.engines.b2_library.fidelity import ColumnKind, profile_column


def _field(name: str = "NOTES") -> FieldSchema:
  return FieldSchema.model_validate({
      "name": name,
      "type": "STRING",
      "mode": "NULLABLE"
  })


def test_mostly_empty_freetext_column():
  rows = [{
      "NOTES": ""
  } for _ in range(90)] + [{
      "NOTES": f"REF {i:04d} SETTLED PAYMENT ORDER"
  } for i in range(60)]
  prof = profile_column(_field(), rows)
  assert prof.kind is ColumnKind.FREE_TEXT
  assert prof.empty_fraction == pytest.approx(90 / 150)
  assert "" not in prof.text_pool
  assert prof.shape_mix is not None


def test_null_and_empty_are_separate_fractions():
  rows = ([{
      "NOTES": None
  } for _ in range(20)] + [{
      "NOTES": ""
  } for _ in range(30)] + [{
      "NOTES": f"LONG DESCRIPTION VALUE NUMBER {i:03d}"
  } for i in range(55)])
  prof = profile_column(_field(), rows)
  assert prof.null_fraction == pytest.approx(20 / 105)
  assert prof.empty_fraction == pytest.approx(30 / 105)


def test_all_empty_column_routes_constant_empty():
  prof = profile_column(_field(), [{"NOTES": ""} for _ in range(40)])
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
  prof = profile_column(_field("CODE"), rows)
  assert prof.kind is ColumnKind.CATEGORICAL
  assert "" in prof.categories
  assert prof.empty_fraction == 0.0
