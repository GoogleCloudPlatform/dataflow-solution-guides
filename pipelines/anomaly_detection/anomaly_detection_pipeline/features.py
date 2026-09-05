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
"""Deterministic demonstration data and the training/serving feature contract."""
import datetime
import json
import math
import random

FEATURE_ORDER = [
    'amount_ratio', 'distance_from_home', 'recent_transaction_count'
]


def profiles(count=100, seed=42):
  rng = random.Random(seed)
  return [{
      'customer_id': f'customer-{i:04d}',
      'average_amount': round(rng.uniform(20, 200), 2)
  } for i in range(count)]


def transaction(profile, index, seed=42, anomalous=False):
  rng = random.Random(seed + index)
  return {
      'transaction_id':
          f'transaction-{seed}-{index}',
      'customer_id':
          profile['customer_id'],
      'timestamp':
          (datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc) +
           datetime.timedelta(seconds=index)).isoformat(),
      'amount':
          round(
              profile['average_amount'] *
              rng.uniform(5, 12) if anomalous else profile['average_amount'] *
              rng.uniform(.5, 1.5), 2),
      'distance_from_home':
          rng.uniform(500, 2000) if anomalous else rng.uniform(0, 30),
      'recent_transaction_count':
          rng.randint(20, 50) if anomalous else rng.randint(0, 5)
  }


def parse_transaction(raw):
  value = json.loads(raw) if isinstance(raw, (bytes, str)) else dict(raw)
  if not isinstance(value, dict):
    raise ValueError('transaction must be a JSON object')
  for key in ('transaction_id', 'customer_id', 'timestamp'):
    if not isinstance(value.get(key), str) or not value[key]:
      raise ValueError(f'{key} must be a nonempty string')
  timestamp = datetime.datetime.fromisoformat(value['timestamp'].replace(
      'Z', '+00:00'))
  if timestamp.tzinfo is None:
    raise ValueError('timestamp must include a timezone')
  for key in ('amount', 'distance_from_home', 'recent_transaction_count'):
    item = value.get(key)
    try:
      finite = math.isfinite(item)
    except (TypeError, OverflowError):
      finite = False
    if isinstance(
        item, bool) or not isinstance(item,
                                      (int, float)) or not finite or item < 0:
      raise ValueError(f'{key} must be finite and nonnegative')
  if int(
      value['recent_transaction_count']) != value['recent_transaction_count']:
    raise ValueError('recent_transaction_count must be an integer')
  return value


def feature_vector(value, average_amount):
  value = parse_transaction(value)
  average_amount = float(average_amount)
  if not math.isfinite(average_amount) or average_amount <= 0:
    raise ValueError('average_amount must be positive and finite')
  vector = [
      value['amount'] / average_amount,
      float(value['distance_from_home']),
      float(value['recent_transaction_count'])
  ]
  if not all(math.isfinite(item) for item in vector):
    raise ValueError('features must be finite')
  return vector


def detection(value, vector, prediction, endpoint):
  if isinstance(prediction, bool) or prediction not in (-1, 1):
    raise ValueError('prediction must be -1 or 1')
  prediction = int(prediction)
  return {
      key: value[key] for key in ('transaction_id', 'customer_id', 'timestamp')
  } | {
      'features': vector,
      'prediction': prediction,
      'is_anomaly': prediction == -1,
      'endpoint': endpoint
  }
