#!/usr/bin/env bash
set -euo pipefail
: "${PROJECT:?Source scripts/00_set_variables.sh}"
: "${REGION:?REGION is required}"
: "${SERVING_CONTAINER_URI:?SERVING_CONTAINER_URI is required}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."
gcloud builds submit --quiet --project="$PROJECT" --region="$REGION" \
  --default-buckets-behavior=regional-user-owned-bucket \
  --config=serving/cloudbuild.yaml --substitutions="_TAG=$SERVING_CONTAINER_URI" .
mkdir -p .deployment
gcloud artifacts docker images describe "$SERVING_CONTAINER_URI" \
  --quiet --project="$PROJECT" --format=json > .deployment/serving-image.json
python3.14 - <<'PY'
import json
from pathlib import Path
import shlex
record = json.loads(Path('.deployment/serving-image.json').read_text())
digest = record['image_summary']['fully_qualified_digest']
if '@sha256:' not in digest:
    raise ValueError('Artifact Registry did not return an immutable image digest')
Path('.deployment/serving_environment.sh').write_text(
    'export SERVING_CONTAINER_URI=' + shlex.quote(digest) + '\n')
PY
