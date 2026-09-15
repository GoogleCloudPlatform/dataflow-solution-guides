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
