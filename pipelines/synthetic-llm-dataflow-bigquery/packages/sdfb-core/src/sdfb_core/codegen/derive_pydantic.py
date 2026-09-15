"""Derive a Pydantic record model from a `TableSchema`.

The generated model:
  - is a subclass of `GeneratedRecord` (inherits `extra='forbid'` etc.)
  - has one field per column, with the Python type mapped from the BQ type
  - enforces `REQUIRED` (no default), `NULLABLE` (default `None`), and
    `REPEATED` (`list[T]`, default `[]`) via Pydantic defaults
  - recurses into `STRUCT`/`RECORD` columns via nested `create_model` calls
  - enforces primary-key non-null at the model level via a dynamic base
    class carrying a `model_validator(mode="after")`

REF: https://docs.pydantic.dev/latest/concepts/models/#dynamic-model-creation
"""

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Self

from pydantic import Field, create_model, model_validator

from sdfb_core.codegen.types import BQ_TO_PYTHON
from sdfb_core.contracts.record import GeneratedRecord
from sdfb_core.contracts.schema import FieldSchema, TableSchema


def derive_record_model(
    table_schema: TableSchema,
    name: str | None = None,
) -> type[GeneratedRecord]:
  """Build a `GeneratedRecord` subclass mirroring the BigQuery schema."""
  model_name = name or f"Record_{_sanitize(table_schema.fqn)}"
  base = _make_pk_base(table_schema.primary_keys
                      ) if table_schema.primary_keys else GeneratedRecord

  field_defs: dict[str, tuple[Any, Any]] = {
      col.name: _field_definition(col, parent_name=model_name)
      for col in table_schema.columns
  }

  # mypy checks the **field_defs unpack against every keyword parameter
  # of create_model (__config__, __doc__, ...), so a dynamic-fields call
  # can never typecheck — pydantic's documented dynamic-model pattern.
  model: type[GeneratedRecord] = create_model(  # type: ignore[call-overload]
      model_name, __base__=base, **field_defs)
  model.__doc__ = (
      f"Dynamically-derived record model for `{table_schema.fqn}` — "
      f"{len(table_schema.columns)} columns, "
      f"PK={table_schema.primary_keys or 'none'}.")
  return model


def _field_definition(field: FieldSchema, parent_name: str) -> tuple[Any, Any]:
  """Return `(annotated_type, FieldInfo)` for `pydantic.create_model`."""
  base_type = _python_type(field, parent_name)
  description = field.description or None

  if field.is_repeated:
    return (
        list[base_type],  # type: ignore[valid-type]
        Field(default_factory=list, description=description),
    )

  if field.mode == "REQUIRED":
    return (base_type, Field(..., description=description))

  # NULLABLE — default None.
  return (base_type | None, Field(default=None, description=description))


def _python_type(field: FieldSchema, parent_name: str) -> Any:
  """Resolve the (non-mode-decorated) Python type for a column."""
  if field.is_struct:
    nested_name = f"{parent_name}_{field.name}"
    nested_defs = {
        sub.name: _field_definition(sub, parent_name=nested_name)
        for sub in (field.fields or [])
    }
    return create_model(  # type: ignore[call-overload]
        nested_name,
        __base__=GeneratedRecord,
        **nested_defs)

  py_type = BQ_TO_PYTHON.get(field.bq_type)
  if py_type is None:
    raise ValueError(
        f"Unsupported BQ type for field '{field.name}': {field.bq_type}")

  # STRING max_length constraint.
  if field.bq_type == "STRING" and field.max_length is not None:
    return Annotated[str, Field(max_length=field.max_length)]

  # NUMERIC / BIGNUMERIC precision + scale (None = constraint unset,
  # same as omitting the keyword).
  if field.bq_type in {
      "NUMERIC", "BIGNUMERIC"
  } and (field.precision is not None or field.scale is not None):
    return Annotated[
        Decimal,
        Field(max_digits=field.precision, decimal_places=field.scale),
    ]

  return py_type


def _make_pk_base(pk_columns: list[str]) -> type[GeneratedRecord]:
  """Build a dynamic base class enforcing non-null on PK columns.

    BQ allows PK columns to be `NULLABLE` even though the docs recommend
    `REQUIRED`. Validation here is defensive: it ensures PK columns are
    non-null whatever the schema mode says.
    """
  pks: tuple[str, ...] = tuple(pk_columns)

  class _PkValidatedRecord(GeneratedRecord):

    @model_validator(mode="after")
    def _enforce_pk_non_null(self) -> Self:
      missing = [c for c in pks if getattr(self, c, None) is None]
      if missing:
        raise ValueError(f"Primary-key column(s) must be non-null: {missing}")
      return self

  return _PkValidatedRecord


def _sanitize(name: str) -> str:
  """Coerce a string into a valid Python identifier."""
  return "".join(c if c.isalnum() else "_" for c in name)
