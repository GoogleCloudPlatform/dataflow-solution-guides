"""Verify a trusted artifact inside the custom Python 3.14 serving container."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import warnings

import joblib
from sklearn.exceptions import InconsistentVersionWarning


def verify(directory):
  """Check versions, serialization and the ordered batched prediction contract."""
  directory = Path(directory)
  metadata = json.loads(
      (directory / 'metadata.json').read_text(encoding='utf-8'))
  versions = {
      name: importlib.metadata.version(name)
      for name in ('scikit-learn', 'numpy', 'scipy', 'joblib', 'threadpoolctl')
  }
  if sys.version_info[:2] != (
      3, 14) or not versions['scikit-learn'].startswith('1.'):
    raise ValueError(
        'expected the Python 3.14 scikit-learn serving environment')
  if metadata['feature_order'] != [
      'amount_ratio', 'distance_from_home', 'recent_transaction_count'
  ]:
    raise ValueError('artifact feature order does not match the pipeline')
  warnings.simplefilter('error', InconsistentVersionWarning)
  model = joblib.load(directory / 'model.joblib')
  if model.n_features_in_ != 3:
    raise ValueError('model must accept exactly three features')
  examples = [[1., 1., 1.], [10., 1500., 40.], [1., 1., 1.]]
  if list(model.predict(examples)) != [1, -1, 1]:
    raise ValueError('unexpected ordered batched predictions')
  report = {
      'versions':
          versions,
      'python':
          sys.version,
      'serving_digest':
          os.environ.get('SERVING_DIGEST', 'custom-serving-python3.14'),
      'model_sha256':
          hashlib.sha256((directory / 'model.joblib').read_bytes()).hexdigest()
  }
  (directory / 'compatibility.json').write_text(
      json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
  target_dir = sys.argv[1] if len(sys.argv) > 1 else '/model'
  verify(target_dir)
