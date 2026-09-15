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
