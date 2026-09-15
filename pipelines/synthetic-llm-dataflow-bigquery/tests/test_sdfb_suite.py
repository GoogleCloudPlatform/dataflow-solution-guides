#  Copyright 2026 The synthetic-llm-dataflow-bigquery Authors
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
"""Runs the full laptop suite from the path DSG CI collects (`tests/`).

The suite lives in `packages/sdfb-tests/tests` and locates repository files
relative to that location, so it cannot be moved or symlinked here. GPU and
GCP tests are excluded exactly as in the golden source's own CI.
"""

import pathlib
import subprocess
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_sdfb_suite_passes():
  result = subprocess.run(
      [
          sys.executable, "-m", "pytest", "packages/sdfb-tests/tests", "-m",
          "not gpu and not gcp", "-q", "-p", "no:cacheprovider"
      ],
      cwd=_ROOT,
      check=False,
  )
  assert result.returncode == 0
