"""Derive a Pandera `DataFrameSchema` from a `TableSchema`.

Used by `PanderaValidateBatchDoFn` (M1 §8) — per-batch validation in
Mode A. Failure set is intentionally a superset of the Pydantic per-record
model's failure set, so anything Pydantic rejects this layer rejects too,
plus batch-level checks (PK uniqueness, optionally cross-row constraints).

REFs:
  - https://pandera.readthedocs.io/en/stable/
  - https://pandera.readthedocs.io/en/stable/lazy_validation.html
"""

from __future__ import annotations

import pandera.pandas as pa  # type: ignore[import-untyped]
from sdfb_core.contracts.schema import FieldSchema, TableSchema

# BQ type → pandera dtype string. STRUCT and REPEATED are handled via
# object-typed columns; deep validation of nested structure happens at
# the per-record Pydantic line (line 1 of defense).
_PANDERA_DTYPE: dict[str, str] = {
    "STRING": "string",
    "BYTES": "object",
    "INTEGER": "Int64",
    "INT64": "Int64",
    "FLOAT": "Float64",
    "FLOAT64": "Float64",
    "NUMERIC": "object",  # Decimal — pandas has no native Decimal dtype
    "BIGNUMERIC": "object",
    "BOOLEAN": "boolean",
    "BOOL": "boolean",
    "DATE": "datetime64[ns]",
    "DATETIME": "datetime64[ns]",
    "TIME": "object",
    "TIMESTAMP": "datetime64[ns, UTC]",
    "JSON": "object",
    "GEOGRAPHY": "string",
}


def derive_pandera_schema(table_schema: TableSchema) -> pa.DataFrameSchema:
  """Build a Pandera DataFrameSchema for per-batch validation."""
  columns: dict[str, pa.Column] = {}
  for col in table_schema.columns:
    if col.is_struct or col.is_repeated:
      # Nested / repeated cells are stored as object; per-record
      # Pydantic enforces the inner structure.
      columns[col.name] = pa.Column(
          dtype="object",
          nullable=col.is_nullable,
          required=True,
          description=col.description or None,
      )
      continue

    columns[col.name] = pa.Column(
        dtype=_PANDERA_DTYPE.get(col.bq_type, "object"),
        nullable=col.is_nullable,
        required=True,
        checks=_build_checks(col),
        description=col.description or None,
    )

  unique_cols = list(
      table_schema.primary_keys) if table_schema.primary_keys else None

  return pa.DataFrameSchema(
      columns=columns,
      unique=unique_cols,
      strict=True,
      ordered=False,
      # Coerce dtypes during validation — pandas 2.x can hand us mixed
      # datetime resolutions (`us` vs `ns`) depending on how the column
      # was constructed; coercion normalizes those without surprising
      # the caller. Type-incompatible coercions still raise.
      coerce=True,
  )


def _build_checks(col: FieldSchema) -> list[pa.Check]:
  checks: list[pa.Check] = []
  if col.bq_type == "STRING" and col.max_length is not None:
    checks.append(pa.Check.str_length(max_value=col.max_length))
  return checks
