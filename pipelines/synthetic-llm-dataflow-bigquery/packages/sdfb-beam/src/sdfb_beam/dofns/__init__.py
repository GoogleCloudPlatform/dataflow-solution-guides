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
"""Mode A in-pipeline DoFns.

  - `generate`        — engine + `ModelClient` invocation
  - `validate_record` — per-record Pydantic check (line 1 of defense)
  - `pandera_batch`   — per-batch Pandera check (line 2 of defense)
  - `uniqueness`      — row/identity duplicate diversion (line 3 of defense)
  - `whylogs_profile` — mergeable profile combiner                  (M1 §11)
"""

from sdfb_beam.dofns.generate import GenerateRecordsDoFn
from sdfb_beam.dofns.pandera_batch import PanderaValidateBatchDoFn
from sdfb_beam.dofns.uniqueness import EnforceUniqueness
from sdfb_beam.dofns.validate_record import ValidateRecordDoFn

__all__ = [
    "EnforceUniqueness",
    "GenerateRecordsDoFn",
    "PanderaValidateBatchDoFn",
    "ValidateRecordDoFn",
]
