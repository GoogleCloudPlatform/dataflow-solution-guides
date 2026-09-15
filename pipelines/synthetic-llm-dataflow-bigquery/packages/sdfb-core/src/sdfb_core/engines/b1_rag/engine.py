"""`B1RagEngine` — the B.1 retrieval-augmented synthesis engine.

Implements the LLM-as-distribution-estimator spine (ADR 0013) with a
retrieval-conditioned twist:

  setup(model_client, ctx):
    1. serialize each reference row (GReaT-style) and embed it (Embedder seam)
    2. build a FAISS IndexFlatIP (exact, normalized, seeded/single-threaded)
    3. profile columns → constant | numeric | categorical | free_text
    4. for free-text columns, retrieve top-k exemplars and ask the LLM ONCE
       (guided JSON) for a bounded unique value pool

  generate_batch(n, cfg):
    - vectorized-sample the bulk columns from the profiled distributions
      (NumPy backend when available, else seeded pure-Python); constants
      copied, numerics clipped to observed range, categoricals at empirical
      frequency
    - patch free-text columns from the bounded LLM pool (sampled w/ replacement)
    - validate each candidate through the derived Pydantic record model;
      drop on failure (DLQ routing happens downstream in the DoFn)

  teardown(): release the index + drop fitted state

`similarity` (GenerationConfig) = retrieval-neighborhood tightness +
sampling variance: →1 mimics nearest exemplars with tight draws; →0 widens
the neighborhood and the sampling spread (always within observed support).

M1 samples each column from its own marginal (constants / numeric range /
empirical categorical), with free-text retrieval-conditioned via the LLM
pool. Joint/conditional sampling over correlated column groups is the next
fidelity primitive (spec §2, NeMo dependency-aware ordering) — deferred.

Pure-Python module: NO `apache_beam` / `torch` / `vllm` / `faiss` / `numpy`
imports at module scope. Heavy deps are deferred into the seams.
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import functools
import json
import logging
import random
import threading
import time
from collections import Counter
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from sdfb_core.codegen import derive_record_model
from sdfb_core.engines.b1_rag._fidelity import ColumnSampler, numpy_available
from sdfb_core.engines.b1_rag.profile import (
    ColumnKind,
    ColumnProfile,
    profile_columns,
)
from sdfb_core.engines.base import (
    FreeTextEmptyYieldError,
    GenerationEngine,
    ModelClientTransientError,
    apply_conditional_overrides,
    conditional_draws,
    escalating_sampling,
)
from sdfb_core.engines.constraint_sampler import (
    ByteTemplateSampler,
    compile_pattern_sampler,
)
from sdfb_core.engines.fanout import FanoutPlan, expand_keys
from sdfb_core.engines.fk_keys import bind_fk_key_pools
from sdfb_core.engines.generation_plan import FREE_TEXT_POOL_MAX
from sdfb_core.engines.generation_plan import (
    build_constraints_detail as _build_constraints_detail,)
from sdfb_core.engines.generation_plan import build_plan as _build_plan
from sdfb_core.engines.generation_plan import (
    build_plan_detail as _build_plan_detail,)
from sdfb_core.engines.generation_plan import (
    log_plan_pretty as _log_plan_pretty,)
from sdfb_core.engines.generation_plan import should_log_plan as _should_log_plan
from sdfb_core.engines.text_shapes import (
    IdentifierArtifacts,
    build_identifier_artifacts,
    build_relaxed_shapes,
    collapsed_mask,
    identifier_sampler_from,
    is_binary_class,
    length_ceiling,
    length_hint,
    mutate_digit_runs,
    pick_relaxed_shape,
    relaxed_shape_charset,
    relaxed_shape_lengths,
    relaxed_shapes_pattern,
    sample_identifier,
    sample_relaxed_identifier,
    shape_mix_can_template,
    shape_mix_is_identifier_like,
)
from sdfb_core.observability import (
    log_milestone,
    log_prompt_debug,
    milestone_scope_value,
    set_milestone_scope_for_thread,
)
from sdfb_core.rag.chunking import (
    CHUNK_KIND_FREE_TEXT_COL,
    CHUNK_KIND_ROW_DOC,
    MAX_ROW_DOC_ROWS,
    compute_row_digest,
)
from sdfb_core.rag.embedding import BgeEmbedder, Embedder, HashingEmbedder
from sdfb_core.rag.index import build_index
from sdfb_core.rag.retrieval import retrieve_centroid_top_k, select_seed_examples
from sdfb_core.rag.serialize import serialize_rows
from sdfb_core.seeding import derive_batch_seed

if TYPE_CHECKING:  # pragma: no cover - typing only
  from collections.abc import Callable, Iterator, Mapping, Sequence

  from sdfb_core.contracts import GeneratedRecord
  from sdfb_core.engines.base import (
      GenerationConfig,
      GenerationContext,
      ModelClient,
  )
  from sdfb_core.rag.index import ExactIPIndex
  from sdfb_core.rag.store import ChunkStore

# Top-k exemplars retrieved to condition the LLM's free-text inference.
_DEFAULT_TOP_K = 8
# Free-text pool scaling (WS2 §4b.2). Per-column target =
# min(num_rows, column_distinct, _FREE_TEXT_POOL_MAX); the 32-value pool
# of the 2026-07-19 run oversampled 3 columns 28-619x. Each LLM call stays
# bounded at _POOL_VALUES_PER_CALL values — multiple bounded calls, never
# per-row work (ADR 0013's FASTGEN spine).
_FREE_TEXT_POOL_MAX = FREE_TEXT_POOL_MAX
_POOL_VALUES_PER_CALL = 32
# Routed-draw rejection bound (ADR 0028): with clause value spaces at
# 1e19+ and forbidden sets at 1e5, a miss is ~1e-14 per try — the bound
# exists to make pathological saturation a loud error, not a hang.
_ROUTED_DRAW_TRIES = 16
# Server-side parallel sampling (2026-07-25 perf fix): each pool HTTP round
# trip requests this many INDEPENDENT array-completions (`n=`) and de-dupes
# across them, multiplying per-call novel yield ~4x. Safe because pool
# requests are UNSEEDED — the 2026-07-16 collapse was n=32 single-value
# choices under a pinned seed, a different shape entirely. The prompt stays
# byte-identical across attempts (stable prefix ⇒ vLLM APC / LMCache-ready).
_POOL_PARALLEL_CHOICES = 4
# Stagnation exit (2026-07-23 E2E): 2 of 3 pool columns rode the full
# 2*ceil(target/32)=32-call budget (~30 min of T4 setup) while marginal
# novel yield had collapsed to cross-call duplicates and prompt echoes.
# Once every escalation level has run, _POOL_STAGNATION_WINDOW consecutive
# attempts each adding fewer than _POOL_STAGNATION_MIN_NOVEL novel values
# end the ladder — more retries at the ceiling level cannot outrun the
# yield decay.
_POOL_STAGNATION_WINDOW = 3
_POOL_STAGNATION_MIN_NOVEL = max(1, _POOL_VALUES_PER_CALL // 8)
# Format-collapse exit (ADR 0033, 2026-08-25/26 R6 A_COL_037): 385/393
# and 386/393 parsed values were format-rejected — 28-char values into a
# fixed 31-char bucket, the model echoing its clause's own off-format
# example — over three ~150 s T4 rounds before the stagnation window
# closed. A full-yield round with ZERO in-format values is a structural
# mismatch temperature cannot fix; two in a row end the ladder.
_POOL_FORMAT_COLLAPSE_ROUNDS = 2
# Ladders for different columns are independent — run them on a bounded
# thread pool. vLLM continuous-batches concurrent requests on the server
# side; the client (openai/httpx) is thread-safe; embedder work is NOT
# (HF "Already borrowed") and therefore finishes before any thread spawns.
_POOL_BUILD_MAX_WORKERS = 4
# Back-compat alias: the historical single-call pool size == one call's batch.
_DEFAULT_FREE_TEXT_POOL = _POOL_VALUES_PER_CALL
# Identifier mask-mix gate: the top-8 masks must cover at least this share
# of the column's DISTINCT values before the mix replaces the collapsed
# template (rigid-mask columns qualify; 36-hex-style random masks must not
# collapse to 8 skeletons).
_MASK_MIX_MIN_COVERAGE = 0.5
# Identity-like NUMERIC gate (wave 4): only integral columns whose sample
# distinct count clears the memorization rule's floor fetch a source domain
# — mirrors `_MEM_MIN_SOURCE_DISTINCT` in the E2E probe.
_NUMERIC_DOMAIN_MIN_DISTINCT = 100
# Collision scrub (wave-4 v2, 2026-08-21 four-run cycle): NUDGE-FIRST,
# then inverse-CDF redraw rounds for saturated neighborhoods, then a final
# nudge. Redraw-first redistributed the rejected mass across the whole
# marginal — a version-number column's decile-KS rose 0.04 → 0.17 while a
# nudge would have kept every scrubbed value inside its quantile
# neighborhood.
_NUMERIC_REDRAW_ROUNDS = 2
_NUMERIC_NUDGE_MAX = 24
_NUMERIC_MULTI_KNOT_MIN = 2
# Keep-set floor: a source value shared by at least this many SOURCE rows
# is k-anonymous enum mass (mirrors the probe's _MEM_KANON_MIN_COUNT). The
# set comes from the store's `fetch_frequent` when available; the sample
# multi-knot heuristic is only the no-store fallback — at ~21x subsampling
# a sample frequency of 2 does not imply source frequency >= 10 (the
# 2026-08-21 cycle measured that gap as COL_009's 0.25-vs-0.14 residual).
_NUMERIC_KANON_MIN_COUNT = 10
# Setup embeds at most this many reference rows. The index those vectors
# feed serves ONLY centroid top-k exemplar retrieval in M1 (generation
# samples marginals — no per-batch retrieval), so embedding the full 10k
# reference sample bought nothing but wall-clock: the 2026-07-16 corp run
# spent 26-92 min PER Dataflow bundle attempt in `embedder.embed`. The
# reference SELECT is fingerprint-ordered (deterministic spread), so a
# prefix is a representative sample. Aliases the population write contract
# (chunking.MAX_ROW_DOC_ROWS) — the persisted row_doc chunk set and this
# read prefix must stay the same set of rows.
_MAX_EMBED_ROWS = MAX_ROW_DOC_ROWS

# Process-level pool cache (2026-07-24 16:35 E2E): a strict failure in ONE
# column's ladder crashes DoFn.setup() and Dataflow retries the bundle with
# a FRESH engine in the SAME worker process — without this cache the retry
# rebuilt every sibling column's pool from scratch (~15-19 min/attempt).
# Keyed on (reference_digest, model_uri, column, target); disabled when the
# digest is empty. Deliberately process-lived (teardown() must not clear
# it) — the module-level vLLM server reuse (_SERVER_REFS) is the precedent.
_POOL_CACHE: dict[tuple[str, str, str, int], tuple[str, ...]] = {}
_POOL_CACHE_LOCK = threading.Lock()


def clear_free_text_pool_cache() -> None:
  """Drop all cached pools (tests / maintenance only)."""
  with _POOL_CACHE_LOCK:
    _POOL_CACHE.clear()


class B1RagEngine(GenerationEngine):
  """Retrieval-augmented, distribution-estimator synthesis engine (B.1)."""

  name = "b1_rag"

  def __init__(self, *, embedder: Embedder | None = None) -> None:
    # `embedder` lets tests inject a deterministic fake. Production wires
    # a `BgeEmbedder` (local weights). When None, we default to a
    # dependency-free `HashingEmbedder` so the engine is constructible and
    # the contract tests run on a bare laptop with HF_HUB_OFFLINE=1.
    self._injected_embedder = embedder

    self._client: ModelClient | None = None
    self._ctx: GenerationContext | None = None
    self._record_model: type[GeneratedRecord] | None = None
    self._profiles: dict[str, ColumnProfile] | None = None
    self._samplers: dict[str, ColumnSampler] | None = None
    # column -> (vectors, texts) captured during the SEQUENTIAL seed
    # phase, so the kcenter_rotate arm can re-seed per ladder attempt
    # without touching the (thread-unsafe) embedder from a worker
    # thread. Written before any ladder thread spawns; read-only after.
    self._seed_space: dict[str, tuple[list, list]] = {}
    # ADR 0028 — column -> ("pattern" | "byte_template", sampler) for
    # constrained columns whose clause routes to a programmatic
    # sampler; plus per-column source-rejection sets and, for PK
    # columns, the per-process emitted set that guarantees
    # uniqueness across batches.
    self._routed: dict[str, tuple[str, Any]] = {}
    self._routed_forbidden: dict[str, frozenset[str]] = {}
    self._routed_emitted: dict[str, set[str]] = {}
    # Enforced FK edges (ADR 0031) — joint parent-key tuple pools,
    # bound in setup(); their columns bypass the per-column samplers.
    self._fk_key_pools: list = []
    self._fk_columns: set[str] = set()
    # A DRIVEN child's recipe (ADR 0036), bound in setup() from
    # ``ctx.fanout``; None keeps generate_batch's num_rows-driven path.
    self._fanout: FanoutPlan | None = None
    self._index: ExactIPIndex | None = None
    self._embedder: Embedder | None = None
    self._ref_vectors: list[list[float]] = []
    self._free_text_pools: dict[str, list[str]] = {}
    # column -> {"target", "attempts", "stagnated"} recorded by
    # `_infer_free_text_pool` so the pool-branch rows persist the REAL
    # build outcome (2026-07-29 postmortem: stored rows carried
    # attempts=0 / stagnated=false / target=achieved-size because these
    # were never recorded). Written from ladder threads — per-key dict
    # assignment, atomic under the GIL.
    self._pool_build_info: dict[str, dict[str, Any]] = {}
    # column -> where its free-text pool came from this setup:
    # "store" | "process_cache" | "llm_ladder". Feeds the
    # generation_plan milestone's pool_sources field.
    self._pool_sources: dict[str, str] = {}
    # Identifier-shaped columns: full source domain (ADR 0023 seam,
    # 2026-08-11 A_TABLE R1 — the sample-only mask table reproduced 38%
    # of source masks) and the per-column draw artifacts built ONCE per
    # setup (mask/positional tables over the domain are too heavy to
    # rebuild per batch).
    self._identifier_domains: dict[str, frozenset[str]] = {}
    self._identifier_artifacts: dict[str, IdentifierArtifacts] = {}
    # Identity-like integral NUMERIC columns (wave 4): full source
    # domain for collision scrubbing (2026-08-20 A_TABLE R1, COL_009:
    # inverse-CDF interpolation in a dense integer band landed 52%
    # substantive copies), plus the multi-knot values (sample freq >= 2)
    # that stay exact as k-anonymous enum mass.
    self._numeric_domains: dict[str, frozenset[str]] = {}
    self._numeric_multi_knots: dict[str, frozenset[str]] = {}
    self._numeric_scrub_logged: set[str] = set()
    self._column_order: list[str] = []
    self._ready: bool = False

  @property
  def constrained_columns(self) -> frozenset[str]:
    """Columns a declared clause owns end to end (Tier P/B)."""
    return frozenset(self._routed)

  # -- lifecycle ----------------------------------------------------------

  def setup(self, model_client: ModelClient, ctx: GenerationContext) -> None:
    if self._ready:
      return  # idempotent — do not re-embed / re-fit / re-call the LLM.

    self._client = model_client
    self._ctx = ctx
    self._column_order = [c.name for c in ctx.table_schema.columns]
    self._record_model = derive_record_model(ctx.table_schema)

    # 3. profile columns (cheap O(N_ref) pass).
    self._profiles = profile_columns(ctx.table_schema, ctx.reference_rows)
    # FK columns take the parent's landed key TUPLES (ADR 0031) —
    # drawn jointly at batch time, so the profile override here only
    # keeps them OFF the free-text/LLM path and out of the per-column
    # samplers; `_draw_fk_columns` owns their values.
    self._bind_fk_key_pools(ctx)
    self._bind_fanout(ctx)
    self._samplers = {
        name: ColumnSampler(prof) for name, prof in self._profiles.items()
    }
    # ADR 0028 — route constrained columns to programmatic samplers
    # BEFORE any pool work: Tier P/B columns never enter the ladder
    # and draw straight from their clause's value space.
    self._route_constraint_samplers(ctx)

    # 1+2. embed + index (only meaningful with reference rows present).
    # Embedder precedence: injected (tests) > real BgeEmbedder if the
    # context carries a (worker-local) embedder path > dependency-free
    # HashingEmbedder default (bare laptop / contract tests).
    if self._injected_embedder is not None:
      self._embedder = self._injected_embedder
    elif ctx.embedder_uri:
      # "auto" = the worker's GPU when present — the bulk 1024-row
      # embed runs before vLLM ignition and demotes right after
      # (ADR 0019), so the two never contend for VRAM.
      self._embedder = BgeEmbedder(ctx.embedder_uri, device="auto")
    else:
      self._embedder = HashingEmbedder(dim=384)
    if ctx.reference_rows:
      # Prefix of the (fingerprint-ordered) reference sample — the
      # exemplar ids returned by `_retrieve_exemplars` index into this
      # same prefix, so `ctx.reference_rows[i]` stays valid.
      embed_rows = ctx.reference_rows[:_MAX_EMBED_ROWS]
      reused = self._vectors_from_store(ctx, embed_rows)
      if reused is not None:
        self._ref_vectors = reused
      else:
        t_embed = time.monotonic()
        texts = serialize_rows(embed_rows, self._column_order)
        self._ref_vectors = self._embedder.embed(texts)
        log_milestone(
            "b1_embed_done",
            rows=len(texts),
            rows_total=len(ctx.reference_rows),
            seconds=round(time.monotonic() - t_embed, 1),
            device=getattr(self._embedder, "device", "cpu"),
        )
      t_index = time.monotonic()
      self._index = build_index(self._ref_vectors, self._embedder.dim)
      log_milestone(
          "b1_index_built",
          seconds=round(time.monotonic() - t_index, 1),
      )
    else:
      self._ref_vectors = []
      self._index = None
    # Bulk embedding is done — release VRAM before vLLM ignition sizes
    # its KV-cache budget (ADR 0019). Later seed-example embeds are
    # tiny and run fine on CPU.
    demote = getattr(self._embedder, "demote_to_cpu", None)
    if callable(demote):
      demote()  # pylint: disable=not-callable  # narrowed by callable() above

    # 4. infer free-text pools ONCE (the only O(1) LLM use in setup).
    t_pools = time.monotonic()
    self._free_text_pools = self._build_free_text_pools(ctx)
    log_milestone(
        "b1_pools_built",
        seconds=round(time.monotonic() - t_pools, 1),
        freetext_cols=len(self._free_text_pools),
    )
    sources = list(self._pool_sources.values())
    warm = sum(1 for s in sources if s in ("store", "process_cache"))
    if "llm_ladder" not in sources and warm:
      # Pools served from the persisted store / process cache: the
      # LLM work was done ONCE by the pool branch (ADR 0020) and this
      # generate setup reads it — the designed steady state, not idle
      # hardware. 2026-08-26 R6 10M: 64 `llm_route_unused` WARNINGs,
      # one per generate DoFn instance, inside a job whose pool
      # branch had just spent 17 GPU-minutes building those pools.
      log_milestone("freetext_pools_warm", columns=warm)
    elif "llm_ladder" not in sources:
      # No column drew a single LLM token this setup (all expandable
      # / binary-fallback / typed routes). 2026-08-21 four-run cycle:
      # such a run billed ~28 GPU-minutes on an idle T4 — say so, so
      # the operator can rerun CPU-only.
      log_milestone(
          "llm_route_unused",
          level=logging.WARNING,
          note="no LLM-routed free-text work this run — GPU workers "
          "stay idle; a CPU-only worker pool serves it at lower cost "
          "(RUN_PLAYBOOK §9c cost note)",
      )
    # 5. identifier-shaped columns: pull the full source domain through
    # the same store the pool ladder uses (ADR 0023) so mask tables and
    # novelty rejection see the whole keyspace, not the sample
    # (2026-08-11 A_TABLE R1: COL_001 mask recall 0.38). Wave 4 extends
    # the seam to identity-like integral NUMERIC columns (COL_009:
    # 52% substantive copies from dense-band interpolation).
    self._fetch_identifier_domains(ctx)
    self._fetch_numeric_domains(ctx)
    self._log_generation_plan(ctx)

    self._ready = True

  def _bind_fk_key_pools(self, ctx: GenerationContext) -> None:
    """Enforced FK edges → joint key pools, and their columns marked.

        The profile override keeps FK columns OFF the free-text/LLM path
        and out of the per-column samplers; `_draw_fk_columns` owns their
        values (ADR 0031).
        """
    self._fk_key_pools = bind_fk_key_pools(ctx)
    self._fk_columns = {c for p in self._fk_key_pools for c in p.cols}
    profiles = self._profiles or {}
    for pool in self._fk_key_pools:
      for i, name in enumerate(pool.cols):
        base = profiles.get(name)
        if base is None:
          continue
        values = tuple(dict.fromkeys(k[i] for k in pool.keys))
        profiles[name] = ColumnProfile(
            name=base.name,
            bq_type=base.bq_type,
            kind=ColumnKind.CATEGORICAL,
            nullable=base.nullable,
            null_fraction=pool.null_fraction,
            categories={v: 1 for v in values},
            observed_values=values,
        )

  def _bind_fanout(self, ctx: GenerationContext) -> None:
    """A driven child's recipe (ADR 0036): the driving-edge columns
        (FK + inherited) leave the per-column samplers exactly as FK
        columns do; the cell columns stay sampled and are overridden."""
    payload = getattr(ctx, "fanout", None)
    if not payload:
      self._fanout = None
      return
    self._fanout = FanoutPlan.from_payload(payload)
    assert self._profiles is not None
    for name in self._fanout.driving_cols:
      self._fk_columns.add(name)
      base = self._profiles.get(name)
      if base is not None:
        self._profiles[name] = ColumnProfile(
            name=base.name,
            bq_type=base.bq_type,
            kind=ColumnKind.CATEGORICAL,
            nullable=base.nullable,
            null_fraction=0.0,
            categories={},
            observed_values=(),
        )
    fields: dict[str, Any] = {
        "driving_cols": ",".join(self._fanout.driving_cols),
        "cells": self._fanout.cells.size if self._fanout.cells else 0,
        "exact_cells": self._fanout.exact_cells,
        "mean_fanout": round(self._fanout.histogram.mean, 3),
        # ADR 0037 (design §8): the count of non-driving conditional
        # edges this plan resolves per key, and the Top-M candidate
        # cap (Task 6, `ctx.fanout["candidate_cap"]`) when the
        # launcher has written one.
        "conditional": len(self._fanout.conditional),
    }
    candidate_cap = payload.get("candidate_cap")
    if candidate_cap is not None:
      fields["candidate_cap"] = candidate_cap
    log_milestone("fanout_bound", **fields)

  def _fetch_identifier_domains(self, ctx: GenerationContext) -> None:
    """Full source domains for identifier-shaped columns, via the
        ADR 0023 `source_value_store` seam (empty dict when no store)."""
    assert self._profiles is not None
    for name, prof in self._profiles.items():
      if (prof.kind is ColumnKind.FREE_TEXT and
          prof.identifier_shape is not None):
        domain = self._fetch_source_values(
            ctx, name, milestone="identifier_source_filter")
        if domain:
          self._identifier_domains[name] = domain

  def _fetch_numeric_domains(self, ctx: GenerationContext) -> None:
    """Full source domains for identity-like integral NUMERIC columns.

        Gated to columns whose SAMPLE distinct count clears the
        memorization rule's cardinality floor (source_distinct > 100):
        below it, collisions are enum reuse the probe's k-anonymity floor
        already exempts, and the domain query would be spent for nothing.
        Multi-knot values (sample frequency >= 2) are recorded so the
        scrubber keeps them exact — under the ~20x sample-to-source scale
        they are the frequent codes the engine must re-emit (the same
        argument as FREE_TEXT head values)."""
    assert self._profiles is not None
    for name, prof in self._profiles.items():
      if prof.kind is not ColumnKind.NUMERIC or not prof.is_integral:
        continue
      values = [str(v) for v in prof.observed_values if v is not None]
      if len(set(values)) <= _NUMERIC_DOMAIN_MIN_DISTINCT:
        continue
      domain = self._fetch_source_values(
          ctx, name, milestone="numeric_source_filter")
      if domain:
        self._numeric_domains[name] = domain
        self._numeric_multi_knots[name] = self._numeric_keep_set(
            ctx, name, values)

  def _numeric_keep_set(self, ctx: GenerationContext, name: str,
                        values: list[str]) -> frozenset[str]:
    """K-anonymous values the scrub must keep exact.

        Preferred: the SOURCE's own frequent values (`fetch_frequent`,
        HAVING COUNT(*) >= 10 — the same floor the probe's substantive
        metric applies), so the scrub and the measurement agree on what
        counts as enum mass. Fallback (store absent / method absent /
        error / over-cap): sample multi-knots, the wave-4 v1 heuristic."""
    store = getattr(ctx, "source_value_store", None)
    fetch_frequent = getattr(store, "fetch_frequent", None)
    if fetch_frequent is not None:
      try:
        frequent = fetch_frequent(name, _NUMERIC_KANON_MIN_COUNT)
      except Exception as exc:  # pylint: disable=broad-exception-caught
        log_milestone(
            "numeric_kanon_filter_error",
            level=logging.WARNING,
            column=name,
            error=type(exc).__name__,
        )
      else:
        if frequent is not None:
          log_milestone(
              "numeric_kanon_filter",
              column=name,
              size=len(frequent),
              min_count=_NUMERIC_KANON_MIN_COUNT,
          )
          return frozenset(frequent)
        log_milestone(
            "numeric_kanon_filter_absent",
            level=logging.WARNING,
            column=name,
        )
    counts = Counter(values)
    return frozenset(
        v for v, c in counts.items() if c >= _NUMERIC_MULTI_KNOT_MIN)

  def _log_generation_plan(self, ctx: GenerationContext) -> None:
    """ONE milestone mapping every column to its generation strategy.

        Answers, without re-deriving it from scattered per-column milestones,
        which fields are LLM free-text pools (and where this run's pools came
        from), which are shaped identifiers routed off the LLM, and which are
        plain samplers. Logged once per (digest, table) per worker process —
        not once per DoFn-thread setup.
        """
    assert self._profiles is not None
    if not _should_log_plan("b1_rag", ctx.reference_digest,
                            ctx.table_schema.fqn):
      return
    log_milestone(
        "generation_plan",
        engine="b1_rag",
        table=ctx.table_schema.fqn,
        columns=len(self._profiles),
        seed_strategy=getattr(ctx, "pool_seed_strategy", "centroid"),
        top_k=_DEFAULT_TOP_K,
        plan=json.dumps(_build_plan(self._profiles), separators=(",", ":")),
        pool_sources=json.dumps(
            dict(sorted(self._pool_sources.items())),
            separators=(",", ":"),
        ),
        columns_detail=json.dumps(
            _build_plan_detail(self._profiles), separators=(",", ":")),
    )
    self._log_prompt_constraints(ctx, "b1_rag")
    _log_plan_pretty(
        "b1_rag",
        ctx,
        self._profiles,
        pool_sources=dict(self._pool_sources),
    )

  def _log_prompt_constraints(self, ctx: GenerationContext,
                              engine: str) -> None:
    """One worker-log line per plan showing the `llm_prompt_constraint`
        clauses actually fetched from the DDL metadata — the launcher
        preflight names columns only, and `columns_detail` only says
        `constraint: true` (2026-08-20 follow-up). Same greppable milestone
        name as the preflight's."""
    assert self._profiles is not None
    detail = _build_constraints_detail(self._profiles)
    if not detail:
      return
    log_milestone(
        "prompt_constraints_found",
        engine=engine,
        table=ctx.table_schema.fqn,
        columns=",".join(detail),
        count=len(detail),
        enabled=bool(getattr(ctx, "prompt_constraints", True)),
        detail=json.dumps(detail, separators=(",", ":")),
    )

  def teardown(self) -> None:
    if self._index is not None:
      self._index.release()
    self._index = None
    self._client = None
    self._ctx = None
    self._record_model = None
    self._profiles = None
    self._samplers = None
    self._embedder = None
    self._ref_vectors = []
    self._free_text_pools = {}
    self._identifier_domains = {}
    self._identifier_artifacts = {}
    self._numeric_domains = {}
    self._numeric_multi_knots = {}
    self._numeric_scrub_logged = set()
    self._routed = {}
    self._routed_forbidden = {}
    self._routed_emitted = {}
    self._column_order = []
    self._ready = False

  def _vectors_from_store(self, ctx: GenerationContext,
                          embed_rows: list[dict]) -> list[list[float]] | None:
    """Row-doc vectors from the persisted RAG layer, or None.

        All-or-nothing: every embed-prefix row must have a chunk in the
        PINNED (embedder_id, embedder_version) space with the embedder's
        exact dim — a partial read would silently mix vector spaces, which
        is worse than re-embedding (WS2 §4b; 2026-07-07 design §4).
        """
    # GenerationContext types the store as `object` to keep base.py free
    # of rag imports; the DoFn only ever injects a ChunkStore.
    store = cast("ChunkStore | None", ctx.chunk_store)
    if store is None or not ctx.reference_digest:
      return None
    t0 = time.monotonic()
    chunks = store.fetch(
        ctx.reference_digest,
        CHUNK_KIND_ROW_DOC,
        ctx.embedder_id,
        ctx.embedder_version,
    )
    by_digest = {c.row_digest: c.embedding for c in chunks if c.embedding}
    if not by_digest:
      return None
    assert self._embedder is not None
    vectors: list[list[float]] = []
    for row in embed_rows:
      emb = by_digest.get(compute_row_digest(row))
      if emb is None or len(emb) != self._embedder.dim:
        return None
      vectors.append(list(emb))
    log_milestone(
        "b1_chunks_reused",
        rows=len(vectors),
        seconds=round(time.monotonic() - t0, 1),
    )
    return vectors

  # -- generation ---------------------------------------------------------

  def generate_batch(self, n: int,
                     cfg: GenerationConfig) -> Iterator[GeneratedRecord]:
    if not self._ready or self._record_model is None or self._samplers is None:
      raise RuntimeError("B1RagEngine.generate_batch called before setup() "
                         "(or after teardown()).")
    if n <= 0 or not self._ctx or not self._ctx.reference_rows:
      return

    similarity = float(cfg.similarity)
    columns = self._sample_columns(n, cfg, similarity)
    free_text = self._sample_free_text(n, cfg, similarity)
    columns.update(free_text)

    for i in range(n):
      raw = {name: columns[name][i] for name in self._column_order}
      try:
        yield self._record_model.model_validate(raw)
      except Exception:  # pylint: disable=broad-exception-caught
        # Repair-loop budget / DLQ routing belong downstream in the
        # DoFn; the engine silently drops un-coercible candidates.
        continue

  def generate_for_keys(
      self,
      keys: Sequence[tuple],
      cfg: GenerationConfig,
      matches: Mapping[str, Sequence[Sequence[Sequence]]] | None = None,
  ) -> Iterator[GeneratedRecord]:
    if not self._ready or self._record_model is None or self._samplers is None:
      raise RuntimeError("B1RagEngine.generate_for_keys called before setup()")
    if self._fanout is None:
      raise RuntimeError("B1RagEngine.generate_for_keys: ctx.fanout is not set")
    assert self._ctx is not None
    plan = self._fanout
    run_id = self._ctx.pipeline_run_id
    similarity = float(cfg.similarity)
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
    for chunk_index, chunk in enumerate(
        expand_keys(
            plan,
            keys,
            run_id,
            chunk_rows,
            draws=draws if plan.conditional else None,
        )):
      # Chunks must not replay each other's "rest" columns: cfg.seed
      # held constant would re-seed _sample_columns/_sample_free_text
      # identically per chunk (the key/cell draws stay fine — they're
      # seeded per key inside expand_keys). The per-chunk seed is a
      # pure function of (run id, cfg.seed, chunk index), so re-runs
      # of the same call still reproduce (test_same_keys_same_children).
      chunk_cfg = cfg.model_copy(update={
          "seed": derive_batch_seed(f"{run_id}:{cfg.seed}", chunk_index)
      })
      n = len(chunk)
      columns = self._sample_columns(n, chunk_cfg, similarity)
      columns.update(self._sample_free_text(n, chunk_cfg, similarity))
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
        raw = {name: columns[name][i] for name in self._column_order}
        try:
          yield self._record_model.model_validate(raw)
        except Exception:  # pylint: disable=broad-exception-caught
          continue

  # -- internals ----------------------------------------------------------

  def _sample_columns(self, n: int, cfg: GenerationConfig,
                      similarity: float) -> dict[str, list]:
    """Vectorized bulk sampling of all non-free-text columns."""
    assert self._samplers is not None
    use_numpy = numpy_available()
    rng = self._make_rng(cfg.seed, use_numpy)
    out: dict[str, list] = {}
    for name in self._column_order:
      sampler = self._samplers[name]
      if sampler.profile.kind is ColumnKind.FREE_TEXT:
        continue  # patched separately from the LLM pool
      if name in self._fk_columns:
        continue  # drawn as whole parent key tuples below
      if use_numpy:
        values = sampler.sample_numpy(rng, n, similarity)
      else:
        values = sampler.sample_python(rng, n, similarity)
      if name in self._numeric_domains:
        values = self._scrub_numeric_collisions(name, sampler, rng, values,
                                                similarity, use_numpy)
      out[name] = values
    out.update(self._draw_fk_columns(n, rng, use_numpy))
    return out

  def _draw_fk_columns(self, n: int, rng, use_numpy: bool) -> dict[str, list]:
    """Enforced FK columns, drawn as whole parent key tuples.

        One draw per EDGE, then transposed onto its columns — the
        combination is what the parent holds, so the combination is what
        the row gets (ADR 0031). A per-column draw here is exactly the
        2026-08-23 orphan defect.
        """
    out: dict[str, list] = {}
    for pool in self._fk_key_pools:
      drawn = pool.draw(n, rng, use_numpy=use_numpy)
      for i, col in enumerate(pool.cols):
        out[col] = [t[i] for t in drawn]
    return out

  def _scrub_numeric_collisions(
      self,
      name,
      sampler,
      rng,
      values: list,
      similarity: float,
      use_numpy: bool,
  ) -> list:
    """Reject rare-source-value collisions on identity-like INT64 draws.

        The inverse-CDF interpolant rounds onto real values wherever the
        integer band is dense (2026-08-20 A_TABLE R1: COL_009 landed 52%
        substantive copies of rare account numbers). NUDGE-FIRST (wave-4
        v2): a colliding draw walks to the nearest non-source integer, so
        the scrubbed value stays inside its quantile neighborhood and the
        marginal barely moves (redraw-first redistributed the rejected
        mass — a version-number column's decile-KS rose 0.04 → 0.17).
        Saturated neighborhoods fall back to inverse-CDF redraws, then one
        final nudge. Keep-set values (source-frequent, k-anonymous) stay
        exact — the numeric twin of FREE_TEXT head values. A value still
        colliding after all of it is kept: by pigeonhole its whole
        neighborhood is real values, and the residual is logged.
        """
    domain = self._numeric_domains[name]
    keep = self._numeric_multi_knots.get(name, frozenset())

    def _collides(v) -> bool:
      return v is not None and str(v) in domain and str(v) not in keep

    def _nudge(idx: list[int]) -> int:
      resolved = 0
      for i in list(idx):
        v = int(values[i])
        for step in range(1, _NUMERIC_NUDGE_MAX + 1):
          lo, hi = v - step, v + step
          if str(lo) not in domain:
            values[i], resolved = lo, resolved + 1
            idx.remove(i)
            break
          if str(hi) not in domain:
            values[i], resolved = hi, resolved + 1
            idx.remove(i)
            break
      return resolved

    idx = [i for i, v in enumerate(values) if _collides(v)]
    collisions = len(idx)
    if not collisions:
      return values
    nudged = _nudge(idx)
    redrawn = 0
    for _ in range(_NUMERIC_REDRAW_ROUNDS):
      if not idx:
        break
      fresh = (
          sampler.sample_numpy(rng, len(idx), similarity)
          if use_numpy else sampler.sample_python(rng, len(idx), similarity))
      before = len(idx)
      for i, v in zip(idx, fresh, strict=True):
        if v is not None:  # keep null parity: a None redraw is a miss
          values[i] = v
      idx = [i for i in idx if _collides(values[i])]
      redrawn += before - len(idx)
    nudged += _nudge(idx)
    if name not in self._numeric_scrub_logged:
      self._numeric_scrub_logged.add(name)
      log_milestone(
          "numeric_source_rejected",
          column=name,
          collisions=collisions,
          nudged=nudged,
          redrawn=redrawn,
          unresolved=len(idx),
      )
    return values

  def _sample_free_text(self, n: int, cfg: GenerationConfig,
                        similarity: float) -> dict[str, list]:
    """Sample free-text columns from their bounded LLM pools (w/ repl)."""
    del similarity  # Unused: pool, routed and expansion draws ignore it.
    assert self._samplers is not None
    out: dict[str, list] = {}
    # A dedicated seeded RNG so free-text draws don't perturb the bulk
    # column RNG stream (keeps both reproducible & independent).
    rng = random.Random(_mix_seed(cfg.seed, "freetext"))
    expansion = getattr(self._ctx, "freetext_expansion", "identifiers")
    for name in self._column_order:
      sampler = self._samplers[name]
      prof = sampler.profile
      if prof.kind is not ColumnKind.FREE_TEXT:
        continue
      null_frac = prof.null_fraction if prof.nullable else 0.0
      # Trimmed-empty parity (2026-08-04 crosscheck dominant root
      # cause): one uniform draw decides null → empty → value, so the
      # two sparsity modes never double-count.
      empty_frac = prof.empty_fraction
      if name in self._routed:
        # ADR 0028 Tier P/B: sample the clause's value space
        # directly — format-exact, unbounded (the pool cap can
        # never truncate a PK again), source-rejecting.
        draw_routed = self._routed_draw(name, rng)
        out[name] = [
            self._sparsity_or(rng, null_frac, empty_frac, draw_routed)
            for _ in range(n)
        ]
        continue
      if prof.identifier_shape is not None:
        # Format-preserving per-row generation — a bounded pool
        # sampled with replacement collapses an identifier column's
        # distinctness (2026-07-17 E2E: ID_COL 30 distinct / 1000).
        draw_one = self._identifier_draw(prof, rng)
        draw_one = self._with_head_values(rng, prof.head_values, draw_one)
        out[name] = [
            self._sparsity_or(rng, null_frac, empty_frac, draw_one)
            for _ in range(n)
        ]
        continue
      pool = self._free_text_pools.get(name) or list(prof.text_examples)
      expand = self._draws_from_expansion(prof)
      if not pool and not expand:
        out[name] = [None] * n
        continue
      observed = frozenset(prof.observed_values) if expand else frozenset()
      mutate = expansion == "all" and prof.shape_mix is None and pool

      def _value(
          prof=prof,
          pool=pool,
          expand=expand,
          observed=observed,
          mutate=mutate,
      ):
        if expand:
          # Draw from the observed shape mix: distinct scales with
          # rows, not with the LLM pool cap (2026-08-03 10M run:
          # synthetic distinct == pool size on all 13 columns).
          # Bucket-stable novelty retry (wave 4, D2): keep the
          # drawn bucket, redraw only the fill — re-picking the
          # bucket migrates mass out of saturated mask families.
          shape = pick_relaxed_shape(prof.shape_mix, rng.randrange)
          for _ in range(8):
            v = sample_identifier(shape, rng.randrange)
            if v not in observed:
              return v
          return v
        v = pool[rng.randrange(len(pool))]
        if mutate:
          v = mutate_digit_runs(v, rng.randrange)
        return v

      value = self._with_head_values(rng, prof.head_values, _value)
      out[name] = [
          self._sparsity_or(rng, null_frac, empty_frac, value) for _ in range(n)
      ]
    return out

  def _draws_from_expansion(self, prof: ColumnProfile) -> bool:
    """One truth for "this column's tail draws come from shape-mix
        expansion" — shared by the draw path AND the pool-build skip so
        they can never diverge (a skipped pool without expansion would
        fall back to observed `text_examples`, i.e. memorization).

        Columns carrying an `llm_prompt_constraint` never expand (wave 4):
        the constraint's enforcement vehicle is the pool prompt and its
        guided `pattern` — expansion was silently bypassing both, which
        made a `format`/`charset` clause on an expandable column
        decorative (2026-08-20 B_TABLE R1, COL_015-class)."""
    if prof.llm_prompt_constraint:
      return False
    expansion = getattr(self._ctx, "freetext_expansion", "identifiers")
    return (prof.shape_mix is not None and expansion != "off" and
            (expansion == "all" or
             shape_mix_is_identifier_like(prof.shape_mix)))

  @staticmethod
  def _with_head_values(
      rng: random.Random,
      heads: tuple[tuple[str, float], ...],
      value: Callable[[], object],
  ) -> Callable[[], object]:
    """Wrap a substantive-value draw with dominant-literal re-emission.

        Heads carry their observed share of SUBSTANTIVE rows, so emitting
        them before the tail draw reproduces the source frequency exactly
        (2026-08-07 A_TABLE R1: `ZZ3000` at 77% share had recall 0 — the
        pool can never contain it, ADR 0023 rejects all source values).
        """
    if not heads:
      return value

    def _draw() -> object:
      r = rng.random()
      acc = 0.0
      for head_value, share in heads:
        acc += share
        if r < acc:
          return head_value
      return value()

    return _draw

  def _identifier_draw(self, prof: ColumnProfile,
                       rng: random.Random) -> Callable[[], object]:
    """Per-row identifier generator: mask mix when the masks are rigid,
        row-weighted mask table (positional alphabets pin fixed prefixes,
        residual tail bucket carries the beyond-cap mass) otherwise,
        collapsed template as last resort.

        Evidence = observed rows minus head values (heads are re-emitted at
        their exact share by `_with_head_values` — leaving them in would
        double-count their mask mass). The full source domain (ADR 0023
        store) travels SEPARATELY (wave 4, D1): concatenated onto the row
        multiset it out-voted the sample — distinct-weighted masks again,
        dominant mask 89.8% → 8.2% of draws — so it now feeds only novelty
        rejection, alphabets/positional evidence and the tail's support.
        Artifacts build once per setup; only the per-batch RNG binding is
        per-call.
        """
    artifacts = self._identifier_artifacts.get(prof.name)
    if artifacts is None:
      shape = prof.identifier_shape
      assert shape is not None
      head_set = {v for v, _ in prof.head_values}
      evidence = [
          str(v) for v in prof.observed_values if str(v) not in head_set
      ]
      artifacts = build_identifier_artifacts(
          shape,
          prof.shape_mix,
          evidence,
          coverage_min=_MASK_MIX_MIN_COVERAGE,
          domain=self._identifier_domains.get(prof.name, frozenset()),
      )
      self._identifier_artifacts[prof.name] = artifacts
    return identifier_sampler_from(artifacts, rng.randrange)

  @staticmethod
  def _sparsity_or(
      rng: random.Random,
      null_frac: float,
      empty_frac: float,
      value: Callable[[], object],
  ):
    """One draw → None / "" / a generated value, at observed rates."""
    r = rng.random()
    if r < null_frac:
      return None
    if r < null_frac + empty_frac:
      return ""
    return value()

  def _stored_pools(self, ctx: GenerationContext) -> dict[str, list[str]]:
    """Pools already persisted for this (reference_digest, model_uri).

        Returns column → values. Empty on any of: no store attached, no
        digest to key on, or a store that raised — in every case the ladder
        runs exactly as it did before WS5.
        """
    store = getattr(ctx, "pool_store", None)
    if store is None or not ctx.reference_digest:
      return {}
    try:
      fetched = store.fetch(ctx.reference_digest, ctx.model_uri)
    except Exception as exc:  # pylint: disable=broad-exception-caught
      log_milestone("freetext_pool_store_error", error=type(exc).__name__)
      return {}
    # An empty values array is not a usable pool — build instead of
    # silently generating from nothing.
    return {p.column: list(p.values) for p in fetched if p.values}

  def _resolve_stored_pools(self, ctx: GenerationContext,
                            free_text_columns: int) -> dict[str, list[str]]:
    """Persisted pools for this run, announcing the store's absence.

        The 2026-07-26 1M run spent 26 of its 53 minutes rebuilding pools
        per worker PROCESS purely because nobody passed
        ``--freetext_pools_table`` — and nothing in the logs said so. The
        absence was only discoverable by noticing that
        ``freetext_pool_store_*`` milestones never appeared, which is
        exactly the silence a milestone exists to break.
        """
    if getattr(ctx, "pool_store", None) is None:
      # The build branch blanks its own store by design (self-read
      # guard) — absent-by-configuration is only worth a WARNING when
      # a generate worker really has nothing to read.
      if not getattr(ctx, "pool_branch", False):
        log_milestone(
            "freetext_pool_store_absent",
            level=logging.WARNING,
            free_text_columns=free_text_columns,
            num_rows=ctx.num_rows,
        )
      return {}
    return self._stored_pools(ctx)

  def _take_stored_pool(
      self,
      column: str,
      ctx: GenerationContext,
      stored: dict[str, list[str]],
      pools: dict[str, list[str]],
  ) -> bool:
    """Serve `column` from the persisted store; True when it was served."""
    hit = stored.get(column)
    if hit:
      pools[column] = list(hit)
      self._pool_sources[column] = "store"
      log_milestone(
          "freetext_pool_store_hit", column=column, pool_size=len(hit))
      return True
    if getattr(ctx, "pool_store", None) is not None:
      log_milestone("freetext_pool_store_miss", column=column)
    return False

  def _build_free_text_pools(self,
                             ctx: GenerationContext) -> dict[str, list[str]]:
    """For each FREE_TEXT column, retrieve exemplars and fill a bounded
        unique pool from batched LLM calls. Falls back to observed examples
        when the LLM returns nothing usable."""
    assert self._profiles is not None
    pools: dict[str, list[str]] = {}
    free_text_cols = [
        p for p in self._profiles.values()
        if p.kind is ColumnKind.FREE_TEXT and p.identifier_shape is None
    ]
    free_text_cols = self._skip_expandable_pools(free_text_cols)
    free_text_cols = self._skip_routed_pools(free_text_cols)
    if not free_text_cols:
      return pools

    # Tier 0 — the persisted store (WS5). Cross-PROCESS and
    # authoritative; `_POOL_CACHE` below stays as the intra-process tier
    # that survives setup() retries inside one worker. A store outage is
    # never fatal: pools are an optimisation, not a dependency.
    stored = self._resolve_stored_pools(ctx, len(free_text_cols))

    exemplars = self._retrieve_exemplars(ctx, _DEFAULT_TOP_K)
    chunks_by_column = self._fetch_free_text_chunks(ctx)
    # Phase 1 — sequential: seed-example retrieval touches the embedder
    # (HF fast tokenizers are not thread-safe), and the source-value
    # fetches share one lazily-built BQ client, so both fully complete
    # before any ladder thread spawns.
    jobs: list[tuple[ColumnProfile, list[str], int, frozenset[str]]] = []
    for prof in free_text_cols:
      job = self._collect_pool_job(prof, ctx, stored, pools, exemplars,
                                   chunks_by_column)
      if job is not None:
        jobs.append(job)

    # Phase 2 — parallel: one bounded ladder per column. Collect EVERY
    # result before re-raising the first failure, so sibling columns'
    # completed builds are never discarded by one column's strict raise
    # (the 2026-07-24 16:35 E2E rebuilt all pools 3x for one column).
    if not jobs:
      return pools
    if len(jobs) == 1:
      prof, seed_examples, pool_target, source_values = jobs[0]
      pools[prof.name] = self._infer_free_text_pool(prof, seed_examples,
                                                    pool_target, source_values)
      return pools
    from concurrent.futures import ThreadPoolExecutor

    first_error: Exception | None = None
    # Ladder threads inherit the milestone table scope (ADR 0030):
    # without this, a multi-table worker's pool milestones lose their
    # table tag the moment they hop threads.
    scope_table = milestone_scope_value()
    with ThreadPoolExecutor(
        max_workers=min(_POOL_BUILD_MAX_WORKERS, len(jobs)),
        thread_name_prefix="sdfb-pool",
        initializer=(functools.partial(set_milestone_scope_for_thread,
                                       scope_table) if scope_table else None),
    ) as executor:
      futures = [(
          job,
          executor.submit(self._infer_free_text_pool, *job),
      ) for job in jobs]
      not_ready: list[tuple[tuple, Exception]] = []
      for job, future in futures:
        try:
          pools[job[0].name] = future.result()
        except ModelClientTransientError as e:
          not_ready.append((job, e))
        except Exception as e:  # re-raised below, after all columns land  # pylint: disable=broad-exception-caught
          if first_error is None:
            first_error = e
    first_error = self._retry_not_ready_ladders(not_ready, pools, first_error)
    if first_error is not None:
      raise first_error
    return pools

  def _retry_not_ready_ladders(
      self,
      not_ready: list[tuple[tuple, Exception]],
      pools: dict[str, list[str]],
      first_error: Exception | None,
  ) -> Exception | None:
    """Rebuild, once and sequentially, every ladder whose thread hit a
        transient client condition — the vLLM fit-wait window expiring
        while a sibling's spawn was about to succeed (2026-08-25 R6, ADR
        0033) — against the client its siblings just used. Before this,
        one lost race re-raised after every sibling ladder had finished
        and failed the bundle: Dataflow then re-embedded, re-fetched and
        re-ran the 70 s ladder eight minutes later. A second failure is a
        real outage: it becomes the error the caller raises."""
    for job, err in not_ready:
      prof = job[0]
      log_milestone(
          "freetext_pool_ladder_retried",
          level=logging.WARNING,
          column=prof.name,
          error=type(err).__name__,
      )
      try:
        pools[prof.name] = self._infer_free_text_pool(*job)
      except Exception as e:  # pylint: disable=broad-exception-caught
        if first_error is None:
          first_error = e
    return first_error

  def _collect_pool_job(
      self,
      prof: ColumnProfile,
      ctx: GenerationContext,
      stored: dict[str, list[str]],
      pools: dict[str, list[str]],
      exemplars: list[dict],
      chunks_by_column: dict[str, list],
  ) -> tuple[ColumnProfile, list[str], int, frozenset[str]] | None:
    """Phase-1 resolution for one column: serve it from the store /
        binary fallback / process cache (returns None), or assemble its
        LLM-ladder job."""
    if self._take_stored_pool(prof.name, ctx, stored, pools):
      return None
    if self._take_binary_fallback(prof, ctx, pools):
      return None
    seed_examples = self._column_seed_examples(prof, ctx, _DEFAULT_TOP_K,
                                               chunks_by_column.get(prof.name))
    if not seed_examples:
      seed_examples = [
          e[prof.name] for e in exemplars if e.get(prof.name) not in (None, "")
      ][:_DEFAULT_TOP_K] or list(prof.text_examples[:_DEFAULT_TOP_K])
    # The source filter is fetched BEFORE the target is sized: its
    # size is the column's exact cardinality, already paid for (ADR
    # 0033 — 2026-08-25/26 R6 A_COL_015: sample distinct 94, filter
    # 4,022, target stayed 94 with Tier-2 stats absent).
    source_values = self._fetch_source_values(ctx, prof.name)
    pool_target = self._pool_target(
        prof, ctx, source_cardinality=len(source_values))
    key = self._pool_cache_key(ctx, prof.name, pool_target)
    if key is not None:
      with _POOL_CACHE_LOCK:
        cached = _POOL_CACHE.get(key)
      if cached is not None:
        pools[prof.name] = list(cached)
        self._pool_sources[prof.name] = "process_cache"
        log_milestone(
            "freetext_pool_cache_hit",
            column=prof.name,
            pool_size=len(cached),
        )
        return None
    self._preflight_constraint_examples(prof)
    return (prof, seed_examples, pool_target, source_values)

  @staticmethod
  def _preflight_constraint_examples(prof: ColumnProfile) -> None:
    """Flag a clause example the column's own format gate would reject.

        ADR 0033 (2026-08-25/26 R6 A_COL_037): a fixed 31-char column
        shipped a 28-char fictitious example; the model echoed the
        example's length and 385/393 candidates were format-rejected. The
        gate that rejects them can judge the example BEFORE a round is
        spent — the operator sees `example_len` vs `gate_lengths` in the
        worker log next to `prompt_constraints_found`.
        """
    examples = getattr(prof, "constraint_examples", ()) or ()
    if not examples:
      return
    gate = _format_gate(prof)
    if gate.prose:
      return
    for ex in examples:
      if gate(ex):
        continue
      log_milestone(
          "prompt_constraint_example_off_format",
          level=logging.WARNING,
          column=prof.name,
          example_len=len(ex),
          gate_lengths=(",".join(str(n) for n in sorted(gate.lengths))
                        if gate.lengths is not None else "mask"),
      )

  def _take_binary_fallback(self, prof: ColumnProfile, ctx: GenerationContext,
                            pools: dict) -> bool:
    """Binary payloads mis-stored as STRING (wave-4 v2): the LLM
        ladder cannot emit control bytes — its candidates were
        format-rejected en masse while the column burned the whole cold
        pool phase (COL_048-class: 8.6 min, 41% of wall time). Straight
        to the template fallback; the pool persists like any other."""
    if not is_binary_class(prof.observed_values):
      return False
    source_values = self._fetch_source_values(ctx, prof.name)
    target = self._pool_target(prof, ctx, source_cardinality=len(source_values))
    fallback = self._shape_fallback_pool(prof, target, set(), source_values)
    pools[prof.name] = fallback
    self._pool_sources[prof.name] = "binary_fallback"
    log_milestone(
        "freetext_pool_binary_fallback",
        column=prof.name,
        pool_size=len(fallback),
        target=target,
    )
    return True

  def _skip_expandable_pools(
      self, free_text_cols: list[ColumnProfile]) -> list[ColumnProfile]:
    """Drop columns whose draw path never reads a pool (wave 4).

        Shape-mix-expandable columns draw from their observed shape mix —
        the ladder work (LLM calls, stagnation waits, store writes) was
        dead cost: the 2026-08-20 B_TABLE R1 spent 28.7 min (53% of wall
        time) in PoolTrigger with several such columns.
        `_draws_from_expansion` is the same predicate the draw path uses,
        so a skipped column is GUARANTEED to expand instead."""
    kept: list[ColumnProfile] = []
    for p in free_text_cols:
      if self._draws_from_expansion(p):
        self._pool_sources[p.name] = "expansion_no_pool"
        log_milestone("freetext_pool_skipped_expandable", column=p.name)
      else:
        kept.append(p)
    return kept

  def _route_constraint_samplers(self, ctx: GenerationContext) -> None:
    """Attach a programmatic sampler to every constrained column whose
        clause defines its value space (ADR 0028).

        Tier B (binary payloads, pinned length) wins over Tier P: an LLM
        or grammar cannot emit control bytes, and the replaced source-copy
        fallback landed `copy_ratio_substantive=1.0` against the clause's
        own privacy note (2026-08-21 run). Tier P compiles the clause
        ``pattern`` — draws are format-exact, unbounded, and family-
        weighted. Anything else keeps its existing route untouched.
        """
    assert self._profiles is not None
    if not getattr(ctx, "prompt_constraints", True):
      return
    # PK and identity both mean "unique per run", so both get the
    # emitted-set rejection on routed draws (ADR 0028; identity added
    # 2026-08-25 when a routed identity column had to stop being
    # overwritten by UUID synthesis).
    unique_cols = set(getattr(ctx, "pk_columns", []) or []) | set(
        getattr(ctx, "identity_columns", []) or [])
    for name, prof in self._profiles.items():
      if (prof.kind is not ColumnKind.FREE_TEXT or
          not prof.llm_prompt_constraint):
        continue
      routed = self._route_one(prof)
      if routed is None:
        continue
      route, sampler = routed
      self._routed[name] = (route, sampler)
      self._routed_forbidden[name] = frozenset(
          str(v) for v in prof.observed_values) | self._fetch_source_values(
              ctx, name) | frozenset(prof.constraint_examples)
      if name in unique_cols:
        self._routed_emitted[name] = set()
      log_milestone(
          "freetext_pool_byte_template"
          if route == "byte_template" else "constraint_sampler_active",
          column=name,
          route=route,
          capacity=f"{float(sampler.capacity):.2e}",
          unique=name in unique_cols,
      )

  def _route_one(self, prof: ColumnProfile) -> tuple[str, Any] | None:
    """One column's route decision, from clause fields alone."""
    if is_binary_class(prof.observed_values):
      length = prof.constraint_length or _mode_length(prof.observed_values)
      prefix = prof.constraint_prefix
      if length and length >= len(prefix):
        return (
            "byte_template",
            ByteTemplateSampler(prefix=prefix, length=length),
        )
      return None  # underivable template: keep the legacy fallback
    if prof.constraint_pattern:
      sampler = compile_pattern_sampler(
          prof.constraint_pattern, families=prof.constraint_families)
      if sampler is not None:
        return ("pattern", sampler)
    return None

  def _skip_routed_pools(
      self, free_text_cols: list[ColumnProfile]) -> list[ColumnProfile]:
    """Drop Tier-P/B columns from the pool build (ADR 0028): their
        draw path samples the clause's value space directly, so ladder
        work (LLM calls, store writes, binary fallback) is dead cost —
        and for binary columns, a privacy violation."""
    kept: list[ColumnProfile] = []
    for p in free_text_cols:
      routed = self._routed.get(p.name)
      if routed is not None:
        self._pool_sources[p.name] = routed[0]
      else:
        kept.append(p)
    return kept

  def _routed_draw(self, name: str, rng: random.Random):
    """Per-row draw closure for a routed column: bounded rejection
        against the source domain (and the emitted set on PK columns).
        Saturation raises loudly — never silently emits a source value."""
    route, sampler = self._routed[name]
    forbidden = self._routed_forbidden.get(name, frozenset())
    emitted = self._routed_emitted.get(name)

    def _draw() -> str:
      for _ in range(_ROUTED_DRAW_TRIES):
        v = cast(str, sampler.sample(rng))
        if v in forbidden:
          continue
        if emitted is not None:
          if v in emitted:
            continue
          emitted.add(v)
        return v
      raise RuntimeError(
          f"routed draw stalled for column {name} (route={route}, "
          f"forbidden={len(forbidden)}, "
          f"emitted={len(emitted) if emitted is not None else 0})")

    return _draw

  def _fetch_free_text_chunks(self, ctx: GenerationContext) -> dict[str, list]:
    """Fetch persisted `free_text_col` chunks ONCE for all columns and
        group by `metadata["column"]` (WS2 review: `_column_seed_examples`
        used to issue one identical store query per free-text column — N
        redundant BQ reads per worker setup). Empty dict when there is no
        store or no reference digest to key the fetch."""
    store = cast("ChunkStore | None", ctx.chunk_store)
    if store is None or not ctx.reference_digest:
      return {}
    chunks = store.fetch(
        ctx.reference_digest,
        CHUNK_KIND_FREE_TEXT_COL,
        ctx.embedder_id,
        ctx.embedder_version,
    )
    by_column: dict[str, list] = {}
    for c in chunks:
      column = c.metadata.get("column")
      if column is None or not c.embedding:
        continue
      by_column.setdefault(column, []).append(c)
    return by_column

  def _column_seed_examples(
      self,
      prof: ColumnProfile,
      ctx: GenerationContext,
      k: int,
      column_chunks: list | None,
  ) -> list[str]:
    """Column-relevant seed exemplars for one free-text column
        (WS2 §4b.3): persisted `free_text_col` chunks — pre-fetched ONCE for
        all columns by `_fetch_free_text_chunks` and passed in as
        ``column_chunks`` — when present, else the column's own values
        embedded locally. Empty list ⇒ caller falls back to row-doc
        exemplars."""
    strategy = getattr(ctx, "pool_seed_strategy", "centroid")
    if column_chunks:
      vectors = [list(c.embedding) for c in column_chunks]
      texts = [c.chunk_text for c in column_chunks]
      self._seed_space[prof.name] = (vectors, texts)
      return select_seed_examples(vectors, texts, k, strategy=strategy)
    if self._embedder is not None:
      values: list[str] = []
      # raw values, not str(v): "5" must not dedup against int 5.
      seen: set[object] = set()
      for row in ctx.reference_rows[:_MAX_EMBED_ROWS]:
        v = row.get(prof.name)
        if v not in (None, "") and v not in seen:
          seen.add(v)
          values.append(str(v))
      if values:
        if len(values) <= k:
          return values
        vectors = self._embedder.embed(values)
        self._seed_space[prof.name] = (vectors, values)
        return select_seed_examples(vectors, values, k, strategy=strategy)
    return []

  def _fetch_source_values(
      self,
      ctx: GenerationContext,
      column: str,
      milestone: str = "freetext_pool_source_filter",
  ) -> frozenset[str]:
    """The column's FULL distinct source values, or an empty set.

        Empty (= filter inactive) on: no store attached, cardinality above
        the store's cap, or a store error — the last two LOUDLY, because a
        pool built without the filter can memorize (2026-08-05 B_TABLE R1:
        33-99% verbatim source values on 10 columns). ``milestone`` names
        the consumer: the pool ladder keeps its historical name, the
        identifier route logs `identifier_source_filter`.
        """
    store = getattr(ctx, "source_value_store", None)
    if store is None:
      return frozenset()
    try:
      values = store.fetch_distinct(column)
    except Exception as exc:  # pylint: disable=broad-exception-caught
      log_milestone(
          f"{milestone}_error",
          level=logging.WARNING,
          column=column,
          error=type(exc).__name__,
      )
      return frozenset()
    if values is None:
      log_milestone(
          f"{milestone}_absent",
          level=logging.WARNING,
          column=column,
      )
      return frozenset()
    log_milestone(milestone, column=column, size=len(values))
    return frozenset(values)

  def _pool_cache_key(self, ctx: GenerationContext, column: str,
                      target: int) -> tuple[str, str, str, int] | None:
    if not ctx.reference_digest:
      return None
    return (ctx.reference_digest, ctx.model_uri, column, target)

  def _pool_target(
      self,
      prof: ColumnProfile,
      ctx: GenerationContext,
      source_cardinality: int = 0,
  ) -> int:
    """min(num_rows, column_distinct, _FREE_TEXT_POOL_MAX), skipping
        unknown (zero/empty) bounds. `column_distinct` prefers the Tier-2
        exact count (`ctx.source_distinct`, ADR 0022), then the source
        filter's cardinality (`source_cardinality`, ADR 0033 — the filter
        IS the exact distinct set when it is under the store's cap), and
        only then the sample distinct, which under-estimates true
        cardinality and starved pools (five-run verdict: sample 95 vs
        source 4k; 2026-08-25/26 R6: 94 vs 4,022 with Tier-2 absent)."""
    bounds = [_FREE_TEXT_POOL_MAX]
    if ctx.num_rows > 0:
      bounds.append(ctx.num_rows)
    distinct = (
        ctx.source_distinct.get(prof.name, 0) or source_cardinality or
        len(set(prof.observed_values)))
    if distinct > 0:
      bounds.append(distinct)
    return max(min(bounds), 1)

  def _column_constraint(self, prof: ColumnProfile) -> str:
    """Per-column prompt steering: DDL constraint (spec C5) + measured
        length band. Gated together by ``prompt_constraints``; both are
        constant per column, so the pool prompt keeps a byte-identical
        shared prefix for vLLM automatic prefix caching (ADR 0018)."""
    if not getattr(self._ctx, "prompt_constraints", True):
      return ""
    # A user-pinned length makes the derived band redundant tokens.
    derived = ("" if getattr(prof, "constraint_sets_length", False) else
               length_hint(prof.observed_values))
    return " ".join(s for s in (prof.llm_prompt_constraint, derived) if s)

  def _retrieve_exemplars(self, ctx: GenerationContext, k: int) -> list[dict]:
    """Top-k reference rows nearest the reference centroid.

        For setup-time distribution inference we condition on the densest
        region of the reference (its centroid's neighbors), giving the LLM a
        representative exemplar set. Deterministic given the index.
        """
    if self._index is None or not self._ref_vectors or self._embedder is None:
      return list(ctx.reference_rows[:k])
    # Exemplar ids index into the same _MAX_EMBED_ROWS prefix the
    # vectors were built from, so ctx.reference_rows[i] stays valid.
    return retrieve_centroid_top_k(self._index, self._ref_vectors,
                                   ctx.reference_rows, k)

  def _rotating_prompt(self, prof: ColumnProfile, per_call: int):
    """A per-attempt prompt builder for the `kcenter_rotate` arm, else
        None.

        `centroid` and `kcenter` keep a byte-identical prompt prefix so vLLM
        prefix caching still applies (ADR 0018); only this arm trades that
        away, which is cheap now a pool is built once per digest (WS5 §2).
        """
    if getattr(self._ctx, "pool_seed_strategy", "centroid") != "kcenter_rotate":
      return None
    space = self._seed_space.get(prof.name)
    if space is None:
      return None
    vectors, texts = space

    constraint = self._column_constraint(prof)

    def _builder(attempt: int) -> tuple[str, list[str]]:
      rotated = select_seed_examples(
          vectors,
          texts,
          _DEFAULT_TOP_K,
          strategy="kcenter_rotate",
          attempt=attempt,
      )
      return (
          _build_pool_prompt(
              prof.name, per_call, rotated, constraint=constraint),
          rotated,
      )

    return _builder

  def _pool_json_schema(self, prof: ColumnProfile) -> tuple[dict, bool]:
    """Guided-decoding schema for one column's pool call.

        A user-supplied pattern (ADR 0024) constrains decoding itself —
        prompting alone does not guarantee format adherence — and takes
        precedence over the derived charset/length regex (the Layer-2
        opt-in, `pool_pattern_guidance`): the derived union pattern is
        deliberately looser (see relaxed_shapes_pattern), so novelty
        pressure stays with the sampler, not the grammar.
        """
    items_schema: dict = {"type": "string"}
    pattern_guided = False
    if (getattr(prof, "constraint_pattern", "") and self._ctx is not None and
        getattr(self._ctx, "prompt_constraints", True)):
      items_schema["pattern"] = prof.constraint_pattern
      pattern_guided = True
    elif self._ctx is not None and self._ctx.pool_pattern_guidance:
      # Prefer the exact shape mix (2026-08-05 spec C2) — its union
      # pattern is tighter than the length-bucket relaxation; fall
      # back to the relaxed builder when no mix exists (e.g. prose).
      pattern_shapes = prof.shape_mix or build_relaxed_shapes(
          [str(v) for v in prof.observed_values])
      if pattern_shapes is not None:
        items_schema["pattern"] = relaxed_shapes_pattern(pattern_shapes)
        pattern_guided = True
    json_schema = {
        "type": "object",
        "properties": {
            "values": {
                "type": "array",
                "items": items_schema
            }
        },
        "required": ["values"],
    }
    return json_schema, pattern_guided

  def _infer_free_text_pool(
      self,
      prof: ColumnProfile,
      seed_examples: list[str],
      target: int,
      source_values: frozenset[str] = frozenset(),
  ) -> list[str]:
    """Fill a bounded unique pool for one free-text column from batched
        LLM calls conditioned on retrieved exemplars."""
    assert self._client is not None
    t_column = time.monotonic()
    self._pool_sources[prof.name] = "llm_ladder"
    per_call = min(target, _POOL_VALUES_PER_CALL)
    constraint = self._column_constraint(prof)
    prompt = _build_pool_prompt(
        prof.name, per_call, seed_examples, constraint=constraint)
    log_prompt_debug(
        getattr(self._ctx, "prompt_debug", "off"),
        prof.name,
        prompt,
        _build_pool_prompt(
            prof.name,
            per_call,
            seed_examples,
            constraint=constraint,
            seeds_repr=f"<{len(seed_examples)} seeds elided>",
        ),
    )
    # ARRAY completions only — never n single-value choices. A choice is
    # blind to its siblings, so "distinct" is unsatisfiable per
    # single-value completion and vLLM collapsed all 32 into the
    # identical modal exemplar echo (2026-07-16 runs: distinct=1,
    # prompt_echoes=96). Inside one array completion the model sees what
    # it already wrote; _pool_llm_yield rides n such arrays per round
    # trip and de-dupes across them.
    json_schema, pattern_guided = self._pool_json_schema(prof)
    # kcenter_rotate is the only arm that varies the prompt across
    # attempts; centroid/kcenter keep a byte-identical prefix so vLLM
    # prefix caching still applies (ADR 0018).
    prompt_for_attempt = self._rotating_prompt(prof, per_call)

    try:
      y = _pool_llm_yield(
          self._client,
          prompt,
          json_schema,
          prof,
          seed_examples,
          target=target,
          prompt_for_attempt=prompt_for_attempt,
          source_values=source_values,
      )
    except ModelClientTransientError:
      # The client was not usable — not "the LLM yielded nothing".
      # Never the exemplar fallback (lax mode included): the caller
      # retries this column once its siblings land (ADR 0033).
      raise
    except Exception as e:  # pylint: disable=broad-exception-caught
      if self._ctx is not None and self._ctx.strict_freetext:
        raise
      # Per-call generation failure: exemplar fallback is allowed, but
      # NEVER silently — a run where the LLM contributed nothing must be
      # visible in worker logs (E2E report §4.2: 100 % memorization).
      log_milestone(
          "freetext_llm_fallback",
          level=logging.WARNING,
          column=prof.name,
          error=type(e).__name__,
      )
      pool = []
      format_rejected = 0
      self._pool_build_info[prof.name] = {
          "target": target,
          "attempts": 0,
          "stagnated": False,
      }
    else:
      pool = self._resolve_pool_yield(prof, y, per_call, target, source_values)
      format_rejected = y.format_rejected
      self._pool_build_info[prof.name] = {
          "target": target,
          "attempts": y.attempts,
          "stagnated": y.stagnated,
      }

    # Fold observed exemplars ONLY when the LLM delivered nothing (lax
    # mode) — loudly, via the fallback milestone emitted above. Every
    # FREE_TEXT column is high-cardinality by classification, so folding
    # real values on top of a delivered pool is memorization, not
    # fidelity: the earlier is_unique_valued-only guard left shared-key
    # columns folding 64 real exemplars each (2026-07-16 E2E: 9 columns
    # at copy_ratio 0.475-0.939; 2026-07-15: copy_ratio=1.0 on all 8
    # unique-valued ones).
    if not pool:
      for ex in prof.text_examples:
        if ex not in pool:
          pool.append(ex)
    # De-dup, preserve order, bound the pool size.
    seen: dict[str, None] = {}
    for v in pool:
      if v not in seen:
        seen[v] = None
    final = list(seen.keys())[:max(target, len(prof.text_examples))]
    if final and self._ctx is not None:
      key = self._pool_cache_key(self._ctx, prof.name, target)
      if key is not None:
        with _POOL_CACHE_LOCK:
          _POOL_CACHE[key] = tuple(final)
    # Per-column build summary — the only milestone that reports
    # format_rejected on a CLEAN build (stagnated/undersized/fallback
    # cover the unhealthy paths), and per-ladder seconds that
    # disaggregate b1_pools_built (whose total absorbs the lazy vLLM
    # ignition inside the first column's first call).
    log_milestone(
        "freetext_pool_built",
        column=prof.name,
        pool_size=len(final),
        target=target,
        format_rejected=format_rejected,
        pattern_guided=pattern_guided,
        seconds=round(time.monotonic() - t_column, 1),
    )
    return final

  def _resolve_pool_yield(
      self,
      prof: ColumnProfile,
      y: _PoolYield,
      per_call: int,
      target: int,
      source_values: frozenset[str] = frozenset(),
  ) -> list[str]:
    """Turn one column's ladder outcome into its final pool: shape
        fallback for copy-saturated builds, shape top-up for undersized
        ones, a strict raise (or loud lax milestone) when nothing usable
        exists."""
    pool = y.pool
    if not pool and y.parsed > 0:
      # Copy-saturated: the model parsed values but every one was
      # an observed copy — deterministic on rebuild, so retrying or
      # failing the bundle buys nothing (the 2026-07-24 16:35 E2E
      # burned 2 full setup() retries exactly here). A relaxed
      # template can still generate verified-novel in-format values.
      shape_pool = self._shape_fallback_pool(
          prof, target, exclude=set(), source_values=source_values)
      if shape_pool:
        log_milestone(
            "freetext_pool_shape_fallback",
            level=logging.WARNING,
            column=prof.name,
            pool_size=len(shape_pool),
            target=target,
            attempts=y.attempts,
            parsed=y.parsed,
            verbatim_copies=y.copies,
            prompt_echoes=y.prompt_echoes,
        )
        return shape_pool
    if not pool:
      # The calls "succeeded" (no exception) yet yielded nothing
      # usable. Counts (never values — reference data must not
      # leak into logs) say WHY: parsed=0 means every choice was
      # dropped at JSON parse; low distinct with prompt_echoes ==
      # verbatim_copies means the model parroted the few exemplars
      # it was SHOWN (sampling/prompt defect); high distinct with
      # prompt_echoes ~ 0 means in-format generations collided with
      # the FULL reference sample the model never saw — a saturated
      # key space where per-column novelty is unattainable.
      diagnosis = (f"attempts={y.attempts}, requested_per_attempt="
                   f"{per_call}, parsed={y.parsed}, "
                   f"distinct={y.distinct}, verbatim_copies={y.copies}, "
                   f"prompt_echoes={y.prompt_echoes}, "
                   f"format_rejected={y.format_rejected}, novel=0")
      if self._ctx is not None and self._ctx.strict_freetext:
        raise FreeTextEmptyYieldError(
            f"LLM calls for free-text column {prof.name!r} "
            f"yielded no usable values ({diagnosis}).")
      log_milestone(
          "freetext_llm_fallback",
          level=logging.WARNING,
          column=prof.name,
          error="EmptyYield",
          attempts=y.attempts,
          parsed=y.parsed,
          distinct=y.distinct,
          verbatim_copies=y.copies,
          prompt_echoes=y.prompt_echoes,
          format_rejected=y.format_rejected,
      )
    elif len(pool) < target:
      # Top up an undersized pool from the template before
      # accepting the shortfall — the 2026-07-24 16:35 run landed
      # COL_052 with 31 distinct values over 1000 rows
      # (diversity collapse).
      top_up = self._shape_fallback_pool(
          prof,
          target - len(pool),
          exclude=set(pool),
          source_values=source_values,
      )
      if top_up:
        log_milestone(
            "freetext_pool_shape_topup",
            level=logging.WARNING,
            column=prof.name,
            added=len(top_up),
            pool_size=len(pool) + len(top_up),
            target=target,
            attempts=y.attempts,
        )
        pool = [*pool, *top_up]
      if len(pool) < target:
        # Every escalation level ran and the pool is still short
        # of target: the column lands with whatever novelty the
        # LLM delivered, but never silently — the 2026-07-17 E2E
        # run accepted a 4-value pool for COL_048 without a trace.
        log_milestone(
            "freetext_pool_undersized",
            level=logging.WARNING,
            column=prof.name,
            pool_size=len(pool),
            target=target,
            attempts=y.attempts,
            parsed=y.parsed,
            distinct=y.distinct,
            verbatim_copies=y.copies,
            prompt_echoes=y.prompt_echoes,
            format_rejected=y.format_rejected,
        )
    return pool

  def _shape_fallback_pool(
      self,
      prof: ColumnProfile,
      count: int,
      exclude: set[str],
      source_values: frozenset[str] = frozenset(),
  ) -> list[str]:
    """Verified-novel values from a relaxed per-position template, or [].

        B.2-parity route (freetext.py:_shape_fallback_pool) for copy-saturated
        or undersized LLM builds: mixed-length identifier-ish columns the
        strict detector rejects still template per length bucket, and every
        emitted value is rejected against the observed reference values (and
        `exclude`, and itself) — nothing here can memorize. Prose columns
        (whitespace) return [] and the caller keeps its existing raise/
        fallback path. Deterministic per (run_id, column).
        """
    # The exact shape mix preserves literal fixed-position runs (leading
    # padding, delimiters, constant prefixes) the length-bucket
    # relaxation collapses — 2026-08-05 B_TABLE R1: 4/13 columns
    # reproduced 0% of source shapes under the relaxed fallback.
    shapes = None
    if shape_mix_can_template(prof.shape_mix):
      shapes = prof.shape_mix
    if shapes is None:
      shapes = build_relaxed_shapes([str(v) for v in prof.observed_values])
    if shapes is None or count <= 0:
      return []
    run_id = self._ctx.pipeline_run_id if self._ctx is not None else ""
    rng = random.Random(_mix_seed(None, f"{run_id}:shape:{prof.name}"))
    observed = {str(v) for v in prof.observed_values}
    out: list[str] = []
    seen: set[str] = set()
    # Bounded rejection sampling: dense keyspaces stop at the cap
    # instead of spinning (same 40x budget as B.2).
    for _ in range(count * 40):
      v = sample_relaxed_identifier(shapes, rng.randrange)
      if v in observed or v in exclude or v in seen or v in source_values:
        continue
      seen.add(v)
      out.append(v)
      if len(out) >= count:
        break
    return out

  # -- helpers ------------------------------------------------------------

  def _make_rng(self, seed: int | None, use_numpy: bool):
    mixed = _mix_seed(seed, "bulk")
    if use_numpy:
      import numpy as np

      return np.random.default_rng(mixed)
    return random.Random(mixed)


