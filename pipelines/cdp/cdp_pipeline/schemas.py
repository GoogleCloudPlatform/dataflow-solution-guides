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
"""BigQuery table schemas and schema loader for Customer Data Platform."""

import json
import os
from typing import Any, Dict, Optional, Union

DEFAULT_OUTPUT_SCHEMA: Dict[str, Any] = {
    "fields": [
        {
            "name": "session_id",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "transaction_id",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "household_key",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "product_id",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "quantity",
            "type": "INTEGER",
            "mode": "NULLABLE"
        },
        {
            "name": "sales_value",
            "type": "FLOAT",
            "mode": "NULLABLE"
        },
        {
            "name": "store_id",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "retail_disc",
            "type": "FLOAT",
            "mode": "NULLABLE"
        },
        {
            "name": "coupon_discount",
            "type": "FLOAT",
            "mode": "NULLABLE"
        },
        {
            "name": "coupon_match_disc",
            "type": "FLOAT",
            "mode": "NULLABLE"
        },
        {
            "name": "coupon_upc",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "campaign",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "day",
            "type": "INTEGER",
            "mode": "NULLABLE"
        },
        {
            "name": "trans_time",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "week_no",
            "type": "INTEGER",
            "mode": "NULLABLE"
        },
        {
            "name": "event_timestamp",
            "type": "TIMESTAMP",
            "mode": "NULLABLE"
        },
        {
            "name": "processed_timestamp",
            "type": "TIMESTAMP",
            "mode": "REQUIRED"
        },
    ]
}

DEFAULT_SESSIONS_SCHEMA: Dict[str, Any] = {
    "fields": [
        {
            "name": "session_id",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "household_key",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "session_start",
            "type": "TIMESTAMP",
            "mode": "REQUIRED"
        },
        {
            "name": "session_end",
            "type": "TIMESTAMP",
            "mode": "REQUIRED"
        },
        {
            "name": "session_duration_sec",
            "type": "INTEGER",
            "mode": "REQUIRED"
        },
        {
            "name": "total_transactions",
            "type": "INTEGER",
            "mode": "REQUIRED"
        },
        {
            "name": "total_items_purchased",
            "type": "INTEGER",
            "mode": "REQUIRED"
        },
        {
            "name": "total_spend",
            "type": "FLOAT",
            "mode": "REQUIRED"
        },
        {
            "name": "total_discount",
            "type": "FLOAT",
            "mode": "REQUIRED"
        },
        {
            "name": "coupons_redeemed_count",
            "type": "INTEGER",
            "mode": "REQUIRED"
        },
        {
            "name": "distinct_products_count",
            "type": "INTEGER",
            "mode": "REQUIRED"
        },
        {
            "name": "campaigns",
            "type": "STRING",
            "mode": "REPEATED"
        },
        {
            "name": "stores_visited",
            "type": "STRING",
            "mode": "REPEATED"
        },
        {
            "name": "processed_timestamp",
            "type": "TIMESTAMP",
            "mode": "REQUIRED"
        },
    ]
}

DEFAULT_DEADLETTER_SCHEMA: Dict[str, Any] = {
    "fields": [
        {
            "name": "source",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "raw_payload",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "error_message",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "timestamp",
            "type": "TIMESTAMP",
            "mode": "REQUIRED"
        },
    ]
}


def load_output_schema(
    schema_path: Optional[str] = None,
    default_filename: str = "unified_table.json",
    fallback_schema: Optional[Dict[str, Any]] = None,
) -> Union[Dict[str, Any], str]:
  """Loads a BigQuery schema from a custom path, packaged file, or fallback dict."""
  if fallback_schema is None:
    fallback_schema = DEFAULT_OUTPUT_SCHEMA

  if schema_path:
    with open(schema_path, encoding="utf-8") as schema_file:
      return json.load(schema_file)

  # Check package schema directory
  default_schema_file = os.path.join(
      os.path.dirname(os.path.dirname(__file__)), "schema", default_filename)
  if os.path.exists(default_schema_file):
    with open(default_schema_file, encoding="utf-8") as schema_file:
      return json.load(schema_file)

  return fallback_schema
