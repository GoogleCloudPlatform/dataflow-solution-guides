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
"""Bounded synthetic publication and an isolated end-to-end smoke check."""
import json
import os
import time
import uuid

from google.api_core.exceptions import DeadlineExceeded, GoogleAPICallError, NotFound
from google.cloud import bigquery, bigtable, pubsub_v1

from .features import profiles, transaction


def required(name):
  """Read a generated deployment setting."""
  value = os.environ.get(name)
  if not value:
    raise ValueError(f'{name} is required')
  return value


def remaining(deadline, maximum=30):
  """Bound individual RPCs by the remaining smoke-test budget."""
  seconds = deadline - time.monotonic()
  if seconds <= 0:
    raise TimeoutError('smoke test deadline exceeded')
  return min(maximum, seconds)


def seed(project):
  """Write reproducible average amounts to the configured profile family."""
  table = bigtable.Client(project=project).instance(
      required('BIGTABLE_INSTANCE')).table(
          os.environ.get('BIGTABLE_TABLE', 'customer_profiles'))
  rows = []
  for profile in profiles():
    row = table.direct_row(profile['customer_id'].encode())
    row.set_cell(
        os.environ.get('BIGTABLE_COLUMN_FAMILY', 'profile'), 'average_amount',
        str(profile['average_amount']).encode())
    rows.append(row)
  if any(status.code for status in table.mutate_rows(rows)):
    raise RuntimeError('profile seeding failed')


def publish(project, count, timeout, smoke=False):
  """Publish a bounded set, optionally correlating both sinks and error routes."""
  deadline = time.monotonic() + timeout
  publisher = pubsub_v1.PublisherClient()
  subscriber = pubsub_v1.SubscriberClient()
  run_id = uuid.uuid4().hex[:12]
  customers = profiles()
  values = [
      transaction(
          customers[index % len(customers)], index, anomalous=index % 5 == 0)
      for index in range(count)
  ]
  for value in values:
    value['transaction_id'] = run_id + '-' + value['transaction_id']
  expected = {value['transaction_id'] for value in values}
  subscriptions = []
  try:
    if smoke:
      for suffix, topic in [('out', required('OUTPUT_TOPIC')),
                            ('errors', required('ERROR_TOPIC'))]:
        name = f'projects/{project}/subscriptions/anomaly-smoke-{run_id}-{suffix}'
        subscriber.create_subscription(
            request={
                'name': name,
                'topic': topic,
                'expiration_policy': {
                    'ttl': {
                        'seconds': 86400
                    }
                }
            },
            retry=None,
            timeout=remaining(deadline))
        subscriptions.append(name)
    for value in values:
      publisher.publish(
          required('INPUT_TOPIC'),
          json.dumps(value).encode()).result(timeout=remaining(deadline))
    print(json.dumps({'transaction_ids': sorted(expected)}))
    if smoke:
      malformed = {'transaction_id': run_id + '-invalid'}
      missing = values[0] | {
          'transaction_id': run_id + '-missing',
          'customer_id': run_id + '-unknown'
      }
      for value in (malformed, missing):
        publisher.publish(
            required('INPUT_TOPIC'),
            json.dumps(value).encode()).result(timeout=remaining(deadline))
      verify_sinks(project, subscriber, subscriptions, expected,
                   {malformed['transaction_id'], missing['transaction_id']},
                   deadline)
  finally:
    # Cleanup has a separate bounded allowance, even after the test deadline.
    cleanup_errors = []
    for name in subscriptions:
      try:
        subscriber.delete_subscription(
            request={'subscription': name}, retry=None, timeout=10)
      except NotFound:
        pass
      except GoogleAPICallError as error:
        cleanup_errors.append(error)
    subscriber.close()
    publisher.stop()
    if cleanup_errors:
      raise RuntimeError(
          'temporary subscription cleanup failed') from cleanup_errors[0]


def verify_sinks(project, subscriber, subscriptions, expected, expected_errors,
                 deadline):
  """Poll isolated subscriptions and a parameterized query until all IDs arrive."""
  table = required('BIGQUERY_TABLE').replace(':', '.')
  parts = table.split('.')
  if len(parts) != 3 or not all(
      part.replace('_', '').replace('-', '').isalnum() for part in parts):
    raise ValueError('invalid BigQuery table name')
  client = bigquery.Client(project=project)
  seen, archived, errors = set(), set(), set()
  while time.monotonic() < deadline:
    for index, name in enumerate(subscriptions):
      try:
        response = subscriber.pull(
            request={
                'subscription': name,
                'max_messages': 100
            },
            retry=None,
            timeout=remaining(deadline, 5))
      except DeadlineExceeded:
        continue
      for received in response.received_messages:
        value = json.loads(received.message.data)
        if index == 0 and value.get('transaction_id') in expected:
          seen.add(value['transaction_id'])
        elif index == 1:
          text = json.dumps(value)
          errors.update(item for item in expected_errors if item in text)
      if response.received_messages:
        subscriber.acknowledge(
            request={
                'subscription': name,
                'ack_ids': [item.ack_id for item in response.received_messages]
            },
            retry=None,
            timeout=remaining(deadline))
    query = client.query(
        f'SELECT transaction_id FROM `{table}` WHERE transaction_id IN UNNEST(@ids)',
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter('ids', 'STRING', sorted(expected))
        ]),
        retry=None,
        timeout=remaining(deadline))
    archived = {
        row.transaction_id for row in query.result(
            timeout=remaining(deadline), retry=None, job_retry=None)
    }
    if expected <= seen and expected <= archived and expected_errors <= errors:
      print(
          'Smoke test passed: Pub/Sub, BigQuery, malformed and missing-profile routing'
      )
      return
    time.sleep(min(2, max(0, deadline - time.monotonic())))
  raise TimeoutError(
      f'smoke timed out: pubsub={len(seen)} bigquery={len(archived)} errors={len(errors)}'
  )
