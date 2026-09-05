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
"""Failure recovery and ownership tests without cloud writes."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from google.api_core.exceptions import NotFound
from google.auth.credentials import AnonymousCredentials
from google.cloud import aiplatform
from google.iam.v1 import policy_pb2

from anomaly_detection_pipeline import lifecycle, workflow


class LifecycleTest(unittest.TestCase):
  """Check recovery, concurrency and deletion boundaries."""

  def setUp(self):
    self.directory = tempfile.TemporaryDirectory()
    self.addCleanup(self.directory.cleanup)
    self.path = Path(self.directory.name) / 'manifest.json'
    self.state = {
        'owner': 'anomaly-detection-workflow',
        'run_id': 'abc',
        'project': 'p',
        'region': 'r',
        'completed': []
    }
    self.resource = SimpleNamespace(
        resource_name='projects/p/locations/r/models/1',
        labels={'workflow_run': 'abc'})

  def test_create_and_reconcile_after_interrupted_response(self):
    create = mock.Mock(side_effect=TimeoutError('response lost'))
    with self.assertRaises(TimeoutError):
      lifecycle.resolve(self.path, self.state, 'model', lambda: [], create)
    self.assertEqual(json.loads(self.path.read_text())['pending'], ['model'])
    lifecycle.resolve(self.path, self.state, 'model', lambda: [self.resource],
                      create)
    self.assertEqual(self.state['model'], self.resource.resource_name)
    self.assertEqual(self.state['pending'], [])
    create.assert_called_once()

  def test_ambiguous_retry_does_not_create_duplicate(self):
    self.state['pending'] = ['model']
    create = mock.Mock()
    with self.assertRaisesRegex(RuntimeError, 'outcome is unknown'):
      lifecycle.resolve(self.path, self.state, 'model', lambda: [], create)
    create.assert_not_called()

  def test_multiple_matches_rejected(self):
    with self.assertRaises(ValueError):
      lifecycle.resolve(self.path, self.state, 'model',
                        lambda: [self.resource, self.resource], mock.Mock())

  def test_wrong_owner_rejected(self):
    self.resource.labels = {'workflow_run': 'someone-else'}
    with self.assertRaises(ValueError):
      lifecycle.owned(self.resource, self.state)

  def test_recorded_resource_in_another_region_is_rejected(self):
    self.resource.resource_name = 'projects/p/locations/other/models/1'
    self.state['model'] = self.resource.resource_name
    with self.assertRaises(ValueError):
      lifecycle.owned(self.resource, self.state)

  def test_manifest_environment_and_concurrent_lock(self):
    with lifecycle.journal(self.path, 'p', 'r'):
      with self.assertRaises(RuntimeError):
        with lifecycle.journal(self.path, 'p', 'r'):
          self.fail('must not acquire a second lock')
    with self.assertRaises(ValueError):
      with lifecycle.journal(self.path, 'other', 'r'):
        self.fail('must reject another project')

  def test_cleanup_retains_unowned_endpoint(self):
    self.state['endpoint'] = 'projects/p/locations/r/endpoints/1'
    endpoint = mock.Mock(
        resource_name=self.state['endpoint'], labels={'workflow_run': 'other'})
    args = SimpleNamespace(
        command='cleanup', endpoint_env=str(self.path.parent / 'endpoint.sh'))
    with mock.patch.dict(
        'os.environ', PROJECT='p', REGION='r',
        BUCKET='b'), mock.patch.object(aiplatform, 'init'), mock.patch.object(
            aiplatform, 'Endpoint', return_value=endpoint):
      with self.assertRaises(ValueError):
        workflow.execute(args, self.state, self.path)
    endpoint.delete.assert_not_called()
    endpoint.undeploy_all.assert_not_called()

  def test_cleanup_resumes_missing_resource(self):
    self.state['model'] = self.resource.resource_name
    args = SimpleNamespace(
        command='cleanup', endpoint_env=str(self.path.parent / 'endpoint.sh'))
    with mock.patch.dict(
        'os.environ', PROJECT='p', REGION='r',
        BUCKET='b'), mock.patch.object(aiplatform, 'init'), mock.patch.object(
            aiplatform, 'Model', side_effect=NotFound('gone')):
      workflow.execute(args, self.state, self.path)
    self.assertNotIn('model', self.state)

  def test_train_waits_for_remote_completion(self):
    self.state['training_job'] = 'projects/p/locations/r/customJobs/1'
    job = mock.Mock(
        resource_name=self.state['training_job'],
        labels={'workflow_run': 'abc'})
    job.wait_for_completion.side_effect = RuntimeError('training failed')
    with mock.patch.dict(
        'os.environ', PROJECT='p', REGION='r',
        BUCKET='b'), mock.patch.object(aiplatform, 'init'), mock.patch.object(
            aiplatform.CustomJob, 'get', return_value=job):
      with self.assertRaisesRegex(RuntimeError, 'training failed'):
        workflow.execute(
            SimpleNamespace(command='train'), self.state, self.path)
    self.assertNotIn('train', self.state['completed'])
    job.wait.assert_not_called()

  def test_training_constructor_with_real_sdk(self):
    aiplatform.init(
        project='test-project',
        location='us-central1',
        credentials=AnonymousCredentials())
    with mock.patch.dict(
        'os.environ',
        BUCKET='bucket',
        TRAINING_SERVICE_ACCOUNT='trainer@example.com'), mock.patch.object(
            aiplatform.CustomJob, 'submit', autospec=True) as submit:
      job = workflow.submit_training(self.state, 'image@sha256:abc')
    self.assertIsInstance(job, aiplatform.CustomJob)
    self.assertEqual(submit.call_args.kwargs['service_account'],
                     'trainer@example.com')

  def test_endpoint_iam_preserves_other_bindings_and_etag(self):
    policy = policy_pb2.Policy(
        etag=b'etag',
        bindings=[
            policy_pb2.Binding(
                role='roles/viewer', members=['user:reader@example.com'])
        ])
    client = mock.Mock()
    client.get_iam_policy.return_value = policy
    with mock.patch(
        'google.cloud.aiplatform_v1.EndpointServiceClient',
        return_value=client):
      lifecycle.grant_predictor('endpoint', 'p', 'r',
                                'worker@p.iam.gserviceaccount.com',
                                'projects/p/roles/anomalyDetectionPredictor')
    result = client.set_iam_policy.call_args.kwargs['request']['policy']
    self.assertEqual(result.etag, b'etag')
    self.assertEqual(result.bindings[0].members, ['user:reader@example.com'])
    self.assertEqual(result.bindings[1].members,
                     ['serviceAccount:worker@p.iam.gserviceaccount.com'])
