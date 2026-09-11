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
# Seeds the Cloud Bigtable feature store with player profiles. The pipeline
# enriches every gameplay event with these features before scoring it, so this
# must run before events are published, otherwise every lookup misses.
#
# Source the Terraform generated environment first:
#   source scripts/00_set_environment.sh
#
# Any extra arguments are passed through to populate_bigtable.py.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for var in PROJECT BIGTABLE_INSTANCE BIGTABLE_TABLE; do
  if [ -z "${!var:-}" ]; then
    echo "ERROR: \$$var is not set. Run 'source scripts/00_set_environment.sh' first." >&2
    exit 1
  fi
done

# shellcheck source=/dev/null
source "${SCRIPT_DIR}/_python_env.sh"

echo "Seeding the Bigtable feature store."
echo "  project  : ${PROJECT}"
echo "  instance : ${BIGTABLE_INSTANCE}"
echo "  table    : ${BIGTABLE_TABLE}"
echo

python3 "${SCRIPT_DIR}/populate_bigtable.py" "$@"

echo
echo "Next: ./scripts/03_launch_pipeline.sh"
