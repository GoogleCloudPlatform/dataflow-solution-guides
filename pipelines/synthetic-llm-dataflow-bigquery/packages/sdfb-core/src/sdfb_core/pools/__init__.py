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
"""Persisted free-text pools (WS5).

Pools are inferred ONCE per (reference_digest, model_uri) and read
thereafter, instead of being rebuilt inside every worker's `DoFn.setup()`.
Protocol and record live here; the BigQuery implementation is in
`sdfb_beam.pools.store`.
"""

from sdfb_core.pools.record import FreeTextPool
from sdfb_core.pools.store import (
    FreeTextPoolStore,
    InMemoryFreeTextPoolStore,
    SourceValueStore,
)

__all__ = [
    "FreeTextPool",
    "FreeTextPoolStore",
    "InMemoryFreeTextPoolStore",
    "SourceValueStore",
]
