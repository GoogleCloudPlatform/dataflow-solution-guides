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
"""Unit tests for Customer Data Platform pipeline transformations and sessionization."""

import json
import unittest

import apache_beam as beam
from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.util import assert_that, equal_to
from apache_beam.transforms.window import IntervalWindow
from apache_beam.utils.timestamp import Timestamp

from cdp_pipeline.customer_data_platform import (
    DEFAULT_DEADLETTER_SCHEMA,
    DEFAULT_SESSIONS_SCHEMA,
    TAG_DEADLETTER,
    TAG_SESSIONS,
    ParseRecordDoFn,
    ProcessCustomerSessionDoFn,
    build_pipeline,
    left_join,
    load_output_schema,
    _unify_data,
)
from cdp_pipeline.options import MyPipelineOptions

# Prevent pytest from treating Apache Beam's TestPipeline as a test case
TestPipeline.__test__ = False


class CustomerDataPlatformTest(unittest.TestCase):

  def test_left_join_with_matching_coupons(self):
    key = ("27601281299", "1")
    transactions = [{
        "transaction_id": "27601281299",
        "household_key": "1",
        "product_id": "941769",
        "coupon_disc": "0.50",
    }]
    coupons = [{
        "transaction_id": "27601281299",
        "household_key": "1",
        "coupon_upc": "10000085364",
        "campaign": "2200",
    }]

    results = list(left_join((key, (transactions, coupons))))
    self.assertEqual(len(results), 1)
    self.assertEqual(
        results[0],
        {
            "transaction_id": "27601281299",
            "household_key": "1",
            "coupon_upc": "10000085364",
            "product_id": "941769",
            "coupon_discount": "0.50",
        },
    )

  def test_left_join_without_matching_coupons(self):
    key = ("27601281299", "1")
    transactions = [{
        "transaction_id": "27601281299",
        "household_key": "1",
        "product_id": "941769",
        "coupon_disc": "0",
    }]
    coupons = []

    results = list(left_join((key, (transactions, coupons))))
    self.assertEqual(len(results), 1)
    self.assertEqual(
        results[0],
        {
            "transaction_id": "27601281299",
            "household_key": "1",
            "coupon_upc": None,
            "product_id": "941769",
            "coupon_discount": "0",
        },
    )

  def test_load_output_schema_default(self):
    schema = load_output_schema(None)
    self.assertIn("fields", schema)
    field_names = [field["name"] for field in schema["fields"]]
    self.assertIn("transaction_id", field_names)
    self.assertIn("household_key", field_names)
    self.assertIn("coupon_upc", field_names)
    self.assertIn("product_id", field_names)
    self.assertIn("coupon_discount", field_names)
    self.assertIn("session_id", field_names)
    self.assertIn("campaign", field_names)

  def test_load_schemas_helpers(self):
    sessions_schema = load_output_schema(None, "customer_sessions.json",
                                         DEFAULT_SESSIONS_SCHEMA)
    self.assertIn("fields", sessions_schema)
    session_fields = [f["name"] for f in sessions_schema["fields"]]
    self.assertIn("session_id", session_fields)
    self.assertIn("total_spend", session_fields)
    self.assertIn("total_transactions", session_fields)

    dlq_schema = load_output_schema(None, "deadletter_table.json",
                                    DEFAULT_DEADLETTER_SCHEMA)
    self.assertIn("fields", dlq_schema)
    dlq_fields = [f["name"] for f in dlq_schema["fields"]]
    self.assertIn("error_message", dlq_fields)
    self.assertIn("raw_payload", dlq_fields)

  def test_parse_record_valid_transaction(self):
    fn = ParseRecordDoFn("transaction")
    fn.setup()
    raw = json.dumps({
        "household_key": "100",
        "transaction_id": "tx-1",
        "product_id": "prod-1",
        "sales_value": 15.50
    }).encode("utf-8")

    results = list(fn.process(raw))
    self.assertEqual(len(results), 1)
    hh_key, data = results[0]
    self.assertEqual(hh_key, "100")
    self.assertEqual(data["transaction_id"], "tx-1")
    self.assertEqual(data["_record_type"], "transaction")

  def test_parse_record_valid_coupon(self):
    fn = ParseRecordDoFn("coupon")
    fn.setup()
    raw = json.dumps({
        "household_key": "100",
        "transaction_id": "tx-1",
        "coupon_upc": "cp-1",
        "campaign": "camp-99"
    })

    results = list(fn.process(raw))
    self.assertEqual(len(results), 1)
    hh_key, data = results[0]
    self.assertEqual(hh_key, "100")
    self.assertEqual(data["coupon_upc"], "cp-1")
    self.assertEqual(data["_record_type"], "coupon")

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
    # Missing household_key
    missing_key = json.dumps({"transaction_id": "tx-1"}).encode("utf-8")

    results = list(fn.process(missing_key))
    self.assertEqual(len(results), 1)
    tagged_output = results[0]
    self.assertIsInstance(tagged_output, beam.pvalue.TaggedOutput)
    self.assertEqual(tagged_output.tag, TAG_DEADLETTER)
    self.assertIn("Missing required household_key",
                  tagged_output.value["error_message"])

  def test_process_customer_session_aggregation(self):
    fn = ProcessCustomerSessionDoFn()
    fn.setup()

    household_key = "hh-42"
    mock_window = IntervalWindow(  # pylint: disable=too-many-function-args
        Timestamp(1000), Timestamp(1300))

    events = [
        {
            "_record_type": "transaction",
            "household_key": "hh-42",
            "transaction_id": "tx-101",
            "product_id": "prod-A",
            "quantity": 2,
            "sales_value": 20.0,
            "retail_disc": 2.0,
            "coupon_disc": 1.0,
            "store_id": "store-1",
        },
        {
            "_record_type": "transaction",
            "household_key": "hh-42",
            "transaction_id": "tx-102",
            "product_id": "prod-B",
            "quantity": 1,
            "sales_value": 10.0,
            "retail_disc": 0.0,
            "coupon_disc": 0.0,
            "store_id": "store-1",
        },
        {
            "_record_type": "coupon",
            "household_key": "hh-42",
            "transaction_id": "tx-101",
            "coupon_upc": "cp-999",
            "campaign": "fall-sale",
        },
    ]

    outputs = list(fn.process((household_key, events), window=mock_window))

    # Main outputs: unified transaction records
    unified = [
        item for item in outputs
        if not isinstance(item, beam.pvalue.TaggedOutput)
    ]
    # Tagged output: Customer 360 session profile
    sessions = [
        item.value for item in outputs if
        isinstance(item, beam.pvalue.TaggedOutput) and item.tag == TAG_SESSIONS
    ]

    # Two transaction items were emitted
    self.assertEqual(len(unified), 2)
    tx_101 = next(u for u in unified if u["transaction_id"] == "tx-101")
    self.assertEqual(tx_101["coupon_upc"], "cp-999")
    self.assertEqual(tx_101["campaign"], "fall-sale")
    self.assertEqual(tx_101["sales_value"], 20.0)

    tx_102 = next(u for u in unified if u["transaction_id"] == "tx-102")
    self.assertIsNone(tx_102["coupon_upc"])

    # Verify session summary
    self.assertEqual(len(sessions), 1)
    session = sessions[0]
    self.assertEqual(session["household_key"], "hh-42")
    self.assertEqual(session["total_transactions"], 2)
    self.assertEqual(session["total_items_purchased"], 3)
    self.assertEqual(session["total_spend"], 30.0)
    self.assertEqual(session["total_discount"], 3.0)
    self.assertEqual(session["coupons_redeemed_count"], 1)
    self.assertEqual(session["distinct_products_count"], 2)
    self.assertIn("fall-sale", session["campaigns"])

  def test_unify_data_transform(self):
    transactions_input = [
        (("t1", "h1"), {
            "transaction_id": "t1",
            "household_key": "h1",
            "product_id": "p1",
            "coupon_disc": "1.0",
        }),
        (("t2", "h2"), {
            "transaction_id": "t2",
            "household_key": "h2",
            "product_id": "p2",
            "coupon_disc": "0.0",
        }),
    ]
    coupons_input = [
        (("t1", "h1"), {
            "transaction_id": "t1",
            "household_key": "h1",
            "coupon_upc": "c1",
        }),
    ]

    expected = [
        {
            "transaction_id": "t1",
            "household_key": "h1",
            "coupon_upc": "c1",
            "product_id": "p1",
            "coupon_discount": "1.0",
        },
        {
            "transaction_id": "t2",
            "household_key": "h2",
            "coupon_upc": None,
            "product_id": "p2",
            "coupon_discount": "0.0",
        },
    ]

    with TestPipeline() as p:
      tx_pcoll = p | "Create Transactions" >> beam.Create(transactions_input)
      cp_pcoll = p | "Create Coupons" >> beam.Create(coupons_input)
      unified = (tx_pcoll, cp_pcoll) | _unify_data()
      assert_that(unified, equal_to(expected))

  def test_build_pipeline_end_to_end_in_memory(self):
    options = MyPipelineOptions(
        session_gap_seconds=10,
        allowed_lateness_seconds=5,
        output_dataset="test_dataset",
        output_table="test_unified",
        output_sessions_table="test_sessions",
    )

    in_memory_tx = [
        json.dumps({
            "household_key": "hh-1",
            "transaction_id": "tx-1",
            "product_id": "p100",
            "quantity": 1,
            "sales_value": 5.0,
        }).encode("utf-8"),
        b"MALFORMED_JSON_PAYLOAD",
    ]
    in_memory_cp = [
        json.dumps({
            "household_key": "hh-1",
            "transaction_id": "tx-1",
            "coupon_upc": "c100",
            "campaign": "summer",
        }).encode("utf-8")
    ]

    with TestPipeline(options=options) as p:
      unified, sessions, deadletters = build_pipeline(
          pipeline=p,
          pipeline_options=options,
          in_memory_transactions=in_memory_tx,
          in_memory_coupons=in_memory_cp,
      )

      assert_that(unified, _check_unified, label="CheckUnified")
      assert_that(sessions, _check_sessions, label="CheckSessions")
      assert_that(deadletters, _check_deadletters, label="CheckDeadletters")


def _check_unified(records):
  assert len(records) == 1
  assert records[0]["transaction_id"] == "tx-1"
  assert records[0]["coupon_upc"] == "c100"
  assert records[0]["campaign"] == "summer"


def _check_sessions(session_records):
  assert len(session_records) == 1
  assert session_records[0]["household_key"] == "hh-1"
  assert session_records[0]["total_spend"] == 5.0


def _check_deadletters(dlq_records):
  assert len(dlq_records) == 1
  assert dlq_records[0]["source"] == "transaction"
  assert "Malformed payload" in dlq_records[0]["error_message"]


if __name__ == "__main__":
  unittest.main()
