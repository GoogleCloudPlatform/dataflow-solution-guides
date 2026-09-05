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
"""Verify worker identity and private subnet flags without submitting a job."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class LaunchTest(unittest.TestCase):
  """Run the shell wrapper with a capturing Python executable."""

  def test_private_launch_and_subnet_fallback(self):
    script = Path(__file__).resolve().parents[1] / 'scripts/02_run_dataflow.sh'
    for subnet, network, expected in [('subnet', 'legacy', 'subnet'),
                                      ('', 'legacy', 'legacy'), ('', '', None)]:
      with self.subTest(
          subnet=subnet,
          network=network), tempfile.TemporaryDirectory() as directory:
        capture = Path(directory) / 'args.txt'
        executable = Path(directory) / 'python'
        executable.write_text(
            '#!/bin/bash\n'
            'if [[ "$1" == "-c" ]]; then echo "3.14"; exit 0; fi\n'
            'printf "%s\\n" "$@" > "$CAPTURE"\n',
            encoding='utf-8')
        executable.chmod(0o755)
        environment = os.environ | dict(
            PATH=directory + os.pathsep + os.environ['PATH'],
            CAPTURE=str(capture),
            PROJECT='test-project',
            REGION='us-central1',
            MODEL_ENDPOINT='123',
            TEMP_LOCATION='gs://bucket/tmp',
            SERVICE_ACCOUNT='worker@test-project.iam.gserviceaccount.com',
            CONTAINER_URI='image:tag',
            INPUT_SUBSCRIPTION='input',
            OUTPUT_TOPIC='output',
            ERROR_TOPIC='errors',
            BIGTABLE_INSTANCE='instance',
            BIGQUERY_TABLE='project.dataset.table',
            MAX_DATAFLOW_WORKERS='1',
            DISK_SIZE_GB='200',
            MACHINE_TYPE='n1-standard-2',
            SUBNETWORK=subnet,
            NETWORK=network)
        subprocess.run(['bash', str(script)],
                       env=environment,
                       check=True,
                       capture_output=True)
        arguments = capture.read_text(encoding='utf-8').splitlines()
        self.assertIn('--no_use_public_ips', arguments)
        self.assertIn(
            '--service_account_email=worker@test-project.iam.gserviceaccount.com',
            arguments)
        self.assertEqual(
            sum(arg.startswith('--project=') for arg in arguments), 1)
        subnet_flags = [
            arg for arg in arguments if arg.startswith('--subnetwork=')
        ]
        self.assertEqual(subnet_flags,
                         [f'--subnetwork={expected}'] if expected else [])

  def test_rejects_non_314_python(self):
    script = Path(__file__).resolve().parents[1] / 'scripts/02_run_dataflow.sh'
    with tempfile.TemporaryDirectory() as directory:
      executable = Path(directory) / 'python'
      executable.write_text(
          '#!/bin/bash\n'
          'if [[ "$1" == "-c" ]]; then echo "3.12"; exit 0; fi\n'
          'exit 0\n',
          encoding='utf-8')
      executable.chmod(0o755)
      environment = os.environ | dict(
          PATH=directory + os.pathsep + os.environ['PATH'],
          PROJECT='test-project',
          REGION='us-central1',
          MODEL_ENDPOINT='123',
          TEMP_LOCATION='gs://bucket/tmp',
          SERVICE_ACCOUNT='worker@test-project.iam.gserviceaccount.com',
          CONTAINER_URI='image:tag',
          INPUT_SUBSCRIPTION='input',
          OUTPUT_TOPIC='output')
      result = subprocess.run(['bash', str(script)],
                              env=environment,
                              capture_output=True,
                              text=True,
                              check=False)
      self.assertNotEqual(result.returncode, 0)
      self.assertIn('Python 3.14 is required', result.stderr)
