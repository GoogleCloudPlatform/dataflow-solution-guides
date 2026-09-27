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
