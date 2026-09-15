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
