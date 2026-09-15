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

# Publish the Flex Template spec that points at the pushed image. The
# parameter contract is docker/flex_template_metadata.json.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -n "${PROJECT:-}" ]] || source "${SCRIPT_DIR}/00_set_variables.sh"
cd "${SCRIPT_DIR}/.."

gcloud dataflow flex-template build "${TEMPLATE_PATH}" \
  --project="${PROJECT}" \
  --image="${CONTAINER_URI}" \
  --sdk-language=PYTHON \
  --metadata-file=docker/flex_template_metadata.json

echo "template: ${TEMPLATE_PATH}"
