"""Base class for dynamically-derived synthetic record models.

The concrete record class for a given table is built at runtime by
`sdfb_core.codegen.derive_pydantic.derive_record_model(table_schema)`. This
module just defines the marker base class and shared configuration.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class GeneratedRecord(BaseModel):
  """Marker base for all dynamically-derived synthetic record models.

    Subclasses are created via `pydantic.create_model` at runtime, one per
    target BigQuery table. They inherit configuration defaults from this
    class:

      - `extra='forbid'`: reject unknown columns at validation time. A
        synthetic record with an extra key is a bug, not a feature.
      - `validate_assignment=True`: re-validate on attribute mutation, so
        post-construction edits stay schema-conformant.
      - Pydantic v2 default lenient type coercion is intentionally kept;
        LLMs emit JSON-strings for timestamps and Decimals, and we want
        those parsed transparently.
    """

  model_config = ConfigDict(
      extra="forbid",
      validate_assignment=True,
      frozen=False,
  )
