#!/usr/bin/env bash
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
python3.14 - <<'PY'
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
