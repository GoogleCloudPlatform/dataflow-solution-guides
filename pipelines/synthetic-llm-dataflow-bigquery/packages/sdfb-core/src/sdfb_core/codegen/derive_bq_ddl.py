"""Derive a BigQuery TableSchema dict from a `TableSchema`.

The output matches the format consumed by:
  - Beam's `WriteToBigQuery(schema={"fields": [...]})`
  - the BQ REST API `tables.insert` / `tables.update`
  - `bq load --schema_from_json`

REF: https://docs.cloud.google.com/bigquery/docs/schemas#creating_a_JSON_schema_file
"""

from __future__ import annotations

from sdfb_core.contracts.schema import FieldSchema, TableSchema

#: Keys the vendored apitools `TableFieldSchema` accepts on a load job
#: (`BigQueryWrapper._insert_load_job` → FILE_LOADS). Anything else —
#: `maxLength`, `precision`, `scale`, `defaultValueExpression` — is accepted
#: by Beam's `WriteToBigQuery(schema=...)` at graph-construction time but
#: raises `AttributeError: May not assign arbitrary value ... to message
#: TableFieldSchema` at RUNTIME. `categories`/`policyTags` are also accepted
#: by apitools but `derive_bq_field` never emits them, so they are omitted
#: from this projection.
_LOAD_SAFE_KEYS = ("name", "type", "mode", "description")


def derive_bq_schema(table_schema: TableSchema) -> dict:
  """Return a BQ `TableSchema` dict (`{"fields": [...]}`)."""
  return {"fields": [derive_bq_field(c) for c in table_schema.columns]}


def derive_bq_load_schema(table_schema: TableSchema) -> dict:
  """Return a load-safe BQ `TableSchema` dict (`{"fields": [...]}`).

    Same shape as `derive_bq_schema`, but each field keeps only the keys
    the FILE_LOADS runtime path accepts (`name`, `type`, `mode`,
    `description`) — parameterized-constraint keys (`maxLength`,
    `precision`, `scale`, `defaultValueExpression`) are dropped, recursing
    into nested `fields` for RECORD/STRUCT columns. Those constraints
    cannot be carried onto an auto-created (`CREATE_IF_NEEDED`) table via
    the load job API; they must be applied out-of-band (e.g. `bq mk`/DDL)
    if needed.
    """
  return {
      "fields": [
          _project_load_safe(f)
          for f in derive_bq_schema(table_schema)["fields"]
      ]
  }


def _project_load_safe(field: dict) -> dict:
  """Recursively strip a `derive_bq_field` dict down to load-safe keys."""
  out = {k: field[k] for k in _LOAD_SAFE_KEYS if k in field}
  if "fields" in field:
    out["fields"] = [_project_load_safe(sub) for sub in field["fields"]]
  return out


def derive_bq_field(field: FieldSchema) -> dict:
  """Return a single BQ field schema dict, canonical key order."""
  out: dict = {
      "name": field.name,
      "type": field.bq_type,
      "mode": field.mode,
  }
  if field.description:
    out["description"] = field.description
  if field.max_length is not None:
    out["maxLength"] = field.max_length
  if field.precision is not None:
    out["precision"] = field.precision
  if field.scale is not None:
    out["scale"] = field.scale
  if field.default_value_expression is not None:
    out["defaultValueExpression"] = field.default_value_expression
  if field.is_struct:
    out["fields"] = [derive_bq_field(sub) for sub in (field.fields or [])]
  return out
