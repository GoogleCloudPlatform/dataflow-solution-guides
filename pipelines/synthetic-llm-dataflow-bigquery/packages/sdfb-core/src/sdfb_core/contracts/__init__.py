"""Pydantic contracts for synthetic-dataflow-bigquery.

`TableSchema` is the parsed `_ddl.json` representation — the single source
of truth from which `GeneratedRecord` (Pydantic), the Pandera schema, and
the BigQuery TableSchema dict are all derived.

REF: https://docs.cloud.google.com/bigquery/docs/schemas#creating_a_JSON_schema_file
REF: https://docs.cloud.google.com/bigquery/docs/primary-foreign-keys
"""

from sdfb_core.contracts.record import GeneratedRecord
from sdfb_core.contracts.schema import (
    BQMode,
    BQType,
    Clustering,
    FieldSchema,
    Partitioning,
    TableInfo,
    TableSchema,
)

__all__ = [
    "BQMode",
    "BQType",
    "Clustering",
    "FieldSchema",
    "GeneratedRecord",
    "Partitioning",
    "TableInfo",
    "TableSchema",
]
