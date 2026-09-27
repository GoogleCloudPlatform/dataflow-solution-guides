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
"""Test suite for synthetic-dataflow-bigquery.

Unit tests run on the laptop by default. Tests marked `@pytest.mark.gpu` or
`@pytest.mark.gcp` are skipped unless explicitly selected — they require the
M4 Pro with GPU access or live GCP credentials.

This `src/sdfb_tests/` package holds reusable test helpers (fake clients,
fixtures, hypothesis strategies). Actual pytest discovery happens in the
sibling `tests/` directory.
"""
