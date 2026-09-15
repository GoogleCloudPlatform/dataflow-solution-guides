"""Round-trip tests: BQ DDL JSON ↔ TableSchema ↔ BQ DDL JSON.

These prove the codegen is an identity on the canonical projection
(name + type + mode + nested-structure + length/precision constraints).
"""

from __future__ import annotations

from sdfb_core.codegen import derive_bq_schema
from sdfb_core.contracts import TableSchema


def _canonical(field: dict) -> dict:
  """Project a field dict to the canonical comparable keys."""
  # Accept either `type` or `field_type` on the way in.
  type_key = field.get("type") or field.get("field_type")
  out: dict = {"name": field["name"], "type": type_key, "mode": field["mode"]}
  for opt in ("maxLength", "max_length", "precision", "scale"):
    if opt in field:
      # Normalize the snake/camel variants.
      out[{"max_length": "maxLength"}.get(opt, opt)] = field[opt]
  if field.get("fields"):
    out["fields"] = [_canonical(sub) for sub in field["fields"]]
  return out


def test_round_trip_narrow(narrow_ddl_dict):
  ts = TableSchema.model_validate(narrow_ddl_dict)
  bq_out = derive_bq_schema(ts)

  in_cols = [_canonical(f) for f in narrow_ddl_dict["schema"]]
  out_cols = [_canonical(f) for f in bq_out["fields"]]
  assert in_cols == out_cols


def test_round_trip_struct(struct_ddl_dict):
  ts = TableSchema.model_validate(struct_ddl_dict)
  bq_out = derive_bq_schema(ts)

  in_cols = [_canonical(f) for f in struct_ddl_dict["schema"]]
  out_cols = [_canonical(f) for f in bq_out["fields"]]
  assert in_cols == out_cols


def test_round_trip_legacy_field_type(legacy_field_type_ddl_dict):
  """Legacy `"field_type"` input → canonical `"type"` output."""
  ts = TableSchema.model_validate(legacy_field_type_ddl_dict)
  bq_out = derive_bq_schema(ts)

  # All output fields use the canonical `type` key.
  assert all("type" in f and "field_type" not in f for f in bq_out["fields"])
  # And the projection is identity.
  in_cols = [_canonical(f) for f in legacy_field_type_ddl_dict["schema"]]
  out_cols = [_canonical(f) for f in bq_out["fields"]]
  assert in_cols == out_cols
