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
"""Unit tests for BigQuery sinks and Storage Write API row formatting."""

from datetime import datetime, timezone
import unittest

import apache_beam as beam
from apache_beam.utils.timestamp import Timestamp

from cdp_pipeline.models import (
    CustomerSessionProfile,
    DeadLetterRecord,
    UnifiedTransactionRecord,
)
from cdp_pipeline.options import MyPipelineOptions
from cdp_pipeline.sinks import (
    _format_deadletter_dict,
    _format_session_dict,
    _format_unified_dict,
    _to_beam_timestamp,
    apply_bigquery_sinks,
)


class SinksTest(unittest.TestCase):
  """Unit tests for BigQuery row formatters and sink attachments."""

  def test_to_beam_timestamp(self):
    self.assertIsNone(_to_beam_timestamp(None))

    beam_ts = Timestamp.of(12345.67)
    self.assertEqual(_to_beam_timestamp(beam_ts), beam_ts)

    numeric_ts = 1700000000
    res = _to_beam_timestamp(numeric_ts)
    self.assertIsInstance(res, Timestamp)
    self.assertEqual(res, Timestamp.of(1700000000.0))

    dt = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
    res_dt = _to_beam_timestamp(dt)
    self.assertIsInstance(res_dt, Timestamp)
    self.assertEqual(res_dt, Timestamp.of(dt.timestamp()))

    iso_str = "2026-09-08T12:00:00Z"
    res_iso = _to_beam_timestamp(iso_str)
    self.assertIsInstance(res_iso, Timestamp)

    invalid_str = "not-a-timestamp"
    res_invalid = _to_beam_timestamp(invalid_str)
    self.assertIsInstance(res_invalid, Timestamp)

  def test_format_unified_dict_storage_api(self):
    record = UnifiedTransactionRecord(
        session_id="sess_1",
        transaction_id="tx_1",
        household_key="hh_1",
        product_id="prod_1",
        quantity=1,
        sales_value=10.0,
        store_id="store_1",
        retail_disc=0.0,
        coupon_discount=0.0,
        coupon_match_disc=0.0,
        coupon_upc=None,
        campaign=None,
        day=1,
        trans_time="1000",
        week_no=1,
        event_timestamp="2026-09-08T10:00:00Z",
        processed_timestamp="2026-09-08T10:05:00Z",
    )

    formatted_storage = _format_unified_dict(record, use_storage_api=True)
    self.assertIsInstance(formatted_storage["event_timestamp"], Timestamp)
    self.assertIsInstance(formatted_storage["processed_timestamp"], Timestamp)
    self.assertEqual(formatted_storage["transaction_id"], "tx_1")

    formatted_raw = _format_unified_dict(record, use_storage_api=False)
    self.assertEqual(formatted_raw["event_timestamp"], "2026-09-08T10:00:00Z")
    self.assertEqual(formatted_raw["processed_timestamp"],
                     "2026-09-08T10:05:00Z")

  def test_format_session_dict_storage_api(self):
    record = CustomerSessionProfile(
        session_id="sess_1",
        household_key="hh_1",
        session_start="2026-09-08T10:00:00Z",
        session_end="2026-09-08T10:15:00Z",
        session_duration_sec=900,
        total_transactions=1,
        total_items_purchased=1,
        total_spend=10.0,
        total_discount=0.0,
        coupons_redeemed_count=0,
        distinct_products_count=1,
        campaigns=[],
        stores_visited=["store_1"],
        processed_timestamp="2026-09-08T10:15:05Z",
    )

    formatted_storage = _format_session_dict(record, use_storage_api=True)
    self.assertIsInstance(formatted_storage["session_start"], Timestamp)
    self.assertIsInstance(formatted_storage["session_end"], Timestamp)
    self.assertIsInstance(formatted_storage["processed_timestamp"], Timestamp)

    formatted_raw = _format_session_dict(record, use_storage_api=False)
    self.assertEqual(formatted_raw["session_start"], "2026-09-08T10:00:00Z")
    self.assertEqual(formatted_raw["session_end"], "2026-09-08T10:15:00Z")

  def test_format_deadletter_dict_storage_api(self):
    record = DeadLetterRecord(
        source="transaction",
        raw_payload='{"bad": "data"}',
        error_message="corrupt",
        timestamp="2026-09-08T10:00:00Z",
    )

    formatted_storage = _format_deadletter_dict(record, use_storage_api=True)
    self.assertIsInstance(formatted_storage["timestamp"], Timestamp)
    self.assertEqual(formatted_storage["source"], "transaction")

    formatted_raw = _format_deadletter_dict(record, use_storage_api=False)
    self.assertEqual(formatted_raw["timestamp"], "2026-09-08T10:00:00Z")

  def test_apply_bigquery_sinks_no_project_or_dataset(self):
    p = beam.Pipeline()
    pcol = p | "Create" >> beam.Create([])
    options_no_project = MyPipelineOptions([])
    # Should safely return without error
    apply_bigquery_sinks(pcol, pcol, pcol, options_no_project)

  def test_apply_bigquery_sinks_graph_construction(self):
    options = MyPipelineOptions([
        "--project=test-project",
        "--output_dataset=test_dataset",
        "--output_table=test_unified",
        "--output_sessions_table=test_sessions",
        "--deadletter_table=test_dlq",
        "--streaming",
    ])
    p = beam.Pipeline(options=options)
    dummy_unified = p | "Create Unified" >> beam.Create([])
    dummy_sessions = p | "Create Sessions" >> beam.Create([])
    dummy_deadletters = p | "Create DLQ" >> beam.Create([])

    apply_bigquery_sinks(
        unified_records=dummy_unified,
        customer_sessions=dummy_sessions,
        all_deadletters=dummy_deadletters,
        pipeline_options=options,
    )


if __name__ == "__main__":
  unittest.main()
