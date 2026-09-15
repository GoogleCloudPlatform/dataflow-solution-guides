"""B.2 library-wrapper generation engine (M1 §6).

Importing this package registers ``B2LibraryEngine`` under the name
``"b2_library"`` in the engine registry, so the Beam DAG can resolve it from
a CLI flag via ``sdfb_core.engines.get_engine("b2_library")``.

Design: ADR 0013 (the LLM-as-distribution-estimator spine). The chosen library
is ``sdgx`` (Apache-2.0); rationale + the deferred SDV upgrade path are in
this package's ``README.md`` (formerly ``SPIKE_LIBRARY_CHOICE.md``).
"""

from __future__ import annotations

from sdfb_core.engines import register_engine
from sdfb_core.engines.b2_library.engine import B2LibraryEngine

register_engine("b2_library", B2LibraryEngine)

__all__ = ["B2LibraryEngine"]
