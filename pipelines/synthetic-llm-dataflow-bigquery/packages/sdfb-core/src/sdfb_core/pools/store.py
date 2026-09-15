"""The `FreeTextPoolStore` seam (WS5 §2.1).

Analogous to `ChunkStore`: the engine depends on this Protocol, the
BigQuery-backed implementation lives in `sdfb_beam.pools.store` so
`sdfb-core` stays GCP-free, and tests inject `InMemoryFreeTextPoolStore`.

A SIBLING of `ChunkStore`, not a `chunk_kind` of it — pools key on
`model_uri` (which LLM produced the values), chunks key on `embedder_id` /
`embedder_version` (which vector space). One fetch signature cannot serve
both without lying about one of them.
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
  from collections.abc import Iterable

  from sdfb_core.pools.record import FreeTextPool


@runtime_checkable
class FreeTextPoolStore(Protocol):
  """Store surface over `synthetic_rag.freetext_pools`."""

  def fetch(self, reference_digest: str, model_uri: str) -> list[FreeTextPool]:
    """Every persisted pool for this reference sample + LLM."""

  def exists(self, reference_digest: str, model_uri: str) -> bool:
    """True when ANY pool exists (the build stage's idempotency check)."""

  def write_rows(self, rows: list[dict]) -> None:
    """Append `pool_to_row`-shaped rows, blocking until they are
        readable by a subsequent `fetch` (the build branch's write path —
        the pipeline gate downstream relies on this blocking contract)."""


@runtime_checkable
class SourceValueStore(Protocol):
  """A column's FULL distinct source values, for pool novelty rejection.

    The 2026-08-05 B_TABLE R1 run landed 33-99% verbatim source values on
    10 free-text columns because the pool ladder rejects only against the
    profiled sample; this seam lets the ladder reject against the whole
    source domain. The BigQuery implementation lives in
    `sdfb_beam.io.source_values` — `sdfb-core` stays GCP-free.
    """

  def fetch_distinct(self, column: str) -> frozenset[str] | None:
    """Every distinct non-NULL value of `column`, as strings — or None
        when the column's cardinality exceeds the store's cap (the caller
        must then behave as if no store were attached, loudly)."""

  # OPTIONAL extension (wave-4 v2, duck-typed via getattr so existing
  # implementations stay valid): `fetch_frequent(column, min_count)
  # -> frozenset[str] | None` returns the values shared by at least
  # `min_count` SOURCE rows — the k-anonymous enum mass the numeric
  # collision scrub must keep exact. Implementations without it fall
  # back to the caller's sample-side heuristic.


class InMemoryFreeTextPoolStore:
  """List-backed `FreeTextPoolStore` for tests and laptop runs."""

  def __init__(self, pools: Iterable[FreeTextPool] = ()) -> None:
    self._pools: list[FreeTextPool] = list(pools)

  def add(self, pools: Iterable[FreeTextPool]) -> None:
    self._pools.extend(pools)

  def fetch(self, reference_digest: str, model_uri: str) -> list[FreeTextPool]:
    return [
        p for p in self._pools
        if p.reference_digest == reference_digest and p.model_uri == model_uri
    ]

  def exists(self, reference_digest: str, model_uri: str) -> bool:
    return any(
        p.reference_digest == reference_digest and p.model_uri == model_uri
        for p in self._pools)

  def write_rows(self, rows: list[dict]) -> None:
    from sdfb_core.pools.record import FreeTextPool

    self._pools.extend(
        FreeTextPool(
            reference_digest=r["reference_digest"],
            model_uri=r["model_uri"],
            column=r["column"],
            target=int(r["target"]),
            values=tuple(r["values"] or ()),
            stagnated=bool(r["stagnated"]),
            attempts=int(r["attempts"]),
        ) for r in rows)


__all__ = ["FreeTextPoolStore", "InMemoryFreeTextPoolStore", "SourceValueStore"]
