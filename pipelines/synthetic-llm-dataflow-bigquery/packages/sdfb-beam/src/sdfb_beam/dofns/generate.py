"""Generation DoFn — instantiates the engine in `setup()`, yields records
from `process()`.

The DoFn carries an engine *name* (string) rather than an engine class
reference, looking the class up at worker setup time via
`sdfb_core.engines.get_engine(name)`. This keeps the DoFn picklable and
avoids leaking package-boundary class references across the
pickle/dill boundary.

Engine internal failures (an unexpected exception inside
`engine.generate_batch`) are caught here and routed to the `failed`
tagged output for the DLQ. Engine *silently dropped* invalid candidates
do NOT surface here — by contract, those are the engine's own
repair-loop concern.

REF: .claude/skills/engine-contract.md
REF: .claude/skills/beam-dofn.md
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import logging
import threading
import time
from contextlib import nullcontext

import apache_beam as beam
from apache_beam.metrics import Metrics
from sdfb_core.engines import (
    GenerationConfig,
    GenerationContext,
    ModelClient,
    get_engine,
)
from sdfb_core.engines.identity import apply_identity_columns
from sdfb_core.observability import (
    log_build_info,
    log_milestone,
    milestone_scope,
)
from sdfb_core.seeding import derive_batch_seed

# Where B.1's embedder weights land after the GCS warm-pull. Offline loaders
# (`transformers`) read from a local directory only — they cannot open a
# gs:// URI — so the DoFn pulls the prefix here before the engine builds its
# embedder. Mirrors the vLLM client's `/local-ssd/model` convention.
# Re-exported from the shared localization module (WS6: one definition
# for every engine-building DoFn — see dofns/localize.py).
from sdfb_beam.dofns.localize import (
    localize_embedder,)
from sdfb_beam.io.fk_pools import per_column_view

# Per-process ledger of failed setup() attempts, keyed "engine:run_id".
# Dataflow retries a failed bundle with a FRESH DoFn in the SAME process;
# only a setup() entered AFTER a recorded failure is a retry — Beam's
# normal N parallel bundle processors all enter cleanly and never match.
# (2026-07-24 16:35 E2E: two silent setup() crashes cost ~35 min with zero
# trace in validation_runs.)
_SETUP_FAILURES: dict[str, int] = {}
_SETUP_FAILURES_LOCK = threading.Lock()


def _reset_setup_failures() -> None:
  """Test hook — production state is deliberately process-lived."""
  with _SETUP_FAILURES_LOCK:
    _SETUP_FAILURES.clear()


# ADR 0034 — ONE engine per (engine, run, landing table, reference digest)
# per worker PROCESS, shared by every bundle thread. The 2026-08-29 R6
# pair ran 32 GenerateRecordsDoFn instances per table and every one built
# its own engine (chunk-store read, FAISS index, pool-store reads,
# source-domain fetches, FK key-pool fit): 2,940 thread-seconds of setup
# on C_TABLE alone, serialized on the process-level single-flight locks,
# for state the sibling threads already held. Holders are refcounted; the
# engine is torn down when the LAST holder releases it, so a lone DoFn
# keeps the historical setup → teardown lifecycle.
class _EngineEntry:
  __slots__ = ("build_lock", "client", "engine", "evicted", "refs")

  def __init__(self) -> None:
    self.build_lock = threading.Lock()
    self.engine = None
    self.client = None  # the builder's ModelClient — torn down with it
    self.refs = 0
    self.evicted = False


_ENGINE_REGISTRY: dict[tuple, _EngineEntry] = {}
_ENGINE_REGISTRY_LOCK = threading.Lock()


def _reset_engine_registry() -> None:
  """Test hook — production state is deliberately process-lived."""
  with _ENGINE_REGISTRY_LOCK:
    _ENGINE_REGISTRY.clear()


def _acquire_shared_engine(key: tuple, build, model_client):
  """The process's engine for ``key``, building it (once) if absent.

    Returns ``(engine, holders)``. The build runs under a per-key lock, so
    concurrent siblings wait for one build instead of racing eight; a
    build that raises leaves nothing behind (the next caller builds). An
    entry evicted by the last release while a caller waited is retried
    against a fresh entry, never handed out torn down.
    """
  while True:
    with _ENGINE_REGISTRY_LOCK:
      entry = _ENGINE_REGISTRY.get(key)
      if entry is None:
        entry = _EngineEntry()
        _ENGINE_REGISTRY[key] = entry
    with entry.build_lock:
      if entry.evicted:
        continue
      if entry.engine is None:
        entry.engine = build()
        entry.client = model_client
      with _ENGINE_REGISTRY_LOCK:
        if _ENGINE_REGISTRY.get(key) is not entry:
          continue  # evicted between the build and the bind
        entry.refs += 1
        return entry.engine, entry.refs


def _release_shared_engine(key: tuple, engine):
  """Drop one holder; returns ``(engine, client)`` to tear down when it
    was the last, else ``None``. Eviction happens under the registry lock
    so no newcomer can bind to an engine about to be torn down."""
  with _ENGINE_REGISTRY_LOCK:
    entry = _ENGINE_REGISTRY.get(key)
    if entry is None or entry.engine is not engine:
      return None
    entry.refs -= 1
    if entry.refs > 0:
      return None
    del _ENGINE_REGISTRY[key]
    entry.evicted = True
  return entry.engine, entry.client


# Parent keys kept verbatim in a crashed key-batch's DLQ envelope. The
# batch itself can hold `--keys_per_batch` (up to 10k) tuples, and the
# envelope is a BigQuery row the gate reads back — a sample plus the
# totals diagnoses the crash; the full list only inflates the table.
_DLQ_KEY_SAMPLE = 10


def _failed_request(request: dict, keys, n: int) -> dict:
  """The `raw_request` an ``engine_failure`` envelope carries.

    A row request passes through unchanged (it is already three scalars).
    A KEY request is summarized: `batch_id`, a bounded key sample,
    `keys_total`, and `n` — which `_dlq_rule_weight` (`sdfb_beam/pipeline
    .py`) reads to weight the crashed batch by its EXPECTED lost rows
    (`len(keys) * mean_fanout`), so dropping it would silently
    under-count against the BLOCKER gate.
    """
  if keys is None:
    return request
  return {
      "batch_id": request.get("batch_id"),
      "n": request.get("n", n),
      "keys": [list(k) for k in keys[:_DLQ_KEY_SAMPLE]],
      "keys_total": len(keys),
  }


class GenerateRecordsDoFn(beam.DoFn):
  """Wraps a `GenerationEngine` inside Beam's worker lifecycle."""

  def __init__(
      self,
      engine_name: str,
      model_client: ModelClient,
      ctx: GenerationContext,
      similarity: float = 0.5,
      seed: int | None = None,
      expect_fk_side: bool = False,
      chunk_rows: int = 1000,
  ) -> None:
    super().__init__()
    self.engine_name = engine_name
    self.model_client = model_client
    self.ctx = ctx
    self.similarity = similarity
    self.base_seed = seed
    # ADR 0030 single-job relational mode: a child table's FK pools
    # arrive as a Beam SIDE INPUT (the parent's landed keys, sampled
    # in-DAG) — side inputs are visible only in process(), so the
    # heavy engine build defers to the FIRST bundle (once, guarded).
    # The setup()-builds-engines rule (CLAUDE.md) is deliberately
    # relaxed here: the build still happens exactly once per DoFn
    # instance, just one hop later.
    self.expect_fk_side = expect_fk_side
    self.chunk_rows = chunk_rows
    self._engine = None  # built in setup() (or first process())
    self._engine_key_held: tuple = ()
    # Identity columns this DoFn synthesizes — resolved once the
    # engine exists, since a constraint-routed column owns itself.
    self._identity_columns: list[str] = list(ctx.identity_columns or ())

    self._yielded = Metrics.counter("generation", "yielded")
    self._failed = Metrics.counter("generation", "failed")
    self._batch_seconds = Metrics.distribution("generation", "batch_msec")
    # NULL policy (ruling B, design 2026-09-11 §4 / ADR 0037): keys
    # dropped pre-generate for lacking a candidate on a non-nullable
    # conditional edge. See `_filter_unmatched_keys`.
    self._keys_unmatched = Metrics.counter("fanout", "keys_unmatched")

  def _scope(self):
    """Tag every milestone of this DoFn's engine with its landing
        table (ADR 0030): N tables interleave in one worker log."""
    prefix = getattr(self.ctx, "log_table_prefix", "")
    return milestone_scope(prefix) if prefix else nullcontext()

  def setup(self):
    with self._scope():
      self._setup_with_scope()

  def _setup_with_scope(self):
    t0 = time.monotonic()
    log_build_info("worker")
    log_milestone("dofn_setup_start", engine=self.engine_name)
    failure_key = f"{self.engine_name}:{self.ctx.pipeline_run_id}"
    with _SETUP_FAILURES_LOCK:
      prior_failures = _SETUP_FAILURES.get(failure_key, 0)
    if prior_failures:
      log_milestone(
          "dofn_setup_retry",
          level=logging.WARNING,
          engine=self.engine_name,
          attempt=prior_failures + 1,
      )
      # Committed only when THIS (surviving) bundle commits — i.e.
      # exactly the silent-retry-then-PASS case worker logs alone
      # could not surface into job metrics.
      Metrics.counter("generation", "setup_retries").inc()
    try:
      self._setup_inner()
    except Exception:
      with _SETUP_FAILURES_LOCK:
        _SETUP_FAILURES[failure_key] = _SETUP_FAILURES.get(failure_key, 0) + 1
      raise
    log_milestone(
        "dofn_setup_done",
        engine=self.engine_name,
        seconds=round(time.monotonic() - t0, 1),
    )

  def _setup_inner(self):
    # The engine's embedder loads from a local directory only, so a gs://
    # `embedder_uri` must be warm-pulled to worker-local disk and the ctx
    # rewritten to the local path before the engine builds its embedder
    # (see GenerationContext.embedder_uri: "local paths … pulled by the
    # DoFn"). The LLM weights need no equivalent here — the ModelClient
    # pulls those itself in its own setup().
    ctx = localize_embedder(self.ctx)
    if ctx is not self.ctx:
      self.ctx = ctx  # cache so a re-entrant setup() skips the pull

    # RAG read path (WS2 §4b.1): the BQ-backed ChunkStore cannot ride
    # the pickled graph — attach it worker-side, mirroring the
    # embedder localization above. Engines see only the ChunkStore
    # Protocol; an empty table just means the engine's fallback runs.
    if ctx.rag_chunks_table and ctx.chunk_store is None:
      from sdfb_beam.rag import store as rag_store

      ctx = ctx.model_copy(update={
          "chunk_store": rag_store.BigQueryChunkStore(ctx.rag_chunks_table)
      })
      self.ctx = ctx

    # WS5 §2 — same worker-side attachment for the persisted pool store.
    # When it answers for this (digest, model), setup() reads pools
    # instead of running the ladder, so the LAZY vLLM ignition below is
    # never triggered and the embedder has the card to itself (the
    # 2026-07-26 CUDA OOM was the embedder asking for 20 MiB while vLLM
    # already held 13.80 of 14.56 GiB in the same process).
    if getattr(ctx, "freetext_pools_table", "") and (getattr(
        ctx, "pool_store", None) is None):
      from sdfb_beam.pools import store as pool_store_mod

      ctx = ctx.model_copy(
          update={
              "pool_store":
                  pool_store_mod.BigQueryFreeTextPoolStore(
                      ctx.freetext_pools_table)
          })
      self.ctx = ctx

    # ADR 0023 — worker-side attach for the full-source-domain
    # rejection set. B.2 builds pools lazily HERE (no pool branch), and
    # a pool-layer-less B.1 run ladders here too; the store's fetches
    # are lazy and process-cached, so store-hit warm paths never pay a
    # BQ read.
    if getattr(ctx, "source_values_table", "") and (getattr(
        ctx, "source_value_store", None) is None):
      from sdfb_beam.io import source_values as source_values_mod

      ctx = ctx.model_copy(
          update={
              "source_value_store": (source_values_mod.BigQuerySourceValueStore(
                  ctx.source_values_table))
          })
      self.ctx = ctx

    # LLM ignition is LAZY (WS1 §3b): VLLMModelClient.generate_json()
    # calls its own idempotent, lock-serialized setup() on first use, so
    # a run whose columns never reach the LLM (b2 with only empirical/
    # identifier/jitter columns) never pays the vLLM bring-up — the
    # 2026-07-20 run spent 519 GPU-s igniting a server that generated
    # nothing. Failure stays loud: under strict_freetext a boot error
    # raises out of the first pool call. teardown() remains unconditional.

    if not self.expect_fk_side:
      self._ensure_engine(ctx)
    # Column name → BQ type, used to shape synthesized identity values
    # (STRING → UUIDv4, INTEGER/INT64 → non-negative int). Built once per
    # worker rather than per row.
    self._column_types = {
        column.name: column.bq_type for column in self.ctx.table_schema.columns
    }
    # Column name → BQ max_length, so a narrow STRING identity column
    # (e.g. VARCHAR(10)-style constraints) gets a truncated deterministic
    # value instead of the 36-char UUID overflowing it. Built once per
    # worker alongside `_column_types`.
    self._column_max_lengths = {
        column.name: column.max_length
        for column in self.ctx.table_schema.columns
    }

  def _ensure_engine(self, ctx, fk_side: list | None = None):
    if self._engine is not None:
      return
    if fk_side:
      empty = sorted(",".join(edge.get("cols") or ())
                     for edge in fk_side
                     if not edge.get("keys"))
      if empty:
        raise RuntimeError(
            f"in-job FK side input delivered EMPTY parent key "
            f"pools for columns {empty} — the parent stage landed "
            f"no rows; refusing to generate the child from "
            f"marginals (ADR 0030).")
      ctx = ctx.model_copy(
          update={
              "fk_key_pools": [*ctx.fk_key_pools, *fk_side],
              "fk_pools": {
                  **ctx.fk_pools,
                  **per_column_view(list(fk_side)),
              },
          })
      self.ctx = ctx
    engine_class = get_engine(self.engine_name)
    key = self._engine_key(ctx)

    def _build():
      engine = engine_class()
      engine.setup(self.model_client, ctx)
      return engine

    self._engine, holders = _acquire_shared_engine(key, _build,
                                                   self.model_client)
    self._engine_key_held = key
    if holders > 1:
      # Evidence line for the next run: N setups became one build.
      log_milestone("engine_shared", engine=self.engine_name, holders=holders)
    self._resolve_identity_columns()

  def _engine_key(self, ctx) -> tuple:
    """What makes two DoFn instances interchangeable engine holders:
        same engine, same run, same landing table (ADR 0030 puts N tables
        in one process), same reference sample."""
    schema = getattr(ctx, "table_schema", None)
    return (
        self.engine_name,
        getattr(ctx, "pipeline_run_id", ""),
        getattr(ctx, "landing_table", "") or getattr(schema, "fqn", ""),
        getattr(ctx, "reference_digest", ""),
    )

  def _resolve_identity_columns(self) -> None:
    """Identity columns this DoFn still synthesizes.

        A column whose declared clause routes to a programmatic sampler
        is generated from that clause's value space and already rejects
        every source value — overwriting it with a UUID destroys the
        declared shape for no privacy gain (2026-08-25: a `pattern`
        identity column landed UUIDv4s). The engine keeps it unique; the
        handover is logged so it is never a silent behaviour change.
        """
    declared = list(self.ctx.identity_columns or ())
    # Duck-typed: an engine without a router (B.2 today, any stub)
    # simply owns nothing and the previous behaviour holds.
    owned = getattr(self._engine, "constrained_columns", frozenset())
    self._identity_columns = [c for c in declared if c not in owned]
    handed_over = [c for c in declared if c in owned]
    if handed_over:
      log_milestone(
          "identity_constraint_owned",
          columns=",".join(handed_over),
          note="generated from the declared clause (unique per run, "
          "source values rejected) instead of UUID synthesis",
      )

  # pylint: disable-next=arguments-renamed  # Beam passes the element positionally
  def process(self, request, fk_side: list | None = None):
    with self._scope():
      yield from self._process_with_scope(request, fk_side)

  def _filter_unmatched_keys(self, keys, request, batch_id: int, n: int):
    """NULL policy (ruling B, design 2026-09-11 §4 / ADR 0037): a key
        with no candidate on a NON-nullable `ctx.fanout["conditional"]`
        edge never reaches the engine — a nullable edge's NULL-fill is
        the engine's own concern (`sdfb_core.engines.base.generate_for_
        keys`). Dropped keys are counted and diverted to the DLQ as
        `fk.unmatched`, weighted by their share of the batch's expected
        rows so the BLOCKER gate does not silently under-count them.

        Returns `(keys, matches, envelopes, expected_rows)`,
        index-aligned; `matches` is never `None` here (the caller only
        passes `None` through when the request carried no `"matches"` key
        at all). `expected_rows` is what the SURVIVING keys are still
        expected to produce: each dropped key's share is already weighted
        into its own `fk.unmatched` envelope, so leaving the request's
        original `n` in place would let a later engine crash claim those
        rows a SECOND time against the BLOCKER ratio (fix wave A5)."""
    matches = request.get("matches") or {}
    fanout_ctx = self.ctx.fanout
    conditional = (fanout_ctx.get("conditional") or []) if fanout_ctx else []
    non_nullable = [
        entry for entry in conditional if entry.get("nullable") is False
    ]
    if not non_nullable:
      return keys, matches, [], request.get("n", n)

    keep_idx: list[int] = []
    offenders: dict[int, str] = {}
    for i in range(len(keys)):
      edge_id = None
      for entry in non_nullable:
        candidates = matches.get(entry["id"])
        if candidates is None or len(candidates) <= i or not candidates[i]:
          edge_id = entry["id"]
          break
      if edge_id is None:
        keep_idx.append(i)
      else:
        offenders[i] = edge_id
    if not offenders:
      return keys, matches, [], request.get("n", n)

    weight_n = request.get("n", n)
    expected = max(1, round(weight_n / len(keys)))
    envelopes = [{
        "raw_request": {
            "batch_id": batch_id,
            "keys": [list(keys[i])],
            "n": expected,
        },
        "error_type": "referential_integrity",
        "error_detail": f"no {offenders[i]} candidate for key {keys[i]!r}",
        "rule_id": "fk.unmatched",
        "stage": "pre_generate",
    } for i in sorted(offenders)]
    filtered_keys = [keys[i] for i in keep_idx]
    filtered_matches = {
        edge_id: (
            cand_list if cand_list is None else
            [cand_list[i] if i < len(cand_list) else []
             for i in keep_idx]) for edge_id, cand_list in matches.items()
    }
    self._keys_unmatched.inc(len(offenders))
    log_milestone(
        "batch_unmatched", batch_id=batch_id, keys_dropped=len(offenders))
    return (
        filtered_keys,
        filtered_matches,
        envelopes,
        max(0, weight_n - expected * len(offenders)),
    )

  def _generate(self, keys, matches, n: int, cfg: GenerationConfig):
    """Call the engine. `matches=` rides along ONLY when the request
        carried a `"matches"` key (`matches is not None`) — otherwise
        today's positional call stays byte-identical, since not every
        `generate_for_keys` test double accepts the kwarg."""
    if keys is None:
      return self._engine.generate_batch(n, cfg)  # type: ignore[union-attr]
    if matches is not None:
      return self._engine.generate_for_keys(  # type: ignore[union-attr]
          [tuple(k) for k in keys],
          cfg,
          matches=matches)
    return self._engine.generate_for_keys([tuple(k) for k in keys],
                                          cfg)  # type: ignore[union-attr]

  def _batch_seeds(self, batch_id: int) -> tuple[int, int]:
    """``(batch seed, pool seed)`` for one request.

        No explicit seed: derive one so batches never replay each other
        while the run stays reproducible per run_id (E2E report §2). The
        pool seed is batch-INDEPENDENT — once-per-worker artifacts (the
        B.2 free-text pool build) must be stable within a run and vary
        across runs via the salted run_id; `batch_id=-1` keeps it outside
        every real batch's seed namespace. With an explicit seed the pool
        build is reproducible across reruns regardless of which batch
        reaches the worker first (P6).
        """
    if self.base_seed is None:
      return (
          derive_batch_seed(self.ctx.pipeline_run_id, batch_id),
          derive_batch_seed(self.ctx.pipeline_run_id, -1),
      )
    return self.base_seed + batch_id, self.base_seed

  def _process_with_scope(self, request, fk_side: list | None = None):
    if self._engine is None:
      self._ensure_engine(self.ctx, fk_side)
    keys = request.get("keys")
    n = len(keys) if keys is not None else int(request["n"])
    batch_id = int(request["batch_id"])
    matches = None
    keys_dropped = 0
    if keys is not None and "matches" in request:
      kept, matches, envelopes, expected_rows = self._filter_unmatched_keys(
          keys, request, batch_id, n)
      keys_dropped = len(keys) - len(kept)
      keys = kept
      if keys_dropped:
        # The batch is now the SURVIVING keys: every count that
        # stands for it — the milestones, and the expected-rows
        # weight a crashed batch carries into the DLQ — follows
        # (fix wave A5). `request` is a Beam element, so it is
        # copied rather than mutated.
        n = len(keys)
        request = {**request, "n": expected_rows}
      for envelope in envelopes:
        yield beam.pvalue.TaggedOutput("failed", envelope)
    seed, pool_seed = self._batch_seeds(batch_id)
    cfg = GenerationConfig(
        seed=seed,
        batch_size=n if keys is None else self.chunk_rows,
        similarity=self.similarity,
        engine_specific={
            "pool_seed":
                pool_seed,
            # B.2 reads per-batch config, not the worker ctx — mirror
            # the ctx flag so both engines see one setting (spec C3).
            "freetext_expansion":
                getattr(self.ctx, "freetext_expansion", "identifiers"),
            "prompt_constraints":
                getattr(self.ctx, "prompt_constraints", True),
            "prompt_debug":
                getattr(self.ctx, "prompt_debug", "off"),
        },
    )
    dropped_field = {"keys_dropped": keys_dropped} if keys_dropped else {}
    if keys is not None:
      log_milestone("batch_start", batch_id=batch_id, keys=n, **dropped_field)
    else:
      log_milestone("batch_start", batch_id=batch_id, n=n)
    t0 = time.monotonic()
    count = 0
    try:
      records = self._generate(keys, matches, n, cfg)
      for row_index, record in enumerate(records):
        self._yielded.inc()
        count += 1
        # Python-mode dump keeps datetime / Decimal as Python
        # objects; downstream stages convert to DataFrame and
        # back as needed.
        row = record.model_dump(mode="python")
        if self._identity_columns:
          row = apply_identity_columns(
              row,
              identity_columns=self._identity_columns,
              column_types=self._column_types,
              column_max_lengths=self._column_max_lengths,
              run_id=self.ctx.pipeline_run_id,
              batch_id=batch_id,
              row_index=row_index,
          )
        yield row
      if keys is not None:
        log_milestone(
            "batch_done",
            batch_id=batch_id,
            keys=n,
            rows=count,
            seconds=round(time.monotonic() - t0, 1),
            **dropped_field,
        )
      else:
        log_milestone(
            "batch_done",
            batch_id=batch_id,
            rows=count,
            seconds=round(time.monotonic() - t0, 1),
        )
      self._batch_seconds.update(int((time.monotonic() - t0) * 1000))
    except Exception as e:  # pylint: disable=broad-exception-caught
      self._failed.inc()
      yield beam.pvalue.TaggedOutput(
          "failed",
          {
              "raw_request": _failed_request(request, keys, n),
              "error_type": "engine",
              "error_detail": f"{type(e).__name__}: {e}",
              "rule_id": "engine_failure",
              "stage": "pre_write",
          },
      )

  def teardown(self):
    # The shared engine (and the ModelClient it was built with) go
    # down with the LAST holder; every DoFn still releases its own
    # client, which is a no-op for a client that never ignited.
    released = None
    try:
      if self._engine is not None:
        try:
          released = _release_shared_engine(self._engine_key_held, self._engine)
          if released is not None:
            released[0].teardown()
        finally:
          self._engine = None
    finally:
      clients = [self.model_client]
      if released is not None and released[1] is not self.model_client:
        clients.append(released[1])
      for client in clients:
        client_teardown = getattr(client, "teardown", None)
        if callable(client_teardown):
          client_teardown()
