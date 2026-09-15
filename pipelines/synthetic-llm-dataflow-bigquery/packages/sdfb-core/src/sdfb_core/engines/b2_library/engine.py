"""``B2LibraryEngine`` — the B.2 library-wrapper synthesis engine (M1 §6).

Implements the shared FASTGEN spine (ADR 0013, design §2/§4):

  setup(model_client, ctx):       # once per Beam worker, idempotent
      profile reference_rows → per-column ColumnProfile
      fit the statistical backend (sdgx CTGAN, NumPy-empirical fallback) ONCE
  generate_batch(n, cfg):         # many times; cheap
      vectorized-sample n rows from the fitted backend (seeded)
      enforce fidelity (constant/range/enum clamps, LOCAL to b2_library)
      patch FREE_TEXT / JSON / high-cardinality columns via the ModelClient hook
      yield GeneratedRecord
  teardown():                     # release the fitted model

The engine is built in ``DoFn.setup()`` and must pickle across the worker
boundary: all state is either plain data (profiles), a NumPy-only object
(EmpiricalBackend), or an sdgx ``Synthesizer`` (picklable). The bulk
sampling is CPU/NumPy — only the bounded free-text pool touches the GPU.

PURE sdfb-core: no ``apache_beam``, no ``vllm``, no GCP. ``sdgx`` (and
torch) are imported lazily inside the backend's ``fit()`` so importing this
module works on a laptop with only base deps installed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import numpy as np

from sdfb_core.codegen import derive_record_model
from sdfb_core.contracts import GeneratedRecord
from sdfb_core.engines.b2_library.backends import EmpiricalBackend, SdgxBackend
from sdfb_core.engines.b2_library.fidelity import (
    ColumnKind,
    ColumnProfile,
    enforce_value,
    profile_table,
)
from sdfb_core.engines.b2_library.freetext import FreeTextHook
from sdfb_core.engines.base import (
    GenerationConfig,
    GenerationContext,
    GenerationEngine,
    ModelClient,
    apply_conditional_overrides,
    conditional_draws,
)
from sdfb_core.engines.fanout import FanoutPlan, expand_keys
from sdfb_core.engines.fk_keys import bind_fk_key_pools
from sdfb_core.engines.generation_plan import (
    build_constraints_detail,
    build_plan,
    build_plan_detail,
    log_plan_pretty,
    should_log_plan,
)
from sdfb_core.observability import log_milestone


class B2LibraryEngine(GenerationEngine):
  """Library-wrapper engine: fit-once statistical model + free-text LLM hook.

    The backend is chosen at ``setup`` time. ``use_sdgx`` (default ``True``)
    attempts the production ``sdgx`` CTGAN fit and transparently falls back
    to the pure-NumPy :class:`EmpiricalBackend` when ``sdgx`` is not
    installed (the laptop / offline case). Set ``use_sdgx=False`` to force
    the deterministic NumPy backend — this is what the ABC contract tests
    use so reproducibility is bit-for-bit.
    """

  name = "b2_library"

  def __init__(self, *, use_sdgx: bool = True) -> None:
    self._use_sdgx = use_sdgx
    self._ctx: GenerationContext | None = None
    self._record_model: type[GeneratedRecord] | None = None
    self._profiles: dict = {}
    self._free_text_cols: list[str] = []
    self._backend = None  # SamplingBackend | None
    self._freetext_hook: FreeTextHook | None = None
    self._fitted: bool = False
    # Enforced FK edges (ADR 0031) — joint parent-key tuple pools.
    self._fk_key_pools: list = []
    # A DRIVEN child's recipe (ADR 0036), bound in setup() from
    # ``ctx.fanout``; None keeps generate_batch's num_rows-driven path.
    self._fanout: FanoutPlan | None = None

  # -- lifecycle ----------------------------------------------------------

  def setup(self, model_client: ModelClient, ctx: GenerationContext) -> None:
    if self._fitted:
      return  # idempotent — never re-fit / re-profile.

    self._ctx = ctx
    self._record_model = derive_record_model(ctx.table_schema)
    self._profiles = profile_table(ctx.table_schema, ctx.reference_rows)
    # FK columns take the parent's landed key TUPLES (ADR 0031),
    # drawn jointly per batch — mirroring B.1. The profile override
    # keeps them off the free-text path; the joint draw below owns
    # the values, overwriting whatever the backend sampled.
    self._fk_key_pools = bind_fk_key_pools(ctx)
    for pool in self._fk_key_pools:
      for i, fk_name in enumerate(pool.cols):
        base = self._profiles.get(fk_name)
        if base is None:
          continue
        values = tuple(dict.fromkeys(k[i] for k in pool.keys))
        self._profiles[fk_name] = ColumnProfile(
            name=base.name,
            bq_type=base.bq_type,
            kind=ColumnKind.CATEGORICAL,
            nullable=base.nullable,
            null_fraction=pool.null_fraction,
            categories=values,
            weights=tuple(1.0 / len(values) for _ in values),
        )
    payload = getattr(ctx, "fanout", None)
    self._fanout = FanoutPlan.from_payload(payload) if payload else None
    if payload:
      assert self._fanout is not None
      for name in self._fanout.driving_cols:
        base = self._profiles.get(name)
        if base is not None:
          self._profiles[name] = ColumnProfile(
              name=base.name,
              bq_type=base.bq_type,
              kind=ColumnKind.CATEGORICAL,
              nullable=base.nullable,
              null_fraction=0.0,
              categories=(),
              weights=(),
          )
      fields: dict[str, Any] = {
          "driving_cols": ",".join(self._fanout.driving_cols),
          "cells": self._fanout.cells.size if self._fanout.cells else 0,
          "exact_cells": self._fanout.exact_cells,
          "mean_fanout": round(self._fanout.histogram.mean, 3),
          # ADR 0037 (design §8): the count of non-driving
          # conditional edges this plan resolves per key, and the
          # Top-M candidate cap (Task 6,
          # `ctx.fanout["candidate_cap"]`) when the launcher has
          # written one.
          "conditional": len(self._fanout.conditional),
      }
      candidate_cap = payload.get("candidate_cap")
      if candidate_cap is not None:
        fields["candidate_cap"] = candidate_cap
      log_milestone("fanout_bound", **fields)
    self._free_text_cols = [
        name for name, p in self._profiles.items()
        if p.kind is ColumnKind.FREE_TEXT
    ]
    self._freetext_hook = FreeTextHook(
        model_client,
        strict=ctx.strict_freetext,
        # ADR 0023 seam: B.2 builds pools lazily (no pool branch), so
        # the full-domain rejection set rides the generate-path ctx.
        source_value_store=getattr(ctx, "source_value_store", None),
    )

    backend = self._make_backend()
    if ctx.reference_rows:
      backend.fit(ctx.reference_rows, self._profiles)
    self._backend = backend
    self._log_generation_plan(ctx)
    self._fitted = True

  def _make_backend(self):
    if self._use_sdgx:
      return SdgxBackend()
    return EmpiricalBackend()

  def _backend_descriptor(self) -> str:
    """Which bulk sampler actually serves this run — sdgx's silent
        empirical fallback (2026-07-22: CTGAN had never run in prod) must be
        visible here, not just in the b2_backend_fallback milestone."""
    if isinstance(self._backend, SdgxBackend):
      fell_back = getattr(self._backend, "_fallback", None) is not None
      return "sdgx_fallback_empirical" if fell_back else "sdgx_ctgan"
    return "empirical"

  def _log_generation_plan(self, ctx: GenerationContext) -> None:
    """ONE milestone mapping every column to its generation strategy —
        the b2 mirror of b1_rag's. B.2 free-text pools build lazily per
        batch (no pool_sources at setup); the run's bulk sampler is
        reported via `backend=` instead of a RAG seed strategy."""
    if not should_log_plan(self.name, ctx.reference_digest,
                           ctx.table_schema.fqn):
      return
    log_milestone(
        "generation_plan",
        engine=self.name,
        table=ctx.table_schema.fqn,
        columns=len(self._profiles),
        backend=self._backend_descriptor(),
        plan=json.dumps(build_plan(self._profiles), separators=(",", ":")),
        columns_detail=json.dumps(
            build_plan_detail(self._profiles), separators=(",", ":")),
    )
    # Fetched constraint clauses, verbatim — the b1 mirror (2026-08-20
    # follow-up: a Terraform description edit must be verifiable from
    # worker logs without a --prompt_debug run).
    constraints = build_constraints_detail(self._profiles)
    if constraints:
      log_milestone(
          "prompt_constraints_found",
          engine=self.name,
          table=ctx.table_schema.fqn,
          columns=",".join(constraints),
          count=len(constraints),
          enabled=bool(getattr(ctx, "prompt_constraints", True)),
          detail=json.dumps(constraints, separators=(",", ":")),
      )
    log_plan_pretty(self.name, ctx, self._profiles)

  # -- pickling across the Beam worker boundary ---------------------------
  #
  # The dynamically-created Pydantic record model (`create_model`) is the
  # one piece of state that may not pickle reliably, so we drop it on dump
  # and rebuild it from the (picklable) `TableSchema` in `ctx` on load. The
  # fitted backend, profiles, and free-text hook are all plain data /
  # NumPy / picklable sdgx `Synthesizer` objects.

  def __getstate__(self) -> dict:
    state = self.__dict__.copy()
    state["_record_model"] = None
    return state

  def __setstate__(self, state: dict) -> None:
    self.__dict__.update(state)
    if self._record_model is None and self._ctx is not None:
      self._record_model = derive_record_model(self._ctx.table_schema)

  def teardown(self) -> None:
    self._backend = None
    self._freetext_hook = None
    self._record_model = None
    self._profiles = {}
    self._free_text_cols = []
    self._ctx = None
    self._fitted = False

  # -- generation ---------------------------------------------------------

  def generate_batch(
      self,
      n: int,
      cfg: GenerationConfig,
  ) -> Iterator[GeneratedRecord]:
    if not self._fitted or self._backend is None or self._record_model is None:
      raise RuntimeError("B2LibraryEngine.generate_batch called before setup() "
                         "(or after teardown()).")
    if self._ctx is None or not self._ctx.reference_rows or n <= 0:
      return

    rng = np.random.default_rng(cfg.seed)
    temperature = _similarity_to_sampling_temperature(cfg.similarity)

    # 1. Vectorized backend sample for all non-free-text columns.
    columns = self._backend.sample_columns(n, rng, temperature=temperature)

    # 2. Free-text columns via the bounded LLM pool hook.
    for name in self._free_text_cols:
      columns[name] = self._freetext_hook.sample(self._profiles[name], n, cfg,
                                                 rng)

    # 2b. Enforced FK columns: whole parent key tuples, overwriting
    # whatever the backend drew per column (ADR 0031).
    for pool in self._fk_key_pools:
      drawn = pool.draw(n, rng, use_numpy=True)
      for i, name in enumerate(pool.cols):
        columns[name] = [t[i] for t in drawn]

    # 3. Assemble rows, enforce fidelity, validate, yield.
    col_order = [c.name for c in self._ctx.table_schema.columns]
    for i in range(n):
      row = self._assemble_row(columns, col_order, i)
      try:
        yield self._record_model.model_validate(row)
      except Exception:  # pylint: disable=broad-exception-caught
        continue

  def _assemble_row(
      self,
      columns: dict[str, list],
      col_order: list[str],
      i: int,
      *,
      passthrough: frozenset[str] = frozenset(),
  ) -> dict:
    row: dict = {}
    for name in col_order:
      raw = columns[name][i] if name in columns else None
      if name in passthrough:
        row[name] = raw
        continue
      profile = self._profiles.get(name)
      row[name] = enforce_value(profile, raw) if profile is not None else raw
    return row

  def generate_for_keys(
      self,
      keys: Sequence[tuple],
      cfg: GenerationConfig,
      matches: Mapping[str, Sequence[Sequence[Sequence]]] | None = None,
  ) -> Iterator[GeneratedRecord]:
    if not self._fitted or self._backend is None or self._record_model is None:
      raise RuntimeError(
          "B2LibraryEngine.generate_for_keys called before setup()")
    if self._fanout is None:
      raise RuntimeError(
          "B2LibraryEngine.generate_for_keys: ctx.fanout is not set")
    assert self._ctx is not None
    plan = self._fanout
    run_id = self._ctx.pipeline_run_id
    rng = np.random.default_rng(cfg.seed)
    temperature = _similarity_to_sampling_temperature(cfg.similarity)
    col_order = [c.name for c in self._ctx.table_schema.columns]
    chunk_rows = max(1, int(cfg.batch_size or 1000))
    # Non-driving FK edges resolved per key (design 2026-09-11 §4,
    # ADR 0037) — the JOINT per-key draw, computed ONCE up front (a
    # no-op dict when the plan carries no conditional edges). It
    # decides each child's cell AND its candidate tuples together, so
    # `expand_keys` and the overrides below stay two halves of one
    # combination index (fix wave A1).
    # `table=` scopes the `fanout_rows_capped` milestone to THIS
    # driven table (final review, E3) — several of them share one
    # worker process in a single-job relational run (ADR 0030).
    draws = conditional_draws(
        plan,
        keys,
        run_id,
        matches,
        table=self._ctx.landing_table or self._ctx.log_table_prefix,
    )
    child_index: dict[tuple, int] = {}
    for chunk in expand_keys(
        plan,
        keys,
        run_id,
        chunk_rows,
        draws=draws if plan.conditional else None,
    ):
      n = len(chunk)
      columns = self._backend.sample_columns(n, rng, temperature=temperature)
      for name in self._free_text_cols:
        columns[name] = self._freetext_hook.sample(self._profiles[name], n, cfg,
                                                   rng)
      # Enforced FK edges OUTSIDE the driving one (side-input or
      # already-landed BQ pools, ADR 0031) still draw whole parent key
      # tuples — `generate_batch` step 2b, which a driven child would
      # otherwise skip entirely and fall back to per-column marginals,
      # re-crossing the tuples it must not cross. Applied BEFORE the
      # driving/cell overrides so the driving edge wins any overlap.
      for pool in self._fk_key_pools:
        drawn = pool.draw(n, rng, use_numpy=True)
        for i, name in enumerate(pool.cols):
          columns[name] = [t[i] for t in drawn]
      cond_columns, skip = apply_conditional_overrides(plan, chunk, draws,
                                                       child_index)
      columns.update(cond_columns)
      for j, name in enumerate(plan.driving_cols):
        columns[name] = [key[j] for key, _ in chunk]
      if plan.cells is not None:
        for j, name in enumerate(plan.cells.cols):
          columns[name] = [cell[j] for _, cell in chunk]
      for i in range(n):
        if skip[i]:
          continue
        row = self._assemble_row(
            columns, col_order, i, passthrough=plan.columns)
        try:
          yield self._record_model.model_validate(row)
        except Exception:  # pylint: disable=broad-exception-caught
          continue


def _similarity_to_sampling_temperature(similarity: float) -> float:
  """Map ``cfg.similarity`` → backend categorical sampling temperature.

    similarity→1 ⇒ temperature→0 (collapse to the empirical mode: maximal
    mimicry); similarity→0 ⇒ temperature→~2 (flatten toward uniform within
    the observed support: maximal diversity). similarity 0.5 ≈ temperature
    1.0 (the empirical distribution itself).
    """
  s = min(max(similarity, 0.0), 1.0)
  return round(2.0 * (1.0 - s), 4)
