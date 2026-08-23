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
"""
Unit tests for Customer Data Platform pipeline transformations.
"""

import unittest
from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.util import assert_that, equal_to
import apache_beam as beam

from cdp_pipeline.customer_data_platform import (
    left_join,
    load_output_schema,
    _unify_data,
)


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


if __name__ == "__main__":
  unittest.main()
