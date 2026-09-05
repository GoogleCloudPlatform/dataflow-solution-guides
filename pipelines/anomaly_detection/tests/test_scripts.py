#  Copyright 2025 Google LLC
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
"""Offline checks for launcher/build command boundaries; no Beam or cloud calls."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


class ScriptTests(unittest.TestCase):
  """Exercise scripts with captured commands instead of cloud submission."""

  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.capture = Path(self.tmp.name) / 'args'
    for name in ('python', 'gcloud'):
      stub = Path(self.tmp.name) / name
      stub.write_text('#!/bin/bash\nprintf "%s\\0" "$@" > "$CAPTURE"\n')
      stub.chmod(0o755)
    self.env = dict(os.environ)
    for name in ('SUBNETWORK', 'NETWORK', 'MODEL_LOCATION', 'MODEL_ENDPOINT'):
      self.env.pop(name, None)
    self.env.update(
        PATH=self.tmp.name + os.pathsep + os.environ['PATH'],
        CAPTURE=str(self.capture),
        PROJECT='test-project',
        REGION='us-central1',
        TEMP_LOCATION='gs://test-bucket/tmp',
        SERVICE_ACCOUNT='worker@test-project.iam.gserviceaccount.com',
        CONTAINER_URI='us-central1-docker.pkg.dev/test-project/dataflow-containers/image:0.1',
        INPUT_SUBSCRIPTION='projects/test-project/subscriptions/transactions-sub',
        OUTPUT_TOPIC='projects/test-project/topics/detections',
        MODEL_ENDPOINT='1234',
        MAX_DATAFLOW_WORKERS='1',
        DISK_SIZE_GB='200',
        MACHINE_TYPE='g2-standard-4')

  def run_script(self, name):
    return subprocess.run([str(SCRIPTS / name)],
                          env=self.env,
                          cwd='/tmp',
                          capture_output=True,
                          check=False,
                          text=True)

  def args(self):
    return self.capture.read_bytes().decode().rstrip('\0').split('\0')

  def test_subnet_cases(self):
    local = 'regions/us-central1/subnetworks/private'
    shared = ('https://www.googleapis.com/compute/v1/projects/host/'
              'regions/us-central1/subnetworks/shared')
    for variables, expected in [({}, None),
                                ({
                                    'SUBNETWORK': '',
                                    'NETWORK': ''
                                }, None), ({
                                    'SUBNETWORK': local
                                }, local), ({
                                    'SUBNETWORK': shared
                                }, shared), ({
                                    'NETWORK': local
                                }, local),
                                ({
                                    'SUBNETWORK': '',
                                    'NETWORK': local
                                }, local),
                                ({
                                    'SUBNETWORK': shared,
                                    'NETWORK': local
                                }, shared)]:
      with self.subTest(variables=variables):
        self.env.pop('SUBNETWORK', None)
        self.env.pop('NETWORK', None)
        self.env.update(variables)
        result = self.run_script('02_run_dataflow.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.args()
        self.assertEqual(
            [a for a in args if a.startswith('--subnetwork=')],
            [] if expected is None else ['--subnetwork=' + expected])
        for arg in [
            '--no_use_public_ip', '--streaming', '--location=us-central1',
            '--model_endpoint=1234',
            '--messages_subscription=' + self.env['INPUT_SUBSCRIPTION'],
            '--responses_topic=' + self.env['OUTPUT_TOPIC'],
            '--service_account_email=' + self.env['SERVICE_ACCOUNT'],
            '--sdk_container_image=' + self.env['CONTAINER_URI']
        ]:
          self.assertIn(arg, args)
        self.assertFalse(any('use_network_tags' in a for a in args))

  def test_endpoint_required_before_submission(self):
    self.env.pop('MODEL_ENDPOINT')
    result = self.run_script('02_run_dataflow.sh')
    self.assertNotEqual(result.returncode, 0)
    self.assertIn('MODEL_ENDPOINT', result.stderr)
    self.assertFalse(self.capture.exists())

  def test_model_region_override_and_quoting(self):
    self.env['MODEL_LOCATION'] = 'europe-west1'
    self.env['MODEL_ENDPOINT'] = 'value with spaces'
    self.assertEqual(self.run_script('02_run_dataflow.sh').returncode, 0)
    self.assertIn('--location=europe-west1', self.args())
    self.assertIn('--model_endpoint=value with spaces', self.args())

  def test_build(self):
    result = self.run_script('01_build_and_push_container.sh')
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertEqual(self.args(), [
        'builds', 'submit', '--quiet', '--project=test-project',
        '--region=us-central1',
        '--default-buckets-behavior=regional-user-owned-bucket',
        '--config=cloudbuild.yaml',
        '--substitutions=_TAG=' + self.env['CONTAINER_URI'], '.'
    ])
    self.assertIn('${_TAG}', (SCRIPTS.parent / 'cloudbuild.yaml').read_text())


if __name__ == '__main__':
  unittest.main()
