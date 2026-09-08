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
from apache_beam.options.pipeline_options import GoogleCloudOptions
from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.util import assert_that
from apache_beam.transforms.window import IntervalWindow
from apache_beam.typehints.schemas import named_tuple_to_schema
from apache_beam.utils.timestamp import Timestamp

from cdp_pipeline.models import (
    CouponRedemption,
    CustomerInteractionEvent,
    CustomerSessionProfile,
    DeadLetterRecord,
    EventType,
    TransactionItem,
    UnifiedTransactionRecord,
)
from cdp_pipeline.options import MyPipelineOptions
from cdp_pipeline.parsing import (
    ParseRecordDoFn,
    TAG_DEADLETTER,
)
from cdp_pipeline.pipeline import build_pipeline
from cdp_pipeline.schemas import load_output_schema
from cdp_pipeline.sessionization import (
    ProcessCustomerSessionDoFn,
    TAG_SESSIONS,
)

# Prevent pytest from treating Apache Beam's TestPipeline as a test case
TestPipeline.__test__ = False


class CustomerDataPlatformTest(unittest.TestCase):

  def test_pipeline_options_project(self):
    options = MyPipelineOptions(["--project=my-test-project"])
    gcp_options = options.view_as(GoogleCloudOptions)
    self.assertEqual(gcp_options.project, "my-test-project")

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

  def test_load_schemas_helpers(self):
    sessions_schema = load_output_schema(None, "customer_sessions.json")
    self.assertIn("fields", sessions_schema)
    session_fields = [f["name"] for f in sessions_schema["fields"]]
    self.assertIn("session_id", session_fields)
    self.assertIn("total_spend", session_fields)
    self.assertIn("total_transactions", session_fields)

    dlq_schema = load_output_schema(None, "deadletter_table.json")
    self.assertIn("fields", dlq_schema)
    dlq_fields = [f["name"] for f in dlq_schema["fields"]]
    self.assertIn("error_message", dlq_fields)
    self.assertIn("raw_payload", dlq_fields)

    with self.assertRaises(FileNotFoundError):
      load_output_schema(None, "non_existent_schema.json")

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
        CustomerInteractionEvent(
            event_type=EventType.TRANSACTION.value,
            household_key="hh-42",
            transaction_id="tx-101",
            transaction=TransactionItem(
                product_id="prod-A",
                quantity=2,
                sales_value=20.0,
                retail_disc=2.0,
                coupon_disc=1.0,
                store_id="store-1",
            ),
        ),
        CustomerInteractionEvent(
            event_type=EventType.TRANSACTION.value,
            household_key="hh-42",
            transaction_id="tx-102",
            transaction=TransactionItem(
                product_id="prod-B",
                quantity=1,
                sales_value=10.0,
                retail_disc=0.0,
                coupon_disc=0.0,
                store_id="store-1",
            ),
        ),
        CustomerInteractionEvent(
            event_type=EventType.COUPON.value,
            household_key="hh-42",
            transaction_id="tx-101",
            coupon=CouponRedemption(
                coupon_upc="cp-999",
                campaign="fall-sale",
            ),
        ),
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
    self.assertIsInstance(unified[0], UnifiedTransactionRecord)
    tx_101 = next(u for u in unified if u.transaction_id == "tx-101")
    self.assertEqual(tx_101.coupon_upc, "cp-999")
    self.assertEqual(tx_101.campaign, "fall-sale")
    self.assertEqual(tx_101.sales_value, 20.0)

    tx_102 = next(u for u in unified if u.transaction_id == "tx-102")
    self.assertIsNone(tx_102.coupon_upc)

    # Verify session summary
    self.assertEqual(len(sessions), 1)
    session = sessions[0]
    self.assertIsInstance(session, CustomerSessionProfile)
    self.assertEqual(session.household_key, "hh-42")
    self.assertEqual(session.total_transactions, 2)
    self.assertEqual(session.total_items_purchased, 3)
    self.assertEqual(session.total_spend, 30.0)
    self.assertEqual(session.total_discount, 3.0)
    self.assertEqual(session.coupons_redeemed_count, 1)
    self.assertEqual(session.distinct_products_count, 2)
    self.assertIn("fall-sale", session.campaigns)

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
            "event_timestamp": "2026-09-08T10:00:00Z",
        }).encode("utf-8"),
        b"MALFORMED_JSON_PAYLOAD",
    ]
    in_memory_cp = [
        json.dumps({
            "household_key": "hh-1",
            "transaction_id": "tx-1",
            "coupon_upc": "c100",
            "campaign": "summer",
            "event_timestamp": "2026-09-08T10:00:02Z",
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
  assert records[0].transaction_id == "tx-1"
  assert records[0].coupon_upc == "c100"
  assert records[0].campaign == "summer"


def _check_sessions(session_records):
  assert len(session_records) == 1
  assert session_records[0].household_key == "hh-1"
  assert session_records[0].total_spend == 5.0


def _check_deadletters(dlq_records):
  assert len(dlq_records) == 1
  assert dlq_records[0]["source"] == "transaction"
  assert "Malformed payload" in dlq_records[0]["error_message"]


class ModelsTest(unittest.TestCase):
  """Unit tests for Beam schema data models and parsing logic."""

  def test_transaction_item_defaults_and_dict(self):
    item = TransactionItem(
        product_id="prod-1",
        quantity=3,
        sales_value=25.50,
        store_id="store-10",
        retail_disc=1.50,
        coupon_disc=0.50,
    )
    self.assertEqual(item.product_id, "prod-1")
    self.assertEqual(item.quantity, 3)
    self.assertEqual(item.sales_value, 25.50)
    item_dict = item.to_dict()
    self.assertEqual(item_dict["product_id"], "prod-1")
    self.assertEqual(item_dict["quantity"], 3)
    self.assertEqual(item_dict["sales_value"], 25.50)
    self.assertEqual(item_dict["store_id"], "store-10")
    self.assertIsNone(item_dict["day"])

  def test_coupon_redemption_defaults_and_dict(self):
    coupon = CouponRedemption(
        coupon_upc="cp-12345",
        campaign="spring-sale",
        day=15,
    )
    self.assertEqual(coupon.coupon_upc, "cp-12345")
    self.assertEqual(coupon.campaign, "spring-sale")
    self.assertEqual(coupon.day, 15)
    c_dict = coupon.to_dict()
    self.assertEqual(c_dict["coupon_upc"], "cp-12345")
    self.assertEqual(c_dict["campaign"], "spring-sale")
    self.assertEqual(c_dict["day"], 15)

  def test_dead_letter_record_dict(self):
    dlq = DeadLetterRecord(
        source="transaction",
        raw_payload='{"bad": "data"}',
        error_message="Missing required field",
        timestamp="2026-09-07T12:00:00Z",
    )
    self.assertEqual(dlq.source, "transaction")
    d_dict = dlq.to_dict()
    self.assertEqual(d_dict["source"], "transaction")
    self.assertEqual(d_dict["error_message"], "Missing required field")

  def test_customer_interaction_event_from_raw_payload_transaction(self):
    payload = json.dumps({
        "household_key": "hh-99",
        "transaction_id": "tx-888",
        "product_id": "prod-abc",
        "quantity": "2",
        "sales_value": "19.99",
        "retail_disc": "1.00",
        "coupon_disc": "0.50",
        "store_id": "st-5",
    }).encode("utf-8")

    event, dlq = CustomerInteractionEvent.from_raw_payload(
        payload, EventType.TRANSACTION)
    self.assertIsNone(dlq)
    self.assertIsNotNone(event)
    self.assertEqual(event.household_key, "hh-99")
    self.assertEqual(event.transaction_id, "tx-888")
    self.assertEqual(event.event_type, EventType.TRANSACTION.value)
    self.assertIsNotNone(event.transaction)
    self.assertEqual(event.transaction.product_id, "prod-abc")
    self.assertEqual(event.transaction.quantity, 2)
    self.assertEqual(event.transaction.sales_value, 19.99)
    self.assertEqual(event.transaction.retail_disc, 1.00)
    self.assertEqual(event.transaction.coupon_disc, 0.50)
    self.assertIsNone(event.coupon)

    event_dict = event.to_dict()
    self.assertEqual(event_dict["household_key"], "hh-99")
    self.assertIsNotNone(event_dict["transaction"])
    self.assertIsNone(event_dict["coupon"])

  def test_customer_interaction_event_from_raw_payload_coupon(self):
    payload = {
        "household_key": "hh-99",
        "transaction_id": "tx-888",
        "coupon_upc": "cp-99999",
        "campaign": "promo-2026",
        "day": 42,
    }

    event, dlq = CustomerInteractionEvent.from_raw_payload(
        payload, EventType.COUPON)
    self.assertIsNone(dlq)
    self.assertIsNotNone(event)
    self.assertEqual(event.household_key, "hh-99")
    self.assertEqual(event.transaction_id, "tx-888")
    self.assertEqual(event.event_type, EventType.COUPON.value)
    self.assertIsNotNone(event.coupon)
    self.assertEqual(event.coupon.coupon_upc, "cp-99999")
    self.assertEqual(event.coupon.campaign, "promo-2026")
    self.assertEqual(event.coupon.day, 42)
    self.assertIsNone(event.transaction)

  def test_customer_interaction_event_from_raw_payload_malformed_json(self):
    event, dlq = CustomerInteractionEvent.from_raw_payload(
        b"NOT_A_JSON_STRING", EventType.TRANSACTION)
    self.assertIsNone(event)
    self.assertIsNotNone(dlq)
    self.assertEqual(dlq.source, EventType.TRANSACTION.value)
    self.assertIn("Malformed payload", dlq.error_message)

  def test_customer_interaction_event_from_raw_payload_missing_keys(self):
    payload = json.dumps({"product_id": "prod-1"})
    event, dlq = CustomerInteractionEvent.from_raw_payload(
        payload, EventType.TRANSACTION)
    self.assertIsNone(event)
    self.assertIsNotNone(dlq)
    self.assertIn("Missing required household_key", dlq.error_message)

  def test_customer_interaction_event_from_raw_payload_unsupported_type(self):
    event, dlq = CustomerInteractionEvent.from_raw_payload(
        12345, EventType.TRANSACTION)  # type: ignore[arg-type]
    self.assertIsNone(event)
    self.assertIsNotNone(dlq)
    self.assertIn("Unsupported payload type", dlq.error_message)

  def test_beam_schema_compatibility(self):
    for model_cls in (
        TransactionItem,
        CouponRedemption,
        CustomerInteractionEvent,
        UnifiedTransactionRecord,
        CustomerSessionProfile,
    ):
      schema = named_tuple_to_schema(model_cls)
      self.assertIsNotNone(schema)
      self.assertGreater(len(schema.fields), 0)


if __name__ == "__main__":
  unittest.main()
