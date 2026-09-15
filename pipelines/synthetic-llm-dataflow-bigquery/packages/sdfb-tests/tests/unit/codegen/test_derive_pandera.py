"""Tests for `derive_pandera_schema` — Pydantic-mirroring DataFrameSchema.

This test file lives under `sdfb-tests/tests/unit/codegen/` (alongside the
sdfb_core codegen tests) but the function-under-test lives in `sdfb_beam`
because pandera + pandas belong to the Beam-layer dep surface.
"""

from __future__ import annotations

import pandas as pd
import pandera.errors as pa_err
import pandera.pandas as pa
import pytest
from sdfb_beam.codegen import derive_pandera_schema
from sdfb_core.contracts import TableSchema


def test_pandera_schema_shape(narrow_ddl_dict):
  ts = TableSchema.model_validate(narrow_ddl_dict)
  schema = derive_pandera_schema(ts)
  assert isinstance(schema, pa.DataFrameSchema)
  assert set(schema.columns.keys()) == {
      "customer_id",
      "email",
      "signup_at",
      "lifetime_value",
  }
  assert schema.columns["customer_id"].nullable is False
  assert schema.columns["lifetime_value"].nullable is True


def test_pandera_schema_accepts_valid_df(narrow_ddl_dict):
  ts = TableSchema.model_validate(narrow_ddl_dict)
  schema = derive_pandera_schema(ts)
  df = pd.DataFrame({
      "customer_id":
          pd.array([1, 2], dtype="Int64"),
      "email":
          pd.array(["a@x.com", "b@x.com"], dtype="string"),
      "signup_at":
          pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"],
                         utc=True),
      "lifetime_value":
          pd.array([None, None], dtype="object"),
  })
  schema.validate(df, lazy=True)


def test_pandera_schema_rejects_duplicate_pk(narrow_ddl_dict):
  ts = TableSchema.model_validate(narrow_ddl_dict)
  schema = derive_pandera_schema(ts)
  df = pd.DataFrame({
      "customer_id":
          pd.array([1, 1], dtype="Int64"),
      "email":
          pd.array(["a@x.com", "b@x.com"], dtype="string"),
      "signup_at":
          pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"],
                         utc=True),
      "lifetime_value":
          pd.array([None, None], dtype="object"),
  })
  with pytest.raises(pa_err.SchemaErrors):
    schema.validate(df, lazy=True)


def test_pandera_schema_rejects_unknown_column(narrow_ddl_dict):
  """`strict=True` rejects DataFrames with extra columns."""
  ts = TableSchema.model_validate(narrow_ddl_dict)
  schema = derive_pandera_schema(ts)
  df = pd.DataFrame({
      "customer_id": pd.array([1], dtype="Int64"),
      "email": pd.array(["a@x.com"], dtype="string"),
      "signup_at": pd.to_datetime(["2026-01-01T00:00:00Z"], utc=True),
      "lifetime_value": pd.array([None], dtype="object"),
      "stray": pd.array(["unexpected"], dtype="string"),
  })
  with pytest.raises(pa_err.SchemaErrors):
    schema.validate(df, lazy=True)


def test_pandera_schema_struct_column_is_object(struct_ddl_dict):
  """STRUCT / REPEATED columns surface as object dtype at the batch layer."""
  ts = TableSchema.model_validate(struct_ddl_dict)
  schema = derive_pandera_schema(ts)
  assert str(schema.columns["line_items"].dtype) in {"object", "Object"}
