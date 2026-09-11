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
# Publishes synthetic gameplay events to the input Pub/Sub topic.
#
# Source the Terraform generated environment first:
#   source scripts/00_set_environment.sh
#
# Any extra arguments are passed through to generate_gameplay_events.py, so:
#   ./scripts/04_publish_events.sh --num_events=100 --rate=10
#   ./scripts/04_publish_events.sh --num_events=0            # continuous
#   ./scripts/04_publish_events.sh --inject_errors           # exercise the
#                                                            # dead-letter path

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for var in PROJECT INPUT_TOPIC; do
  if [ -z "${!var:-}" ]; then
    echo "ERROR: \$$var is not set. Run 'source scripts/00_set_environment.sh' first." >&2
    exit 1
  fi
done

# shellcheck source=/dev/null
source "${SCRIPT_DIR}/_python_env.sh"

echo "Publishing gameplay events."
echo "  project : ${PROJECT}"
echo "  topic   : ${INPUT_TOPIC}"
echo

python3 "${SCRIPT_DIR}/generate_gameplay_events.py" "$@"
