"""The `GenerationEngine` ABC and its supporting types.

This is the single seam between the Beam pipeline and the synthesis
logic. The pipeline (Beam DAG, M1 §8) constructs an engine in
`DoFn.setup()` from a CLI flag and calls `generate_batch()` per request.
It never knows whether it's holding a B.1 RAG engine or a B.2
library-wrapper — both satisfy this interface.

REF: https://beam.apache.org/documentation/ml/large-language-modeling/
REF: https://beam.apache.org/releases/pydoc/current/apache_beam.ml.inference.base.html
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from typing import NamedTuple, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from sdfb_core.contracts import GeneratedRecord, TableSchema
from sdfb_core.engines.fanout import FanoutPlan, KeyDraw, joint_key_draw
from sdfb_core.observability import log_milestone


class FreeTextEmptyYieldError(RuntimeError):
  """The LLM call for a free-text pool succeeded but yielded zero usable
    values (e.g. every choice was dropped at JSON parse).

    Raised under ``strict_freetext`` so a run whose LLM contributes nothing
    fails loudly instead of silently degrading to exemplar memorization —
    the 2026-07-15 E2E failure mode (all 8x32 guided-JSON choices dropped,
    gates PASSED, copy_ratio=1.0 on every free-text column).
    """


class ModelClientTransientError(RuntimeError):
  """The model client could not serve THIS call, but the condition is
    expected to clear without operator action (ADR 0033).

    Raised by a `ModelClient` when the server is not (yet) usable for a
    reason that resolves on its own — a GPU transiently too full to host
    the model while sibling embedders demote. The engine treats it as
    "not ready", never as "the LLM yielded nothing": a ladder thread that
    hits it is retried in-process once its siblings have landed, and it is
    NEVER swallowed into the lax exemplar fallback (the 2026-07-10 root
    cause — setup() never ran — was universal memorization). 2026-08-25
    R6: one thread's fit-wait expired 60 s before a sibling's spawn
    succeeded; the bundle failed after 10 min of finished sibling work.
    """


def escalating_temperatures(start: float = 0.7) -> tuple[float, ...]:
  """Sampling temperatures for free-text pool retries, ascending from
    ``start`` up to 1.3.

    An LLM pool call whose NOVEL yield is empty (every value a verbatim copy
    of a reference value — the 2026-07-16 corp run, where Qwen3-4B echoed
    the seed exemplars on all 32 choices) is retried at each successive
    temperature before the engine gives up. Higher temperature diversifies
    sampling away from the exemplar echoes; 1.3 is the ceiling B.2 already
    uses for maximum divergence (`similarity_to_temperature`).
    """
  ceiling = 1.3
  start = min(max(start, 0.0), ceiling)
  steps = (1.0, ceiling)
  return (start, *(t for t in steps if t > start + 1e-9))


class SamplingLevel(NamedTuple):
  """One free-text pool attempt's sampling configuration.

    ``None`` for ``top_p`` / ``top_k`` leaves the served model's own defaults
    (its ``generation_config.json``) in force; explicit values override them
    per-request.
    """

  temperature: float
  top_p: float | None = None
  top_k: int | None = None


def escalating_sampling(start: float = 0.7) -> tuple[SamplingLevel, ...]:
  """Sampling configurations for free-text pool retries.

    Temperature alone is NOT enough: a served model can pin sampling
    truncation via its shipped ``generation_config.json`` (Qwen3-4B:
    ``top_k=20, top_p=0.8`` — vLLM logs an override warning). When the model
    is confident in echoing an exemplar, that nucleus collapses to the echo
    token and temperature has nothing left to diversify — the 2026-07-16 corp
    run produced 96/96 verbatim copies at 0.7, 1.0 AND 1.3. So the first
    attempt keeps the vendor-tuned defaults, and every retry escalates
    temperature (via `escalating_temperatures`) with the truncation fully
    unclamped: ``top_p=1.0`` and ``top_k=0`` (vLLM: consider all tokens). A
    ``start`` already at the temperature ceiling still gets one unclamped
    retry — that is the retry that can actually change the outcome.
    """
  temps = escalating_temperatures(start)
  retries = [SamplingLevel(t, top_p=1.0, top_k=0) for t in temps[1:]]
  if not retries:
    retries = [SamplingLevel(temps[0], top_p=1.0, top_k=0)]
  return (SamplingLevel(temps[0]), *retries)


@runtime_checkable
class ModelClient(Protocol):
  """Thin facade engines call to invoke the LLM.

    Real implementation: `sdfb_beam.handlers.vllm_client.VLLMModelClient`
    (M1 §9, M4-only). Test implementation:
    `sdfb_tests.fakes.FakeModelClient`. Engines never know which is in
    use — they import only this Protocol.

    The contract is intentionally narrow: a JSON-schema-guided generation
    call. The vLLM backend (via Beam's `VLLMCompletionsModelHandler`)
    enforces the schema with vLLM's guided decoding; outlines /
    lm-format-enforcer are fallback knobs. See ADR 0011.

    REFs:
      - docs/adr/0011-adopt-beam-vllm-model-handler.md
      - https://docs.vllm.ai/en/latest/usage/structured_outputs.html
    """

  def generate_json(
      self,
      prompt: str,
      json_schema: dict,
      *,
      max_tokens: int = 2048,
      temperature: float = 0.7,
      n: int = 1,
      seed: int | None = None,
      top_p: float | None = None,
      top_k: int | None = None,
  ) -> list[dict]:
    """Return up to `n` JSON dicts conforming to `json_schema`.

        ``top_p`` / ``top_k`` are per-request truncation overrides: ``None``
        keeps the served model's defaults (its ``generation_config.json``);
        explicit values override them (``top_k=0`` = consider all tokens).
        See `escalating_sampling` for why retries must send them.
        """


class GenerationConfig(BaseModel):
  """Per-batch knobs for a generation call.

    `similarity` is engine-interpreted: 0.0 = pure random (within schema),
    1.0 = mimic reference closely. B.2 maps it to library sampling
    temperature; B.1 maps it to a retrieval-vs-perturbation balance.
    """

  model_config = ConfigDict(frozen=True)

  similarity: float = Field(default=0.5, ge=0.0, le=1.0)
  batch_size: int = Field(default=16, ge=1)
  max_retries: int = Field(default=2, ge=0)
  seed: int | None = None
  engine_specific: dict = Field(default_factory=dict)


class GenerationContext(BaseModel):
  """Per-worker setup context.

    Stable across all `generate_batch` calls within a single Beam
    worker's lifetime — built once from side inputs (DDL + reference
    rows + digest + run id) and handed to `setup()`.
    """

  model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

  table_schema: TableSchema
  reference_rows: list[dict] = Field(default_factory=list)
  reference_digest: str = ""
  pipeline_run_id: str = ""
  # Model locations for engines that build their own backends. `model_uri`
  # is the LLM weights (the ModelClient also gets it); `embedder_uri` is
  # B.1's embedder. On the worker these are local paths (warm-pulled from
  # GCS by the DoFn); empty ⇒ the engine uses its dependency-free default
  # (e.g. HashingEmbedder), which is what the contract tests exercise.
  model_uri: str = ""
  embedder_uri: str = ""
  # Columns that must be per-row-unique and NEVER sampled from reference
  # data (PK / UUID / account-number style). See engines/identity.py.
  identity_columns: list[str] = Field(default_factory=list)
  # Declared primary-key columns (relational contract / --pk_cols). The
  # 2026-08-21 run proved generation must know the PK, not just the
  # gate: the declared PK drew from a 512-cap pool and 999 488 rows
  # were pk.duplicate by construction (ADR 0028). Routed constraint
  # samplers keep a per-process emitted set for these columns.
  pk_columns: list[str] = Field(default_factory=list)
  # When True (real-LLM runs), a failed free-text LLM call re-raises
  # instead of silently falling back to reference exemplars. The 2026-07-10
  # E2E runs shipped 100% memorized identifiers because the fallback was
  # only a WARNING. Fake/mock clients keep the lenient default.
  strict_freetext: bool = False
  # Opt-in (2026-07-25 hallucination fix, layer 2): for identifier-ish
  # free-text columns, constrain pool completions at DECODE time with a
  # charset/length regex (`items.pattern` in the guided-JSON schema) so
  # format junk is unrepresentable. Off by default until an E2E confirms
  # no vLLM/xgrammar throughput cliff on T4 — the post-hoc format gate in
  # `_pool_llm_yield` protects the pool either way.
  pool_pattern_guidance: bool = False
  # --- persisted free-text pools (WS5 §2) -----------------------------
  # A `FreeTextPoolStore` (Protocol in sdfb_core.pools.store), attached
  # worker-side by the DoFn. When it already holds this digest+model's
  # pools, setup() reads them and never ignites vLLM: the 2026-07-26 1M
  # run rebuilt the same three pools 36 times for 68,805 s of LLM service
  # time, because `_POOL_CACHE` is process-scoped and every autoscale wave
  # starts a fresh process. Typed `object` so sdfb-core keeps no import.
  # `freetext_pools_table` threads the FQN through the pickled graph; the
  # DoFn attaches the store worker-side, mirroring rag_chunks_table /
  # chunk_store above.
  freetext_pools_table: str = ""
  pool_store: object | None = None
  # A `SourceValueStore` (Protocol in sdfb_core.pools.store) holding each
  # column's FULL distinct source values, attached worker-side by the
  # pool-build DoFn. When present, the pool ladder and shape fallback
  # reject candidates against the whole source domain, not just the
  # profiled sample — the 2026-08-05 B_TABLE R1 run landed 33-99%
  # verbatim source values exactly because the sample was the only
  # rejection set. None ⇒ pre-fix behavior, unchanged.
  source_value_store: object | None = None
  # True only inside BuildFreeTextPoolsDoFn, which blanks pool_store by
  # design (self-read guard): suppresses the `freetext_pool_store_absent`
  # WARNING that two E2E reports misread as a store outage.
  pool_branch: bool = False
  # Reference-table FQN for the worker-side `BigQuerySourceValueStore`
  # attach (mirrors rag_chunks_table / freetext_pools_table). B.2 builds
  # pools LAZILY inside Generate DoFns — no pool branch — so the ADR 0023
  # rejection set must ride the generate path too; fetches are lazy
  # (only when a ladder/pool actually builds) and process-cached.
  # Empty ⇒ no attach.
  source_values_table: str = ""
  # --- pool seeding experiment (WS5 §3) -------------------------------
  # "centroid" (control, today's behavior) | "kcenter" | "kcenter_rotate".
  # Retrieval runs 3x per setup and only picks 8 prompt seeds, so this is
  # the cheapest lever on novel-yield-per-call there is. One build, three
  # arms — the runs differ in exactly one variable.
  pool_seed_strategy: str = "centroid"
  # Shape-preserving expander (2026-08-05 spec C3): "off" draws from the
  # bounded pool only (distinct capped at pool size — the 10M-run
  # diversity ceiling), "identifiers" (default) expands code-like columns
  # from their observed shape mix, "all" also mutates digit runs inside
  # texty pool draws. Never adds an LLM call on any setting.
  freetext_expansion: str = "identifiers"
  # Enforced FK edges → the parent's landed key TUPLES (ADR 0031):
  # ``[{"cols": [...], "keys": [[v, …], …]}, …]``. The tuple is the
  # unit of referential integrity, so it is the unit of the draw —
  # per-column pools cannot express "this combination exists".
  fk_key_pools: list[dict] = Field(default_factory=list)
  # Per-column projection of the same keys (ADR 0021 shape). Kept for
  # profiling/plan metadata and for single-column edges arriving from
  # the legacy driver-side loader; sampling truth is fk_key_pools.
  fk_pools: dict[str, tuple] = Field(default_factory=dict)
  # Relational E2E metadata for the once-per-plan `relational_e2e`
  # worker log entry (ADR 0028 follow-up): where this run lands, and
  # each declared FK edge as {"cols": [...], "ref": "ds.parent",
  # "parent_landing": "project.landing.parent"}. Display-only — the
  # sampling truth stays in fk_pools.
  landing_table: str = ""
  fk_edges: list[dict] = Field(default_factory=list)
  # The relationship card the LAUNCHER rendered from
  # `config/relationships/` (ADR 0032), carried verbatim so the worker
  # log shows the same model the driver planned from — no second graph
  # implementation, nothing to drift.
  relationship_card: str = ""
  # Design 2026-09-10 (ADR 0036): a DRIVEN child's recipe — the
  # `FanoutPlan.to_payload()` dict (driving edge columns, the SOURCE
  # fan-out histogram, the PK-completing cell table). None = this table
  # generates from `--num_rows` batch requests as before.
  fanout: dict | None = None
  # Multi-table launches (ADR 0030): the landing table NAME used to
  # qualify column references in pretty log payloads
  # (`<LANDING>.<col>`) so oss/ replacements stay unambiguous when N
  # tables share one worker log. Empty = single-table, bare names.
  log_table_prefix: str = ""
  # Attach the per-column llm_prompt_constraint (parsed from the column's
  # DDL description JSON) to pool prompts. Per-column CONSTANT suffix —
  # prefix-cache-safe (ADR 0018).
  prompt_constraints: bool = True
  # Log each built pool prompt as a `freetext_pool_prompt` milestone
  # (ADR 0024 §3c): "off" (default) logs nothing; "redacted" elides seed
  # exemplars (reference values never reach logs); "full" logs verbatim
  # prompts at WARNING — explicit debug-run opt-in only.
  prompt_debug: str = "off"
  # Full-table distinct counts per column, driver-populated from the
  # Tier-2 exact stats pass (--source_stats=exact, ADR 0022). The 10k
  # reference sample under-estimates cardinality (five-run verdict:
  # sample distinct 95 vs source 4k starved the pool at 95); these lift
  # the pool target back toward _FREE_TEXT_POOL_MAX. Empty = Tier 1 only,
  # sample-distinct behavior unchanged. Workers stay stats-TABLE-agnostic:
  # the value arrives through this context, never a BQ read.
  source_distinct: dict[str, int] = Field(default_factory=dict)
  # --- RAG layer (WS2 §4b) -------------------------------------------
  # Requested synthetic row count — bounds the free-text pool target
  # (min(num_rows, column_distinct, _FREE_TEXT_POOL_MAX)). 0 = unknown.
  num_rows: int = 0
  # Pinned vector space for rag_chunks reads. Derived DRIVER-side from
  # the original embedder URI (the worker only sees the localized path).
  embedder_id: str = ""
  embedder_version: str = ""
  # FQN of synthetic_rag.rag_chunks; empty ⇒ the read path is off. The
  # live store object is attached worker-side (it cannot be pickled):
  # the DoFn does ctx.model_copy(update={"chunk_store": store}).
  rag_chunks_table: str = ""
  chunk_store: object | None = None


class GenerationEngine(ABC):
  """Abstract base for synthetic-data generation engines.

    Concrete subclasses live in `engines/b1_rag/` (RAG) and
    `engines/b2_library/` (library-wrapper). Both must pass the contract
    tests in `sdfb-tests/tests/unit/engines/test_abc_contract.py`.

    Lifecycle (called by the Beam `DoFn`):
      `DoFn.setup`     → `engine.setup(model_client, ctx)`
      `DoFn.process`   → `engine.generate_batch(n, cfg)` (many times)
      `DoFn.teardown`  → `engine.teardown()`
    """

  name: str = ""

  @property
  def constrained_columns(self) -> frozenset[str]:
    """Columns this engine generates from a declared clause's own
        value space (ADR 0028 Tier P/B).

        The DoFn asks so that identity synthesis does not overwrite them
        (2026-08-25: a `pattern`-routed identity column landed UUIDs).
        Such a generator already satisfies what identity synthesis is
        for — it draws from the clause, never from the source domain —
        and the engine gives it per-run uniqueness in exchange.
        Engines with no router return the empty set and behave as before.
        """
    return frozenset()

  @abstractmethod
  def setup(self, model_client: ModelClient, ctx: GenerationContext) -> None:
    """Called once per worker before any `generate_batch`.

        Heavy init lives here: vector index build (B.1), tabular library
        fit (B.2). Must be idempotent — a second call with the same
        arguments is a no-op (does not re-fit / re-embed).
        """

  @abstractmethod
  def generate_batch(
      self,
      n: int,
      cfg: GenerationConfig,
  ) -> Iterator[GeneratedRecord]:
    """Yield up to `n` schema-conformant records.

        Yielding fewer than `n` is allowed; the caller will request more
        if needed. Candidates that fail the engine's internal validation
        (schema, repair-loop budget exhausted) are silently dropped here —
        the Beam DoFn routes Pydantic and Pandera failures to the DLQ
        from a downstream stage, not from inside the engine.

        Raises `RuntimeError` if `setup()` has not run, or has been
        followed by `teardown()`.
        """

  def generate_for_keys(
      self,
      keys: Sequence[tuple],
      cfg: GenerationConfig,
      matches: Mapping[str, Sequence[Sequence[Sequence]]] | None = None,
  ) -> Iterator[GeneratedRecord]:
    """Yield the children of ``keys`` (design 2026-09-10, ADR 0036):
        per key, the fan-out and the PK-completing cells come from
        ``ctx.fanout`` (`sdfb_core.engines.fanout.expand_keys`), inherited
        columns are copied from the key tuple, and every other column is
        this engine's own sampling. ``cfg.batch_size`` bounds the rows
        sampled at once (chunked emission). Engines that cannot be driven
        keep this default.

        ``matches`` (design 2026-09-11 §4, ADR 0037) resolves
        ``ctx.fanout``'s ``conditional`` edges: ``matches[edge.id][i]`` is
        the candidate list for ``keys[i]`` — a missing edge id, ``None``,
        or a too-short list means no candidates for that key. Child row
        ``j`` (0-based within that key's fan-out) of a matched key gets
        ``conditional_values(run_id, key, edge.id, candidates, k)[j]`` on
        ``edge.cols``, applied after the pool draws and before the
        driving-column / cell overrides. No candidates on a ``nullable``
        edge sets every one of ``edge.cols`` to ``None``; no candidates on
        a non-nullable edge means that key's rows are not emitted at all
        (defensive — the DoFn drops such keys first). ``matches=None``
        (or a plan with no conditional edges) reproduces today's output
        byte-for-byte."""
    raise NotImplementedError(
        f"{type(self).__name__} cannot generate from parent keys")

  @abstractmethod
  def teardown(self) -> None:
    """Release worker resources (vector index, fitted model, GPU
        references). After `teardown()`, `generate_batch` must raise
        `RuntimeError` until `setup()` is called again."""


# ---------------------------------------------------------------------------
# `generate_for_keys` conditional-edge wiring (design 2026-09-11 §4,
# ADR 0037), shared between `engines/b1_rag/engine.py` and
# `engines/b2_library/engine.py` so the two engines resolve
# `ctx.fanout.conditional` identically. Not part of the ABC — both engines
# call these from their own `generate_for_keys`.
# ---------------------------------------------------------------------------


def _candidates_for(
    matches: Mapping[str, Sequence[Sequence[Sequence]]] | None,
    edge_id: str,
    i: int,
) -> Sequence[Sequence]:
  """``matches[edge_id][i]``, or ``()`` for a missing edge id, a
    ``None`` ``matches`` mapping, a too-short per-key list, or a ``None``
    entry — all of these mean "no candidates" for that key."""
  if not matches:
    return ()
  per_key = matches.get(edge_id)
  if not per_key or i >= len(per_key):
    return ()
  candidates = per_key[i]
  return candidates if candidates else ()


# Capping is a per-run property of one driven table's MODEL (its declared
# PK cannot represent the source fan-out), not of a key — so it is said
# once per TABLE instead of once per hot parent. The guard key is that
# table's LANDING table (`ctx.landing_table`): the narrowest identifier
# the generation context carries that cannot collide between two driven
# tables of one job, with the display prefix as fallback. A single-job
# relational run (ADR 0030) generates several driven tables in ONE worker
# process, and a process-global guard reported only the FIRST of them to
# cap — every later table's capping was invisible (final review, E3).
# Process-lived by design; `_reset_rows_capped_log` is the test hook.
_ROWS_CAPPED_LOGGED: set[str] = set()


def _reset_rows_capped_log() -> None:
  """Test hook — production state is deliberately process-lived."""
  _ROWS_CAPPED_LOGGED.clear()


def _log_rows_capped(draw: KeyDraw, table: str) -> None:
  """One WARNING per driven table. The line's ``table=`` field comes
    from the ambient `milestone_scope` the generate DoFn sets, so it is
    spelled exactly like every other engine milestone in the run; only
    the GUARD is keyed on the landing table."""
  if table in _ROWS_CAPPED_LOGGED:
    return
  _ROWS_CAPPED_LOGGED.add(table)
  log_milestone(
      "fanout_rows_capped",
      level=logging.WARNING,
      requested=draw.requested,
      emitted=draw.n_children,
      capacity=draw.capacity,
      note="a parent key's source fan-out exceeds what its PK can "
      "represent (cells x conditional candidates); the extra children "
      "are NOT emitted — they would be PK duplicates. Raise "
      "--fk_candidate_cap, or fix the `pk:` in the relationship model. "
      "Logged once per worker process PER TABLE.",
  )


def conditional_draws(
    plan: FanoutPlan,
    keys: Sequence[tuple],
    run_id: str,
    matches: Mapping[str, Sequence[Sequence[Sequence]]] | None,
    *,
    table: str = "",
) -> dict[tuple, KeyDraw | None]:
  """Per parent key, the JOINT draw its children come from
    (`joint_key_draw`) — or ``None`` when a non-nullable conditional edge
    has no candidates for that key, which means the caller must emit NONE
    of its rows.

    `expand_keys` reads the cells off these draws and
    `apply_conditional_overrides` reads the per-edge candidate tuples, so
    a child's cell and its candidates are two halves of ONE combination
    index. Keys whose fan-out drew 0 are absent, exactly as before.

    Returns ``{}`` for a plan with no conditional edges — the ADR 0036
    path, which draws its cells inside `expand_keys` and never allocates
    any of this.

    ``table`` scopes the once-per-table `fanout_rows_capped` milestone
    (`_log_rows_capped`): the engines pass their landing table, so each
    driven table of a single-job relational run reports its own capping.
    """
  if not plan.conditional:
    return {}
  out: dict[tuple, KeyDraw | None] = {}
  for i, key in enumerate(keys):
    key_t = tuple(key)
    candidates: list[Sequence[Sequence]] = []
    dropped = False
    for edge in plan.conditional:
      edge_candidates = _candidates_for(matches, edge.id, i)
      if not edge_candidates and not edge.nullable:
        dropped = True
        break
      candidates.append(edge_candidates)
    if dropped:
      out[key_t] = None
      continue
    draw = joint_key_draw(plan, key_t, run_id, candidates)
    if draw.requested <= 0:
      continue
    if draw.shortfall:
      _log_rows_capped(draw, table)
    out[key_t] = draw
  return out


def apply_conditional_overrides(
    plan: FanoutPlan,
    chunk: Sequence[tuple[tuple, tuple]],
    draws: Mapping[tuple, KeyDraw | None],
    child_index: dict[tuple, int],
) -> tuple[dict[str, list], list[bool]]:
  """Per-row conditional-edge column overrides + a skip mask for one
    `expand_keys` chunk, from the `conditional_draws` cache.

    ``child_index`` is the caller's running per-key child counter — it
    MUST be created once per `generate_for_keys` call and passed to every
    chunk unmutated-elsewhere, because `expand_keys` can split one key's
    children across more than one chunk. Returns ``({}, [False, ...])``
    (a no-op) when the plan has no conditional edges."""
  n = len(chunk)
  skip = [False] * n
  if not plan.conditional:
    return {}, skip
  columns: dict[str, list] = {
      name: [None] * n for edge in plan.conditional for name in edge.cols
  }
  for i, (key, _) in enumerate(chunk):
    j = child_index.get(key, 0)
    child_index[key] = j + 1
    draw = draws.get(key)
    if draw is None:
      skip[i] = True
      continue
    for edge in plan.conditional:
      row_values = draw.values[edge.id][j]
      if row_values:
        for p, name in enumerate(edge.cols):
          columns[name][i] = row_values[p]
      # else: leave the pre-seeded None — the nullable-no-candidates case.
  return columns, skip
