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
"""Train an isolated synthetic Isolation Forest and export its contract."""
import importlib.metadata
import json
import os
import platform
from pathlib import Path
import tempfile
import joblib
from sklearn.ensemble import IsolationForest
from sklearn.metrics import classification_report
from google.cloud import storage
from anomaly_detection_pipeline.features import FEATURE_ORDER, feature_vector, profiles, transaction


def train(output, seed=42):
  customers = profiles(seed=seed)
  examples = [
      feature_vector(
          transaction(customers[i % 100], i, seed),
          customers[i % 100]['average_amount']) for i in range(5000)
  ]
  model = IsolationForest(
      n_estimators=100, contamination=.02, random_state=seed).fit(examples)
  evaluation = [
      feature_vector(
          transaction(
              customers[i % 100], i, seed + 100000, anomalous=i % 2 == 0),
          customers[i % 100]['average_amount']) for i in range(1000)
  ]
  expected = [-1 if i % 2 == 0 else 1 for i in range(1000)]
  metadata = {
      'seed':
          seed,
      'python':
          platform.python_version(),
      'evaluation_seed':
          seed + 100000,
      'training_examples':
          5000,
      'evaluation_examples':
          1000,
      'feature_order':
          FEATURE_ORDER,
      'versions': {
          name: importlib.metadata.version(name)
          for name in ('scikit-learn', 'numpy', 'scipy', 'joblib',
                       'threadpoolctl')
      },
      'metrics':
          classification_report(
              expected, model.predict(evaluation), output_dict=True)
  }
  output = Path(output)
  output.mkdir(parents=True, exist_ok=True)
  joblib.dump(model, output / 'model.joblib')
  (output / 'metadata.json').write_text(
      json.dumps(metadata, indent=2), encoding='utf-8')
  return metadata


def main():
  """Export locally for validation or to Vertex AI's managed artifact directory."""
  destination = os.environ.get('AIP_MODEL_DIR', './model')
  if not destination.startswith('gs://'):
    train(destination)
    return
  with tempfile.TemporaryDirectory() as directory:
    train(directory)
    bucket, _, prefix = destination[5:].partition('/')
    client = storage.Client()
    for path in Path(directory).iterdir():
      client.bucket(bucket).blob(prefix.rstrip('/') + '/' +
                                 path.name).upload_from_filename(path)


if __name__ == '__main__':
  main()
