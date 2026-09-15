"""Tests for `derive_bq_schema` — TableSchema → BigQuery TableSchema dict."""

from __future__ import annotations

from sdfb_core.codegen import derive_bq_field, derive_bq_load_schema, derive_bq_schema
from sdfb_core.contracts import TableSchema

_LOAD_SAFE_KEYS = {"name", "type", "mode", "description", "fields"}


def _assert_load_safe(fields: list[dict]) -> None:
  """Recursively assert every field dict's keys are a subset of the
    load-safe projection (no `maxLength`/`precision`/`scale`/
    `defaultValueExpression` — apitools `TableFieldSchema` rejects them)."""
  for f in fields:
    assert set(f.keys()) <= _LOAD_SAFE_KEYS, f
    if "fields" in f:
      _assert_load_safe(f["fields"])


def test_bq_schema_basic_shape(narrow_ddl_dict):
  ts = TableSchema.model_validate(narrow_ddl_dict)
  bq = derive_bq_schema(ts)
  assert "fields" in bq
  assert len(bq["fields"]) == 4

  customer_id = bq["fields"][0]
  assert customer_id == {
      "name": "customer_id",
      "type": "INT64",
      "mode": "REQUIRED",
  }


def test_bq_schema_preserves_max_length(narrow_ddl_dict):
  ts = TableSchema.model_validate(narrow_ddl_dict)
  bq = derive_bq_schema(ts)
  email = next(f for f in bq["fields"] if f["name"] == "email")
  assert email["maxLength"] == 255


def test_bq_schema_preserves_numeric_precision_scale(narrow_ddl_dict):
  ts = TableSchema.model_validate(narrow_ddl_dict)
  bq = derive_bq_schema(ts)
  ltv = next(f for f in bq["fields"] if f["name"] == "lifetime_value")
  assert ltv["precision"] == 18
  assert ltv["scale"] == 2


def test_bq_schema_recurses_into_struct(struct_ddl_dict):
  ts = TableSchema.model_validate(struct_ddl_dict)
  bq = derive_bq_schema(ts)

  line_items = next(f for f in bq["fields"] if f["name"] == "line_items")
  assert line_items["type"] == "RECORD"
  assert line_items["mode"] == "REPEATED"
  assert len(line_items["fields"]) == 2

  sku = line_items["fields"][0]
  assert sku == {"name": "sku", "type": "STRING", "mode": "REQUIRED"}


def test_derive_bq_field_skips_unset_optionals(narrow_ddl_dict):
  """Optional keys (description, precision, scale, etc.) are omitted when unset."""
  ts = TableSchema.model_validate(narrow_ddl_dict)
  customer_id = derive_bq_field(ts.columns[0])
  assert "description" not in customer_id
  assert "maxLength" not in customer_id
  assert "precision" not in customer_id


# --- derive_bq_load_schema — load-safe projection (WS4 CRITICAL-1) ---------
#
# `WriteToBigQuery(method=FILE_LOADS)` routes through the vendored apitools
# `TableFieldSchema`, which rejects any key outside
# {categories, description, fields, mode, name, policyTags, type} at RUNTIME
# (`derive_bq_schema`'s `maxLength`/`precision`/`scale`/
# `defaultValueExpression` all blow up there). `derive_bq_load_schema` must
# strip those down to the load-safe projection while keeping name/type/mode
# (and recursing into nested RECORD `fields`) identical to `derive_bq_schema`.


def test_bq_load_schema_strips_max_length(narrow_ddl_dict):
  ts = TableSchema.model_validate(narrow_ddl_dict)
  load = derive_bq_load_schema(ts)
  _assert_load_safe(load["fields"])

  email = next(f for f in load["fields"] if f["name"] == "email")
  assert "maxLength" not in email
  assert email["type"] == "STRING"
  assert email["mode"] == "REQUIRED"


def test_bq_load_schema_strips_precision_and_scale(narrow_ddl_dict):
  ts = TableSchema.model_validate(narrow_ddl_dict)
  load = derive_bq_load_schema(ts)

  ltv = next(f for f in load["fields"] if f["name"] == "lifetime_value")
  assert "precision" not in ltv
  assert "scale" not in ltv
  assert ltv["type"] == "NUMERIC"
  assert ltv["mode"] == "NULLABLE"


def test_bq_load_schema_recurses_into_struct(struct_ddl_dict):
  ts = TableSchema.model_validate(struct_ddl_dict)
  load = derive_bq_load_schema(ts)
  _assert_load_safe(load["fields"])

  line_items = next(f for f in load["fields"] if f["name"] == "line_items")
  assert line_items["type"] == "RECORD"
  assert line_items["mode"] == "REPEATED"
  assert len(line_items["fields"]) == 2
  assert line_items["fields"][0] == {
      "name": "sku",
      "type": "STRING",
      "mode": "REQUIRED",
  }


def test_bq_load_schema_matches_derive_bq_schema_names_types_modes(
    narrow_ddl_dict,):
  """The load-safe projection must not diverge from `derive_bq_schema` on
    the fields that both share — only the parameterized-constraint keys are
    dropped."""
  ts = TableSchema.model_validate(narrow_ddl_dict)
  full = derive_bq_schema(ts)
  load = derive_bq_load_schema(ts)

  assert [f["name"] for f in load["fields"]
         ] == [f["name"] for f in full["fields"]]
  for full_field, load_field in zip(
      full["fields"], load["fields"], strict=True):
    assert load_field["name"] == full_field["name"]
    assert load_field["type"] == full_field["type"]
    assert load_field["mode"] == full_field["mode"]
