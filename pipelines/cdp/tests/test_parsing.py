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
"""Unit tests for record parsing, validation, and timestamp assignment transforms."""

from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest

import apache_beam as beam
from apache_beam.transforms.window import TimestampedValue

from cdp_pipeline.models import (
    CustomerInteractionEvent,
    EventType,
)
from cdp_pipeline.parsing import (
    AssignEventTimestampDoFn,
    ParseRecordDoFn,
    TAG_DEADLETTER,
)


class ParsingTest(unittest.TestCase):
  """Unit tests for ParseRecordDoFn and AssignEventTimestampDoFn."""

  def test_parse_record_valid_transaction(self):
    fn = ParseRecordDoFn(EventType.TRANSACTION)
    fn.setup()
    raw = json.dumps({
        "household_key": "100",
        "transaction_id": "tx-1",
        "product_id": "prod-1",
        "sales_value": 15.50
    }).encode("utf-8")

    results = list(fn.process(raw))
    self.assertEqual(len(results), 1)
    hh_key, event = results[0]
    self.assertEqual(hh_key, "100")
    self.assertIsInstance(event, CustomerInteractionEvent)
    self.assertEqual(event.transaction_id, "tx-1")
    self.assertEqual(event.event_type, EventType.TRANSACTION.value)
    self.assertIsNotNone(event.transaction)
    self.assertEqual(event.transaction.product_id, "prod-1")
    self.assertEqual(event.transaction.sales_value, 15.50)
    self.assertIsNone(event.coupon)

  def test_parse_record_valid_coupon(self):
    fn = ParseRecordDoFn(EventType.COUPON)
    fn.setup()
    raw = json.dumps({
        "household_key": "100",
        "transaction_id": "tx-1",
        "coupon_upc": "cp-1",
        "campaign": "camp-99"
    })

    results = list(fn.process(raw))
    self.assertEqual(len(results), 1)
    hh_key, event = results[0]
    self.assertEqual(hh_key, "100")
    self.assertIsInstance(event, CustomerInteractionEvent)
    self.assertEqual(event.transaction_id, "tx-1")
    self.assertEqual(event.event_type, EventType.COUPON.value)
    self.assertIsNotNone(event.coupon)
    self.assertEqual(event.coupon.coupon_upc, "cp-1")
    self.assertEqual(event.coupon.campaign, "camp-99")
    self.assertIsNone(event.transaction)

  def test_parse_record_string_event_type(self):
    fn = ParseRecordDoFn("transaction")
    fn.setup()
    self.assertEqual(fn.record_type, EventType.TRANSACTION)

  def test_parse_record_malformed_json_dlq(self):
    fn = ParseRecordDoFn("transaction")
    fn.setup()
    bad_bytes = b"BROKEN_JSON_DATA{{{"

    results = list(fn.process(bad_bytes))
    self.assertEqual(len(results), 1)
    tagged_output = results[0]
    self.assertIsInstance(tagged_output, beam.pvalue.TaggedOutput)
    self.assertEqual(tagged_output.tag, TAG_DEADLETTER)
    self.assertIn("Malformed payload", tagged_output.value["error_message"])

  def test_parse_record_missing_keys_dlq(self):
    fn = ParseRecordDoFn("transaction")
    fn.setup()
    missing_key = json.dumps({"transaction_id": "tx-1"}).encode("utf-8")

    results = list(fn.process(missing_key))
    self.assertEqual(len(results), 1)
    tagged_output = results[0]
    self.assertIsInstance(tagged_output, beam.pvalue.TaggedOutput)
    self.assertEqual(tagged_output.tag, TAG_DEADLETTER)
    self.assertIn("Missing required household_key",
                  tagged_output.value["error_message"])

  def test_assign_event_timestamp_with_iso_string(self):
    fn = AssignEventTimestampDoFn()
    iso_ts = "2026-09-08T10:00:00Z"
    event = CustomerInteractionEvent(
        event_type=EventType.TRANSACTION.value,
        household_key="hh-1",
        transaction_id="tx-1",
        event_timestamp=iso_ts,
    )
    results = list(fn.process(("hh-1", event)))
    self.assertEqual(len(results), 1)
    timestamped = results[0]
    self.assertIsInstance(timestamped, TimestampedValue)
    expected_seconds = datetime.fromisoformat(
        "2026-09-08T10:00:00+00:00").timestamp()
    self.assertEqual(timestamped.timestamp, expected_seconds)
    self.assertEqual(timestamped.value, ("hh-1", event))

  def test_assign_event_timestamp_fallback_to_micros(self):
    fn = AssignEventTimestampDoFn()
    event = CustomerInteractionEvent(
        event_type=EventType.TRANSACTION.value,
        household_key="hh-1",
        transaction_id="tx-1",
        event_timestamp=None,
    )
    mock_timestamp = SimpleNamespace(micros=1700000000000000)
    results = list(fn.process(("hh-1", event), timestamp=mock_timestamp))
    self.assertEqual(len(results), 1)
    timestamped = results[0]
    self.assertEqual(timestamped.timestamp, 1700000000.0)

  def test_assign_event_timestamp_fallback_to_utc_now(self):
    fn = AssignEventTimestampDoFn()
    event = CustomerInteractionEvent(
        event_type=EventType.TRANSACTION.value,
        household_key="hh-1",
        transaction_id="tx-1",
        event_timestamp="INVALID_DATE_STRING",
    )
    mock_timestamp = SimpleNamespace(micros=-1)
    before_ts = datetime.now(timezone.utc).timestamp()
    results = list(fn.process(("hh-1", event), timestamp=mock_timestamp))
    after_ts = datetime.now(timezone.utc).timestamp()
    self.assertEqual(len(results), 1)
    timestamped = results[0]
    self.assertTrue(before_ts <= timestamped.timestamp <= after_ts)


if __name__ == "__main__":
  unittest.main()
