"""Per-column free-text LLM hook for B.2 (design §4 step 4).

Columns the statistical library handles poorly — FREE_TEXT prose, JSON
blobs, very-high-cardinality strings (see ``fidelity._classify``) — are
patched here through the :class:`ModelClient` Protocol instead of being
sampled by the backend.

FASTGEN spine (ADR 0013): the LLM runs **O(1)**, not O(N). We ask the
client for a *bounded unique pool* of candidate values per free-text
column once, then sample-with-replacement (seeded) to fill the N rows. The
bulk N never hits the GPU.

``cfg.similarity`` is honored two ways:
  * it sets the LLM sampling ``temperature`` (similarity→1 ⇒ low temp,
    mimic the reference exemplars; similarity→0 ⇒ high temp, diverge), and
  * it biases the sample-with-replacement draw toward the observed
    reference pool (high similarity) vs. the freshly-generated pool (low
    similarity).

Engines import only the ``ModelClient`` Protocol — never ``vllm``.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from sdfb_core.engines.b2_library.fidelity import ColumnProfile
from sdfb_core.engines.base import (
    FreeTextEmptyYieldError,
    GenerationConfig,
    ModelClient,
    escalating_sampling,
)
from sdfb_core.engines.text_shapes import (
    build_relaxed_shapes,
    collapsed_mask,
    identifier_sampler,
    length_hint,
    mutate_digit_runs,
    sample_relaxed_identifier,
    shape_mix_can_template,
    shape_mix_is_identifier_like,
)
from sdfb_core.observability import log_milestone, log_prompt_debug
from sdfb_core.pools.store import SourceValueStore

# Bounded pool size — the LLM emits at most this many unique candidates per
# free-text column regardless of N (the O(1) cost cap). Sized small so the
# guided-JSON call stays cheap; tune on the M4.
_DEFAULT_POOL_SIZE = 32

# Reference-blend privacy bound, mirroring the memorization probe rule
# (copy_ratio flagged when source_distinct > 100). A column whose observed
# pool exceeds this is identity-like: blending its reference values verbatim
# IS the leak — the 2026-07-23 b2 E2E landed COL_048/053/054 (source_distinct
# 19 815 / 1 298 / 3 030) at copy_ratio ≈ 0.51, exactly the similarity=0.5
# blend mass. Above the bound the blend is disabled and every landed value
# comes from the novel LLM/shape pool, whatever `similarity` says.
_REFERENCE_BLEND_MAX_DISTINCT = 100


def similarity_to_temperature(similarity: float) -> float:
  """Map ``cfg.similarity`` ∈ [0,1] → LLM temperature ∈ [~0.1, ~1.3].

    similarity→1 ⇒ temp→0.1 (mimic exemplars closely); similarity→0 ⇒
    temp→1.3 (diverge). Linear; clamped to keep the LLM out of degenerate
    greedy/over-random regimes.
    """
  s = min(max(similarity, 0.0), 1.0)
  return round(1.3 - 1.2 * s, 4)


def _collapsed_gate(profile: ColumnProfile):
  """Collapsed-mask candidate gate (B.1 parity, wave-2 §4c).

    For shape-rigid whitespace columns the LLM normalizes literal space
    runs (2026-08-09 B_TABLE R1, COL_038: ␣␣␣ → ␣ on every pool value) —
    candidates must reproduce an observed run-collapsed mask. Prose (mix
    cannot template) stays ungated.
    """
  gate_masks: set[str] | None = None
  if (build_relaxed_shapes(list(profile.text_pool)) is None and
      shape_mix_can_template(profile.shape_mix)):
    gate_masks = {collapsed_mask(str(v)) for v in profile.text_pool if v}

  def gate(v: str) -> bool:
    return gate_masks is None or collapsed_mask(v) in gate_masks

  return gate


def _pool_schema(column_name: str) -> dict:  # pylint: disable=unused-argument
  """JSON schema for the bounded-pool guided-decoding call.

    Asks for an object with a ``values`` array of strings — the production
    vLLM client enforces this via guided JSON; the FakeModelClient ignores
    the schema and returns canned/echo dicts.
    """
  return {
      "type": "object",
      "properties": {
          "values": {
              "type": "array",
              "items": {
                  "type": "string"
              },
          }
      },
      "required": ["values"],
  }


class FreeTextHook:
  """Generates + caches a bounded value pool per free-text column.

    Built in the engine's ``setup`` (so the O(1) LLM call can happen once
    per worker per column the first time a column is sampled) and consumed
    in ``generate_batch``. The pool is cached keyed by ``(column,
    similarity)`` — NOT ``seed`` — so it is built genuinely **once per
    worker per column** (the FASTGEN O(1) guarantee, ADR 0013). Batch-to-
    batch value diversity does not come from rebuilding the pool: it comes
    from the per-batch-seeded with-replacement *draw* in :meth:`sample`
    (the ``rng`` argument), which is reseeded per batch by the caller
    (``derive_batch_seed(run_id, batch_id)`` in
    ``GenerateRecordsDoFn.process``) precisely so that repeated draws over
    the same pool do not repeat the same rows.

    Defect history (2026-07-20 b2 E2E run, JOB_STATE_FAILED): the cache used
    to be keyed ``(column, seed, similarity)``. Because ``cfg.seed`` is
    deliberately re-derived per batch (anti-replay design — a fixed seed
    across ~63 batches would replay the same draw every batch), that key
    never repeated, so the cache never hit: every one of ~63 batches
    rebuilt every FREE_TEXT column's pool via a fresh LLM call (63x the
    intended O(1) cost), and under ``strict_freetext`` each rebuild was a
    fresh chance for ``FreeTextEmptyYieldError`` to kill the whole batch
    (~60/63 batches failed). Dropping ``seed`` from the key fixes both: the
    cost regression and the failure amplification.

    A failed or empty-strict-yield build (see ``_generate_pool``'s
    exception path and its ``FreeTextEmptyYieldError`` raise) never reaches
    ``self._cache[key] = pool`` — the entry is left unset, so the next
    batch's call retries the build rather than being poisoned by a
    permanently-missing/empty cache entry.

    Caching contract (2026-07-21 review hardening; strict-failure caching
    added after the 2026-07-22 b2 E2E incident): genuine LLM pools —
    full-size or undersized-but-nonempty — ARE cached, since they represent
    real (if degraded) novel generation. Exemplar-fallback results (the
    caught-exception path and the empty-novel-yield path in non-strict mode)
    are NEVER cached: a transient LLM hiccup on the first build must not
    permanently lock a column to exemplar-only values for the worker's
    lifetime — the next batch retries the build instead. The fallback value
    is still used for the *current* batch; only the caching is skipped.

    Strict mode splits failure by shape. A raised client exception is
    transient-shaped: it re-raises uncached, so the next batch retries. An
    empty NOVEL yield after every escalation level is deterministic-shaped
    (saturated key space / exemplar echo — same outcome on every rebuild).
    A *copy-saturated* one (``parsed > 0``, every value an observed copy)
    first tries a relaxed per-position character template
    (``build_relaxed_shapes``) to generate verified-novel in-format values
    without the LLM — the COL_052 failure mode of the 2026-07-22 b2
    E2E runs, where qwen echoed the 32 shown exemplars on all 3 attempts.
    A successful shape pool is a genuine novel pool and is cached normally.
    Only when no template applies (prose) or the template keyspace is
    exhausted does the strict raise happen, and that failure is
    negative-cached in ``_failed``: subsequent batches on this worker
    re-raise ``FreeTextEmptyYieldError`` immediately (with a DEBUG
    ``freetext_pool_empty_cached`` milestone) instead of re-paying the
    escalation ladder. The first 2026-07-22 run paid 63 batches x 3 LLM
    calls (~35 min of GPU) rebuilding a pool that could never succeed, on
    a run the BLOCKER gate was already guaranteed to fail.
    Concurrent DoFn threads on one worker race ``_pool_for``'s check-then-act
    over the plain-dict cache (see ``_SETUP_LOCK`` in
    ``sdfb_beam.handlers.vllm_client`` for the same class of race); ``_lock``
    double-checks under an instance lock so at most one genuine build happens
    per key. First-builder-wins is harmless regardless of which batch
    triggers the build: the pool build itself uses the batch-*independent*
    ``cfg.engine_specific["pool_seed"]`` (``GenerateRecordsDoFn.process`` sets
    it to the explicit base seed, or to a run_id-derived value at a reserved
    ``batch_id=-1`` namespace, never a per-batch seed) — so the pool's
    content does not depend on which batch happens to win the race. Explicit
    ``--seed`` reruns therefore reproduce the pool build (modulo LLM-server
    determinism) regardless of Beam's non-deterministic batch-to-worker
    scheduling (P6); derived-mode runs still vary run-to-run via the salted
    ``run_id``.
    """

  def __init__(
      self,
      model_client: ModelClient,
      *,
      pool_size: int = _DEFAULT_POOL_SIZE,
      strict: bool = False,
      source_value_store: SourceValueStore | None = None,
  ) -> None:
    self._client = model_client
    self._pool_size = pool_size
    self._strict = strict
    # ADR 0023 seam (B.2 parity, R5 prerequisite): the column's FULL
    # distinct source values, consulted by the pool novelty filter and
    # the shape fallback. None ⇒ sample-only rejection, unchanged.
    self._source_value_store = source_value_store
    self._source_cache: dict[str, frozenset[str]] = {}
    self._cache: dict[tuple[str, float], list[str]] = {}
    # Negative cache: key → the FreeTextEmptyYieldError message of a
    # deterministic strict-mode build failure (see class docstring).
    self._failed: dict[tuple[str, float], str] = {}
    self._lock = threading.Lock()

  def __getstate__(self) -> dict:
    # `threading.Lock` is not picklable (Beam workers pickle the fitted
    # engine, e.g. across `generate_batch` boundaries in tests / bundle
    # snapshotting) — drop it from the pickled state and rebuild a fresh
    # one on unpickle rather than carrying lock state across processes.
    state = self.__dict__.copy()
    del state["_lock"]
    return state

  def __setstate__(self, state: dict) -> None:
    self.__dict__.update(state)
    self._lock = threading.Lock()

  def sample(
      self,
      profile: ColumnProfile,
      n: int,
      cfg: GenerationConfig,
      rng: np.random.Generator,
  ) -> list[str | None]:
    """Return ``n`` values for one free-text column.

        Builds (or reuses) the bounded pool via the LLM, then draws ``n``
        values with replacement. ``cfg.similarity`` biases the mix between
        the LLM-generated pool and the observed reference pool.

        Identifier-shaped columns never reach the LLM (or the reference
        blend — reference identifiers in the output are the leak): they
        generate format-preserving values per row from the profile's
        per-position template.
        """
    # One uniform draw decides null → empty → value, so the two sparsity
    # modes never double-count (2026-08-05 spec C1; mirrors B.1).
    null_frac = profile.null_fraction if profile.nullable else 0.0
    empty_frac = profile.empty_fraction
    u = rng.random(n)
    null_mask = u < null_frac
    empty_mask = ~null_mask & (u < null_frac + empty_frac)
    fill = int(n - int(null_mask.sum()) - int(empty_mask.sum()))

    def pick(k: int) -> int:
      return int(rng.integers(0, k))

    generated = self._generate_fill(profile, cfg, fill, pick, rng)
    if generated is None:
      return [None] * n

    out: list[str | None] = []
    gen_iter = iter(generated)
    for i in range(n):
      if null_mask[i]:
        out.append(None)
      elif empty_mask[i]:
        out.append("")
      else:
        out.append(next(gen_iter))
    return out

  def _generate_fill(
      self,
      profile: ColumnProfile,
      cfg: GenerationConfig,
      fill: int,
      pick,
      rng: np.random.Generator,
  ) -> list[str | None] | None:
    """The `fill` substantive values of one batch, or None when no
        source of values exists (caller emits all-None)."""
    expansion = str(
        cfg.engine_specific.get("freetext_expansion", "identifiers"))

    if profile.identifier_shape is not None:
      # Shared wave-2 sampler: mask mix above coverage, full
      # row-weighted mask table (positional alphabets pin fixed
      # prefixes/nibbles, 2026-08-11 A_TABLE R1) below it — the
      # collapsed template scrambled long-tail mask families
      # (2026-08-09 A_TABLE R1, COL_001-class).
      sampler = identifier_sampler(
          profile.identifier_shape,
          profile.shape_mix,
          profile.text_pool,
          pick,
      )
      return [sampler() for _ in range(fill)]

    if (profile.shape_mix is not None and expansion != "off" and
        (expansion == "all" or
         shape_mix_is_identifier_like(profile.shape_mix))):
      # Shape-preserving expansion: distinct scales with rows, not
      # with the pool cap, and the LLM is never called (spec C3).
      observed = set(profile.text_pool)
      generated: list[str | None] = []
      for _ in range(fill):
        v = ""
        for _ in range(3):
          v = sample_relaxed_identifier(profile.shape_mix, pick)
          if v not in observed:
            break
        generated.append(v)
      return generated

    pool = self._pool_for(profile, cfg)
    ref_pool = list(profile.text_pool)
    if len(ref_pool) > _REFERENCE_BLEND_MAX_DISTINCT:
      # Identity-like cardinality: the reference blend is the leak
      # (see _REFERENCE_BLEND_MAX_DISTINCT). `_blend_pools` shifts
      # all mass to the novel pool when the reference is empty.
      ref_pool = []

    # similarity high ⇒ favor the observed reference pool (mimic);
    # similarity low ⇒ favor the freshly-generated LLM pool (diverge).
    combined, probs = _blend_pools(pool, ref_pool, cfg.similarity)
    if not combined:
      return None
    picks = rng.choice(len(combined), size=fill, p=probs)
    drawn: list[str | None] = [combined[int(i)] for i in picks]
    if expansion == "all":
      drawn = [mutate_digit_runs(v, pick) if v else v for v in drawn]
    return drawn

  def _pool_for(self, profile: ColumnProfile,
                cfg: GenerationConfig) -> list[str]:
    key = (profile.name, round(cfg.similarity, 4))
    # Fast path: no lock. Safe because dict reads never race a dict
    # write in CPython, and a cached entry is never mutated in place.
    cached = self._cache.get(key)
    if cached is not None:
      return cached
    failed = self._failed.get(key)
    if failed is not None:
      # DEBUG, once per batch: the fail-fast is otherwise visible only
      # as DLQ envelopes (2026-07-22 re-run: 55 log-silent batch
      # deaths).
      log_milestone(
          "freetext_pool_empty_cached",
          level=logging.DEBUG,
          column=profile.name,
      )
      raise FreeTextEmptyYieldError(failed)
    with self._lock:
      # Re-check: a sibling thread may have built this key while we
      # waited on the lock (double-checked locking — pool builds are
      # seconds-long LLM calls; serializing duplicate builds is the
      # point, contention beyond that is negligible at ≤1 build per
      # column per worker).
      cached = self._cache.get(key)
      if cached is not None:
        return cached
      failed = self._failed.get(key)
      if failed is not None:
        log_milestone(
            "freetext_pool_empty_cached",
            level=logging.DEBUG,
            column=profile.name,
        )
        raise FreeTextEmptyYieldError(failed)
      try:
        pool, cacheable = self._generate_pool(profile, cfg)
      except FreeTextEmptyYieldError as e:
        # Deterministic-shaped: every escalation level yielded zero
        # novel values. Rebuilding cannot succeed — cache the failure
        # so later batches fail fast instead of re-paying the LLM
        # ladder (2026-07-22 b2 E2E). Client exceptions (transient-
        # shaped) are NOT caught here and stay uncached.
        self._failed[key] = str(e)
        raise
      if cacheable:
        self._cache[key] = pool
      return pool

  def _source_values(self, column: str) -> frozenset[str]:
    """The column's FULL distinct source values, or an empty set —
        LOUDLY on cap-exceeded/store-error, silently when no store is
        attached (pre-seam behavior). Fetched once per column per hook;
        milestone names mirror B.1's for one cross-engine readout."""
    if self._source_value_store is None:
      return frozenset()
    cached = self._source_cache.get(column)
    if cached is not None:
      return cached
    values: frozenset[str] | None
    try:
      values = self._source_value_store.fetch_distinct(column)
    except Exception as exc:  # pylint: disable=broad-exception-caught
      log_milestone(
          "freetext_pool_source_filter_error",
          level=logging.WARNING,
          column=column,
          error=type(exc).__name__,
      )
      values = None
    else:
      if values is None:
        log_milestone(
            "freetext_pool_source_filter_absent",
            level=logging.WARNING,
            column=column,
        )
      else:
        log_milestone(
            "freetext_pool_source_filter",
            column=column,
            size=len(values),
        )
    result = frozenset(values or ())
    self._source_cache[column] = result
    return result

  def _pool_prompt_and_schema(self, profile: ColumnProfile,
                              cfg: GenerationConfig,
                              exemplars: list[str]) -> tuple[str, dict]:
    """Prompt + guided-decoding schema for one column's pool build.

        Per-column constant suffixes (spec C5 + measured length band, ADR
        0022) append after the shared prefix — prefix-cache-safe. A
        user-pinned length (ADR 0024) makes the derived band redundant
        tokens; a user pattern constrains decoding itself. Also emits the
        `--prompt_debug` milestone (ADR 0024 §3c) with the seed-elided
        rebuild.
        """

    def _prompt(examples_repr: str) -> str:
      return (f"You generate synthetic tabular data. First identify the exact "
              f"format of these example values for the column '{profile.name}' "
              f"(e.g. UUID, hexadecimal identifier, numeric code, date, "
              f"timestamp, natural-language text), then generate up to "
              f"{self._pool_size} NEW, distinct, fictitious values in exactly "
              f"that format. Never copy an example verbatim. Examples: "
              f"{examples_repr}. Return JSON {{\"values\": [...]}}.")

    prompt = _prompt(str(exemplars))
    pool_schema = _pool_schema(profile.name)
    if cfg.engine_specific.get("prompt_constraints", True):
      constraint = profile.llm_prompt_constraint
      hint = ("" if profile.constraint_sets_length else length_hint(
          profile.text_pool))
      prompt += (f" Column constraint: {constraint}."
                 if constraint else "") + (f" {hint}" if hint else "")
      if profile.constraint_pattern:
        pool_schema["properties"]["values"]["items"]["pattern"] = (
            profile.constraint_pattern)
    # Everything after the shared base is the constant per-column
    # suffix — reattach it to the seed-elided rebuild.
    suffix = prompt[len(_prompt(str(exemplars))):]
    log_prompt_debug(
        str(cfg.engine_specific.get("prompt_debug", "off")),
        profile.name,
        prompt,
        _prompt(f"<{len(exemplars)} seeds elided>") + suffix,
    )
    return prompt, pool_schema

  def _generate_pool(self, profile: ColumnProfile,
                     cfg: GenerationConfig) -> tuple[list[str], bool]:
    exemplars = list(profile.text_pool[:self._pool_size])
    prompt, pool_schema = self._pool_prompt_and_schema(profile, cfg, exemplars)
    # Novelty filter: LLM values that equal observed reference values are
    # copies, not generations. The LLM pool is the "diverge" side of the
    # similarity blend — observed values reach the output only via the
    # reference pool, weighted by `cfg.similarity`. An empty NOVEL yield
    # (all copies / all parse-drops) is retried at escalating temperature
    # before falling back (2026-07-16 corp run: the model echoed the seed
    # exemplars verbatim at the base temperature).
    # ADR 0023: reject against the profiled sample PLUS (when a
    # SourceValueStore is attached) the column's full source domain —
    # a candidate equal to ANY real value is a copy, whether the
    # profiler sampled it or not.
    # Constraint examples are canonical fictitious values from the DDL
    # description (ADR 0024) — a verbatim echo must never land as data.
    observed = (
        set(profile.text_pool)
        | self._source_values(profile.name)
        | set(profile.constraint_examples or ()))
    shown = set(exemplars)
    gate = _collapsed_gate(profile)
    pool: list[str] = []
    pool_seen: set[str] = set()
    seen: set[str] = set()
    n_parsed = 0
    n_copies = 0
    n_echoes = 0
    attempts = 0
    base_seed = cfg.engine_specific.get("pool_seed", cfg.seed)
    try:
      for level in escalating_sampling(
          similarity_to_temperature(cfg.similarity)):
        attempts += 1
        responses = self._client.generate_json(
            prompt=prompt,
            json_schema=pool_schema,
            max_tokens=2048,
            temperature=level.temperature,
            n=1,
            # Walk the seed per attempt: a pinned seed repeated the
            # exact same echo on every escalation level (2026-07-22
            # b2 E2E, COL_052: 3 identical 32-echo responses),
            # making the ladder's diversity partly illusory. Still
            # P6-reproducible — derived from the same base.
            seed=None if base_seed is None else base_seed + attempts - 1,
            top_p=level.top_p,
            top_k=level.top_k,
        )
        values = _extract_values(responses)
        novel = [v for v in values if v not in observed and gate(v)]
        n_parsed += len(values)
        n_copies += len(values) - len(novel)
        n_echoes += sum(1 for v in values if v in shown)
        seen.update(values)
        # Accumulate ACROSS levels until the pool target is met —
        # breaking on the first non-empty yield let one conservative
        # completion define the whole pool (2026-07-17 B.1 run: 4
        # distinct values over 1000 rows) and the unclamped retry
        # levels never executed.
        for v in novel:
          if v not in pool_seen:
            pool_seen.add(v)
            pool.append(v)
        if len(pool) >= self._pool_size:
          break
    except Exception as e:  # pylint: disable=broad-exception-caught
      if self._strict:
        raise
      # Per-call generation failure: exemplar fallback is allowed, but
      # NEVER silently — a run where the LLM contributed nothing must be
      # visible in worker logs (E2E report §4.2: 100 % memorization).
      log_milestone(
          "freetext_llm_fallback",
          level=logging.WARNING,
          column=profile.name,
          error=type(e).__name__,
      )
      return exemplars, False

    if not pool:
      # The calls "succeeded" (no exception) yet yielded nothing usable.
      # Counts (never values — reference data must not leak into logs)
      # say WHY: parsed=0 means every choice was dropped at JSON parse;
      # low distinct with prompt_echoes == verbatim_copies means the
      # model parroted the shown exemplars; high distinct with low
      # prompt_echoes means in-format generations collided with the
      # full reference pool — a saturated key space. Exactly as loud as
      # the exception path: the 2026-07-15 E2E run memorized 100 % of
      # free-text values through this hole.
      diagnosis = (f"attempts={attempts}, parsed={n_parsed}, "
                   f"distinct={len(seen)}, verbatim_copies={n_copies}, "
                   f"prompt_echoes={n_echoes}, novel=0")
      if n_parsed > 0:
        # Copy-saturated: the model parsed values but every one was
        # an observed copy (exemplar echo or keyspace collision) —
        # deterministic on rebuild, so retrying or failing the batch
        # buys nothing. A relaxed per-position template can still
        # generate verified-novel in-format values without the LLM
        # (2026-07-22 b2 E2E: COL_052, 96/96 echoes, run
        # FAILED). Parse failures (parsed=0) skip this — they are
        # config/transport-shaped, not a property of the column.
        shape_pool = self._shape_fallback_pool(profile, base_seed)
        if shape_pool:
          log_milestone(
              "freetext_pool_shape_fallback",
              level=logging.WARNING,
              column=profile.name,
              pool_size=len(shape_pool),
              target=self._pool_size,
              attempts=attempts,
              parsed=n_parsed,
              verbatim_copies=n_copies,
              prompt_echoes=n_echoes,
          )
          return shape_pool, True
      if self._strict:
        # As loud in worker logs as the lax fallback path: the raise
        # itself lands in a DLQ envelope in BigQuery, which the
        # 2026-07-22 b2 E2E showed is invisible when triaging from
        # worker logs alone.
        log_milestone(
            "freetext_pool_empty",
            level=logging.ERROR,
            column=profile.name,
            attempts=attempts,
            parsed=n_parsed,
            distinct=len(seen),
            verbatim_copies=n_copies,
            prompt_echoes=n_echoes,
        )
        raise FreeTextEmptyYieldError(
            f"LLM calls for free-text column {profile.name!r} "
            f"yielded no usable values ({diagnosis}).")
      log_milestone(
          "freetext_llm_fallback",
          level=logging.WARNING,
          column=profile.name,
          error="EmptyYield",
          attempts=attempts,
          parsed=n_parsed,
          distinct=len(seen),
          verbatim_copies=n_copies,
          prompt_echoes=n_echoes,
      )
      return exemplars, False
    if len(pool) < self._pool_size:
      # Levels exhausted below target: the column lands with whatever
      # novelty the LLM delivered, but never silently.
      log_milestone(
          "freetext_pool_undersized",
          level=logging.WARNING,
          column=profile.name,
          pool_size=len(pool),
          target=self._pool_size,
          attempts=attempts,
          parsed=n_parsed,
          distinct=len(seen),
          verbatim_copies=n_copies,
          prompt_echoes=n_echoes,
      )
    # The LLM pool stays novel-only; `_blend_pools` already mixes the
    # observed reference pool back in proportionally to `cfg.similarity`,
    # so folding exemplars HERE double-counted them and turned the
    # "diverge" side of the blend into more memorization.
    return pool[:self._pool_size], True

  def _shape_fallback_pool(self, profile: ColumnProfile,
                           seed: int | None) -> list[str] | None:
    """A verified-novel pool from a relaxed character template, or None.

        Only called for copy-saturated builds. Values are rejected against
        the observed pool (and each other), so nothing here can memorize; a
        template whose keyspace is too saturated to fill even one novel
        value returns None and the caller falls through to its existing
        raise/exemplar path. Seeded from the batch-independent pool seed —
        deterministic per run (P6), like the LLM build it replaces.
        """
    # Prefer the exact shape mix (B.1 parity): it preserves literal
    # fixed-position runs — leading padding, delimiters, interior space
    # runs — that the length-bucket relaxation rejects wholesale
    # (whitespace ⇒ None), which left gate-rejected whitespace columns
    # with no template rescue at all.
    shapes = None
    if shape_mix_can_template(profile.shape_mix):
      shapes = profile.shape_mix
    if shapes is None:
      shapes = build_relaxed_shapes(list(profile.text_pool))
    if shapes is None:
      return None
    rng = np.random.default_rng(seed)

    def pick(k: int) -> int:
      return int(rng.integers(0, k))

    observed = set(profile.text_pool) | self._source_values(profile.name)
    pool: list[str] = []
    seen: set[str] = set()
    # Bounded rejection sampling: dense keyspaces stop at the cap
    # instead of spinning.
    for _ in range(self._pool_size * 40):
      value = sample_relaxed_identifier(shapes, pick)
      if value in observed or value in seen:
        continue
      seen.add(value)
      pool.append(value)
      if len(pool) >= self._pool_size:
        break
    return pool or None


