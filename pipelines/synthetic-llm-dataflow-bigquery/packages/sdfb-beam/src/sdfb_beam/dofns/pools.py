"""Build free-text pools ONCE, in their own branch (WS5 §2).

Mirrors the rag_chunks population branch: an independent stage running
concurrently with Generate, writing an artifact keyed on the reference
digest. Without it every autoscaled worker process rebuilt all pools —
108 rebuilds and 68,805 s of LLM service time on the 2026-07-26 1M run,
because `_POOL_CACHE` lives for exactly one worker process.
"""

from __future__ import annotations

import logging
import time
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

import apache_beam as beam
from sdfb_core.engines import get_engine
from sdfb_core.observability import log_milestone, milestone_scope
from sdfb_core.pools import FreeTextPool

from sdfb_beam.dofns.localize import localize_embedder
from sdfb_beam.pools.store import pool_to_row

if TYPE_CHECKING:  # pragma: no cover - typing only
  from collections.abc import Iterator


class BuildFreeTextPoolsDoFn(beam.DoFn):
  """Run the pool ladder once and emit one `freetext_pools` row per column.

    The engine is built in `setup()` (never in `process()`) exactly as
    `GenerateRecordsDoFn` does; `process` only serialises what the ladder
    already produced.
    """

  def __init__(
      self,
      engine_name: str,
      model_client,
      ctx,
      store=None,
      source_value_store=None,
  ) -> None:
    super().__init__()
    self.engine_name = engine_name
    self.model_client = model_client
    self.ctx = ctx
    # The branch's own write path (2026-07-29): rows land via a blocking
    # `store.write_rows` BEFORE they are emitted, so the DAG gate fed by
    # this DoFn's output releases Generate only once a store fetch hits.
    # Distinct from ctx.pool_store, which setup() blanks (self-read guard).
    self.store = store
    # Full-domain rejection (2026-08-05 B_TABLE R1: pools memorized
    # 33-99% of 10 columns). Attached worker-side onto the ctx so the
    # engine's ladder rejects against the whole source, not the sample.
    self.source_value_store = source_value_store
    self._engine: Any = None

  def setup(self) -> None:
    # A gs:// embedder_uri must become a worker-local path BEFORE the
    # engine builds its embedder (2026-07-28 R1: skipping this handed
    # the raw gs:// URI to AutoTokenizer.from_pretrained and killed the
    # job on HFValidationError). Shared with GenerateRecordsDoFn.
    ctx = localize_embedder(self.ctx)
    # The build branch must never read its own output — otherwise it
    # would short-circuit itself into writing nothing on a re-run.
    ctx = ctx.model_copy(
        update={
            "pool_store": None,
            "freetext_pools_table": "",
            "source_value_store": self.source_value_store,
            "pool_branch": True,
        })
    self.ctx = ctx
    engine_class = get_engine(self.engine_name)
    self._engine = engine_class()
    t0 = time.monotonic()
    with self._scope():
      self._engine.setup(self.model_client, ctx)
      log_milestone(
          "pool_branch_setup_done",
          seconds=round(time.monotonic() - t0, 1),
          engine=self.engine_name,
      )

  def _scope(self):
    prefix = getattr(self.ctx, "log_table_prefix", "")
    return milestone_scope(prefix) if prefix else nullcontext()

  # pylint: disable-next=arguments-renamed  # Beam passes the element positionally
  def process(self, element) -> Iterator[dict]:
    del element  # the single trigger element carries no data
    with self._scope():
      yield from self._process_scoped()

  def _process_scoped(self) -> Iterator[dict]:
    pools = getattr(self._engine, "_free_text_pools", None) or {}
    build_info = getattr(self._engine, "_pool_build_info", None) or {}
    rows = []
    for column, values in pools.items():
      if not values:
        continue
      info = build_info.get(column, {})
      rows.append(
          pool_to_row(
              FreeTextPool(
                  reference_digest=self.ctx.reference_digest,
                  model_uri=self.ctx.model_uri,
                  column=column,
                  target=int(info.get("target", len(values))),
                  values=tuple(values),
                  stagnated=bool(info.get("stagnated", False)),
                  attempts=int(info.get("attempts", 0)),
              )))
    if self.store is not None and rows:
      t_write = time.monotonic()
      try:
        self.store.write_rows(rows)
        log_milestone(
            "freetext_pool_store_written",
            rows=len(rows),
            seconds=round(time.monotonic() - t_write, 1),
        )
      except Exception as e:  # pylint: disable=broad-exception-caught
        # Pools are an optimisation, never a dependency: emit the
        # rows anyway so the AwaitFreeTextPools gate opens and
        # Generate falls back to building pools itself.
        log_milestone(
            "freetext_pool_store_write_error",
            level=logging.WARNING,
            error=type(e).__name__,
        )
    yield from rows
    log_milestone("pool_branch_emitted", columns=len(pools))

  def teardown(self) -> None:
    if self._engine is not None:
      self._engine.teardown()


__all__ = ["BuildFreeTextPoolsDoFn"]
