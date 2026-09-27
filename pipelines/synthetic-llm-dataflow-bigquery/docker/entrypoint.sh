#!/bin/sh
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

# Dispatch entrypoint for the single SDFB image (ADR-0009).
#
# The same image serves two Dataflow roles that need DIFFERENT entrypoints:
#
#   * Flex Template LAUNCHER  -> /opt/google/dataflow/python_template_launcher
#       Run on the launcher VM. Invoked with template flags (e.g.
#       -template-container-args) or none; NEVER with Beam FnAPI boot flags.
#
#   * Runner v2 WORKER harness -> /opt/apache/beam/boot
#       Run on each worker. Dataflow runs the image ENTRYPOINT and appends the
#       Beam FnAPI boot flags (--id, --logging_endpoint, --control_endpoint,
#       --artifact_endpoint, --provision_endpoint). It does NOT override the
#       entrypoint — the earlier assumption that it did caused the worker
#       `sdk-0-0` CrashLoopBackOff: the launcher binary received --logging_endpoint
#       and died with "flag provided but not defined: -logging_endpoint".
#
# Discriminator: only the worker boot passes the FnAPI *_endpoint / --id flags,
# so if we see any of them, exec boot; otherwise exec the launcher.

for arg in "$@"; do
  case "$arg" in
    --id=* | --logging_endpoint=* | --control_endpoint=* | \
    --artifact_endpoint=* | --provision_endpoint=*)
      exec /opt/apache/beam/boot "$@"
      ;;
  esac
done

exec /opt/google/dataflow/python_template_launcher "$@"
