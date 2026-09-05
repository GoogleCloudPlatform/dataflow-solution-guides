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
"""Model training and evaluation tests in Python 3.14."""
import json
from pathlib import Path
import tempfile
import unittest
import joblib
from training.train import train
from anomaly_detection_pipeline.features import feature_vector, profiles, transaction


class ModelTest(unittest.TestCase):
  """Check deterministic fitting, independent evaluation and serialization."""

  def test_train_serialize_and_predict(self):
    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory(
    ) as second:
      metadata = train(first)
      self.assertEqual(metadata, train(second))
      self.assertTrue(metadata['versions']['scikit-learn'].startswith('1.'))
      self.assertGreater(metadata['metrics']['-1']['recall'], .95)
      self.assertGreater(metadata['metrics']['1']['recall'], .90)
      model = joblib.load(Path(first) / 'model.joblib')
      other = joblib.load(Path(second) / 'model.joblib')
      profile = profiles()[0]
      vectors = [
          feature_vector(
              transaction(profile, index, anomalous=index % 2 == 0),
              profile['average_amount']) for index in range(20)
      ]
      self.assertEqual(
          list(model.predict(vectors)), list(other.predict(vectors)))
      self.assertEqual(
          list(model.predict([[1., 1., 1.], [10., 1500., 40.]])), [1, -1])
      self.assertEqual(
          json.loads((Path(first) / 'metadata.json').read_text()), metadata)
