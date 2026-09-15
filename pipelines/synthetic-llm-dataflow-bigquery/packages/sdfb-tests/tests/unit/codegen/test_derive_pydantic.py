"""Tests for `derive_record_model` — the dynamic Pydantic class factory."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=invalid-name

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sdfb_core.codegen import derive_record_model
from sdfb_core.contracts import GeneratedRecord, TableSchema


def test_record_inherits_from_generated_record(narrow_ddl_dict):
  ts = TableSchema.model_validate(narrow_ddl_dict)
  Record = derive_record_model(ts)
  assert issubclass(Record, GeneratedRecord)


def test_record_accepts_valid_row(narrow_ddl_dict):
  ts = TableSchema.model_validate(narrow_ddl_dict)
  Record = derive_record_model(ts)
  r = Record(
      customer_id=1,
      email="a@example.com",
      signup_at=datetime(2026, 1, 1),
      lifetime_value=Decimal("123.45"),
  )
  assert r.customer_id == 1
  assert r.lifetime_value == Decimal("123.45")


def test_record_accepts_iso_timestamp_string(narrow_ddl_dict):
  """LLMs emit timestamps as ISO strings — Pydantic must parse them."""
  ts = TableSchema.model_validate(narrow_ddl_dict)
  Record = derive_record_model(ts)
  r = Record(
      customer_id=1,
      email="a@example.com",
      signup_at="2026-01-01T00:00:00Z",
      lifetime_value=None,
  )
  assert isinstance(r.signup_at, datetime)


def test_record_rejects_missing_required(narrow_ddl_dict):
  ts = TableSchema.model_validate(narrow_ddl_dict)
  Record = derive_record_model(ts)
  with pytest.raises(ValidationError):
    Record(
        # customer_id missing — REQUIRED
        email="a@example.com",
        signup_at=datetime(2026, 1, 1),
        lifetime_value=None,
    )


def test_record_rejects_extra_column(narrow_ddl_dict):
  ts = TableSchema.model_validate(narrow_ddl_dict)
  Record = derive_record_model(ts)
  with pytest.raises(ValidationError):
    Record(
        customer_id=1,
        email="a@example.com",
        signup_at=datetime(2026, 1, 1),
        lifetime_value=None,
        unexpected_col="surprise",  # extra='forbid'
    )


def test_string_max_length_enforced(narrow_ddl_dict):
  ts = TableSchema.model_validate(narrow_ddl_dict)
  Record = derive_record_model(ts)
  with pytest.raises(ValidationError):
    Record(
        customer_id=1,
        email="x" * 1000,  # exceeds max_length=255
        signup_at=datetime(2026, 1, 1),
        lifetime_value=None,
    )


def test_repeated_struct_record(struct_ddl_dict):
  ts = TableSchema.model_validate(struct_ddl_dict)
  Record = derive_record_model(ts)
  r = Record(
      order_id="ORD-1",
      line_items=[{
          "sku": "SKU-A",
          "qty": 3
      }, {
          "sku": "SKU-B",
          "qty": 1
      }],
  )
  assert len(r.line_items) == 2
  assert r.line_items[0].sku == "SKU-A"
  assert r.line_items[1].qty == 1


def test_repeated_defaults_to_empty_list(struct_ddl_dict):
  """REPEATED columns default to []."""
  # Make line_items REPEATED but not REQUIRED at the table-schema level
  # by re-parsing with mode NULLABLE on the parent: actually REPEATED
  # already defaults to [] regardless of explicit mode handling.
  ts = TableSchema.model_validate(struct_ddl_dict)
  Record = derive_record_model(ts)
  r = Record(order_id="ORD-2")  # line_items not supplied
  assert r.line_items == []


def test_pk_validator_fires_when_pk_is_nullable():
  """If a PK column is NULLABLE in BQ (allowed but discouraged), the model
    validator should still reject a null PK."""
  schema = {
      "table_info": {
          "table_id": "demo.t"
      },
      "schema": [
          {
              "name": "id",
              "type": "INT64",
              "mode": "NULLABLE"
          },
          {
              "name": "label",
              "type": "STRING",
              "mode": "NULLABLE"
          },
      ],
      "primary_keys": ["id"],
  }
  ts = TableSchema.model_validate(schema)
  Record = derive_record_model(ts)
  with pytest.raises(ValidationError, match="Primary-key"):
    Record(id=None, label="anything")
