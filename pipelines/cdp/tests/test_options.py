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
"""Unit tests for Customer Data Platform pipeline options."""

import unittest

from apache_beam.options.pipeline_options import GoogleCloudOptions

from cdp_pipeline.options import MyPipelineOptions


class OptionsTest(unittest.TestCase):
  """Unit tests for MyPipelineOptions CLI argument parsing and defaults."""

  def test_pipeline_options_defaults(self):
    options = MyPipelineOptions([])
    self.assertIsNone(options.transactions_topic)
    self.assertIsNone(options.transactions_subscription)
    self.assertIsNone(options.coupons_redemption_topic)
    self.assertIsNone(options.coupons_redemption_subscription)
    self.assertEqual(options.output_dataset, "cdp_dataset")
    self.assertEqual(options.output_table, "unified_customer_data")
    self.assertEqual(options.output_sessions_table, "customer_sessions")
    self.assertIsNone(options.deadletter_table)
    self.assertEqual(options.session_gap_seconds, 900)
    self.assertEqual(options.allowed_lateness_seconds, 60)
    self.assertTrue(options.use_storage_write_api)
    self.assertIsNone(options.output_schema_path)
    self.assertIsNone(options.output_sessions_schema_path)
    self.assertIsNone(options.deadletter_schema_path)

  def test_pipeline_options_project(self):
    options = MyPipelineOptions(["--project=my-test-project"])
    gcp_options = options.view_as(GoogleCloudOptions)
    self.assertEqual(gcp_options.project, "my-test-project")

  def test_pipeline_options_custom_flags(self):
    flags = [
        "--transactions_topic=projects/p/topics/tx",
        "--transactions_subscription=projects/p/subscriptions/tx-sub",
        "--coupons_redemption_topic=projects/p/topics/cp",
        "--coupons_redemption_subscription=projects/p/subscriptions/cp-sub",
        "--output_dataset=custom_dataset",
        "--output_table=custom_unified",
        "--output_sessions_table=custom_sessions",
        "--deadletter_table=custom_dlq",
        "--session_gap_seconds=300",
        "--allowed_lateness_seconds=30",
        "--output_schema_path=/path/to/unified.json",
        "--output_sessions_schema_path=/path/to/sessions.json",
        "--deadletter_schema_path=/path/to/dlq.json",
    ]
    options = MyPipelineOptions(flags)
    self.assertEqual(options.transactions_topic, "projects/p/topics/tx")
    self.assertEqual(options.transactions_subscription,
                     "projects/p/subscriptions/tx-sub")
    self.assertEqual(options.coupons_redemption_topic, "projects/p/topics/cp")
    self.assertEqual(options.coupons_redemption_subscription,
                     "projects/p/subscriptions/cp-sub")
    self.assertEqual(options.output_dataset, "custom_dataset")
    self.assertEqual(options.output_table, "custom_unified")
    self.assertEqual(options.output_sessions_table, "custom_sessions")
    self.assertEqual(options.deadletter_table, "custom_dlq")
    self.assertEqual(options.session_gap_seconds, 300)
    self.assertEqual(options.allowed_lateness_seconds, 30)
    self.assertEqual(options.output_schema_path, "/path/to/unified.json")
    self.assertEqual(options.output_sessions_schema_path,
                     "/path/to/sessions.json")
    self.assertEqual(options.deadletter_schema_path, "/path/to/dlq.json")


if __name__ == "__main__":
  unittest.main()
