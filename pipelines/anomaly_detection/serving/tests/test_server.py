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
"""Unit tests for the custom Vertex AI prediction server."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sklearn.ensemble import IsolationForest
import joblib

from serving import predict_server


class PredictServerTest(unittest.TestCase):
  """Verify FastAPI predict server endpoints with a trained IsolationForest."""

  def setUp(self):
    self.temp_dir = tempfile.TemporaryDirectory()
    self.model_path = Path(self.temp_dir.name) / "model.joblib"

    # Train and save a small Isolation Forest
    model = IsolationForest(n_estimators=10, random_state=42)
    model.fit([[1.0, 1.0, 1.0], [1.1, 1.2, 0.9], [1.0, 0.8, 1.1]])
    joblib.dump(model, self.model_path)

    predict_server.load_model(str(self.temp_dir.name))
    self.client = TestClient(predict_server.app)

  def tearDown(self):
    self.temp_dir.cleanup()
    predict_server.MODEL = None

  def test_health_check(self):
    response = self.client.get("/health")
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json(), {"status": "healthy"})

  def test_ping_check(self):
    response = self.client.get("/ping")
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json(), {"status": "healthy"})

  def test_predict_valid_batch(self):
    instances = [[1.0, 1.0, 1.0], [100.0, 5000.0, 80.0]]
    response = self.client.post("/predict", json={"instances": instances})
    self.assertEqual(response.status_code, 200)
    data = response.json()
    self.assertIn("predictions", data)
    predictions = data["predictions"]
    self.assertEqual(len(predictions), 2)
    for pred in predictions:
      self.assertIn(pred, (-1, 1))

  def test_predict_invalid_payloads(self):
    for payload in ({}, {"instances": []}, {"instances": "invalid"}):
      with self.subTest(payload=payload):
        response = self.client.post("/predict", json=payload)
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
  unittest.main()
