"""Tests for `TableSchema` deserialization, validation, and dialect handling."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sdfb_core.contracts import TableSchema


def test_table_schema_parses_narrow(narrow_ddl_dict):
  ts = TableSchema.model_validate(narrow_ddl_dict)
  assert ts.fqn == "demo_project.demo_dataset.customers"
  assert len(ts.columns) == 4
  assert ts.primary_keys == ["customer_id"]
  assert ts.columns[0].bq_type == "INT64"
  assert ts.columns[1].max_length == 255


def test_field_type_alias_accepted(legacy_field_type_ddl_dict):
  """The legacy `field_type` key from bigquery_ddl_metadata.py must parse."""
  ts = TableSchema.model_validate(legacy_field_type_ddl_dict)
  assert ts.columns[0].bq_type == "INT64"
  assert ts.columns[1].bq_type == "STRING"


def test_columns_alias_accepted(narrow_ddl_dict):
  """`columns` is the Python attribute; `schema` is the JSON key."""
  narrow_ddl_dict["columns"] = narrow_ddl_dict.pop("schema")
  ts = TableSchema.model_validate(narrow_ddl_dict)
  assert len(ts.columns) == 4


def test_pk_must_reference_existing_column(narrow_ddl_dict):
  narrow_ddl_dict["primary_keys"] = ["nonexistent_col"]
  with pytest.raises(ValidationError, match="unknown columns"):
    TableSchema.model_validate(narrow_ddl_dict)


def test_struct_field_requires_nested(struct_ddl_dict):
  ts = TableSchema.model_validate(struct_ddl_dict)
  line_items = ts.columns[1]
  assert line_items.is_struct
  assert line_items.is_repeated
  assert line_items.fields is not None
  assert len(line_items.fields) == 2


def test_non_struct_with_nested_fields_rejected():
  bad = {
      "table_info": {
          "table_id": "demo.t"
      },
      "schema": [{
          "name": "weird",
          "type": "STRING",
          "mode": "REQUIRED",
          "fields": [{
              "name": "x",
              "type": "INT64",
              "mode": "REQUIRED"
          }],
      }],
  }
  with pytest.raises(ValidationError):
    TableSchema.model_validate(bad)


def test_struct_without_nested_fields_rejected():
  bad = {
      "table_info": {
          "table_id": "demo.t"
      },
      "schema": [{
          "name": "addr",
          "type": "RECORD",
          "mode": "REQUIRED"
      }],
  }
  with pytest.raises(ValidationError):
    TableSchema.model_validate(bad)


def test_serialization_uses_canonical_keys(narrow_ddl_dict):
  """`model_dump(by_alias=True)` should output canonical BQ keys."""
  ts = TableSchema.model_validate(narrow_ddl_dict)
  out = ts.model_dump(by_alias=True, exclude_none=True)
  assert "schema" in out
  assert "columns" not in out
  assert out["schema"][0]["type"] == "INT64"
  assert "field_type" not in out["schema"][0]
  assert out["schema"][1]["maxLength"] == 255
