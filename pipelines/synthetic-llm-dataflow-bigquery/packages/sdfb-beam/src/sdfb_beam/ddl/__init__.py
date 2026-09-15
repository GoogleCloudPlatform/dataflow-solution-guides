"""DDL metadata extraction for BigQuery tables.

Public surface:
  - `extract_ddl_metadata(project, dataset, table, ...)` — pure function
    returning a dict directly consumable by `TableSchema.model_validate`.
  - `build_pipeline(...)` + DoFns — Beam wrappers for jobified extraction
    (preferred when output goes to GCS).
  - `cli.main()` — argparse entry point invoked from `scripts/extract_ddl.py`.

REF: https://docs.cloud.google.com/bigquery/docs/schemas#creating_a_JSON_schema_file
REF: https://docs.cloud.google.com/bigquery/docs/primary-foreign-keys
"""

from sdfb_beam.ddl.extractor import extract_ddl_metadata, extract_table_schema
from sdfb_beam.ddl.pipeline import (
    ExtractDDLMetadataDoFn,
    WriteDDLToJSON,
    build_pipeline,
    get_output_path,
)

__all__ = [
    "ExtractDDLMetadataDoFn",
    "WriteDDLToJSON",
    "build_pipeline",
    "extract_ddl_metadata",
    "extract_table_schema",
    "get_output_path",
]