class _PoolYield(NamedTuple):
  """Outcome counts of the escalating-sampling pool calls for one column.

    ``copies`` are values found anywhere in the FULL observed reference
    sample; ``prompt_echoes`` is the subset that was actually SHOWN to the
    model as a seed exemplar. The gap between the two separates parroting
    (sampling/prompt defect) from reference collisions (saturated key space).
    """

  pool: list[str]
  parsed: int
  distinct: int
  copies: int
  prompt_echoes: int
  attempts: int
  format_rejected: int = 0
  # True when the ladder exited on yield-decay (the stagnation break), as
  # opposed to reaching target or exhausting the attempt budget. Persisted
  # per column into `freetext_pools.stagnated` (2026-07-29: rows stored
  # hardcoded false because nothing recorded this).
  stagnated: bool = False


def _mode_length(values: tuple[object, ...]) -> int:
  """Most common substantive value length, 0 when nothing substantive —
    the Tier-B length stand-in when the clause does not pin one."""
  lengths = Counter(
      len(str(v)) for v in values if v is not None and str(v).strip())
  if not lengths:
    return 0
  return lengths.most_common(1)[0][0]


def _build_pool_prompt(
    column: str,
    per_call: int,
    seed_examples: list[str],
    constraint: str = "",
    seeds_repr: str | None = None,
) -> str:
  """The pool prompt. One definition — the kcenter_rotate arm rebuilds it
    per attempt with a different seed set, and the two must not drift.

    `constraint` is a per-column CONSTANT (parsed from the DDL description,
    spec C5): the prompt stays byte-identical across attempts, preserving
    vLLM prefix caching (ADR 0018). Empty ⇒ byte-identical to the pre-C5
    prompt (regression-pinned in tests). ``seeds_repr`` substitutes the
    seed-example interpolation — the `--prompt_debug=redacted` rebuild
    (ADR 0024 §3c); None ⇒ unchanged."""
  examples = str(seed_examples) if seeds_repr is None else seeds_repr
  prompt = (f"You generate synthetic tabular data. First identify the exact "
            f"format of these example values for the column '{column}' "
            f"(e.g. UUID, hexadecimal identifier, numeric code, date, "
            f"timestamp, natural-language text), then generate "
            f"{per_call} NEW, distinct, fictitious values in "
            f"exactly that format. Never copy an example verbatim. "
            f'Examples: {examples}. Return JSON {{"values": [...]}}.')
  if constraint:
    prompt += f" Column constraint: {constraint}."
  return prompt


