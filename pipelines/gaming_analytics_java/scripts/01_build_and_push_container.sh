#!/usr/bin/env bash
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
#
# Builds the custom Python SDK harness container and publishes it to Artifact
# Registry. The recommendation model is trained during the build and baked into
# the image, so this step must run before the pipeline is launched.
#
# Source the Terraform generated environment first:
#   source scripts/00_set_environment.sh

set -euo pipefail

for var in PROJECT REGION CONTAINER_URI; do
  if [ -z "${!var:-}" ]; then
    echo "ERROR: \$$var is not set. Run 'source scripts/00_set_environment.sh' first." >&2
    exit 1
  fi
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_DIR="$(dirname "${SCRIPT_DIR}")"

# Must match the path the pipeline reads at run time. Both default to the same
# value; 03_launch_pipeline.sh passes it through as --modelUri.
MODEL_PATH="${MODEL_PATH:-/opt/gaming_analytics/recommender.pkl}"

echo "Building the Python SDK harness container for the gaming analytics pipeline."
echo "  image           : ${CONTAINER_URI}"
echo "  model path      : ${MODEL_PATH}"
echo "  training rows   : ${TRAINING_SAMPLES:-20000}"
echo "  random seed     : ${RANDOM_SEED:-42}"
echo

gcloud builds submit "${PIPELINE_DIR}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --config="${PIPELINE_DIR}/cloudbuild.yaml" \
  --default-buckets-behavior=regional-user-owned-bucket \
  --substitutions="_TAG=${CONTAINER_URI},_MODEL_PATH=${MODEL_PATH},_TRAINING_SAMPLES=${TRAINING_SAMPLES:-20000},_RANDOM_SEED=${RANDOM_SEED:-42}"

echo
echo "Published ${CONTAINER_URI}"
echo "Next: ./scripts/02_populate_bigtable.sh"
