#  Copyright 2026 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
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
