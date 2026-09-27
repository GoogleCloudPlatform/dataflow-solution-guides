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
"""BigQuery ↔ Python type maps.

`RECORD`/`STRUCT` and `ARRAY` (`mode="REPEATED"`) are handled out-of-band
in the codegen modules — they need recursion or wrapping.

REF: https://docs.cloud.google.com/bigquery/docs/schemas#creating_a_JSON_schema_file
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

BQ_TO_PYTHON: dict[str, type] = {
    "STRING": str,
    "BYTES": bytes,
    "INTEGER": int,
    "INT64": int,
    "FLOAT": float,
    "FLOAT64": float,
    "NUMERIC": Decimal,
    "BIGNUMERIC": Decimal,
    "BOOLEAN": bool,
    "BOOL": bool,
    "DATE": date,
    "DATETIME": datetime,
    "TIME": time,
    "TIMESTAMP": datetime,
    "JSON": dict,
    "GEOGRAPHY": str,
}

# One-way reverse: ambiguous Python types pick the wider BQ variant.
PYTHON_TO_BQ: dict[type, str] = {
    str: "STRING",
    bytes: "BYTES",
    int: "INT64",
    float: "FLOAT64",
    Decimal: "NUMERIC",
    bool: "BOOL",
    date: "DATE",
    datetime: "TIMESTAMP",
    time: "TIME",
    dict: "JSON",
}