class _FormatGate:
  """Format-plausibility gate over pool candidates for one column.

    Identifier-ish columns (relaxed template exists): observed length
    bucket + observed charset + no column-name echo. Whitespace columns
    disable that gate entirely — which let the 2026-08-09 B_TABLE R1 pool
    fill with whitespace-NORMALIZED values (COL_038: source ␣␣␣ →
    synthetic ␣ on all 512, shape recall 0) — so when the shape mix can
    template, candidates must instead reproduce an observed run-collapsed
    mask: digit/letter run lengths stay free (novelty), whitespace runs
    and punctuation are exact. Prose columns (neither applies) pass all
    — `prose` is True there, so callers skip the example preflight.
    `length_blind` (prose OR mask-gated) is where the fixed-width length
    ceiling applies (ADR 0033 / ADR 0034).
    """

  def __init__(self, prof: ColumnProfile) -> None:
    shapes = build_relaxed_shapes([str(v) for v in prof.observed_values])
    self.lengths = relaxed_shape_lengths(shapes) if shapes else None
    self._charset = relaxed_shape_charset(shapes) if shapes else None
    self._masks: set[str] | None = None
    if shapes is None and shape_mix_can_template(prof.shape_mix):
      self._masks = {collapsed_mask(str(v)) for v in prof.observed_values if v}
    self._name_lower = prof.name.lower()
    self.prose = self.lengths is None and self._masks is None
    # A mask gate collapses letter/digit runs and cannot see length
    # (ADR 0034: A_COL_019 ran to 48 chars under a mask gate against a
    # 35-char source); only the length-bucket gate pins length itself.
    self.length_blind = self.prose or self._masks is not None

  def __call__(self, v: str) -> bool:
    if self.lengths is not None and self._charset is not None:
      # `lengths` is narrowed to a set by the `is not None` guard above.
      return (len(v) in self.lengths and set(v) <= self._charset and  # pylint: disable=unsupported-membership-test
              self._name_lower not in v.lower())
    if self._masks is not None:
      return (collapsed_mask(v) in self._masks and
              self._name_lower not in v.lower())
    return True


