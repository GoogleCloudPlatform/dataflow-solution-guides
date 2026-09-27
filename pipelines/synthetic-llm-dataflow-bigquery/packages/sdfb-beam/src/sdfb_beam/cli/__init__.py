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
"""CLI entrypoints for sdfb-beam.

`run_pipeline.py` is the Flex Template `FLEX_TEMPLATE_PYTHON_PY_FILE`
target invoked by the python_template_launcher on Dataflow. The same
script also runs under DirectRunner with `--client_type=fake` or
`--client_type=mlx` for M4 smoke testing (see docs/M4_LOCAL_SMOKE.md).
"""
