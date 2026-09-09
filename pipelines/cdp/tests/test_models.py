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
"""Unit tests for Beam schema data models and event payload parsing."""

import json
import unittest

from apache_beam.typehints.schemas import named_tuple_to_schema

from cdp_pipeline.models import (
    CouponRedemption,
    CustomerInteractionEvent,
    CustomerSessionProfile,
    DeadLetterRecord,
    EventType,
    TransactionItem,
    UnifiedTransactionRecord,
)


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
