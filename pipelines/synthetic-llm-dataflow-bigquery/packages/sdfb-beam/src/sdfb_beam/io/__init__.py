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
"""I/O transforms for the synthesis pipeline.

  - `digest`        — canonical reference-row digest (provenance)
  - `local_sinks`   — JSONL file sinks for DirectRunner / local dev
  - `bq_sources`    — driver-side BigQuery reference reader        (M1 §11)
  - `bq_sinks`      — BigQuery `FILE_LOADS` write transforms       (M1 §11)
"""

from sdfb_beam.io.bq_sources import load_reference_rows
from sdfb_beam.io.digest import compute_reference_digest
from sdfb_beam.io.local_sinks import WriteToJsonLines

__all__ = [
    "WriteToJsonLines", "compute_reference_digest", "load_reference_rows"
]
