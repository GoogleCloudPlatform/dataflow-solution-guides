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
"""Unit tests for the synthetic Customer Data Platform event simulator."""

import unittest
from unittest.mock import MagicMock

from simulator.generator import generate_synthetic_session_events
from simulator.publisher import get_topic_path


class SimulatorTest(unittest.TestCase):
  """Unit tests for data generation and Pub/Sub topic path formatting."""

  def test_generate_synthetic_session_events(self):
    household_key = "hh-100"
    base_tx_id = 999000

    transactions, coupons = generate_synthetic_session_events(
        household_key, base_tx_id)

    self.assertIsInstance(transactions, list)
    self.assertGreater(len(transactions), 0)
    for tx in transactions:
      self.assertEqual(tx["household_key"], "hh-100")
      self.assertEqual(tx["transaction_id"], "999000")
      self.assertIn("product_id", tx)
      self.assertIn("sales_value", tx)
      self.assertGreater(tx["quantity"], 0)
      self.assertGreater(tx["sales_value"], 0.0)
      self.assertIn("event_timestamp", tx)

    self.assertIsInstance(coupons, list)
    for cp in coupons:
      self.assertEqual(cp["household_key"], "hh-100")
      self.assertEqual(cp["transaction_id"], "999000")
      self.assertIn("coupon_upc", cp)
      self.assertIn("campaign", cp)
      self.assertIn("event_timestamp", cp)

  def test_get_topic_path_fully_qualified(self):
    mock_publisher = MagicMock()
    full_path = "projects/test-proj/topics/test-topic"
    res = get_topic_path(mock_publisher, "other-proj", full_path)
    self.assertEqual(res, full_path)
    mock_publisher.topic_path.assert_not_called()

  def test_get_topic_path_short_name(self):
    mock_publisher = MagicMock()
    mock_publisher.topic_path.return_value = "projects/my-proj/topics/my-topic"
    res = get_topic_path(mock_publisher, "my-proj", "my-topic")
    self.assertEqual(res, "projects/my-proj/topics/my-topic")
    mock_publisher.topic_path.assert_called_once_with("my-proj", "my-topic")


if __name__ == "__main__":
  unittest.main()
