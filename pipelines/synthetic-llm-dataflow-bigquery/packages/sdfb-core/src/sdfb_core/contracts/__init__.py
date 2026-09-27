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
