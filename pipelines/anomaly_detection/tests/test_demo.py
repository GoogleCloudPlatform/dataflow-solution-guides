"""Smoke-test timeouts and subscription/query correlation without cloud access."""
import json
from types import SimpleNamespace
import time
import unittest
from unittest import mock

from google.api_core.exceptions import AlreadyExists
from anomaly_detection_pipeline import demo


class DemoTest(unittest.TestCase):
  """Check positive and timed-out verification with fake services."""

  def test_timeout_is_explicit(self):
    with self.assertRaises(TimeoutError):
      demo.remaining(time.monotonic() - 1)

  def test_correlates_both_sinks_and_errors(self):
    subscriber = mock.Mock()

    def pull(request, **_):
      values = [{
          'transaction_id': 'one'
      }] if request['subscription'] == 'out' else [{
          'input': {
              'transaction_id': 'bad'
          }
      }, {
          'input': {
              'transaction_id': 'missing'
          }
      }]
      return SimpleNamespace(received_messages=[
          SimpleNamespace(
              message=SimpleNamespace(data=json.dumps(value).encode()),
              ack_id=str(index)) for index, value in enumerate(values)
      ])

    subscriber.pull.side_effect = pull
    client = mock.Mock()
    client.query.return_value.result.return_value = [
        SimpleNamespace(transaction_id='one')
    ]
    with mock.patch.dict(
        'os.environ',
        BIGQUERY_TABLE='project.dataset.detections'), mock.patch.object(
            demo.bigquery, 'Client', return_value=client):
      demo.verify_sinks('project', subscriber, ['out', 'errors'], {'one'},
                        {'bad', 'missing'},
                        time.monotonic() + 10)
    self.assertEqual(subscriber.acknowledge.call_count, 2)
    self.assertIn('UNNEST(@ids)', client.query.call_args.args[0])
    self.assertIsNone(client.query.call_args.kwargs['retry'])

  def test_existing_subscription_is_never_deleted(self):
    subscriber = mock.Mock()
    subscriber.create_subscription.side_effect = AlreadyExists(
        'foreign subscription')
    with mock.patch.dict(
        'os.environ',
        OUTPUT_TOPIC='out', ERROR_TOPIC='error'), mock.patch.object(
            demo.pubsub_v1, 'PublisherClient'), mock.patch.object(
                demo.pubsub_v1, 'SubscriberClient',
                return_value=subscriber), self.assertRaises(AlreadyExists):
      demo.publish('project', 1, 30, smoke=True)
    subscriber.delete_subscription.assert_not_called()

  def test_missing_outputs_fail_at_deadline(self):
    with mock.patch.dict(
        'os.environ',
        BIGQUERY_TABLE='project.dataset.detections'), mock.patch.object(
            demo.bigquery, 'Client'), self.assertRaises(TimeoutError):
      demo.verify_sinks('project', mock.Mock(), ['out', 'errors'], {'one'},
                        {'bad'},
                        time.monotonic() - 1)
