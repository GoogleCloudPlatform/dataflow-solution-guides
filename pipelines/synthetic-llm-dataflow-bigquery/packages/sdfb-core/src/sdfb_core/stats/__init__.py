"""Source-table statistics — inputs to generation, persisted for humans.

Pure sdfb-core: no Beam, no GCP. The Beam driver computes these from the
eager reference read it already pays for and persists them via
``sdfb_beam.io.stats_store`` (2026-08-05 spec, WS-B).
"""

from sdfb_core.stats.source_stats import (
    PROFILER_VERSION,
    profile_source_table,
    stats_rows,
)

__all__ = ["PROFILER_VERSION", "profile_source_table", "stats_rows"]
