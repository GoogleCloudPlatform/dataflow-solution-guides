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
