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
"""End-to-end pipeline test using Apache Beam TestPipeline."""

import os
import pickle
import unittest
import apache_beam as beam
from apache_beam.ml.inference.base import KeyedModelHandler
from apache_beam.testing.test_pipeline import TestPipeline as BeamTestPipeline
from apache_beam.testing.util import assert_that
import numpy as np
from sklearn.ensemble import RandomForestClassifier

from marketing_intelligence_pipeline.pipeline import (
    ExtractFeaturesDoFn,
    FirestoreEnrichmentDoFn,
    FormatPredictionDoFn,
    SklearnModelHandlerNumpyProb,
)


def _validate_scored_records(actual_elements):
  """Top-level validation function for Beam assert_that."""
  assert len(actual_elements) == 2
  for rec in actual_elements:
    assert "event_id" in rec
    assert "user_id" in rec
    assert "propensity_score" in rec
    assert "predicted_purchase" in rec
    assert "recommended_offer" in rec
    assert "processed_timestamp" in rec
    assert 0.0 <= rec["propensity_score"] <= 1.0
    assert rec["predicted_purchase"] in [0, 1]


class TestMarketingIntelligencePipeline(unittest.TestCase):
  """End-to-end unit tests for the Marketing Intelligence Beam pipeline."""

  @classmethod
  def setUpClass(cls):
    """Trains and serializes a temporary RandomForestClassifier for testing."""
    cls.test_model_path = "/tmp/test_marketing_model.pkl"
    x = np.array(
        [
            [300.0, 100.0, 1500.0, 10.0, 5.0, 1.0, 3.0],
            [30.0, 15.0, 20.0, 1.0, 120.0, 0.0, 1.0],
            [600.0, 250.0, 3500.0, 25.0, 2.0, 1.0, 4.0],
            [50.0, 25.0, 100.0, 2.0, 60.0, 0.0, 2.0],
        ],
        dtype=np.float32,
    )
    y = np.array([1, 0, 1, 0], dtype=int)
    model = RandomForestClassifier(n_estimators=10, random_state=42)
    model.fit(x, y)
    with open(cls.test_model_path, "wb") as f:
      pickle.dump(model, f)

  @classmethod
  def tearDownClass(cls):
    if os.path.exists(cls.test_model_path):
      os.remove(cls.test_model_path)

  def test_end_to_end_pipeline_transform(self):
    """Executes a local pipeline transforming raw events to scored predictions."""
    test_events = [
        {
            "event_id": "evt_001",
            "user_id": "user_1042",
            "timestamp": "2026-08-26T08:30:00Z",
            "event_type": "view_item",
            "item_id": "prod_elec_441",
            "item_category": "electronics",
            "session_duration_sec": 300,
            "cart_value": 100.0,
        },
        {
            "event_id": "evt_002",
            "user_id": "user_9999",
            "timestamp": "2026-08-26T08:31:00Z",
            "event_type": "view_item",
            "item_id": "prod_home_102",
            "item_category": "home",
            "session_duration_sec": 30,
            "cart_value": 15.0,
        },
    ]

    with BeamTestPipeline() as p:
      raw = p | "Create Inputs" >> beam.Create(test_events)
      enriched = raw | "Enrich" >> beam.ParDo(
          FirestoreEnrichmentDoFn(project=None, collection="customer_profiles"))
      keyed_features = enriched | "Features" >> beam.ParDo(
          ExtractFeaturesDoFn())
      model_handler = KeyedModelHandler(
          SklearnModelHandlerNumpyProb(model_uri=self.test_model_path))
      scored = keyed_features | "Inference" >> beam.ml.inference.base.RunInference(
          model_handler)
      formatted = scored | "Format" >> beam.ParDo(FormatPredictionDoFn())

      assert_that(formatted, _validate_scored_records)


if __name__ == "__main__":
  unittest.main()
