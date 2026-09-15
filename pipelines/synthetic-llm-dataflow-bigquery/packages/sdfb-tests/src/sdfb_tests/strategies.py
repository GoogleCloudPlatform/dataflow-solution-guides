"""Hypothesis strategies for synthetic-data property tests.

`record_strategy(schema)` produces a hypothesis strategy that generates
record dicts conforming to a `TableSchema`. Used by property tests like
'every hypothesis-generated record passes the derived Pydantic model' —
the strongest available evidence that the Pydantic and the schema-driven
generation are mutually consistent.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from hypothesis import strategies as st
from sdfb_core.contracts.schema import FieldSchema, TableSchema

_MIN_DT = datetime(2000, 1, 1, tzinfo=UTC)
_MAX_DT = datetime(2099, 12, 31, tzinfo=UTC)


def _decimal_strategy(precision: int, scale: int) -> st.SearchStrategy[Decimal]:
  integer_digits = max(0, precision - scale)
  upper = Decimal(10)**integer_digits - Decimal(1).scaleb(-scale)
  lower = -upper
  return st.decimals(
      min_value=lower,
      max_value=upper,
      places=scale,
      allow_nan=False,
      allow_infinity=False,
  )


def _scalar_strategy(  # noqa: PLR0911 — type dispatch; sequential returns read clearer than nesting
    field: FieldSchema) -> st.SearchStrategy:
  """Strategy for a single non-repeated, non-struct field."""
  t = field.bq_type
  if t == "STRING":
    max_len = field.max_length if field.max_length else 50
    return st.text(min_size=1, max_size=max_len)
  if t in {"BYTES"}:
    return st.binary(min_size=0, max_size=32)
  if t in {"INT64", "INTEGER"}:
    return st.integers(min_value=-(2**31), max_value=2**31 - 1)
  if t in {"FLOAT64", "FLOAT"}:
    return st.floats(
        min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False)
  if t in {"NUMERIC", "BIGNUMERIC"}:
    return _decimal_strategy(field.precision or 18, field.scale or 2)
  if t in {"BOOL", "BOOLEAN"}:
    return st.booleans()
  if t == "DATE":
    return st.dates(min_value=date(2000, 1, 1), max_value=date(2099, 12, 31))
  if t in {"DATETIME", "TIMESTAMP"}:
    return st.datetimes(
        min_value=_MIN_DT.replace(tzinfo=None),
        max_value=_MAX_DT.replace(tzinfo=None),
        timezones=st.just(UTC))
  if t == "TIME":
    return st.times()
  if t == "JSON":
    return st.dictionaries(
        st.text(max_size=10), st.text(max_size=10), max_size=5)
  if t == "GEOGRAPHY":
    return st.just("POINT(0 0)")  # minimal WKT
  raise ValueError(f"Unsupported BQ type for hypothesis strategy: {t}")


def _field_strategy(field: FieldSchema) -> st.SearchStrategy:
  """Strategy for a `FieldSchema`, respecting mode (NULLABLE / REPEATED)."""
  base = (
      _struct_strategy(field.fields or [])
      if field.is_struct else _scalar_strategy(field))

  if field.is_repeated:
    return st.lists(base, min_size=0, max_size=4)
  if field.is_nullable:
    return st.one_of(st.none(), base)
  return base


def _struct_strategy(columns: list[FieldSchema]) -> st.SearchStrategy[dict]:
  return st.fixed_dictionaries({c.name: _field_strategy(c) for c in columns})


def record_strategy(schema: TableSchema) -> st.SearchStrategy[dict]:
  """Build a hypothesis strategy that generates valid records for `schema`."""
  return _struct_strategy(schema.columns)
