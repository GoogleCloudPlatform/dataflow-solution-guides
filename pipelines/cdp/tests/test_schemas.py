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
"""Unit tests for BigQuery table schema loading."""

import json
import tempfile
import unittest

from cdp_pipeline.schemas import load_output_schema


class SchemasTest(unittest.TestCase):
  """Unit tests for load_output_schema with packaged and custom schemas."""

  def test_load_output_schema_default(self):
    schema = load_output_schema()
    self.assertIn("fields", schema)
    field_names = [field["name"] for field in schema["fields"]]
    self.assertIn("transaction_id", field_names)
    self.assertIn("household_key", field_names)
    self.assertIn("coupon_upc", field_names)
    self.assertIn("product_id", field_names)
    self.assertIn("coupon_discount", field_names)
    self.assertIn("session_id", field_names)
    self.assertIn("campaign", field_names)
    self.assertIn("sales_value", field_names)
    self.assertIn("quantity", field_names)
    self.assertIn("processed_timestamp", field_names)

  def test_load_schemas_helpers(self):
    sessions_schema = load_output_schema(None, "customer_sessions.json")
    self.assertIn("fields", sessions_schema)
    session_fields = [f["name"] for f in sessions_schema["fields"]]
    self.assertIn("session_id", session_fields)
    self.assertIn("household_key", session_fields)
    self.assertIn("total_spend", session_fields)
    self.assertIn("total_transactions", session_fields)
    self.assertIn("total_items_purchased", session_fields)
    self.assertIn("coupons_redeemed_count", session_fields)

    dlq_schema = load_output_schema(None, "deadletter_table.json")
    self.assertIn("fields", dlq_schema)
    dlq_fields = [f["name"] for f in dlq_schema["fields"]]
    self.assertIn("source", dlq_fields)
    self.assertIn("error_message", dlq_fields)
    self.assertIn("raw_payload", dlq_fields)
    self.assertIn("timestamp", dlq_fields)

  def test_load_output_schema_non_existent(self):
    with self.assertRaises(FileNotFoundError):
      load_output_schema(None, "non_existent_schema.json")

  def test_load_output_schema_custom_path(self):
    custom_content = {
        "fields": [{
            "name": "custom_field",
            "type": "STRING",
            "mode": "REQUIRED"
        }]
    }
    with tempfile.NamedTemporaryFile(
        "w+", encoding="utf-8", delete=True) as temp_file:
      json.dump(custom_content, temp_file)
      temp_file.flush()
      loaded = load_output_schema(temp_file.name)
      self.assertEqual(loaded, custom_content)


if __name__ == "__main__":
  unittest.main()
