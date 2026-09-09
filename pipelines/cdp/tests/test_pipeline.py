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
"""Integration and DAG construction tests for the Customer Data Platform pipeline."""

import json
import unittest

from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.util import assert_that, equal_to

from cdp_pipeline.options import MyPipelineOptions
from cdp_pipeline.pipeline import build_pipeline

# Prevent pytest from treating Apache Beam's TestPipeline as a test case
TestPipeline.__test__ = False


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


class PipelineTest(unittest.TestCase):
  """Integration and DAG assembly tests for the CDP streaming pipeline."""

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

  def test_build_pipeline_empty_inputs(self):
    options = MyPipelineOptions(
        session_gap_seconds=10,
        allowed_lateness_seconds=0,
    )
    with TestPipeline(options=options) as p:
      unified, sessions, deadletters = build_pipeline(
          pipeline=p,
          pipeline_options=options,
          in_memory_transactions=[],
          in_memory_coupons=[],
      )
      assert_that(unified, equal_to([]), label="CheckEmptyUnified")
      assert_that(sessions, equal_to([]), label="CheckEmptySessions")
      assert_that(deadletters, equal_to([]), label="CheckEmptyDeadletters")

  def test_build_pipeline_pubsub_topic_branch_dag(self):
    options = MyPipelineOptions([
        "--transactions_topic=projects/test-p/topics/tx-topic",
        "--coupons_redemption_topic=projects/test-p/topics/cp-topic",
    ])
    p = TestPipeline(options=options)
    unified, sessions, deadletters = build_pipeline(
        pipeline=p,
        pipeline_options=options,
    )
    self.assertIsNotNone(unified)
    self.assertIsNotNone(sessions)
    self.assertIsNotNone(deadletters)

  def test_build_pipeline_pubsub_subscription_branch_dag(self):
    options = MyPipelineOptions([
        "--transactions_subscription=projects/test-p/subscriptions/tx-sub",
        "--coupons_redemption_subscription=projects/test-p/subscriptions/cp-sub",
    ])
    p = TestPipeline(options=options)
    unified, sessions, deadletters = build_pipeline(
        pipeline=p,
        pipeline_options=options,
    )
    self.assertIsNotNone(unified)
    self.assertIsNotNone(sessions)
    self.assertIsNotNone(deadletters)


if __name__ == "__main__":
  unittest.main()
