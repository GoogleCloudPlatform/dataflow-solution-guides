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
"""Unit tests for customer session aggregation and session profile generation."""

import unittest

import apache_beam as beam
from apache_beam.transforms.window import IntervalWindow
from apache_beam.utils.timestamp import Timestamp

from cdp_pipeline.models import (
    CouponRedemption,
    CustomerInteractionEvent,
    CustomerSessionProfile,
    EventType,
    TransactionItem,
    UnifiedTransactionRecord,
)
from cdp_pipeline.sessionization import (
    ProcessCustomerSessionDoFn,
    TAG_SESSIONS,
)


class SessionizationTest(unittest.TestCase):
  """Unit tests for ProcessCustomerSessionDoFn."""

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
    self.assertEqual(session.session_duration_sec, 300)

  def test_process_customer_session_empty_events(self):
    fn = ProcessCustomerSessionDoFn()
    fn.setup()
    mock_window = IntervalWindow(  # pylint: disable=too-many-function-args
        Timestamp(1000), Timestamp(1300))
    outputs = list(fn.process(("hh-empty", []), window=mock_window))
    self.assertEqual(len(outputs), 0)

  def test_process_customer_session_without_coupons(self):
    fn = ProcessCustomerSessionDoFn()
    fn.setup()
    mock_window = IntervalWindow(  # pylint: disable=too-many-function-args
        Timestamp(1000), Timestamp(1200))

    events = [
        CustomerInteractionEvent(
            event_type=EventType.TRANSACTION.value,
            household_key="hh-50",
            transaction_id="tx-201",
            transaction=TransactionItem(
                product_id="prod-X",
                quantity=1,
                sales_value=5.0,
            ),
        ),
    ]

    outputs = list(fn.process(("hh-50", events), window=mock_window))
    unified = [
        item for item in outputs
        if not isinstance(item, beam.pvalue.TaggedOutput)
    ]
    sessions = [
        item.value for item in outputs if
        isinstance(item, beam.pvalue.TaggedOutput) and item.tag == TAG_SESSIONS
    ]

    self.assertEqual(len(unified), 1)
    self.assertIsNone(unified[0].coupon_upc)
    self.assertIsNone(unified[0].campaign)

    self.assertEqual(len(sessions), 1)
    self.assertEqual(sessions[0].coupons_redeemed_count, 0)
    self.assertEqual(sessions[0].campaigns, [])

  def test_process_customer_session_multiple_coupons_same_tx(self):
    fn = ProcessCustomerSessionDoFn()
    fn.setup()
    mock_window = IntervalWindow(  # pylint: disable=too-many-function-args
        Timestamp(1000), Timestamp(1200))

    events = [
        CustomerInteractionEvent(
            event_type=EventType.TRANSACTION.value,
            household_key="hh-60",
            transaction_id="tx-301",
            transaction=TransactionItem(
                product_id="prod-Y",
                quantity=2,
                sales_value=12.0,
            ),
        ),
        CustomerInteractionEvent(
            event_type=EventType.COUPON.value,
            household_key="hh-60",
            transaction_id="tx-301",
            coupon=CouponRedemption(
                coupon_upc="cp-1",
                campaign="camp-A",
            ),
        ),
        CustomerInteractionEvent(
            event_type=EventType.COUPON.value,
            household_key="hh-60",
            transaction_id="tx-301",
            coupon=CouponRedemption(
                coupon_upc="cp-2",
                campaign="camp-B",
            ),
        ),
    ]

    outputs = list(fn.process(("hh-60", events), window=mock_window))
    unified = [
        item for item in outputs
        if not isinstance(item, beam.pvalue.TaggedOutput)
    ]
    sessions = [
        item.value for item in outputs if
        isinstance(item, beam.pvalue.TaggedOutput) and item.tag == TAG_SESSIONS
    ]

    # One unified record per matching coupon
    self.assertEqual(len(unified), 2)
    coupon_upcs = {u.coupon_upc for u in unified}
    self.assertEqual(coupon_upcs, {"cp-1", "cp-2"})

    self.assertEqual(len(sessions), 1)
    self.assertEqual(sessions[0].coupons_redeemed_count, 2)
    self.assertEqual(sessions[0].campaigns, ["camp-A", "camp-B"])


if __name__ == "__main__":
  unittest.main()
