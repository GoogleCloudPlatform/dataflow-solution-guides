"""Unit tests for `sdfb_beam.ddl.extractor`.

The extractor is exercised against mocked `bigquery.Client` instances —
no live GCP credentials required. The strongest assertion: the
extractor's output deserializes cleanly into `TableSchema`, closing the
loop with the codegen test suite.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,protected-access

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sdfb_beam.ddl.extractor import _field_to_dict, extract_ddl_metadata
from sdfb_core.contracts import TableSchema


def _make_field(
    name,
    field_type,
    *,
    mode="NULLABLE",
    description=None,
    fields=(),
    max_length=None,
    precision=None,
    scale=None,
):
  """Build a MagicMock that quacks like a `bigquery.SchemaField`."""
  f = MagicMock()
  f.name = name
  f.field_type = field_type
  f.mode = mode
  f.description = description
  f.fields = fields
  f.max_length = max_length
  f.precision = precision
  f.scale = scale
  return f


def _make_table(schema_fields,
                *,
                primary_keys=None,
                partitioning=None,
                clustering=None):
  """Build a MagicMock that quacks like a `bigquery.Table`."""
  t = MagicMock()
  t.schema = schema_fields
  t.created = None
  t.modified = None
  t.expires = None
  t.location = "EU"
  t.description = ""
  t.labels = {}
  t.table_type = "TABLE"
  t.encryption_configuration = None
  t.time_partitioning = partitioning
  t.range_partitioning = None
  t.clustering_fields = clustering
  t.num_rows = 100
  t.require_partition_filter = False
  t._properties = {}

  if primary_keys:
    constraints = MagicMock()
    constraints.primary_key.columns = list(primary_keys)
    t.table_constraints = constraints
  else:
    t.table_constraints = None
  return t


def _fake_extraction_client():
  """Build a mocked `bigquery.Client` for standard extraction tests.

    Returns a client configured with a customers table: three columns
    (customer_id REQUIRED INT64, email REQUIRED STRING(255),
    lifetime_value NULLABLE NUMERIC(18,2)) with primary key on
    customer_id. INFORMATION_SCHEMA queries return empty result.
    """
  table = _make_table(
      schema_fields=[
          _make_field("customer_id", "INT64", mode="REQUIRED"),
          _make_field("email", "STRING", mode="REQUIRED", max_length=255),
          _make_field(
              "lifetime_value",
              "NUMERIC",
              mode="NULLABLE",
              precision=18,
              scale=2,
          ),
      ],
      primary_keys=["customer_id"],
  )
  client = MagicMock()
  client.get_table.return_value = table
  # INFORMATION_SCHEMA query should not crash — return empty result.
  client.query.return_value.result.return_value = iter([])
  return client


# ---------------------------------------------------------------------------
# _field_to_dict — canonical key, recursion, constraints.
# ---------------------------------------------------------------------------


def test_field_to_dict_emits_canonical_type_key():
  """Output uses `"type"`, not `"field_type"` (canonical BQ JSON schema)."""
  f = _make_field("customer_id", "INT64", mode="REQUIRED")
  out = _field_to_dict(f)
  assert "type" in out
  assert "field_type" not in out
  assert out == {
      "name": "customer_id",
      "type": "INT64",
      "mode": "REQUIRED",
      "description": "",
  }


def test_field_to_dict_recurses_into_struct():
  """STRUCT / RECORD fields preserve nested schema (original script dropped this)."""
  inner_a = _make_field("city", "STRING", mode="REQUIRED")
  inner_b = _make_field("country", "STRING", mode="REQUIRED", max_length=2)
  outer = _make_field(
      "address", "RECORD", mode="NULLABLE", fields=(inner_a, inner_b))
  out = _field_to_dict(outer)
  assert out["type"] == "RECORD"
  assert "fields" in out
  assert len(out["fields"]) == 2
  assert out["fields"][0] == {
      "name": "city",
      "type": "STRING",
      "mode": "REQUIRED",
      "description": "",
  }
  assert out["fields"][1]["max_length"] == 2


def test_field_to_dict_includes_string_max_length():
  f = _make_field("email", "STRING", mode="REQUIRED", max_length=255)
  out = _field_to_dict(f)
  assert out["max_length"] == 255


def test_field_to_dict_includes_numeric_precision_scale():
  f = _make_field("price", "NUMERIC", mode="REQUIRED", precision=18, scale=2)
  out = _field_to_dict(f)
  assert out["precision"] == 18
  assert out["scale"] == 2


def test_field_to_dict_omits_unset_constraints():
  f = _make_field("flag", "BOOL", mode="REQUIRED")
  out = _field_to_dict(f)
  assert "max_length" not in out
  assert "precision" not in out
  assert "scale" not in out


# ---------------------------------------------------------------------------
# extract_ddl_metadata — end-to-end with a mocked client.
# ---------------------------------------------------------------------------


def test_extractor_output_parses_as_table_schema():
  """The strongest gate: extractor → TableSchema is identity (no glue needed)."""
  client = _fake_extraction_client()

  result = extract_ddl_metadata(
      project="demo_project",
      dataset="demo_ds",
      table="customers",
      client=client,
  )

  # Output is consumable by TableSchema.
  ts = TableSchema.model_validate(result)
  assert ts.fqn == "demo_project.demo_ds.customers"
  assert ts.primary_keys == ["customer_id"]
  assert len(ts.columns) == 3
  assert ts.columns[0].bq_type == "INT64"
  assert ts.columns[1].max_length == 255
  assert ts.columns[2].precision == 18
  assert ts.columns[2].scale == 2


def test_extractor_handles_struct_columns():
  """STRUCT recursion survives the round trip into TableSchema."""
  inner = _make_field("sku", "STRING", mode="REQUIRED")
  qty = _make_field("qty", "INT64", mode="REQUIRED")
  table = _make_table(
      schema_fields=[
          _make_field("order_id", "STRING", mode="REQUIRED"),
          _make_field(
              "line_items",
              "RECORD",
              mode="REPEATED",
              fields=(inner, qty),
          ),
      ],
      primary_keys=["order_id"],
  )
  client = MagicMock()
  client.get_table.return_value = table
  client.query.return_value.result.return_value = iter([])

  result = extract_ddl_metadata(
      project="demo_project",
      dataset="demo_ds",
      table="orders",
      client=client,
  )
  ts = TableSchema.model_validate(result)
  line_items = ts.columns[1]
  assert line_items.is_struct
  assert line_items.is_repeated
  assert line_items.fields is not None
  assert len(line_items.fields) == 2
  assert line_items.fields[0].name == "sku"


def test_extractor_falls_back_to_description_pk():
  """PK can be parsed from description when table_constraints is unset."""
  table = _make_table(
      schema_fields=[_make_field("id", "INT64", mode="REQUIRED")],)
  table.description = "Test table.\nPRIMARY KEY: id\nOther notes."
  table.table_constraints = None

  client = MagicMock()
  client.get_table.return_value = table
  client.query.return_value.result.return_value = iter([])

  result = extract_ddl_metadata(
      project="p", dataset="d", table="t", client=client)
  assert result["primary_keys"] == ["id"]


def test_extractor_continues_when_partition_query_fails():
  """INFORMATION_SCHEMA partition query failure must not abort extraction."""
  table = _make_table(
      schema_fields=[_make_field("id", "INT64", mode="REQUIRED")],)
  client = MagicMock()
  client.get_table.return_value = table
  client.query.side_effect = RuntimeError(
      "permission denied on INFORMATION_SCHEMA")

  # Should NOT raise — partition count is best-effort.
  result = extract_ddl_metadata(
      project="p", dataset="d", table="t", client=client)
  assert result["storage_info"]["num_partitions"] is None


# ---------------------------------------------------------------------------
# extract_table_schema — FQN parsing + validation wrapper (WS4 §6b).
# ---------------------------------------------------------------------------


def test_extract_table_schema_rejects_malformed_fqn():
  """FQN validation: must be exactly 'project.dataset.table' format."""
  from sdfb_beam.ddl import extract_table_schema

  for bad in ("dataset.table", "a.b.c.d", "a..c", "", "solo"):
    with pytest.raises(ValueError, match=r"project\.dataset\.table"):
      extract_table_schema(bad)


def test_extract_table_schema_returns_validated_schema():
  """extract_table_schema returns a live validated TableSchema."""
  from sdfb_beam.ddl import extract_table_schema

  client = _fake_extraction_client()
  schema = extract_table_schema("proj.ds.tbl", client=client)
  assert schema.fqn  # a real TableSchema, not a dict
  assert [c.name for c in schema.columns]  # columns materialized
