#!/usr/bin/env bash
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

# Build the single launcher + worker image with Cloud Build and push it to
# Artifact Registry. Run from anywhere; takes ~30-40 min on a cold cache.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -n "${PROJECT:-}" ]] || source "${SCRIPT_DIR}/00_set_variables.sh"
cd "${SCRIPT_DIR}/.."

GIT_COMMIT="$(python3 -c 'import json; print(json.load(open(".sync-source.json"))["sha"][:12])' 2>/dev/null || echo unknown)"

gcloud builds submit \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --config=cloudbuild.yaml \
  --service-account="projects/${PROJECT}/serviceAccounts/${BUILD_SERVICE_ACCOUNT}" \
  --default-buckets-behavior=regional-user-owned-bucket \
  --substitutions="_TAG=${CONTAINER_URI},_GIT_COMMIT=${GIT_COMMIT}" \
  .
