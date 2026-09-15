"""Codegen: `TableSchema` → Pydantic record model, BigQuery TableSchema dict.

All derivations flow one direction (`TableSchema` is the source of truth).
Never hand-edit a derived artifact — regenerate from the `TableSchema`.

The Pandera derivation lives in `sdfb_beam.codegen.derive_pandera` rather
than here because `pandera` + `pandas` belong to the Beam-layer dep
surface; `sdfb_core` stays installable on the laptop with `pydantic` alone.
"""

from sdfb_core.codegen.derive_bq_ddl import (
    derive_bq_field,
    derive_bq_load_schema,
    derive_bq_schema,
)
from sdfb_core.codegen.derive_pydantic import derive_record_model

__all__ = [
    "derive_bq_field",
    "derive_bq_load_schema",
    "derive_bq_schema",
    "derive_record_model",
]
