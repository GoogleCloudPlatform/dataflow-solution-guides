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
"""The evaluator version stamped onto every registry row.

`evaluation_key = blake2b(generation_job_id | run_ids | tables,
catalogue_version, evaluator_version, mode, knobs)` — bumping this value
changes the key for otherwise-identical inputs, so it moves only when a
change to this package would change a metric's value.
"""

EVALUATOR_VERSION = "0.1.0"