def _extract_values(responses: list[dict]) -> list[str]:
  """Pull string values out of the client's JSON responses, tolerantly.

    Accepts the guided ``{"values": [...]}`` shape, a bare list, or echoed
    reference-row dicts (the FakeModelClient's reference-pool/canned modes).
    """
  out: list[str] = []
  for resp in responses:
    if isinstance(resp, dict) and "values" in resp and isinstance(
        resp["values"], list):
      out.extend(str(v) for v in resp["values"])
    elif isinstance(resp, dict):
      # Echoed reference row — take its string-valued fields.
      out.extend(str(v) for v in resp.values() if isinstance(v, str))
    elif isinstance(resp, str):
      out.append(resp)
  return [v for v in out if v]


def _blend_pools(
    llm_pool: list[str],
    ref_pool: list[str],
    similarity: float,
) -> tuple[list[str], np.ndarray]:
  """Combine the two pools into one value list + a probability vector.

    Mass ``similarity`` goes to the reference pool, ``1 - similarity`` to the
    LLM pool. Each pool's internal mass is uniform. Degenerate cases (one
    pool empty) put all mass on the non-empty pool.
    """
  s = min(max(similarity, 0.0), 1.0)
  combined = _dedupe_stable(ref_pool + llm_pool)
  if not combined:
    return [], np.asarray([])

  ref_set = set(ref_pool)
  has_ref = bool(ref_pool)
  has_llm = bool(llm_pool)
  if not has_ref:
    s = 0.0
  if not has_llm:
    s = 1.0

  n_ref = sum(1 for v in combined if v in ref_set)
  n_llm = len(combined) - n_ref
  probs = np.zeros(len(combined), dtype=float)
  for i, v in enumerate(combined):
    if v in ref_set:
      probs[i] = s / n_ref if n_ref else 0.0
    else:
      probs[i] = (1.0 - s) / n_llm if n_llm else 0.0
  total = probs.sum()
  probs = np.full(len(combined), 1.0 /
                  len(combined)) if total <= 0 else probs / total
  return combined, probs


def _dedupe_stable(items: list[str]) -> list[str]:
  seen: set[str] = set()
  out: list[str] = []
  for item in items:
    if item not in seen:
      seen.add(item)
      out.append(item)
  return out
