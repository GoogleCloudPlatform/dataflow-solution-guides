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
"""The `sdfb-eval` command line: `main` parses and dispatches, `driver`
plans and runs one evaluation (registry events around the pipeline),
`planview` prints a plan, `gate` turns a finished run into an exit code,
`fixture` plans rows from JSON files offline, and `run_evaluation` is
the Dataflow flex-template entry.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""
