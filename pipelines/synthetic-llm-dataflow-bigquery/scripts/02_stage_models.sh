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

# Stage the LLM + embedder weights into the models bucket, once, with Cloud
# Build. MODEL=gemma4-e4b-it (default) reads Kaggle credentials from Secret
# Manager — accept the Gemma license on Kaggle, then add the secret values:
#   printf '%s' "$KAGGLE_USERNAME" | gcloud secrets versions add kaggle-username --data-file=-
#   printf '%s' "$KAGGLE_KEY"      | gcloud secrets versions add kaggle-key --data-file=-
# MODEL=qwen3-4b needs no credentials (ModelScope).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -n "${PROJECT:-}" ]] || source "${SCRIPT_DIR}/00_set_variables.sh"
cd "${SCRIPT_DIR}/.."

gcloud builds submit \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --no-source \
  --config=cloudbuild_stage_models.yaml \
  --service-account="projects/${PROJECT}/serviceAccounts/${BUILD_SERVICE_ACCOUNT}" \
  --default-buckets-behavior=regional-user-owned-bucket \
  --substitutions="_BUCKET=${BUCKET},_MODEL=${MODEL}"

gcloud storage ls "${MODEL_URI}" "${EMBEDDER_URI}"
