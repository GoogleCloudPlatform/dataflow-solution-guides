"""Parsed `_ddl.json` representation — the source of truth.

The bigquery_ddl_metadata.py extractor emits a JSON dict with a top-level
`schema` (list of column dicts) plus table-level metadata. This module
defines the Pydantic models that deserialize that dict, with two input
dialects accepted on the type key:

  - canonical BQ JSON schema (`"type": "STRING"`)
  - the existing extractor output (`"field_type": "STRING"`)

Both deserialize to the Python attribute `bq_type`; canonical output
serialization uses `"type"`.

REF: https://docs.cloud.google.com/bigquery/docs/schemas#creating_a_JSON_schema_file
REF: https://docs.cloud.google.com/bigquery/docs/primary-foreign-keys
"""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

BQMode = Literal["REQUIRED", "NULLABLE", "REPEATED"]

BQType = Literal[
    "STRING",
    "BYTES",
    "INTEGER",
    "INT64",
    "FLOAT",
    "FLOAT64",
    "NUMERIC",
    "BIGNUMERIC",
    "BOOLEAN",
    "BOOL",
    "DATE",
    "DATETIME",
    "TIME",
    "TIMESTAMP",
    "JSON",
    "RECORD",
    "STRUCT",
    "GEOGRAPHY",
]


class FieldSchema(BaseModel):
  """A single column / nested-field definition from a BigQuery DDL."""

  model_config = ConfigDict(
      populate_by_name=True,
      extra="ignore",
      frozen=True,
  )

  name: str
  bq_type: BQType = Field(
      validation_alias=AliasChoices("type", "field_type", "bq_type"),
      serialization_alias="type",
  )
  mode: BQMode = "NULLABLE"
  description: str = ""

  # STRUCT / RECORD only — nested fields.
  fields: list[FieldSchema] | None = None

  # Per-type constraints from the BQ JSON schema spec.
  max_length: int | None = Field(
      default=None,
      validation_alias=AliasChoices("max_length", "maxLength"),
      serialization_alias="maxLength",
  )
  precision: int | None = None
  scale: int | None = None
  default_value_expression: str | None = Field(
      default=None,
      validation_alias=AliasChoices("default_value_expression",
                                    "defaultValueExpression"),
      serialization_alias="defaultValueExpression",
  )

  @model_validator(mode="after")
  def _nested_fields_only_on_struct(self) -> FieldSchema:
    is_struct = self.bq_type in {"RECORD", "STRUCT"}
    has_nested = self.fields is not None and len(self.fields) > 0
    if has_nested and not is_struct:
      raise ValueError(f"Field '{self.name}' has nested 'fields' but type "
                       f"'{self.bq_type}' is not RECORD/STRUCT.")
    if is_struct and not has_nested:
      raise ValueError(
          f"Field '{self.name}' has type '{self.bq_type}' but no nested 'fields'."
      )
    return self

  @property
  def is_repeated(self) -> bool:
    return self.mode == "REPEATED"

  @property
  def is_nullable(self) -> bool:
    return self.mode == "NULLABLE"

  @property
  def is_struct(self) -> bool:
    return self.bq_type in {"RECORD", "STRUCT"}


class TableInfo(BaseModel):
  """Top-level metadata from `table_info` in `_ddl.json`."""

  model_config = ConfigDict(extra="allow", frozen=True)

  table_id: str
  created: str | None = None
  last_modified: str | None = None
  data_location: str | None = None
  description: str = ""
  table_type: str = "TABLE"


class Partitioning(BaseModel):
  model_config = ConfigDict(extra="allow", frozen=True)
  type: str
  field: str | None = None
  expiration_days: float | None = None
  require_partition_filter: bool = False


class Clustering(BaseModel):
  model_config = ConfigDict(frozen=True)
  fields: list[str]


class TableSchema(BaseModel):
  """Full parsed representation of a `_ddl.json` file."""

  model_config = ConfigDict(
      populate_by_name=True,
      extra="allow",
      frozen=True,
  )

  table_info: TableInfo
  columns: list[FieldSchema] = Field(
      validation_alias=AliasChoices("schema", "columns"),
      serialization_alias="schema",
  )
  primary_keys: list[str] | None = None
  partitioning: Partitioning | None = None
  clustering: Clustering | None = None

  @model_validator(mode="after")
  def _pks_must_reference_existing_columns(self) -> TableSchema:
    if not self.primary_keys:
      return self
    col_names = {c.name for c in self.columns}
    # `primary_keys` is a pydantic field narrowed by the early return above.
    unknown = [pk for pk in self.primary_keys if pk not in col_names]  # pylint: disable=not-an-iterable
    if unknown:
      raise ValueError(f"primary_keys reference unknown columns: {unknown}")
    return self

  @property
  def fqn(self) -> str:
    """Fully-qualified table name (`project.dataset.table`)."""
    return self.table_info.table_id
