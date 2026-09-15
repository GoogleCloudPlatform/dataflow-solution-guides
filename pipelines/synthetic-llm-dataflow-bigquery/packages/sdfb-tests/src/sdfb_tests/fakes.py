"""Test fakes for the `GenerationEngine` + `ModelClient` contracts.

Re-exports `FakeModelClient` from its production home in `sdfb_beam`,
plus defines `MinimalEngine` — the smallest engine that exercises the
`GenerationEngine` ABC. `MinimalEngine` is a test driver only; real
engines (B.1 RAG, B.2 library-wrapper) live in their respective
worktrees under `sdfb_core/engines/`.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=broad-exception-caught,wrong-import-position

from __future__ import annotations

import random
from collections.abc import Iterator

from sdfb_beam.handlers.fake_client import FakeModelClient
from sdfb_core.codegen import derive_record_model
from sdfb_core.contracts import GeneratedRecord
from sdfb_core.engines.base import (
    GenerationConfig,
    GenerationContext,
    GenerationEngine,
    ModelClient,
)


class MinimalEngine(GenerationEngine):
  """Smallest engine satisfying the `GenerationEngine` contract.

    Strategy: for each requested record, pick a reference row (seeded
    RNG), ask the `ModelClient` to "perturb" it, validate the response
    through the derived record model, yield on success.

    Test driver — not a production engine. B.1 (RAG) and B.2
    (library-wrapper) are the real engines.
    """

  name = "minimal"

  def __init__(self) -> None:
    self._client: ModelClient | None = None
    self._ctx: GenerationContext | None = None
    self._record_model: type[GeneratedRecord] | None = None

  def setup(self, model_client: ModelClient, ctx: GenerationContext) -> None:
    if self._record_model is not None:
      return  # idempotent
    self._client = model_client
    self._ctx = ctx
    self._record_model = derive_record_model(ctx.table_schema)

  def generate_batch(
      self,
      n: int,
      cfg: GenerationConfig,
  ) -> Iterator[GeneratedRecord]:
    if self._record_model is None or self._client is None or self._ctx is None:
      raise RuntimeError("MinimalEngine.generate_batch called before setup() "
                         "(or after teardown()).")
    if not self._ctx.reference_rows:
      return

    rng = random.Random(cfg.seed)
    budget = n * (cfg.max_retries + 1)
    emitted = 0
    for _ in range(budget):
      if emitted >= n:
        return
      anchor_idx = rng.randrange(len(self._ctx.reference_rows))
      anchor = self._ctx.reference_rows[anchor_idx]
      candidates = self._client.generate_json(
          prompt=f"perturb similarity={cfg.similarity} anchor={anchor}",
          json_schema={},
          n=1,
          seed=cfg.seed,
      )
      for raw in candidates:
        try:
          yield self._record_model.model_validate(raw)
          emitted += 1
          if emitted >= n:
            return
        except Exception:
          continue

  def teardown(self) -> None:
    self._client = None
    self._ctx = None
    self._record_model = None


# Auto-register MinimalEngine at import time so the Beam DAG can look it
# up via `sdfb_core.engines.get_engine("minimal")` after the pipeline
# driver imports this module.
from sdfb_core.engines import register_engine  # noqa: E402

register_engine("minimal", MinimalEngine)

__all__ = ["FakeModelClient", "MinimalEngine"]
