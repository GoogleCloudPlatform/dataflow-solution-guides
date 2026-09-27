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
"""B.2 library-wrapper generation engine (M1 §6).

Importing this package registers ``B2LibraryEngine`` under the name
``"b2_library"`` in the engine registry, so the Beam DAG can resolve it from
a CLI flag via ``sdfb_core.engines.get_engine("b2_library")``.

Design: ADR 0013 (the LLM-as-distribution-estimator spine). The chosen library
is ``sdgx`` (Apache-2.0); rationale + the deferred SDV upgrade path are in
this package's ``README.md`` (formerly ``SPIKE_LIBRARY_CHOICE.md``).

Design: docs/DESIGN.md §2 Engines
(ADR 0013).
"""

from __future__ import annotations

from sdfb_core.engines import register_engine
from sdfb_core.engines.b2_library.engine import B2LibraryEngine

register_engine("b2_library", B2LibraryEngine)

__all__ = ["B2LibraryEngine"]
