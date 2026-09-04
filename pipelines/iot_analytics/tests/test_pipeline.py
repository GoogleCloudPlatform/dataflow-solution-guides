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
"""End-to-end unit tests for IoT Analytics Beam pipeline transforms."""

import json
import os
import pickle
import unittest
import apache_beam as beam
from apache_beam.ml.inference.base import KeyedModelHandler, RunInference
from apache_beam.ml.inference.sklearn_inference import ModelFileType, SklearnModelHandlerNumpy
from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.util import assert_that
from apache_beam.utils.timestamp import Timestamp
import numpy as np
from sklearn.linear_model import LogisticRegression

from iot_analytics_pipeline.pipeline import custom_join
from iot_analytics_pipeline.trigger_inference import (
    ExtractFeaturesDoFn,
    FormatPredictionDoFn,
    format_alert_payload,
)


def _check_scored_records(actual):
  assert len(actual) == 2, f"Expected 2 scored records, got {len(actual)}"
  for rec in actual:
    assert "vehicle_id" in rec
    assert "max_temperature" in rec
    assert "max_vibration" in rec
    assert "needs_maintenance" in rec
    assert rec["needs_maintenance"] in [0, 1]


class TestIoTAnalyticsPipeline(unittest.TestCase):
  """Unit tests for IoT analytics pipeline transforms and inference."""

  @classmethod
  def setUpClass(cls):
    cls.test_model_path = "/tmp/test_iot_maintenance_model.pkl"
    # Train a simple test model: high temp or high vibration => 1
    x = np.array(
        [
            [60.0, 0.20, 30.0],
            [90.0, 0.70, 200.0],
            [55.0, 0.15, 60.0],
            [95.0, 0.65, 250.0],
        ],
        dtype=np.float32,
    )
    y = np.array([0, 1, 0, 1], dtype=int)
    model = LogisticRegression(random_state=42)
    model.fit(x, y)
    with open(cls.test_model_path, "wb") as f:
      pickle.dump(model, f)

  @classmethod
  def tearDownClass(cls):
    if os.path.exists(cls.test_model_path):
      os.remove(cls.test_model_path)

  def test_custom_join_with_metadata(self):
    left = beam.Row(
        vehicle_id="1001",
        max_temperature=85,
        max_vibration=0.45,
        max_timestamp=Timestamp(100),
        avg_mileage=50000,
    )
    right = {
        "maintenance": {
            "last_service_date": "2026-01-15",
            "maintenance_type": "oil_change",
            "model": "F-150",
        }
    }
    enriched = custom_join(left, right)
    self.assertEqual(enriched["vehicle_id"], "1001")
    self.assertEqual(enriched["max_temperature"], 85)
    self.assertEqual(enriched["max_vibration"], 0.45)
    self.assertEqual(enriched["last_service_date"], "2026-01-15")
    self.assertEqual(enriched["maintenance_type"], "oil_change")
    self.assertEqual(enriched["model"], "F-150")

  def test_custom_join_with_missing_metadata(self):
    left = beam.Row(
        vehicle_id="1002",
        max_temperature=70,
        max_vibration=0.20,
        max_timestamp=Timestamp(100),
        avg_mileage=30000,
    )
    # Empty right row (no Bigtable record found)
    right = {}
    enriched = custom_join(left, right)
    self.assertEqual(enriched["vehicle_id"], "1002")
    self.assertEqual(enriched["last_service_date"], "")
    self.assertEqual(enriched["maintenance_type"], "unknown")
    self.assertEqual(enriched["model"], "unknown")

  def test_format_alert_payload(self):
    record = {
        "vehicle_id": "1003",
        "max_temperature": 92,
        "max_vibration": 0.65,
        "last_service_date": "2025-10-01",
        "model": "F-150",
        "needs_maintenance": 1,
    }
    alert_bytes = format_alert_payload(record)
    self.assertIsInstance(alert_bytes, bytes)
    payload = json.loads(alert_bytes.decode("utf-8"))
    self.assertEqual(payload["vehicle_id"], "1003")
    self.assertEqual(payload["alert_type"], "PREDICTIVE_MAINTENANCE_REQUIRED")
    self.assertEqual(payload["severity"], "HIGH")

  def test_end_to_end_inference_flow(self):
    test_enriched = [
        {
            "vehicle_id": "1001",
            "max_temperature": 55,
            "max_vibration": 0.15,
            "latest_timestamp": Timestamp(1000),
            "avg_mileage": 25000,
            "last_service_date": "2026-08-01",
            "maintenance_type": "inspection",
            "model": "F-150",
        },
        {
            "vehicle_id": "1002",
            "max_temperature": 95,
            "max_vibration": 0.70,
            "latest_timestamp": Timestamp(1001),
            "avg_mileage": 85000,
            "last_service_date": "2025-01-01",
            "maintenance_type": "oil_change",
            "model": "F-150",
        },
    ]

    with TestPipeline() as p:
      raw = p | "CreateInputs" >> beam.Create(test_enriched)
      features = raw | "ExtractFeatures" >> beam.ParDo(ExtractFeaturesDoFn())
      model_handler = KeyedModelHandler(
          SklearnModelHandlerNumpy(
              model_uri=self.test_model_path,
              model_file_type=ModelFileType.PICKLE,
          ))
      scored = features | "RunInference" >> RunInference(model_handler)
      predictions = scored | "FormatPredictions" >> beam.ParDo(
          FormatPredictionDoFn())

      assert_that(predictions, _check_scored_records)


if __name__ == "__main__":
  unittest.main()
