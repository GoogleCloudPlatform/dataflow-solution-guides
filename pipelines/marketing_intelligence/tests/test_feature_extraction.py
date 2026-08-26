#  Copyright 2025 Google LLC
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
"""Unit tests for feature extraction, model handler, and prediction formatting."""

import unittest
from unittest.mock import MagicMock
from apache_beam.ml.inference.base import PredictionResult
import numpy as np
from marketing_intelligence_pipeline.pipeline import (
    ExtractFeaturesDoFn,
    FormatPredictionDoFn,
    SklearnModelHandlerNumpyProb,
)


class TestFeatureExtractionAndFormatting(unittest.TestCase):
  """Unit tests for feature vector extraction and prediction formatting."""

  def test_extract_features_category_match(self):
    """Verifies feature extraction vector calculation when category matches."""
    dofn = ExtractFeaturesDoFn()
    enriched = {
        "event_id": "evt_1",
        "user_id": "user_100",
        "session_duration_sec": 300,
        "cart_value": 75.50,
        "total_lifetime_spend": 1200.0,
        "total_orders": 10,
        "days_since_last_order": 5,
        "item_category": "electronics",
        "preferred_category": "electronics",
        "loyalty_tier": "Gold",
    }

    results = list(dofn.process(enriched))
    self.assertEqual(len(results), 1)
    keyed_record, feature_vec = results[0]

    self.assertEqual(keyed_record["event_id"], "evt_1")
    self.assertEqual(feature_vec.shape, (7,))
    self.assertEqual(feature_vec.dtype, np.float32)

    # Features: [duration, cart_val, lifetime_spend, orders, days_since,
    # category_match, loyalty_score]
    expected = np.array([300.0, 75.50, 1200.0, 10.0, 5.0, 1.0, 3.0],
                        dtype=np.float32)
    np.testing.assert_array_almost_equal(feature_vec, expected)

  def test_extract_features_category_mismatch(self):
    """Verifies feature extraction vector calculation when category does not match."""
    dofn = ExtractFeaturesDoFn()
    enriched = {
        "event_id": "evt_2",
        "user_id": "user_200",
        "session_duration_sec": 45,
        "cart_value": 15.0,
        "total_lifetime_spend": 50.0,
        "total_orders": 2,
        "days_since_last_order": 60,
        "item_category": "clothing",
        "preferred_category": "electronics",
        "loyalty_tier": "Bronze",
    }

    results = list(dofn.process(enriched))
    _, feature_vec = results[0]

    np.testing.assert_array_almost_equal(
        feature_vec,
        np.array([45.0, 15.0, 50.0, 2.0, 60.0, 0.0, 1.0], dtype=np.float32),
    )

  def test_model_handler_prob_inference(self):
    """Verifies custom SklearnModelHandler returns both class and probability."""
    handler = SklearnModelHandlerNumpyProb(model_uri="dummy.pkl")

    mock_model = MagicMock()
    mock_model.predict.return_value = np.array([1, 0])
    mock_model.predict_proba.return_value = np.array([[0.1, 0.9], [0.8, 0.2]])

    batch = [
        np.array([100.0, 50.0, 500.0, 5.0, 10.0, 1.0, 2.0], dtype=np.float32),
        np.array([20.0, 10.0, 0.0, 0.0, 999.0, 0.0, 1.0], dtype=np.float32),
    ]

    inferences = handler.run_inference(batch, mock_model)
    self.assertEqual(len(inferences), 2)
    self.assertEqual(inferences[0].inference["predicted_purchase"], 1)
    self.assertAlmostEqual(inferences[0].inference["propensity_score"], 0.9)
    self.assertEqual(inferences[1].inference["predicted_purchase"], 0)
    self.assertAlmostEqual(inferences[1].inference["propensity_score"], 0.2)

  def test_format_prediction_offers(self):
    """Verifies offer assignment based on propensity score thresholds."""
    dofn = FormatPredictionDoFn()

    test_cases = [
        (0.92, "VIP 20% Instant Discount"),
        (0.70, "10% Category Boost Coupon"),
        (0.50, "Free Expedited Shipping"),
        (0.20, "Standard Catalog Recommendation"),
    ]

    for score, expected_offer in test_cases:
      enriched = {
          "event_id": f"evt_{score}",
          "user_id": "u1",
          "timestamp": "2026-08-26T00:00:00Z",
          "event_type": "view_item",
          "item_id": "item_1",
          "item_category": "electronics",
          "session_duration_sec": 100,
          "cart_value": 50.0,
          "loyalty_tier": "Gold",
          "total_lifetime_spend": 1000.0,
          "total_orders": 10,
          "days_since_last_order": 10,
          "preferred_category": "electronics",
      }
      pred_result = PredictionResult(
          example=None,
          inference={
              "predicted_purchase": 1 if score >= 0.5 else 0,
              "propensity_score": score
          },
      )

      records = list(dofn.process((enriched, pred_result)))
      self.assertEqual(len(records), 1)
      rec = records[0]
      self.assertEqual(rec["recommended_offer"], expected_offer)
      self.assertAlmostEqual(rec["propensity_score"], score)
      self.assertIn("processed_timestamp", rec)


if __name__ == "__main__":
  unittest.main()