def _format_gate(prof: ColumnProfile) -> _FormatGate:
  return _FormatGate(prof)


def _pool_llm_yield(
    client: ModelClient,
    prompt: str,
    json_schema: dict,
    prof: ColumnProfile,
    seed_examples: list[str],
    target: int = _DEFAULT_FREE_TEXT_POOL,
    n_choices: int = _POOL_PARALLEL_CHOICES,
    prompt_for_attempt=None,
    source_values: frozenset[str] = frozenset(),
) -> _PoolYield:
  """Run the pool call at escalating sampling levels, accumulating novel
    values until the pool reaches ``target``. Breaking on the FIRST
    non-empty yield let one conservative completion (still under the served
    model's generation_config truncation pin) define the whole pool — the
    2026-07-17 E2E run landed a 4-value pool over 1000 rows and the
    unclamped retry levels never executed.

    Calls are bounded at `max(len(levels), 2*ceil(target/_POOL_VALUES_PER_CALL))`,
    cycling the escalation ladder (last level repeats). After every level has
    run once, `_POOL_STAGNATION_WINDOW` consecutive low-novelty attempts end
    the loop early (`freetext_pool_stagnated` milestone) — the 2026-07-23 E2E
    run burned 30 min of T4 setup on attempts that only re-emitted duplicates.

    No request seed: a pinned seed with n>1 collapses all n vLLM choices
    into one completion (2026-07-15 run). Unseeded, the n choices sample
    independently, so one round trip carries n distinct 32-value arrays —
    the call budget scales down by the same factor (`per_round`).
    """
  # Rejection set: the profiled sample PLUS (when a SourceValueStore is
  # attached) the column's full source domain — a candidate equal to ANY
  # real value is a copy, whether the profiler sampled it or not
  # (2026-08-05 B_TABLE R1: 33-99% verbatim values from exactly this gap).
  # Constraint examples are canonical fictitious values from the DDL
  # description (ADR 0024) — a verbatim echo must never land as data.
  observed = (
      set(prof.observed_values)
      | source_values
      | set(getattr(prof, "constraint_examples", ()) or ()))
  shown = set(seed_examples)
  # Format-plausibility gate (2026-07-25 10:52 E2E): novelty alone let
  # hallucinated meta-tokens into the pool — an echo of the COLUMN NAME
  # from the prompt and an echo of the prompt's own format examples
  # ('UUID-…') are trivially "novel". For identifier-ish columns (a
  # relaxed template exists) a candidate must also match an observed
  # length bucket, stay within the observed charset, and never contain
  # the column name. Prose columns (no template) skip the gate.
  in_format = _format_gate(prof)
  # Length ceiling for length-blind gates (ADR 0033 prose, ADR 0034
  # masks): a fixed-width source field truncates at its width; the prose
  # gate passes everything and the mask gate collapses runs, so the
  # prompt's length band was advisory (2026-08-26 R6 A_COL_019: source
  # max 35, pool values to 62; 2026-08-29 under a mask gate: to 48).
  # Enforce it the way the source does — by truncation — BEFORE the
  # format and novelty checks, so a clamped value is still rejected if
  # it collides with a real one.
  ceiling = (
      length_ceiling(prof.observed_values) if in_format.length_blind else None)
  n_clamped = 0
  collapse_rounds = 0

  pool: list[str] = []
  pool_seen: set[str] = set()
  seen: set[str] = set()
  n_parsed = 0
  n_copies = 0
  n_echoes = 0
  n_format_rejected = 0
  attempts = 0
  levels = escalating_sampling()
  per_round = _POOL_VALUES_PER_CALL * max(1, n_choices)
  max_calls = max(len(levels), 2 * -(-target // per_round))
  stagnant = 0
  hit_stagnation = False
  while attempts < max_calls and len(pool) < target:
    level = levels[min(attempts, len(levels) - 1)]
    # kcenter_rotate (WS5 §3): re-seed the prompt each attempt so the
    # model sees a different region of the column's manifold. Seeds come
    # from a vector space captured BEFORE any thread spawned, so this
    # never touches the (thread-unsafe) embedder from here.
    call_prompt = prompt
    if prompt_for_attempt is not None:
      call_prompt, rotated_seeds = prompt_for_attempt(attempts)
      shown.update(rotated_seeds)
    attempts += 1
    results = client.generate_json(
        prompt=call_prompt,
        json_schema=json_schema,
        n=max(1, n_choices),
        max_tokens=2048,
        temperature=level.temperature,
        top_p=level.top_p,
        top_k=level.top_k,
    )
    parsed_values, clamped = _clamp_to_ceiling(
        _string_values(results, prof.name), ceiling)
    n_clamped += clamped
    values = [v for v in parsed_values if in_format(v)]
    n_format_rejected += len(parsed_values) - len(values)
    # Format collapse: a full-yield round with zero in-format values.
    collapse_rounds = _collapse_rounds(collapse_rounds, parsed_values, values)
    if collapse_rounds >= _POOL_FORMAT_COLLAPSE_ROUNDS:
      n_parsed += len(parsed_values)
      log_milestone(
          "freetext_pool_format_collapse",
          level=logging.WARNING,
          column=prof.name,
          attempts=attempts,
          parsed=n_parsed,
          format_rejected=n_format_rejected,
          pool_size=len(pool),
          target=target,
      )
      hit_stagnation = True
      break
    n_parsed += len(parsed_values)
    added, copies, echoes = _absorb_round(values, observed, shown, pool,
                                          pool_seen)
    n_copies += copies
    n_echoes += echoes
    seen.update(values)
    stagnant = stagnant + 1 if added < _POOL_STAGNATION_MIN_NOVEL else 0
    if stagnant >= _POOL_STAGNATION_WINDOW and attempts >= len(levels):
      log_milestone(
          "freetext_pool_stagnated",
          level=logging.WARNING,
          column=prof.name,
          attempts=attempts,
          pool_size=len(pool),
          target=target,
          format_rejected=n_format_rejected,
      )
      hit_stagnation = True
      break
  if n_clamped:
    log_milestone(
        "freetext_pool_length_clamped",
        column=prof.name,
        clamped=n_clamped,
        max_len=ceiling,
    )
  return _PoolYield(
      pool,
      n_parsed,
      len(seen),
      n_copies,
      n_echoes,
      attempts,
      n_format_rejected,
      hit_stagnation,
  )


def _absorb_round(
    values: list[str],
    observed: set[object],
    shown: set[str],
    pool: list[str],
    pool_seen: set[str],
) -> tuple[int, int, int]:
  """Fold one round's in-format values into the pool; returns (novel
    values added, copies of observed/source values, prompt-seed echoes)."""
  novel = [v for v in values if v not in observed]
  added = 0
  for v in novel:
    if v not in pool_seen:
      pool_seen.add(v)
      pool.append(v)
      added += 1
  echoes = sum(1 for v in values if v in shown)
  return added, len(values) - len(novel), echoes


def _clamp_to_ceiling(values: list[str],
                      ceiling: int | None) -> tuple[list[str], int]:
  """Truncate candidates past a fixed-width ceiling (ADR 0033); returns
    the values and how many were clamped. None ceiling ⇒ untouched."""
  if ceiling is None:
    return values, 0
  n_long = sum(1 for v in values if len(v) > ceiling)
  if not n_long:
    return values, 0
  return [v[:ceiling] for v in values], n_long


def _collapse_rounds(streak: int, parsed: list[str],
                     in_format: list[str]) -> int:
  """Consecutive full-yield rounds (≥ one array's worth parsed) with zero
    in-format values — the format-collapse signature (ADR 0033)."""
  if len(parsed) >= _POOL_VALUES_PER_CALL and not in_format:
    return streak + 1
  return 0


def _string_values(results: list, name: str) -> list[str]:
  """Extract non-empty string values from LLM pool results.

    Primary shape is the guided ``{"values": [...]}`` array; column-keyed
    dicts (the FakeModelClient's echo mode) are tolerated. Novelty filtering
    (dropping values that equal observed reference values — copies, not
    generations) happens in the caller so parsed-vs-copied counts stay
    visible: an all-copies response must be diagnosable as such, not
    misreported as a parse failure (2026-07-16 corp run)."""
  out: list[str] = []
  for r in results:
    if not isinstance(r, dict):
      continue
    if isinstance(r.get("values"), list):
      out.extend(str(v) for v in r["values"] if v)
      continue
    val = r.get(name)
    if isinstance(val, str) and val:
      out.append(val)
  return out


def _mix_seed(seed: int | None, salt: str) -> int:
  """Derive a stable sub-stream seed from (seed, salt).

    Keeps independent RNG streams (bulk vs free-text) reproducible without
    them sharing state. A None seed maps to a fixed default so output stays
    deterministic across calls — the contract requires same-seed
    reproducibility, and a default makes the no-seed case stable too.
    """
  base = 0 if seed is None else int(seed)
  h = 1469598103934665603  # FNV offset basis (64-bit)
  for ch in f"{base}:{salt}":
    h = (h ^ ord(ch)) * 1099511628211
    h &= 0xFFFFFFFFFFFFFFFF
  return h


__all__ = ["B1RagEngine", "clear_free_text_pool_cache"]
