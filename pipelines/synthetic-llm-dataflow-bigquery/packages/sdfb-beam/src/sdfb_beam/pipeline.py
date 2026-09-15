"""Main Beam DAG composer for synthetic-dataflow-bigquery.

Build the synthesis pipeline by wiring sources / sinks into an existing
`beam.Pipeline`. The caller decides DirectRunner-vs-Dataflow and local
files-vs-BigQuery I/O — `build_pipeline()` is runner-agnostic.

Layouts:
  M1 §8 (laptop):   DirectRunner + in-memory `reference_rows` +
                    `WriteToJsonLines` sinks                  (this file)
  M1 §11 (M4):      DataflowRunner + `ReadFromBigQuery` +
                    `WriteToBigQuery` sinks                   (cli.py TBD)

REFs:
  - .claude/skills/beam-dofn.md
  - .claude/skills/validation-mode-a.md
  - .claude/skills/reference-data.md
  - https://beam.apache.org/documentation/programming-guide/#additional-outputs
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

import functools
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

import apache_beam as beam
from apache_beam.metrics import Metrics
from apache_beam.transforms import combiners
from sdfb_core.contracts import TableSchema
from sdfb_core.contracts.model_adjustment import (
    REPEAT_SHARE_TOLERANCE,
    ModelAdjustment,
)
from sdfb_core.engines import GenerationContext, ModelClient
from sdfb_core.engines.fanout import FanoutPlan, conditional_edge_id
from sdfb_core.engines.pk_capacity import FK_KEY_SAMPLE_FLOOR
from sdfb_core.observability import log_milestone
from sdfb_core.rag.chunking import (
    MAX_ROW_DOC_ROWS,
    chunk_free_text_value,
    distinct_free_text_values,
)
from sdfb_core.validation import (
    STATUS_FAILED_BLOCKER,
    BlockerThresholdExceeded,
    Thresholds,
    build_run_summary,
    normalize_dlq_record,
)

from sdfb_beam.dofns import (
    EnforceUniqueness,
    GenerateRecordsDoFn,
    PanderaValidateBatchDoFn,
    ValidateRecordDoFn,
)
from sdfb_beam.dofns.fk_integrity import EnforceFkIntegrityDoFn
from sdfb_beam.dofns.pools import BuildFreeTextPoolsDoFn
from sdfb_beam.io.digest import compute_reference_digest
from sdfb_beam.rag.population import ChunkReferenceRowsDoFn, EmbedChunksDoFn


@dataclass
class PipelineConfig:
  """All non-I/O knobs for one pipeline run.

    Source / sink wiring is passed separately to `build_pipeline()` so
    that the same config can drive DirectRunner-local and Dataflow-prod
    invocations.
    """

  table_schema: TableSchema
  engine_name: str
  model_client: ModelClient
  num_rows: int
  batch_size: int = 16
  similarity: float = 0.5
  seed: int | None = None
  # Per-row-unique columns (PK/UUID) synthesized fresh each row; never
  # sampled from reference data. See engines/identity.py.
  identity_columns: tuple[str, ...] = ()
  # Declared primary-key columns; duplicate PK tuples divert to the DLQ as
  # rule_id=pk.duplicate (BLOCKER). Empty = PK not declared (rule idle).
  # ADR 0038: EFFECTIVE, not declared — an adjusted table's is empty,
  # so nothing keys, dedupes or caps on a key the source disproved.
  pk_columns: tuple[str, ...] = ()
  # ADR 0038 — the DECLARED PK of an adjusted table, kept for
  # MEASUREMENT only. `EnforceUniqueness` still counts `pk.duplicate`
  # against it (in `streaming` mode, so no row is removed), which is
  # how the run proves the landing table reproduces the source's
  # key-repeat share. Nothing else reads it.
  pk_measure_columns: tuple[str, ...] = ()
  # ADR 0038 — rule_ids excluded from the BLOCKER gate's numerator for
  # this table, and why. `pk.duplicate` on an adjusted table: the
  # duplicates are the point, so counting them would fail every
  # faithful run. Named in the summary row, never hidden.
  gate_excluded_rules: tuple[str, ...] = ()
  # ADR 0038 — the SOURCE key-repeat share this table must reproduce:
  # `1 - key_tuples / rows` over the DECLARED PK, measured on the
  # source child at launch (fix J), which is the same column set
  # `pk_measure_columns` above counts `pk.duplicate` over. The run
  # summary compares it with the landed share.
  source_repeat_share: float | None = None
  # Real-LLM runs re-raise on free-text LLM failure instead of silently
  # copying exemplars. Set from client_type at the CLI boundary.
  strict_freetext: bool = False
  run_id: str = "local-run"
  # Worker-local model paths surfaced to engines via GenerationContext.
  # model_uri = the LLM (also given to the ModelClient); embedder_uri =
  # B.1's embedder. Empty ⇒ the engine uses its dependency-free default.
  model_uri: str = ""
  embedder_uri: str = ""
  # §12 — Mode-A run-level gate + provenance for synthetic_data_quality.
  reference_table: str = ""
  landing_table: str = ""
  thresholds: Thresholds | None = None
  fail_on_blocker: bool = True
  # RAG layer (WS2 §4b). rag_chunks_table threads the READ path into the
  # worker ctx (self-gating on data); embedder identity pins the vector
  # space and must come from the ORIGINAL embedder URI (driver-side).
  rag_chunks_table: str = ""
  embedder_id: str = ""
  embedder_version: str = ""
  # Opt-in decode-time format constraint for identifier-ish free-text
  # pools (2026-07-25 hallucination fix, layer 2). See
  # GenerationContext.pool_pattern_guidance.
  pool_pattern_guidance: bool = False
  # Persisted free-text pools (WS5 §2). Threads the READ path into the
  # worker ctx exactly as rag_chunks_table does; the build branch is
  # gated separately by the driver passing `freetext_pools_store`.
  freetext_pools_table: str = ""
  # WS5 §3 seeding experiment: centroid | kcenter | kcenter_rotate.
  pool_seed_strategy: str = "centroid"
  # Shape-preserving expander (2026-08-05 spec C3): off | identifiers
  # (default) | all. See GenerationContext.freetext_expansion.
  freetext_expansion: str = "identifiers"
  # Attach per-column llm_prompt_constraint (from column-description
  # JSON) to pool prompts (spec C5). See GenerationContext.prompt_constraints.
  prompt_constraints: bool = True
  # off | redacted | full — log each built pool prompt as a
  # freetext_pool_prompt milestone (ADR 0024 §3c). See
  # GenerationContext.prompt_debug.
  prompt_debug: str = "off"
  # Tier-2 exact per-column distinct counts (--source_stats=exact,
  # ADR 0022) — feeds free-text pool sizing. Empty = Tier 1 only.
  source_distinct: dict = field(default_factory=dict)
  # Reference-table FQN for the worker-side ADR 0023 source-value store
  # attach (B.2 builds pools lazily in Generate workers). Empty = off.
  source_values_table: str = ""
  # Enforced FK edges → the parent's landed key TUPLES (ADR 0031),
  # loaded driver-side by io/fk_pools for a parent that landed in an
  # earlier job. Same-job parents deliver theirs as a side input.
  fk_key_pools: list = field(default_factory=list)
  # Per-column projection of the same keys — plan/metadata only.
  fk_pools: dict = field(default_factory=dict)
  # Declared FK edges as display metadata for the worker's
  # `relational_e2e` pretty log (ADR 0028 follow-up): each
  # {"cols": [...], "ref": "ds.parent", "parent_landing": fqn}.
  fk_edges: tuple = ()
  # Multi-table launches (ADR 0030): landing table NAME qualifying
  # column references in logs; empty = single-table bare names.
  log_table_prefix: str = ""
  # The relationship card the LAUNCHER rendered from
  # `config/relationships/` (ADR 0032), carried to the workers so both
  # logs show the same model. Empty = this table is in no model.
  relationship_card: str = ""
  # WS6 W3: "exact" (default, today) diverts every duplicate to the DLQ
  # behind up to three shuffle barriers; "streaming" lands rows as they
  # are generated and measures the duplicate rate instead.
  uniqueness_mode: str = "exact"
  # RAG population embed (ADR 0034 D8, rev. 2026-09-08). The device is
  # the worker GPU on every topology ("auto"); moving it to CPU under
  # multi starved the model pull and the vLLM engine init instead
  # (09-08 cold: 177 s pull, 598 s ignition, 15-min stage). What bounds
  # the contention is the FAN-OUT: at most `rag_embed_shards` embedders
  # run at once (each a CUDA context + 130 MB of weights) instead of
  # one per SDK process, and the pool branch waits for them to finish
  # before its first LLM call spawns vLLM.
  rag_embed_device: str = "auto"
  rag_embed_shards: int = 2
  # ADR 0036 driven-child recipe: a DRIVEN child's `FanoutPlan.to_payload()`
  # dict (driving edge columns, the source fan-out histogram, the
  # PK-completing cell table). Threaded to `GenerationContext.fanout`;
  # None = this table generates from `--num_rows` batch requests as
  # before.
  fanout: dict | None = None


def _default_requests(p: beam.Pipeline, config: PipelineConfig,
                      label_prefix: str):
  """Driver-side batch request specs from ``--num_rows``/``batch_size``
    — the request stream for a table with no fanout-mode parent edge
    (ADR 0036's ``requests`` override replaces this for a driven child)."""
  request_specs: list[dict] = []
  remaining = config.num_rows
  batch_id = 0
  while remaining > 0:
    n = min(config.batch_size, remaining)
    request_specs.append({"batch_id": batch_id, "n": n})
    remaining -= n
    batch_id += 1
  return p | f"{label_prefix}CreateRequests" >> beam.Create(request_specs)


def build_pipeline(
    p: beam.Pipeline,
    *,
    reference_rows: list[dict],
    config: PipelineConfig,
    landing_sink: beam.PTransform,
    dlq_sink: beam.PTransform,
    validation_runs_sink: beam.PTransform | None = None,
    rag_chunks_sink: beam.PTransform | None = None,
    freetext_pools_store: Any = None,
    source_value_store: Any = None,
    label_prefix: str = "",
    fk_side: Any = None,
    requests: Any = None,
) -> dict[str, Any]:
  """Wire the synthesis DAG onto an existing Beam Pipeline.

    Returns a metadata dict with the reference digest, run id, and
    handles to the resulting PCollections (`valid`, `dlq`) for callers
    that want to attach further transforms (metrics, additional sinks).

    ``label_prefix`` namespaces every transform label so N tables can
    share ONE pipeline (ADR 0030 single-job relational mode); ``fk_side``
    is that mode's parent-keys side input (`AsSingleton` of a list of
    ``{"cols", "keys"}`` edge payloads, ADR 0031) — it defers the child's
    engine build to the first bundle (`GenerateRecordsDoFn.expect_fk_side`).
    ``requests`` is ADR 0036's DRIVEN-child alternative: a PCollection of
    request dicts (`_fanout_requests`) that REPLACES the driver-built
    ``Create(request_specs)`` below — a driven child has no `--num_rows`
    batch plan, only its parent's landed key batches.
    """
  for label, cols in (
      ("identity_columns", config.identity_columns),
      ("pk_columns", config.pk_columns),
      ("pk_measure_columns", config.pk_measure_columns),
  ):
    if cols:
      valid_columns = {c.name for c in config.table_schema.columns}
      unknown = [c for c in cols if c not in valid_columns]
      if unknown:
        raise ValueError(f"{label} not found on {config.table_schema.fqn}: "
                         f"{unknown}. Valid columns: {sorted(valid_columns)}")

  digest = compute_reference_digest(reference_rows)
  ctx = GenerationContext(
      table_schema=config.table_schema,
      reference_rows=reference_rows,
      reference_digest=digest,
      pipeline_run_id=config.run_id,
      model_uri=config.model_uri,
      embedder_uri=config.embedder_uri,
      identity_columns=list(config.identity_columns),
      pk_columns=list(config.pk_columns),
      strict_freetext=config.strict_freetext,
      num_rows=config.num_rows,
      embedder_id=config.embedder_id,
      embedder_version=config.embedder_version,
      rag_chunks_table=config.rag_chunks_table,
      pool_pattern_guidance=config.pool_pattern_guidance,
      freetext_pools_table=config.freetext_pools_table,
      pool_seed_strategy=config.pool_seed_strategy,
      freetext_expansion=config.freetext_expansion,
      prompt_constraints=config.prompt_constraints,
      prompt_debug=config.prompt_debug,
      fk_pools=config.fk_pools,
      fk_key_pools=[dict(e) for e in config.fk_key_pools],
      fk_edges=[dict(e) for e in config.fk_edges],
      landing_table=config.landing_table,
      log_table_prefix=config.log_table_prefix,
      relationship_card=config.relationship_card,
      source_distinct=config.source_distinct,
      source_values_table=config.source_values_table,
      fanout=config.fanout,
  )

  if requests is None:
    requests = _default_requests(p, config, label_prefix)

  # WS2 §4b.1 — optional rag_chunks population branch. The driver decides
  # (existence check) whether to pass a sink; None ⇒ branch absent, DAG
  # unchanged (the validation_runs_sink precedent). Feeds on
  # `reference_rows` — the driver-loaded ≤10k sample whose digest is this
  # run's provenance key — NOT a full-table read; scope rationale in
  # sdfb_beam/rag/population.py. Built BEFORE the pool branch so the
  # pool trigger can wait on the embedded chunks (ADR 0034 D8).
  chunks = None
  if rag_chunks_sink is not None:
    chunks = _population_branch(
        p, config, reference_rows, digest, label_prefix=label_prefix)
    _ = chunks | f"{label_prefix}WriteRagChunks" >> rag_chunks_sink

  # WS5 §2 / 2026-07-29 four-run postmortem — optional free-text pool
  # build branch. The driver decides (digest existence check) whether to
  # pass a store; None ⇒ branch absent, DAG unchanged. The branch builds
  # the pool ladder ONCE, writes `freetext_pools` itself (blocking load
  # job inside the DoFn), and its OUTPUT gates Generate below: on the
  # R1/R3 cold runs an ungated Generate raced the branch and every pool
  # was built twice concurrently on the same GPU (R3: 2x 13 ladders,
  # 2,005 s + 2,026 s of duplicated LLM time). The AsList side input is a
  # runner-level barrier — Generate bundles are not scheduled until the
  # branch (build + store write) completes, so every Generate setup's
  # store fetch hits.
  if freetext_pools_store is not None:
    trigger = p | f"{label_prefix}PoolTrigger" >> beam.Create([None])
    if chunks is not None:
      # ADR 0034 D8: the branch's first LLM call spawns vLLM; wait
      # for the population embedders to finish (and demote) so the
      # spawn meets a free card and idle cores (09-07 R7m: 8 x 20 s
      # unfittable waits; 09-08 cold: a 598 s ignition beside 56
      # CPU embedders).
      trigger = trigger | f"{label_prefix}AwaitRagPopulation" >> beam.Map(
          lambda x, _chunks: x, _chunks=beam.pvalue.AsList(chunks))
    pool_rows = (
        trigger
        | f"{label_prefix}BuildFreeTextPools" >> beam.ParDo(
            BuildFreeTextPoolsDoFn(
                config.engine_name,
                config.model_client,
                ctx,
                store=freetext_pools_store,
                source_value_store=source_value_store,
            )))
    requests = requests | f"{label_prefix}AwaitFreeTextPools" >> beam.Map(
        lambda spec, _pools: spec, _pools=beam.pvalue.AsList(pool_rows))

  generated = (
      requests
      | f"{label_prefix}Generate" >> _generate_pardo(
          config, ctx, fk_side).with_outputs("failed", main="main"))

  # Line 4 of defense (ADR 0031) — referential integrity, measured per
  # run instead of assumed. Present only when this table declares
  # enforced FK edges; absent ⇒ DAG unchanged.
  generated_main, fk_invalid = _fk_integrity_stage(generated.main, config,
                                                   fk_side, label_prefix)

  record_validated = (
      generated_main
      | f"{label_prefix}ValidateRecord" >> beam.ParDo(
          ValidateRecordDoFn(table_schema=config.table_schema)).with_outputs(
              "invalid", main="main"))

  batched = (
      record_validated.main
      # WS6 F3 (2026-07-27_10_42_52 E2E): 10-100-row batches meant Pandera
      # validated 1M rows as 10k-100k MICRO-DataFrames — the per-frame
      # construction + schema-compile overhead made PanderaValidate the
      # funnel inside the fused Generate->KeyByRowDigest stage (~0.88k
      # rows/s). Pandera's cost is amortized over rows in the frame, so
      # validate thousands at a time, not tens.
      | f"{label_prefix}Batch" >> beam.BatchElements(
          min_batch_size=1_000, max_batch_size=10_000))
  batch_validated = (
      batched
      | f"{label_prefix}PanderaValidate" >> beam.ParDo(
          PanderaValidateBatchDoFn(table_schema=config.table_schema))
      .with_outputs("invalid", main="main"))

  # Line 3 of defense — full-row and identity-column duplicates divert to
  # the DLQ instead of landing (first occurrence per key wins).
  uniq = batch_validated.main | f"{label_prefix}EnforceUniqueness" >> EnforceUniqueness(
      identity_columns=list(config.identity_columns),
      # ADR 0038 — an ADJUSTED table has no effective PK, but its
      # DECLARED one is exactly the claim under test: `pk.duplicate`
      # must still be MEASURED against it. The launcher pins such a
      # table to `streaming`, where the PK branch is digest-only and
      # removes nothing, so measuring here cannot divert a row.
      pk_columns=list(config.pk_columns or config.pk_measure_columns),
      mode=config.uniqueness_mode,
      # ADR 0034: the exact barrier shuffles rows as value tuples in
      # schema order — half the bytes of a keyed dict per row.
      columns=[c.name for c in config.table_schema.columns],
  )

  # Landing sink — valid, unique records only.
  _ = uniq["unique"] | f"{label_prefix}WriteLanding" >> landing_sink

  # DLQ — flatten every failure tag (five with the FK gate wired),
  # then normalize the heterogeneous envelopes into the uniform
  # dead_letter schema before writing.
  dlq_raw = _dlq_inputs(generated, fk_invalid, record_validated,
                        batch_validated,
                        uniq) | f"{label_prefix}FlattenDLQ" >> beam.Flatten()
  dlq = dlq_raw | f"{label_prefix}NormalizeDLQ" >> beam.Map(
      normalize_dlq_record, run_id=config.run_id)
  _ = dlq | f"{label_prefix}WriteDLQ" >> dlq_sink

  result: dict[str, Any] = {
      "reference_digest": digest,
      "run_id": config.run_id,
      "valid": uniq["unique"],
      "dlq": dlq,
      "generation_context": ctx,
  }

  # §12 — run-level summary row + BLOCKER gate. Only wired when a
  # validation_runs sink is supplied; happy-path DirectRunner tests that
  # don't assert run metadata omit it.
  if validation_runs_sink is not None:
    thresholds = config.thresholds or Thresholds(
        env="dev", blocker_failure_ratio=1.0)
    valid_count, dlq_by_rule = _gate_inputs(uniq, dlq_raw,
                                            config.uniqueness_mode,
                                            label_prefix)
    summary_rows = (
        p
        | f"{label_prefix}SummarySeed" >> beam.Create([None])
        | f"{label_prefix}BuildValidationRun" >> beam.Map(
            _build_validation_run_row,
            valid_count=beam.pvalue.AsSingleton(valid_count),
            dlq_by_rule=beam.pvalue.AsSingleton(dlq_by_rule),
            thresholds=thresholds,
            run_id=config.run_id,
            reference_digest=digest,
            num_rows=config.num_rows,
            reference_table=config.reference_table,
            landing_table=config.landing_table,
            engine=config.engine_name,
            model_uri=config.model_uri,
            excluded_blocker_rules=tuple(config.gate_excluded_rules),
            source_repeat_share=config.source_repeat_share,
        ))
    write_result = summary_rows | f"{label_prefix}WriteValidationRun" >> validation_runs_sink
    if config.fail_on_blocker:
      gate_kwargs = {}
      load_jobs = getattr(write_result, "destination_load_jobid_pairs", None)
      if load_jobs is not None:
        # Order the gate AFTER the FILE_LOADS load jobs commit — a
        # tripped gate must fail the JOB, not suppress the FAILED
        # run's own summary row (2026-07-20 b2 run: zero
        # validation_runs trace). Non-BQ sinks (DirectRunner tests)
        # expose no WriteResult and keep the sibling wiring.
        gate_kwargs["wait_on_write"] = beam.pvalue.AsIter(load_jobs)
      _ = summary_rows | f"{label_prefix}BlockerGate" >> beam.ParDo(
          _BlockerGateDoFn(), **gate_kwargs)
    result["validation_run"] = summary_rows

  return result


def _dlq_inputs(generated, fk_invalid, record_validated, batch_validated, uniq):
  """Every failure tag that feeds the DLQ, in stage order. `fk.orphan`
    rides between generation and per-record validation — a row that
    references a parent that does not exist is a failure of the run, not
    of the record's shape."""
  tags = [
      generated.failed,
      record_validated.invalid,
      batch_validated.invalid,
      uniq["duplicates"],
  ]
  if fk_invalid is not None:
    tags.insert(1, fk_invalid)
  return tuple(tags)


def _fk_integrity_stage(generated_main, config, fk_side, label_prefix: str):
  """``(rows to validate, orphan rows or None)``.

    A table with no enforced FK edge keeps its DAG shape unchanged — the
    optional-branch idiom used by the pool/RAG branches."""
  if fk_side is None and not config.fk_key_pools:
    return generated_main, None
  dofn = EnforceFkIntegrityDoFn(
      fk_key_pools=[dict(e) for e in config.fk_key_pools])
  pardo = (
      beam.ParDo(dofn, fk_side=fk_side)
      if fk_side is not None else beam.ParDo(dofn))
  checked = generated_main | f"{label_prefix}EnforceFkIntegrity" >> (
      pardo.with_outputs("invalid", main="main"))
  return checked.main, checked.invalid


def _generate_pardo(config: PipelineConfig, ctx, fk_side):
  """The Generate ParDo; a child table's parent-key side input rides
    as a process() kwarg and defers the engine build (ADR 0030)."""
  dofn = GenerateRecordsDoFn(
      engine_name=config.engine_name,
      model_client=config.model_client,
      ctx=ctx,
      similarity=config.similarity,
      seed=config.seed,
      expect_fk_side=fk_side is not None,
      chunk_rows=config.batch_size,
  )
  if fk_side is not None:
    return beam.ParDo(dofn, fk_side=fk_side)
  return beam.ParDo(dofn)


# In-DAG FK key-pool cap (ADR 0030) — the floor: a child samples from at
# most this many parent key tuples unless its PK contains the FK, in which
# case preflight sizes the edge's `key_sample_cap` from num_rows and the
# sibling PK members (ADR 0035 — the 2026-09-09 C_TABLE collapse).
_FK_SIDE_SAMPLE_CAP = FK_KEY_SAMPLE_FLOOR


@dataclass(frozen=True)
class FkEdgeSpec:
  """One enforced FK edge resolved INSIDE the job (ADR 0030): the
    child's ``child_cols`` sample from the parent's landed ``ref_cols``,
    delivered as a side input — no BQ round-trip, integrity by
    construction. ``parent_pk`` lets the composer skip the Distinct
    shuffle when the ref tuple IS the parent PK (already unique after
    EnforceUniqueness)."""

  child_cols: tuple[str, ...]
  ref_cols: tuple[str, ...]
  parent_landing: str
  # The parent's BARE model name (`FkEdge.ref`), which `edge_id` needs to
  # tell two edges from the same columns to different parents apart
  # (ruling 14). `parent_landing` stays the FQN the composer looks the
  # built PCollection up by.
  parent_table: str = ""
  parent_pk: tuple[str, ...] = ()
  # Parent key tuples the child sees (ADR 0035): the floor unless the
  # child's PK contains this edge's columns, then preflight's sizing.
  key_sample_cap: int = FK_KEY_SAMPLE_FLOOR
  # ADR 0036: "side_input" (ADR 0030 key sample), "fanout" (this edge DRIVES
  # the child — its parent's keys are the child's generation input) or
  # "implied" (satisfied by construction through the driving edge; no DAG
  # edge at all). ADR 0037 adds "conditional": the edge shares columns
  # with the driving edge, so its remaining columns are CO-PARTITIONED —
  # joined on the shared values instead of drawn from a pool.
  mode: str = "side_input"
  keys_per_batch: int = 100
  # ADR 0037 — conditional edges only. `overlap` is the child columns
  # this edge shares with the driving edge (child names, in
  # `child_cols` order); the rest come from a parent row that holds the
  # shared value. `candidate_cap` is the Top-M bound per shared value
  # (`--fk_candidate_cap`), so a hot shared key never carries an
  # unbounded candidate list into a request. `nullable` says whether
  # every `rest` column is NULLABLE in the landing schema — the engine's
  # NULL policy for a key with no candidate (design §4 ruling B).
  overlap: tuple[str, ...] = ()
  candidate_cap: int = 64
  nullable: bool = False

  @property
  def edge_id(self) -> str:
    """The name the payload, the plan and the engine agree on for
        this edge (`ConditionalEdge.id`) — `(child_cols)->parent_table`,
        built by the ONE helper every producer calls."""
    return conditional_edge_id(self.child_cols, self.parent_table)


@dataclass(frozen=True)
class TableSpec:
  """One table of a single-job relational launch (ADR 0030)."""

  config: PipelineConfig
  reference_rows: list[dict]
  landing_sink: beam.PTransform
  dlq_sink: beam.PTransform
  validation_runs_sink: beam.PTransform | None = None
  rag_chunks_sink: beam.PTransform | None = None
  freetext_pools_store: Any = None
  source_value_store: Any = None
  parent_edges: tuple[FkEdgeSpec, ...] = ()
  # ADR 0038 — what the full-source measurement forced on this table's
  # DECLARED relationship model. The launcher collects them across
  # every spec and announces the launch's adjustments once.
  adjustments: tuple[ModelAdjustment, ...] = ()


def _edge_key_pools(parent_valid, edge: FkEdgeSpec, prefix: str):
  """The parent's landed key TUPLES for one edge → a one-element
    PCollection holding ``[{"cols": [...], "keys": [(v, …), …]}]``.

    The tuple survives end to end (ADR 0031): splitting it into
    per-column pools here is exactly what let a child assemble a
    combination its parent never held. A key containing NULL is dropped —
    SQL equality never matches NULL, so it is not referenceable.
    """
  tuples = (
      parent_valid
      | f"{prefix}FkTuples" >>
      beam.Map(lambda r, rc=edge.ref_cols: tuple(r[c] for c in rc))
      | f"{prefix}FkDropNullKeys" >>
      beam.Filter(lambda t: all(v is not None for v in t)))
  if tuple(sorted(edge.ref_cols)) != tuple(sorted(edge.parent_pk)):
    tuples = tuples | f"{prefix}FkDistinct" >> beam.Distinct()
  sampled = (
      tuples
      | f"{prefix}FkSample" >> combiners.Sample.FixedSizeGlobally(
          edge.key_sample_cap))
  return sampled | f"{prefix}FkPools" >> beam.Map(
      _key_pool_payload, cols=list(edge.child_cols), cap=edge.key_sample_cap)


def _key_pool_payload(keys,
                      cols: list[str],
                      cap: int = _FK_SIDE_SAMPLE_CAP) -> list[dict]:
  """One edge's side-input payload. The cap is announced, never
    silent: a child sampling 100k of a larger parent's keys references
    only those, which caps its own FK distinct count."""
  if len(keys) >= cap:
    log_milestone(
        "fk_key_pool_capped",
        level=logging.WARNING,
        columns=",".join(cols),
        cap=cap,
        detail=("the parent holds at least as many distinct keys as the "
                "cap; the child references a uniform sample of them, so "
                "its FK distinct count cannot exceed the cap"),
    )
  return [{"cols": cols, "keys": list(keys)}]


def _join_key_positions(edge: FkEdgeSpec) -> tuple[int, ...]:
  """Positions inside ``edge.ref_cols`` that must not be NULL.

    A widened driving edge (D4) carries INHERITED columns next to the join
    key, and an inherited NULL is ordinary data — the child copies it
    verbatim. Only the join key itself has to be present, so the gate is
    the parent PK's positions inside the ref tuple. With no declared
    parent PK (or a PK that does not appear in the tuple at all) there is
    nothing to single out and every position gates, as before.
    """
  pk = set(edge.parent_pk)
  positions = tuple(i for i, c in enumerate(edge.ref_cols) if c in pk)
  return positions or tuple(range(len(edge.ref_cols)))


class _DropNullJoinKeysDoFn(beam.DoFn):
  """Drop a parent key tuple whose JOIN-KEY columns are NULL, counting
    each drop as ``fanout / keys_dropped_null``.

    A dropped tuple costs the child every row that parent would have
    produced, so the count is a run-level fact the report reads next to
    the derived row count — never a silent Filter.
    """

  def __init__(self, positions: tuple[int, ...]) -> None:
    super().__init__()
    self._positions = tuple(positions)
    self._dropped = Metrics.counter("fanout", "keys_dropped_null")

  # pylint: disable-next=arguments-renamed  # Beam passes the element positionally
  def process(self, key: tuple):
    if any(key[i] is None for i in self._positions):
      self._dropped.inc()
      return
    yield key


class _DropNullCandidateKeysDoFn(beam.DoFn):
  """Drop a co-parent candidate whose SHARED (join-key) value is NULL,
    counting each drop as ``fanout / candidates_dropped_null``.

    SQL equality never matches NULL, so such a row is not referenceable —
    but it is also a row the child could have inherited a value from, so
    the loss is counted, never silently filtered.
    """

  def __init__(self) -> None:
    super().__init__()
    self._dropped = Metrics.counter("fanout", "candidates_dropped_null")

  def process(self, element: tuple):
    join_key, _ = element
    if any(v is None for v in join_key):
      self._dropped.inc()
      return
    yield element


def _candidate_rank(element: tuple, run_id: str) -> tuple:
  """``(join_key, rest_value)`` → ``(join_key, (rank, rest_value))``.

    The rank is `blake2b(run_id + rest_value)`, never Python's
    process-salted `hash()`: the Top-M below must pick the SAME M
    candidates on a retried bundle and on a re-run of the same run_id.
    The combine orders on the rank ALONE (`_candidate_rank_only`) — only
    the JOIN KEY is NULL-checked, so a `rest_value` legitimately holds a
    None, and ordering on the whole pair would let a 64-bit rank tie
    advance the comparison to `None < "a"` and kill the bundle (and its
    retry, identically).
    """
  join_key, rest_value = element
  rank = int.from_bytes(
      hashlib.blake2b((run_id + repr(rest_value)).encode(),
                      digest_size=8).digest(),
      "big",
  )
  return (join_key, (rank, rest_value))


def _conditional_candidates(parent_valid, edge: FkEdgeSpec, prefix: str,
                            run_id: str) -> Any:
  """One conditional edge's co-parent → ``(join_key, [rest_value, …])``
    (design 2026-09-11 §4).

    ``join_key`` is the edge's SHARED columns (the ones the driving key
    already fixes), ``rest_value`` everything else the edge owns, so the
    CoGroupByKey below hands a driving key only values its co-parent
    actually holds for that shared value. Both sides are keyed on narrow
    tuples — never on rows — and the per-key list is capped at
    ``candidate_cap`` by the combine, so a hot shared value costs a
    bounded amount of shuffle and a bounded request payload.
    """
  join_cols = tuple(
      edge.ref_cols[edge.child_cols.index(o)] for o in edge.overlap)
  rest_cols = tuple(edge.ref_cols[edge.child_cols.index(c)]
                    for c in edge.child_cols
                    if c not in edge.overlap)
  projected = (
      parent_valid
      | f"{prefix}CandidateProject" >>
      beam.Map(lambda r, jc=join_cols, rc=rest_cols: (
          tuple(r[c] for c in jc),
          tuple(r[c] for c in rc),
      ))
      |
      f"{prefix}CandidateDropNull" >> beam.ParDo(_DropNullCandidateKeysDoFn()))
  # The parent PK inside the projection makes every projected pair
  # unique already (EnforceUniqueness landed it), so the Distinct
  # shuffle is pure cost — the `_edge_key_pools` precedent.
  pk_projected = bool(edge.parent_pk) and set(
      edge.parent_pk) <= set(join_cols + rest_cols)
  if not pk_projected:
    projected = projected | f"{prefix}CandidateDistinct" >> beam.Distinct()
  return (projected
          |
          f"{prefix}CandidateRank" >> beam.Map(_candidate_rank, run_id=run_id)
          | f"{prefix}CandidateTopM" >> combiners.Top.SmallestPerKey(
              edge.candidate_cap, key=_candidate_rank_only)
          | f"{prefix}Candidates" >> beam.Map(_drop_candidate_rank))


def _candidate_rank_only(ranked: tuple) -> int:
  """Order key for the Top-M combine: the rank, never the value."""
  return ranked[0]


def _drop_candidate_rank(element: tuple) -> tuple:
  """``(join_key, [(rank, rest_value), …])`` → ``(join_key, [rest_value, …])``."""
  join_key, ranked = element
  return (join_key, [value for _, value in ranked])


def _key_by_overlap(element, positions: tuple[int, ...], paired: bool) -> tuple:
  """A driving key (or a key that already carries matches) keyed by the
    columns it shares with one conditional edge.

    The driving key tuple is aligned to the driving edge's ``ref_cols``,
    and ``child_cols[i]`` names ``ref_cols[i]``, so the position of a
    shared CHILD column inside the driving edge's ``child_cols`` is its
    position inside the key. ``paired`` is decided at graph-construction
    time — never sniffed from the element — so the second conditional
    edge in a chain cannot mistake a key for a pair.
    """
  key, matches = element if paired else (element, {})
  return (tuple(key[i] for i in positions), (key, matches))


def _emit_matches(element: tuple, edge_id: str):
  """One CoGroupByKey group → its driving keys, each carrying this
    edge's candidate list (``[]`` when the co-parent has none for the
    shared value — the key must still reach the engine, which owns the
    NULL policy)."""
  _, group = element
  candidates = list(group["c"])
  matched = candidates[0] if candidates else []
  for key, matches in group["k"]:
    yield (key, {**matches, edge_id: matched})


def _attach_matches(keyed, cands, edge: FkEdgeSpec, prefix: str) -> Any:
  """Join one conditional edge's candidates onto the driving keys.

    ``keyed`` elements are ``(join_key, (key, matches_so_far))`` and
    ``cands`` ``(join_key, [rest_value, …])``; the result is
    ``(key, matches)`` ready for the next edge in the chain."""
  return ({
      "k": keyed,
      "c": cands
  }
          | f"{prefix}CandidateJoin" >> beam.CoGroupByKey()
          | f"{prefix}AttachMatches" >> beam.FlatMap(
              _emit_matches, edge_id=edge.edge_id))


def _fanout_requests(
    parent_valid,
    edge: FkEdgeSpec,
    prefix: str,
    mean_fanout: float,
    conditional: tuple[tuple[int, FkEdgeSpec, Any], ...] = (),
    run_id: str = "",
) -> Any:
  """The parent's landed key tuples as key-batch requests for a DRIVEN
    child (ADR 0036): project, Distinct only when the tuple lacks the
    parent PK, join each conditional edge's candidates on the shared
    columns (ADR 0037), Reshuffle off the parent's write path, batch.

    Each request carries ``n`` — the batch's EXPECTED child-row count
    (``len(keys) * mean_fanout``, never below 1) — alongside ``keys``.
    ``GenerateRecordsDoFn`` prefers ``keys`` for actual generation, but
    the BLOCKER gate weights an ``engine_failure`` DLQ envelope by
    ``raw_request["n"]`` (`_dlq_rule_weight`): without it, a crashed key
    batch would count as a single lost row instead of the ~mean_fanout
    rows it was expected to produce.
    """
  keys = parent_valid | f"{prefix}FanoutKeys" >> beam.Map(
      lambda r, rc=edge.ref_cols: tuple(
          r[c] for c in rc)) | f"{prefix}FanoutDropNull" >> beam.ParDo(
              _DropNullJoinKeysDoFn(_join_key_positions(edge)))
  # An undeclared parent PK (`parent_pk == ()`) proves nothing about
  # uniqueness, so the projection is deduplicated — otherwise a
  # duplicated key in the request stream yields PK-IDENTICAL children
  # (the per-key seed replays the same key and the same cells; the free
  # columns differ, since those are sampled per chunk position),
  # colliding on the child's own PK.
  pk_in_tuple = bool(edge.parent_pk) and set(edge.parent_pk) <= set(
      edge.ref_cols)
  if not pk_in_tuple:
    keys = keys | f"{prefix}FanoutDistinct" >> beam.Distinct()
  # One CoGroupByKey per conditional edge, BEFORE the Reshuffle: the
  # candidates ride with their key into the batch, so generation stays
  # a pure per-request function (no second side input, no lookup).
  paired = False
  for j, cond, cond_parent in conditional:
    cond_prefix = f"{prefix}cond{j}/"
    positions = tuple(edge.child_cols.index(o) for o in cond.overlap)
    candidates = _conditional_candidates(cond_parent, cond, cond_prefix, run_id)
    keyed = keys | f"{cond_prefix}KeyByOverlap" >> beam.Map(
        _key_by_overlap, positions=positions, paired=paired)
    keys = _attach_matches(keyed, candidates, cond, cond_prefix)
    paired = True
  batches = (
      keys
      | f"{prefix}FanoutReshuffle" >> beam.Reshuffle()
      | f"{prefix}FanoutBatch" >> beam.BatchElements(
          min_batch_size=edge.keys_per_batch,
          max_batch_size=edge.keys_per_batch))
  return batches | f"{prefix}FanoutRequests" >> beam.Map(
      _fanout_request_payload, mean_fanout=mean_fanout, paired=paired)


def _fanout_request_payload(ks: list, mean_fanout: float, *,
                            paired: bool) -> dict:
  """One key-batch request. Stable ``batch_id`` — never Python's
    process-salted `hash()` — so a retried bundle reproduces the same
    seed (`derive_batch_seed`).

    63 bits, not 32: at the 100M-key ceiling a 32-bit id collides on the
    birthday bound long before the batch count does, and two batches
    sharing an id share a seed. The final ``>> 1`` keeps it positive so it
    survives a BigQuery INT64 round trip in a DLQ envelope.

    ``paired`` says whether a conditional join ran, so the elements are
    ``(key, matches)`` pairs rather than bare key tuples. It is a
    GRAPH-construction fact, never inferred from the element: a widened
    driving edge (D4) can carry an inherited RECORD column, and
    ``(cid, {...})`` is then a plain two-column key that a shape sniff
    would read as a pair — scalar keys, a moved ``batch_id``, and a
    spurious ``"matches"`` tripping the DoFn's gate.

    When paired, the payload gains ``"matches": {edge_id:
    [candidates_for_key_0, …]}``, POSITIONALLY aligned with ``keys`` — an
    unmatched key holds ``[]`` rather than disappearing. Otherwise the
    payload is byte for byte what ADR 0036 produced, so no downstream
    seed moves.
    """
  keys = [key for key, _ in ks] if paired else list(ks)
  batch_id = (
      int.from_bytes(
          hashlib.blake2b(repr(keys[0]).encode(), digest_size=8).digest(),
          "big") >> 1)
  payload = {
      "batch_id": batch_id,
      "keys": keys,
      "n": max(1, round(len(ks) * mean_fanout)),
  }
  if not paired:
    return payload
  # Every element carries every edge id (each one went through the same
  # `_attach_matches` chain), so this is O(keys x edges) with O(1)
  # membership — and it still holds if a future chain skips one.
  edge_ids = dict.fromkeys(edge_id for _, matches in ks for edge_id in matches)
  payload["matches"] = {
      edge_id: [matches.get(edge_id, []) for _, matches in ks]
      for edge_id in edge_ids
  }
  return payload


def _partition_parent_edges(
    spec: TableSpec, valid_by_landing: dict[str, Any]
) -> tuple[
    tuple[FkEdgeSpec, Any] | None,
    list[tuple[int, FkEdgeSpec, Any]],
    list[tuple[int, FkEdgeSpec, Any]],
]:
  """``(fanout_edge, side_input_edges, conditional_edges)`` for one
    table's ``parent_edges`` — the four roles of ADR 0037 §2 mapped onto
    the three DAG paths: "fanout" DRIVES the child (at most one — its
    parent's keys become the generation request stream); "side_input" is
    an INDEPENDENT edge on the ADR 0030/0031 sampled-key-pool path;
    "conditional" is co-partitioned with the driving key
    (`_conditional_candidates`); "implied" is satisfied by construction
    through the driving edge and contributes nothing (not even a parent
    lookup)."""
  fanout_edge: tuple[FkEdgeSpec, Any] | None = None
  side_input_edges: list[tuple[int, FkEdgeSpec, Any]] = []
  conditional_edges: list[tuple[int, FkEdgeSpec, Any]] = []
  for j, edge in enumerate(spec.parent_edges):
    if edge.mode == "implied":
      continue
    parent = valid_by_landing.get(edge.parent_landing)
    if parent is None:
      raise ValueError(f"{spec.config.landing_table}: parent "
                       f"{edge.parent_landing!r} not built earlier in the "
                       f"spec list — specs must be parents-first")
    if edge.mode == "fanout":
      if fanout_edge is not None:
        raise ValueError(f"{spec.config.landing_table}: at most one "
                         f"fanout-mode parent edge is supported, got a "
                         f"second one at index {j}")
      fanout_edge = (edge, parent)
    elif edge.mode == "side_input":
      side_input_edges.append((j, edge, parent))
    elif edge.mode == "conditional":
      conditional_edges.append((j, edge, parent))
    else:
      raise ValueError(f"{spec.config.landing_table}: unknown FkEdgeSpec.mode "
                       f"{edge.mode!r} on edge {j}")
  if conditional_edges and fanout_edge is None:
    # A conditional edge draws its candidates by joining on the
    # columns it SHARES with the driving key; with no driving edge
    # there is no join key, and the edge would silently fall back to
    # the child's own marginals (the ADR 0036 D4 corruption).
    raise ValueError(f"{spec.config.landing_table}: conditional parent edges "
                     f"{[list(e.child_cols) for _, e, _ in conditional_edges]} "
                     f"need a fanout-mode (driving) edge on the same table to "
                     f"join against, and this table has none (ADR 0037)")
  if fanout_edge is not None:
    _check_edges_against_driving(spec, fanout_edge[0], side_input_edges,
                                 conditional_edges)
  return fanout_edge, side_input_edges, conditional_edges


def _check_edges_against_driving(
    spec: TableSpec,
    driving: FkEdgeSpec,
    side_input_edges: list[tuple[int, FkEdgeSpec, Any]],
    conditional_edges: list[tuple[int, FkEdgeSpec, Any]],
) -> None:
  """What each non-driving edge must look like next to the driving one,
    AND what every pair of in-job edges must look like next to each other
    (ADR 0037 §4/§5) — every violation here would otherwise surface as a
    stalled job, a measured `fk.orphan` rate, or `tuple.index(x): x not
    in tuple` from deep inside the graph build."""
  driving_cols = set(driving.child_cols)
  for _, edge, _ in side_input_edges:
    # The star is sound only because the independent path is DISJOINT
    # from the driving key: the engine draws a whole pool tuple and
    # the driving key then OVERWRITES the shared column, landing a
    # combination the parent never held (the ADR 0036 D1 corruption).
    # The gate can only measure that; this names it.
    shared = [c for c in edge.child_cols if c in driving_cols]
    if shared:
      raise ValueError(
          f"{spec.config.landing_table}: side_input edge "
          f"{list(edge.child_cols)} shares {shared} with the "
          f"driving edge {list(driving.child_cols)} — an edge that "
          f"overlaps the driving key must be conditional "
          f"(mode='conditional', overlap={shared}), ADR 0037 §5")
  for _, edge, _ in conditional_edges:
    if not edge.overlap:
      # Both sides would key on `()`: the Top-M combine and the
      # CoGroupByKey collapse onto ONE key — no parallelism for the
      # whole driving stream — and an edge with nothing in common
      # with the driving key is independent by definition.
      raise ValueError(
          f"{spec.config.landing_table}: conditional edge "
          f"{list(edge.child_cols)} declares an EMPTY overlap — an "
          f"edge with no column shared with the driving edge "
          f"{list(driving.child_cols)} is independent; use "
          f"mode='side_input' (ADR 0037 §2)")
    # The join key is read off the DRIVING key tuple by position, and
    # the candidates off THIS edge's own columns, so an overlap column
    # missing from either side fails as `x not in tuple` deep in the
    # graph build.
    unshared = [c for c in edge.overlap if c not in driving_cols]
    if unshared:
      raise ValueError(f"{spec.config.landing_table}: conditional edge "
                       f"{list(edge.child_cols)} declares overlap columns "
                       f"{unshared} that the driving edge "
                       f"{list(driving.child_cols)} does not carry — the "
                       f"overlap is the edge's columns SHARED with the driving "
                       f"edge (ADR 0037 §4)")
    unowned = [c for c in edge.overlap if c not in edge.child_cols]
    if unowned:
      raise ValueError(f"{spec.config.landing_table}: conditional edge "
                       f"{list(edge.child_cols)} declares overlap columns "
                       f"{unowned} that are not its own — the overlap is a "
                       f"SUBSET of the edge's child columns (ADR 0037 §4)")
  # Every PAIR of in-job edges, not only each edge against the DRIVING
  # one: the guard was one-sided, so a side_input edge disjoint from
  # the driving key but sharing a column with a CONDITIONAL edge's
  # `rest` built without complaint — and `apply_conditional_overrides`,
  # which runs AFTER the pool draws, then clobbered the pool-drawn
  # tuple. `overlap` is DECLARED rather than derived, so the driving
  # edge belongs in the pairing too: a column it carries that the
  # conditional edge omits from `overlap` falls into that edge's `rest`
  # and overwrites the driving key itself. Defence in depth behind
  # `RelationshipRegistry.edge_roles`, which stops the same shapes for
  # model-declared launches; this catches hand-built specs.
  in_job = [driving, *(e for _, e, _ in side_input_edges + conditional_edges)]
  for index, first in enumerate(in_job):
    for second in in_job[index + 1:]:
      written = _written_child_cols(second)
      shared = [c for c in _written_child_cols(first) if c in written]
      if shared:
        raise ValueError(
            f"{spec.config.landing_table}: edges "
            f"{list(first.child_cols)} (mode={first.mode!r}) and "
            f"{list(second.child_cols)} (mode={second.mode!r}) both "
            f"WRITE {shared} — one child column cannot be owned by "
            f"two edges: the second draw overwrites the first and "
            f"lands a tuple its parent never held (ADR 0037 §2)")


def _written_child_cols(edge: FkEdgeSpec) -> tuple[str, ...]:
  """The child columns this edge actually WRITES into a generated row.

    A `conditional` edge writes only its `rest` — the shared columns come
    from the driving key, never from the candidate. Every other in-job
    mode writes its whole tuple: the driving key itself, or a whole pool
    draw. `implied` edges never reach here — `_partition_parent_edges`
    drops them precisely because they write nothing at all.
    """
  if edge.mode == "conditional":
    return tuple(c for c in edge.child_cols if c not in edge.overlap)
  return edge.child_cols


def _side_input_pools(side_input_edges: list[tuple[int, FkEdgeSpec, Any]],
                      prefix: str):
  """The ADR 0030/0031 merged parent-key side input for a table's
    ``side_input``-mode edges (unchanged path; ``fk_side=None`` when
    there are none)."""
  if not side_input_edges:
    return None
  edge_pools = [
      _edge_key_pools(parent, edge, f"{prefix}edge{j}/")
      for j, edge, parent in side_input_edges
  ]
  if len(edge_pools) == 1:
    merged = edge_pools[0]
  else:
    merged = (
        tuple(edge_pools)
        | f"{prefix}FkEdgeFlatten" >> beam.Flatten()
        | f"{prefix}FkMerge" >> beam.CombineGlobally(
            lambda payloads: [edge for p in payloads for edge in p]))
  return beam.pvalue.AsSingleton(merged)


def _route_parent_edges(spec: TableSpec, valid_by_landing: dict[str, Any],
                        prefix: str) -> tuple[Any, Any]:
  """``(fk_side, requests)`` for one table (ADR 0036/0037). The three
    paths are independent, and a STAR-schema child takes two of them at
    once: a fanout edge supplies the request stream (with its conditional
    edges' candidates already joined on), while independent edges keep
    the ADR 0030/0031 sampled-key-pool side input — which is also what
    wires `_fk_integrity_stage`'s gate, correctly, over exactly the edges
    that are not integral by construction.

    ADR 0036 D1 forbade the combination because a side-input edge sharing
    columns with the driving edge would be overwritten; that shape is
    `conditional` now, so what is left on the side-input path is disjoint
    from the driving key by construction (design §5)."""
  fanout_edge, side_input_edges, conditional_edges = _partition_parent_edges(
      spec, valid_by_landing)
  fanout_payload = spec.config.fanout
  if fanout_edge is not None and fanout_payload is None:
    raise ValueError(f"{spec.config.landing_table}: a fanout-mode parent "
                     f"edge needs PipelineConfig.fanout set (ADR 0036)")
  fk_side = _side_input_pools(side_input_edges, prefix)
  if fanout_edge is None or fanout_payload is None:
    return fk_side, None
  edge, parent = fanout_edge
  mean_fanout = FanoutPlan.from_payload(fanout_payload).histogram.mean
  requests = _fanout_requests(
      parent,
      edge,
      prefix,
      mean_fanout,
      conditional=tuple(conditional_edges),
      run_id=spec.config.run_id,
  )
  return fk_side, requests


def build_relational_pipeline(
    p: beam.Pipeline, specs: list[TableSpec]) -> dict[str, dict[str, Any]]:
  """N tables, ONE pipeline (ADR 0030): each table's full subgraph
    (pools, RAG, generation, validation, DLQ, gate) label-namespaced by
    its landing table name; a child's FK columns take the parent's
    landed keys as an in-DAG side input, which is also the runner-level
    ordering barrier — children never generate before parents. One
    worker fleet, one vLLM ignition, serves every table.

    ``specs`` must arrive parents-first (`plan_launch` order). Returns
    {landing_table: build_pipeline result}."""
  valid_by_landing: dict[str, Any] = {}
  results: dict[str, dict[str, Any]] = {}
  for spec in specs:
    name = spec.config.landing_table.rsplit(".", 1)[-1]
    prefix = f"{name}/"
    side, requests = _route_parent_edges(spec, valid_by_landing, prefix)
    results[spec.config.landing_table] = build_pipeline(
        p,
        reference_rows=spec.reference_rows,
        config=spec.config,
        landing_sink=spec.landing_sink,
        dlq_sink=spec.dlq_sink,
        validation_runs_sink=spec.validation_runs_sink,
        rag_chunks_sink=spec.rag_chunks_sink,
        freetext_pools_store=spec.freetext_pools_store,
        source_value_store=spec.source_value_store,
        label_prefix=prefix,
        fk_side=side,
        requests=requests,
    )
    valid_by_landing[spec.config.landing_table] = results[
        spec.config.landing_table]["valid"]
  return results


def _population_branch(
    p: beam.Pipeline,
    config: PipelineConfig,
    reference_rows: list[dict],
    digest: str,
    *,
    label_prefix: str,
):
  """The rag_chunks population branch (WS2 §4b.1): reference rows and
    distinct free-text values → chunks → a BOUNDED embed fan-out
    (ADR 0034 D8). Returns the embedded-chunks PCollection; the caller
    writes it and lets the pool trigger wait on it."""
  free_text_columns = _rag_free_text_columns(config.table_schema,
                                             reference_rows)
  # Population is scoped to what its consumers can read (ADR 0019):
  # row_doc chunks cover EXACTLY the engine's read prefix
  # (`_vectors_from_store` is all-or-nothing over rows[:1024]) — the
  # 2026-07-25 06:18 run embedded all 10k rows and 90 % could never
  # be read back. free_text_col chunks dedupe to distinct
  # (column, value), computed driver-side (reference_rows is already
  # in memory here); Beam still fans the embed itself out below.
  distinct_values = distinct_free_text_values(reference_rows, free_text_columns)
  row_doc_chunks = (
      p
      | f"{label_prefix}RagReferenceRows" >> beam.Create(
          reference_rows[:MAX_ROW_DOC_ROWS])
      | f"{label_prefix}RagChunkRows" >> beam.ParDo(
          ChunkReferenceRowsDoFn(
              source_fqn=config.table_schema.fqn,
              reference_digest=digest,
              column_order=[c.name for c in config.table_schema.columns],
              free_text_columns=[],  # value chunks come deduped below
              pk_columns=list(config.pk_columns),
              embedder_id=config.embedder_id,
              embedder_version=config.embedder_version,
          )))
  value_chunks = (
      p
      | f"{label_prefix}RagDistinctValues" >> beam.Create(
          [(c, v) for c, vals in distinct_values.items() for v in vals])
      | f"{label_prefix}RagValueChunks" >> beam.MapTuple(
          functools.partial(
              chunk_free_text_value,
              source_fqn=config.table_schema.fqn,
              reference_digest=digest,
              embedder_id=config.embedder_id,
              embedder_version=config.embedder_version,
          )))
  shards = max(1, int(config.rag_embed_shards))
  chunks = (
      (row_doc_chunks, value_chunks)
      | f"{label_prefix}RagAllChunks" >> beam.Flatten()
      # BOUNDED fan-out (ADR 0034 D8): `rag_embed_shards` keyed
      # groups ⇒ at most that many concurrent embedders, whatever
      # the SDK topology. The unbounded Reshuffle put one embedder
      # in every SDK process — eight CUDA contexts per worker under
      # `multi` — beside the vLLM spawn.
      | f"{label_prefix}RagShard" >> beam.Map(lambda c, k=shards:
                                              (_rag_shard_key(c, k), c))
      | f"{label_prefix}RagShardGroup" >> beam.GroupByKey()
      | f"{label_prefix}RagShardValues" >> beam.FlatMap(lambda kv: kv[1])
      | f"{label_prefix}RagBatchChunks" >> beam.BatchElements(
          min_batch_size=32, max_batch_size=256)
      | f"{label_prefix}RagEmbedChunks" >> beam.ParDo(
          EmbedChunksDoFn(config.embedder_uri, device=config.rag_embed_device)))
  return chunks


def _rag_shard_key(chunk, shards: int) -> int:
  """Deterministic shard for one chunk — ``shards`` keyed groups bound
    the population's embedder concurrency (ADR 0034 D8)."""
  digest = hashlib.blake2b(
      str(chunk.chunk_id).encode("utf-8"), digest_size=4).digest()
  return int.from_bytes(digest, "big") % max(1, int(shards))


def _rag_free_text_columns(table_schema: TableSchema,
                           reference_rows: list[dict]) -> list[str]:
  """Columns that get `free_text_col` chunks — B.1's own FREE_TEXT
    classification, minus identifier-shaped ones (retrieval-worthy prose,
    not per-row IDs)."""
  from sdfb_core.engines.b1_rag.profile import ColumnKind, profile_columns

  profiles = profile_columns(table_schema, reference_rows)
  return [
      p.name
      for p in profiles.values()
      if p.kind is ColumnKind.FREE_TEXT and p.identifier_shape is None
  ]


def _gate_inputs(uniq: dict,
                 dlq_raw,
                 uniqueness_mode: str,
                 label_prefix: str = ""):
  """`(valid_count, dlq_by_rule)` singletons for the BLOCKER gate.

    `build_run_summary` computes ``total = valid_count + dlq_count``. In
    STREAMING mode duplicates land instead of diverting, so counting landed
    rows would push `total` above the rows actually generated and quietly
    dilute the blocker ratio — a silently weaker gate. `EnforceUniqueness`
    publishes `distinct_count` for exactly this reason: distinct + excess is
    the number of rows generated, so the arithmetic is identical in both
    modes.

    The same reasoning reaches the EXCLUDED rules (ADR 0038 fix H1): a
    rule dropped from the gate's numerator leaves its denominator too
    (`validation.summary.gate_total`), or every other blocker rule on
    that table would be divided by the duplicates the table is supposed
    to land.
    """
  if uniqueness_mode == "streaming":
    valid_count = uniq["distinct_count"]
  else:
    valid_count = (
        uniq["unique"]
        | f"{label_prefix}CountValid" >> beam.combiners.Count.Globally())
  dlq_by_rule = ((
      dlq_raw
      | f"{label_prefix}DlqRulePairs" >> beam.Map(_dlq_rule_weight),
      # Streaming reports duplicates as measured counts rather than
      # diverted envelopes; the gate folds them identically.
      uniq["rule_counts"],
  )
                 | f"{label_prefix}AllRulePairs" >> beam.Flatten()
                 | f"{label_prefix}DlqRuleCounts" >> beam.CombinePerKey(sum)
                 | f"{label_prefix}DlqRuleDict" >> beam.combiners.ToDict())
  return valid_count, dlq_by_rule


def _dlq_rule_weight(envelope: dict) -> tuple[str, int]:
  """Map a DLQ envelope to a ``(rule_id, weight)`` pair for the BLOCKER
    gate's per-rule counts.

    Every rule counts 1 envelope = 1 lost row, EXCEPT the two whose
    envelope stands for a whole KEY BATCH or a whole KEY, both carrying
    their lost-row count in ``raw_request["n"]``:

    - ``engine_failure`` — one crashed batch request (`raw_request` is the
      batch dict ``{"batch_id": ..., "n": ...}``; `GenerateRecordsDoFn`'s
      ``failed`` tagged output).
    - ``fk.unmatched`` (ADR 0037 §4 ruling B) — one DRIVING KEY dropped
      for having no conditional candidate, weighted by the rows that key
      was expected to produce (`n / len(keys)`).

    Without the weight a half-failed run scores a misleadingly low
    observed_blocker_ratio and wrongly PASSES. Module-level (not a
    lambda) to stay picklable for the Dataflow worker harness.
    """
  rule_id = envelope.get("rule_id", "unknown")
  if rule_id in ("engine_failure", "fk.unmatched"):
    try:
      return (rule_id, max(1, int(envelope.get("raw_request", {}).get("n", 1))))
    except (TypeError, ValueError):
      return (rule_id, 1)
  return (rule_id, 1)


def _build_validation_run_row(
    unused_seed,
    *,
    valid_count: int,
    dlq_by_rule: dict[str, int],
    thresholds: Thresholds,
    run_id: str,
    reference_digest: str,
    num_rows: int,
    reference_table: str,
    landing_table: str,
    engine: str,
    model_uri: str,
    excluded_blocker_rules: tuple[str, ...] = (),
    source_repeat_share: float | None = None,
) -> dict:
  """Driver of the single validation_runs row (side inputs are singletons).

    ADR 0038: an ADJUSTED table arrives with ``excluded_blocker_rules``
    (its `pk.duplicate`, which is now expected) and the SOURCE repeat
    share it must reproduce. The comparison is logged as well as
    written — the summary row is the audit trail, the milestone is what
    an operator greps while the job is still running.
    """
  summary = build_run_summary(
      run_id=run_id,
      reference_digest=reference_digest,
      valid_count=valid_count,
      dlq_by_rule=dlq_by_rule,
      thresholds=thresholds,
      num_rows_requested=num_rows,
      reference_table=reference_table,
      landing_table=landing_table,
      engine=engine,
      model_uri=model_uri,
      excluded_blocker_rules=excluded_blocker_rules,
      source_repeat_share=source_repeat_share,
  )
  if summary.source_repeat_share is not None:
    log_milestone(
        "model_adjustment_repeat_share",
        # A WARNING is for a copy that MISSED its source, or for a
        # pair that produced no landing reading at all. Fix J makes
        # the two shares describe the same columns in every case, so
        # a delta outside tolerance is always evidence.
        level=(logging.INFO if summary.repeat_share_within_tolerance is True
               else logging.WARNING),
        table=landing_table,
        source=round(summary.source_repeat_share, 4),
        landing=(round(summary.landing_repeat_share, 4)
                 if summary.landing_repeat_share is not None else ""),
        delta=(round(summary.repeat_share_delta, 4)
               if summary.repeat_share_delta is not None else ""),
        tolerance=REPEAT_SHARE_TOLERANCE,
        within_tolerance=summary.repeat_share_within_tolerance,
        excluded_blocker_rules=summary.excluded_blocker_rules,
    )
  return summary.to_bq_row()


class _BlockerGateDoFn(beam.DoFn):
  """Fails the Dataflow job when the run summary tripped the BLOCKER gate.

    ``wait_on_write`` is an optional ``AsIter`` side input over the
    upstream sink's FILE_LOADS ``destination_load_jobid_pairs`` (see
    `build_pipeline`). It is never read in the body — its only purpose is
    the Dataflow-graph ordering edge it creates, forcing this DoFn's stage
    to run after the load jobs commit. Without it, a tripped gate tears
    the job down concurrently with (and can race ahead of) the
    `validation_runs` FILE_LOADS write, so a FAILED run's own summary row
    never lands — exactly what happened on the 2026-07-20 b2 E2E run,
    which left zero trace in `synthetic_data_quality.validation_runs`.
    """

  # pylint: disable-next=arguments-renamed  # Beam passes the element positionally
  def process(self, row: dict, wait_on_write=None):  # pylint: disable=unused-argument
    if row.get("status") == STATUS_FAILED_BLOCKER:
      raise BlockerThresholdExceeded(
          f"run_id={row.get('run_id')} blocker_count={row.get('blocker_count')} "
          f"observed={row.get('observed_blocker_ratio')} > "
          f"gate={row.get('blocker_failure_ratio')} (env={row.get('env')})")
    yield row
