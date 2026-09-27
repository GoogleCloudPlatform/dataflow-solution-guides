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
