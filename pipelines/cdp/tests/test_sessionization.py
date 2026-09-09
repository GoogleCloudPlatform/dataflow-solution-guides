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
from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.util import assert_that, equal_to
from apache_beam.transforms.window import Sessions, TimestampedValue

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

# Prevent pytest from treating Apache Beam's TestPipeline as a test case
TestPipeline.__test__ = False


class SessionizationTest(unittest.TestCase):
  """Unit tests for ProcessCustomerSessionDoFn using TestPipeline."""

  def test_process_customer_session_aggregation(self):
    events = [
        (("hh-42",
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
          )), 1000),
        (("hh-42",
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
          )), 1100),
        (("hh-42",
          CustomerInteractionEvent(
              event_type=EventType.COUPON.value,
              household_key="hh-42",
              transaction_id="tx-101",
              coupon=CouponRedemption(
                  coupon_upc="cp-999",
                  campaign="fall-sale",
              ),
          )), 1150),
    ]

    with TestPipeline() as p:
      results = (
          p
          | beam.Create(events)
          | beam.Map(lambda x: TimestampedValue(x[0], x[1]))
          | beam.WindowInto(Sessions(300))
          | beam.GroupByKey()
          | beam.ParDo(ProcessCustomerSessionDoFn()).with_outputs(
              TAG_SESSIONS, main="unified_records"))

      def check_unified(records):
        assert len(records) == 2
        assert all(isinstance(r, UnifiedTransactionRecord) for r in records)
        tx_101 = next(u for u in records if u.transaction_id == "tx-101")
        assert tx_101.coupon_upc == "cp-999"
        assert tx_101.campaign == "fall-sale"
        assert tx_101.sales_value == 20.0

        tx_102 = next(u for u in records if u.transaction_id == "tx-102")
        assert tx_102.coupon_upc is None

      def check_sessions(sessions):
        assert len(sessions) == 1
        session = sessions[0]
        assert isinstance(session, CustomerSessionProfile)
        assert session.household_key == "hh-42"
        assert session.total_transactions == 2
        assert session.total_items_purchased == 3
        assert session.total_spend == 30.0
        assert session.total_discount == 3.0
        assert session.coupons_redeemed_count == 1
        assert session.distinct_products_count == 2
        assert "fall-sale" in session.campaigns
        assert session.session_duration_sec == 450

      assert_that(results.unified_records, check_unified, label="CheckUnified")
      assert_that(results[TAG_SESSIONS], check_sessions, label="CheckSessions")

  def test_process_customer_session_empty_events(self):
    with TestPipeline() as p:
      results = (
          p
          | beam.Create([("hh-empty", [])])
          | beam.ParDo(ProcessCustomerSessionDoFn()).with_outputs(
              TAG_SESSIONS, main="unified_records"))

      assert_that(results.unified_records, equal_to([]), label="CheckEmptyU")
      assert_that(results[TAG_SESSIONS], equal_to([]), label="CheckEmptyS")

  def test_process_customer_session_without_coupons(self):
    events = [
        (("hh-50",
          CustomerInteractionEvent(
              event_type=EventType.TRANSACTION.value,
              household_key="hh-50",
              transaction_id="tx-201",
              transaction=TransactionItem(
                  product_id="prod-X",
                  quantity=1,
                  sales_value=5.0,
              ),
          )), 1000),
    ]

    with TestPipeline() as p:
      results = (
          p
          | beam.Create(events)
          | beam.Map(lambda x: TimestampedValue(x[0], x[1]))
          | beam.WindowInto(Sessions(300))
          | beam.GroupByKey()
          | beam.ParDo(ProcessCustomerSessionDoFn()).with_outputs(
              TAG_SESSIONS, main="unified_records"))

      def check_unified(records):
        assert len(records) == 1
        assert records[0].coupon_upc is None
        assert records[0].campaign is None

      def check_sessions(sessions):
        assert len(sessions) == 1
        assert sessions[0].coupons_redeemed_count == 0
        assert sessions[0].campaigns == []

      assert_that(
          results.unified_records, check_unified, label="CheckNoCouponU")
      assert_that(results[TAG_SESSIONS], check_sessions, label="CheckNoCouponS")

  def test_process_customer_session_multiple_coupons_same_tx(self):
    events = [
        (("hh-60",
          CustomerInteractionEvent(
              event_type=EventType.TRANSACTION.value,
              household_key="hh-60",
              transaction_id="tx-301",
              transaction=TransactionItem(
                  product_id="prod-Y",
                  quantity=2,
                  sales_value=12.0,
              ),
          )), 1000),
        (("hh-60",
          CustomerInteractionEvent(
              event_type=EventType.COUPON.value,
              household_key="hh-60",
              transaction_id="tx-301",
              coupon=CouponRedemption(
                  coupon_upc="cp-1",
                  campaign="camp-A",
              ),
          )), 1010),
        (("hh-60",
          CustomerInteractionEvent(
              event_type=EventType.COUPON.value,
              household_key="hh-60",
              transaction_id="tx-301",
              coupon=CouponRedemption(
                  coupon_upc="cp-2",
                  campaign="camp-B",
              ),
          )), 1020),
    ]

    with TestPipeline() as p:
      results = (
          p
          | beam.Create(events)
          | beam.Map(lambda x: TimestampedValue(x[0], x[1]))
          | beam.WindowInto(Sessions(300))
          | beam.GroupByKey()
          | beam.ParDo(ProcessCustomerSessionDoFn()).with_outputs(
              TAG_SESSIONS, main="unified_records"))

      def check_unified(records):
        assert len(records) == 2
        coupon_upcs = {u.coupon_upc for u in records}
        assert coupon_upcs == {"cp-1", "cp-2"}

      def check_sessions(sessions):
        assert len(sessions) == 1
        assert sessions[0].coupons_redeemed_count == 2
        assert sessions[0].campaigns == ["camp-A", "camp-B"]

      assert_that(results.unified_records, check_unified, label="CheckMultiU")
      assert_that(results[TAG_SESSIONS], check_sessions, label="CheckMultiS")


if __name__ == "__main__":
  unittest.main()
