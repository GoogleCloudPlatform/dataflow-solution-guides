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
# Shared helper, sourced by the Python wrapper scripts in this directory. It is
# not meant to be executed on its own.
#
# Creates (once) and activates a virtual environment under scripts/.venv with
# the local tooling dependencies installed. Using a dedicated environment keeps
# this guide from disturbing the operator's global interpreter, and means the
# guide runs on a clean workstation without any manual pip step.
#
# Nothing installed here ever reaches a Dataflow worker: the worker
# dependencies are baked into the container built by
# 01_build_and_push_container.sh.

set -euo pipefail

_PYTHON_ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_VENV_DIR="${_PYTHON_ENV_DIR}/.venv"
_REQUIREMENTS="${_PYTHON_ENV_DIR}/requirements-tools.txt"
_STAMP="${_VENV_DIR}/.requirements.sha256"

if [ ! -d "${_VENV_DIR}" ]; then
  echo "Creating the helper virtual environment in ${_VENV_DIR}"
  python3 -m venv "${_VENV_DIR}"
fi

# shellcheck disable=SC1091
source "${_VENV_DIR}/bin/activate"

# Reinstall only when requirements-tools.txt has actually changed, so that
# re-running a script is fast and works offline.
_CURRENT_HASH="$(sha256sum "${_REQUIREMENTS}" | cut -d' ' -f1)"
if [ ! -f "${_STAMP}" ] || [ "$(cat "${_STAMP}")" != "${_CURRENT_HASH}" ]; then
  echo "Installing helper dependencies from $(basename "${_REQUIREMENTS}")"
  pip install --quiet --upgrade pip
  pip install --quiet -r "${_REQUIREMENTS}"
  echo "${_CURRENT_HASH}" >"${_STAMP}"
fi
