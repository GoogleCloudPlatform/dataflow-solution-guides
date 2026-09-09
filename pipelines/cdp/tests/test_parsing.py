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
import unittest

import apache_beam as beam
from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.util import assert_that, equal_to

from cdp_pipeline.models import (
    CustomerInteractionEvent,
    EventType,
)
from cdp_pipeline.parsing import (
    AssignEventTimestampDoFn,
    ParseRecordDoFn,
    TAG_DEADLETTER,
)

# Prevent pytest from treating Apache Beam's TestPipeline as a test case
TestPipeline.__test__ = False


class ParsingTest(unittest.TestCase):
  """Unit tests for ParseRecordDoFn and AssignEventTimestampDoFn."""

  def test_parse_record_valid_transaction(self):
    raw = json.dumps({
        "household_key": "100",
        "transaction_id": "tx-1",
        "product_id": "prod-1",
        "sales_value": 15.50
    }).encode("utf-8")

    with TestPipeline() as p:
      results = (
          p
          | beam.Create([raw])
          | beam.ParDo(ParseRecordDoFn(EventType.TRANSACTION)).with_outputs(
              TAG_DEADLETTER, main="valid"))

      def check_valid(elements):
        assert len(elements) == 1
        hh_key, event = elements[0]
        assert hh_key == "100"
        assert event.transaction_id == "tx-1"
        assert event.event_type == EventType.TRANSACTION.value
        assert event.transaction is not None
        assert event.transaction.product_id == "prod-1"
        assert event.transaction.sales_value == 15.50
        assert event.coupon is None

      assert_that(results.valid, check_valid)
      assert_that(results[TAG_DEADLETTER], equal_to([]))

  def test_parse_record_valid_coupon(self):
    raw = json.dumps({
        "household_key": "100",
        "transaction_id": "tx-1",
        "coupon_upc": "cp-1",
        "campaign": "camp-99"
    })

    with TestPipeline() as p:
      results = (
          p
          | beam.Create([raw])
          | beam.ParDo(ParseRecordDoFn(EventType.COUPON)).with_outputs(
              TAG_DEADLETTER, main="valid"))

      def check_valid(elements):
        assert len(elements) == 1
        hh_key, event = elements[0]
        assert hh_key == "100"
        assert event.transaction_id == "tx-1"
        assert event.event_type == EventType.COUPON.value
        assert event.coupon is not None
        assert event.coupon.coupon_upc == "cp-1"
        assert event.coupon.campaign == "camp-99"
        assert event.transaction is None

      assert_that(results.valid, check_valid)
      assert_that(results[TAG_DEADLETTER], equal_to([]))

  def test_parse_record_string_event_type(self):
    fn = ParseRecordDoFn("transaction")
    self.assertEqual(fn.record_type, EventType.TRANSACTION)

  def test_parse_record_malformed_json_dlq(self):
    bad_bytes = b"BROKEN_JSON_DATA{{{"

    with TestPipeline() as p:
      results = (
          p
          | beam.Create([bad_bytes])
          | beam.ParDo(ParseRecordDoFn("transaction")).with_outputs(
              TAG_DEADLETTER, main="valid"))

      def check_dlq(elements):
        assert len(elements) == 1
        assert "Malformed payload" in elements[0]["error_message"]

      assert_that(results.valid, equal_to([]))
      assert_that(results[TAG_DEADLETTER], check_dlq)

  def test_parse_record_missing_keys_dlq(self):
    missing_key = json.dumps({"transaction_id": "tx-1"}).encode("utf-8")

    with TestPipeline() as p:
      results = (
          p
          | beam.Create([missing_key])
          | beam.ParDo(ParseRecordDoFn("transaction")).with_outputs(
              TAG_DEADLETTER, main="valid"))

      def check_dlq(elements):
        assert len(elements) == 1
        assert "Missing required household_key" in elements[0]["error_message"]

      assert_that(results.valid, equal_to([]))
      assert_that(results[TAG_DEADLETTER], check_dlq)

  def test_assign_event_timestamp_with_iso_string(self):
    iso_ts = "2026-09-08T10:00:00Z"
    event = CustomerInteractionEvent(
        event_type=EventType.TRANSACTION.value,
        household_key="hh-1",
        transaction_id="tx-1",
        event_timestamp=iso_ts,
    )
    expected_seconds = datetime.fromisoformat(
        "2026-09-08T10:00:00+00:00").timestamp()

    with TestPipeline() as p:
      output = (
          p
          | beam.Create([("hh-1", event)])
          | beam.ParDo(AssignEventTimestampDoFn())
          | beam.Map(lambda el, ts=beam.DoFn.TimestampParam: float(ts.micros) /
                     1000000.0))

      assert_that(output, equal_to([expected_seconds]))

  def test_assign_event_timestamp_fallback_to_utc_now(self):
    event = CustomerInteractionEvent(
        event_type=EventType.TRANSACTION.value,
        household_key="hh-1",
        transaction_id="tx-1",
        event_timestamp="INVALID_DATE_STRING",
    )
    before_ts = datetime.now(timezone.utc).timestamp()

    with TestPipeline() as p:
      output = (
          p
          | beam.Create([("hh-1", event)])
          | beam.ParDo(AssignEventTimestampDoFn())
          | beam.Map(lambda el, ts=beam.DoFn.TimestampParam: float(ts.micros) /
                     1000000.0))

      def check_fallback(elements):
        assert len(elements) == 1
        assert elements[0] >= before_ts

      assert_that(output, check_fallback)


if __name__ == "__main__":
  unittest.main()
