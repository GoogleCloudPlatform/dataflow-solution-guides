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
# With an argument, validate the downloaded managed-training artifact.
# Without one, build/train locally for the CI compatibility gate.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ $# -gt 1 ]]; then
  echo 'Usage: verify_serving_container.sh [model-directory]' >&2
  exit 2
fi
model_dir="${1:-$PWD/.deployment/model}"
mkdir -p "$model_dir"
model_dir="$(cd "$model_dir" && pwd)"

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  if [[ $# -eq 0 ]]; then
    docker build -f training/Dockerfile -t anomaly-training-check .
    docker run --rm --user "$(id -u):$(id -g)" \
      -v "$model_dir:/model" -e AIP_MODEL_DIR=/model anomaly-training-check
  fi
  docker build -f serving/Dockerfile -t anomaly-serving-check .
  serving_digest="$(docker image inspect anomaly-serving-check --format '{{index .Id}}')"
  docker run --rm --user "$(id -u):$(id -g)" \
    -e "SERVING_DIGEST=$serving_digest" \
    -v "$model_dir:/model" -v "$PWD/training/verify_model.py:/verify_model.py:ro" \
    --entrypoint python \
    anomaly-serving-check /verify_model.py /model
else
  PYTHON_BIN="python3.14"
  if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
    PYTHON_BIN="${VIRTUAL_ENV}/bin/python"
  elif [[ -x "$PWD/.venv/bin/python" ]]; then
    PYTHON_BIN="$PWD/.venv/bin/python"
  fi
  export PYTHONPATH="${PYTHONPATH:-}:$PWD"
  if [[ $# -eq 0 ]]; then
    AIP_MODEL_DIR="$model_dir" "$PYTHON_BIN" training/train.py
  fi
  "$PYTHON_BIN" training/verify_model.py "$model_dir"
fi
