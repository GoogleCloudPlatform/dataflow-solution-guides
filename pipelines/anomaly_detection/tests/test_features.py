"""Tests for the shared training and streaming feature contract."""
import json
import unittest
from anomaly_detection_pipeline.features import (FEATURE_ORDER, detection,
                                                 feature_vector,
                                                 parse_transaction, profiles,
                                                 transaction)


class FeatureTest(unittest.TestCase):
  """Exercise deterministic data, input validation and output serialization."""

  def setUp(self):
    self.profile = profiles()[0]
    self.value = transaction(self.profile, 0)

  def test_deterministic_and_independent_samples(self):
    self.assertEqual(profiles(), profiles())
    self.assertEqual(self.value, transaction(self.profile, 0))
    self.assertNotEqual(self.value, transaction(self.profile, 0, seed=100042))
    self.assertNotEqual(profiles(seed=42), profiles(seed=43))

  def test_shared_feature_order(self):
    self.assertEqual(
        FEATURE_ORDER,
        ['amount_ratio', 'distance_from_home', 'recent_transaction_count'])
    self.assertEqual(
        feature_vector(self.value, self.profile['average_amount']), [
            self.value['amount'] / self.profile['average_amount'],
            self.value['distance_from_home'],
            self.value['recent_transaction_count']
        ])
    self.assertEqual(
        parse_transaction(json.dumps(self.value).encode()), self.value)

  def test_malformed_messages(self):
    for raw in (b'no json', b'[]', b'null', b'1', b'"text"', b'{}', b'\xff'):
      with self.subTest(raw=raw), self.assertRaises((ValueError, UnicodeError)):
        parse_transaction(raw)

  def test_invalid_numbers_and_timestamp(self):
    for field, value in [('amount', True), ('amount', float('nan')),
                         ('distance_from_home', -1),
                         ('recent_transaction_count', 1.5),
                         ('timestamp', '2026-01-01'), ('customer_id', '')]:
      with self.subTest(field=field), self.assertRaises(ValueError):
        parse_transaction(self.value | {field: value})
    for average in (0, -1, float('inf')):
      with self.assertRaises(ValueError):
        feature_vector(self.value, average)

  def test_output_schema_and_predictions(self):
    vector = feature_vector(self.value, self.profile['average_amount'])
    row = detection(self.value, vector, -1,
                    'projects/p/locations/r/endpoints/1')
    self.assertTrue(row['is_anomaly'])
    self.assertEqual(json.loads(json.dumps(row)), row)
    self.assertEqual(
        set(row), {
            'transaction_id', 'customer_id', 'timestamp', 'features',
            'prediction', 'is_anomaly', 'endpoint'
        })
    for prediction in (True, 1.5, 0, '1'):
      with self.assertRaises(ValueError):
        detection(self.value, vector, prediction, 'endpoint')
