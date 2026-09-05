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
set -euo pipefail
: "${PROJECT:?Source scripts/00_set_variables.sh}"
: "${REGION:?REGION is required}"
: "${TRAINING_CONTAINER_URI:?TRAINING_CONTAINER_URI is required}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."
gcloud builds submit --quiet --project="$PROJECT" --region="$REGION" \
  --default-buckets-behavior=regional-user-owned-bucket \
  --config=training/cloudbuild.yaml --substitutions="_TAG=$TRAINING_CONTAINER_URI" .
mkdir -p .deployment
gcloud artifacts docker images describe "$TRAINING_CONTAINER_URI" \
  --quiet --project="$PROJECT" --format=json > .deployment/training-image.json
python - <<'PY'
import json
from pathlib import Path
import shlex
record = json.loads(Path('.deployment/training-image.json').read_text())
digest = record['image_summary']['fully_qualified_digest']
if '@sha256:' not in digest:
    raise ValueError('Artifact Registry did not return an immutable image digest')
Path('.deployment/training_environment.sh').write_text(
    'export TRAINING_CONTAINER_URI=' + shlex.quote(digest) + '\n')
PY
