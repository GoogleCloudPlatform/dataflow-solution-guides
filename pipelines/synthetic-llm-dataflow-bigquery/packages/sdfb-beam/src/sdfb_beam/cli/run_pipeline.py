"""Flex Template entrypoint for the synthesis pipeline.

Set via `FLEX_TEMPLATE_PYTHON_PY_FILE` in `docker/Dockerfile`. The Python
launcher invokes this with argparse args populated from the Flex Template
parameters declared in `docker/flex_template_metadata.json`.

Runtime modes:
  - Production (Dataflow + L4 + Gemma 4 via vLLM):
      --runner=DataflowRunner --client_type=vllm --model_uri=gs://…
  - Local smoke on M4 (MLX backend, see docs/M4_LOCAL_SMOKE.md):
      --runner=DirectRunner --client_type=mlx --model_uri=./models/…
  - Deterministic CI integration test (no real LLM):
      --runner=DirectRunner --client_type=fake

REFs:
  - .claude/skills/beam-dofn.md
  - docs/DEPLOYMENT_PREREQUISITES.md
  - docs/MODEL_LAYOUT.md
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import apache_beam as beam
# Side-effect import: populates ENGINE_REGISTRY at import time.
import sdfb_core.engines  # noqa: F401  # pylint: disable=unused-import
import yaml
from apache_beam.io.filesystems import FileSystems
from apache_beam.io.gcp.bigquery import BigQueryDisposition, WriteToBigQuery
from apache_beam.options.pipeline_options import (
    DebugOptions,
    GoogleCloudOptions,
    PipelineOptions,
    SetupOptions,
    StandardOptions,
    WorkerOptions,
)
from sdfb_core.codegen import derive_bq_load_schema
from sdfb_core.contracts import TableSchema
from sdfb_core.contracts.model_adjustment import (
    ModelAdjustment,
    adjusted_model_yaml,
    adjusted_models,
    adjustment_banner,
    landed_distinct_keys,
    pk_measurement_from_histogram,
)
from sdfb_core.contracts.prompt_constraint import parse_llm_prompt_constraint
from sdfb_core.contracts.relationships import (
    RelationshipError,
    RelationshipRegistry,
)
from sdfb_core.contracts.row_projection import (
    TableProjection,
    project_table,
    projected_total,
    projection_banner,
    projection_warnings,
)
from sdfb_core.engines.b1_rag.profile import profile_columns
from sdfb_core.engines.fanout import conditional_edge_id
from sdfb_core.engines.pk_capacity import FK_KEY_SAMPLE_FLOOR
from sdfb_core.observability import (
    log_build_info,
    log_milestone,
    log_milestone_pretty,
    log_milestone_text,
)
from sdfb_core.rag.embedding import embedder_identity
from sdfb_core.stats import PROFILER_VERSION, profile_source_table, stats_rows
from sdfb_core.validation import Thresholds

from sdfb_beam.cli.preflight import (
    DEFAULT_FK_CANDIDATE_CAP,
    ON_CONFLICT_ADJUST,
    ON_MODEL_CONFLICT_MODES,
    edge_supplied_members,
    pk_cell_columns,
    preflight,
)
from sdfb_beam.ddl import extract_table_schema
from sdfb_beam.dofns.uniqueness import MODE_STREAMING, UNIQUENESS_MODES
from sdfb_beam.io.bq_sources import load_reference_rows
from sdfb_beam.io.digest import compute_reference_digest
from sdfb_beam.io.fanout_stats import (
    BigQueryFanoutStatsStore,
    fanout_payload,
    log_fanout_measured,
    log_pk_measured,
    measure_fanout,
    measure_pk_uniqueness,
)
from sdfb_beam.io.fk_pools import (
    load_fk_key_pools,
    parent_landing_fqn,
    per_column_view,
)
from sdfb_beam.io.relationships import (
    DEFAULT_RELATIONSHIPS_URI,
    load_relationship_registry,
)
from sdfb_beam.io.source_values import (
    BigQuerySourceValueStore,
    pool_source_overlap,
)
from sdfb_beam.io.stats_store import BigQuerySourceStatsStore
from sdfb_beam.pipeline import (
    FkEdgeSpec,
    PipelineConfig,
    TableSpec,
    build_pipeline,
    build_relational_pipeline,
)
from sdfb_beam.pools.store import BigQueryFreeTextPoolStore
from sdfb_beam.rag.store import BigQueryChunkStore

if TYPE_CHECKING:
  from sdfb_core.contracts.relationships import FkEdge
  from sdfb_core.engines import ModelClient

logger = logging.getLogger(__name__)

# Worker boot disk. The GPU image (torch + vLLM + CUDA runtime libs) is multi-GB;
# the Dataflow default of 25GB overflows while the kubelet unpacks it. Pinned in
# code — like ``sdk_container_image`` below — because the Flex Template
# ``environment.diskSizeGb`` does NOT propagate to the worker harness (observed:
# workers booted at the 25GB default despite the DAG requesting 200).
_DEFAULT_WORKER_DISK_GB = 200
# ADR 0034: the single-barrier uniqueness stage reads its PK/identity
# collision groups as side inputs; Beam's default state cache re-fetched
# them per bundle ("Retrieving state 62 times costed 60 seconds",
# 2026-09-07 R7). 512 MB on an n1-highmem-8 is a rounding error.
_DEFAULT_STATE_CACHE_MB = 512

# batch_size is rows-per-element. At the historic fixed default of 16, a 1M-row
# run produced 62,500 elements and paid per-element Python overhead 62,500
# times over instead of amortising it across vectorized draws (2026-07-26
# E2E). Scale toward ~1,000 elements, but never below the historic default so
# small runs — and their goldens — are untouched.
DEFAULT_BATCH_SIZE = 16
_TARGET_ELEMENTS = 1_000

# ADR 0036: keys per fan-out request element. A near-zero measured mean
# fan-out would otherwise divide `batch_size` by ~1e-6 and put millions of
# key tuples in ONE element — a single unsplittable bundle and a huge DLQ
# envelope if it crashes.
_MAX_KEYS_PER_BATCH = 10_000

# ADR 0037 — the role a `RelationshipRegistry.edge_roles` edge plays in the
# DAG (design §2): the driving edge's parent keys ARE the generation input,
# an implied edge rides on it, an independent edge keeps the ADR 0030/0031
# side-input pool, and a conditional edge is co-partitioned on the columns
# it shares with the driving edge. `external` is absent on purpose — its
# parent is outside the launch, so it never reaches `in_set_parent_edges`.
_EDGE_MODES = {
    "driving": "fanout",
    "implied": "implied",
    "independent": "side_input",
    "conditional": "conditional",
}
# ADR 0037 §7 — a fan-out request carries `keys_per_batch * M` candidate
# tuples per conditional edge; `keys_per_batch` is lowered so the whole
# request stays under this many values, whatever the cap is set to.
_MAX_CONDITIONAL_VALUES_PER_REQUEST = 100_000

# WS5 §3 — the seeding experiment's only variable. Three arms off ONE build
# so the E2E runs differ in exactly one thing.
POOL_SEED_STRATEGIES = ("centroid", "kcenter", "kcenter_rotate")


def validate_seed_strategy(value: str) -> str:
  """Reject a typo at launch: silently degrading to the control arm would
    corrupt the comparison the flag exists for."""
  if value not in POOL_SEED_STRATEGIES:
    raise ValueError(
        f"--pool_seed_strategy must be one of {POOL_SEED_STRATEGIES}, "
        f"got {value!r}")
  return value


def validate_candidate_cap(value) -> int:
  """``--fk_candidate_cap`` is a Top-M bound: it must be at least 1.

    Rejected at parse time, not repaired: 0 divides by zero inside the
    `keys_per_batch` bound (`in_set_parent_edges`) and aborts the
    launcher with a raw traceback instead of the collect-then-fail
    preflight summary, and a negative cap passes silently into the
    Top-M combine, where it yields ZERO candidates for every shared key
    — the whole conditional edge degrades to "unmatched" with nothing in
    the log naming the flag (review round 1, finding B2)."""
  try:
    cap = int(value)
  except (TypeError, ValueError):
    cap = 0
  if cap < 1:
    raise SystemExit(
        f"[launch] --fk_candidate_cap must be an integer >= 1, got "
        f"{value!r}. It is the number of parent candidates kept per "
        f"SHARED value on a conditional edge (ADR 0037): 0 divides the "
        f"keys_per_batch bound by zero, and a negative cap empties "
        f"every candidate list silently. Use {DEFAULT_FK_CANDIDATE_CAP} "
        f"(the default) or higher.")
  return cap


def candidate_cap_of(args) -> int:
  """``--fk_candidate_cap`` off a launcher namespace, validated.

    Every read site goes through this, so a namespace built by hand (a
    test, a programmatic launch) cannot smuggle a 0 past `parse_args`
    into the divisor or the Top-M combine."""
  return validate_candidate_cap(
      getattr(args, "fk_candidate_cap", DEFAULT_FK_CANDIDATE_CAP))


def resolve_batch_size(requested: int, num_rows: int) -> int:
  """Rows per element. An explicit non-default ``--batch_size`` always wins."""
  if requested != DEFAULT_BATCH_SIZE:
    return requested
  if num_rows <= 0:
    return DEFAULT_BATCH_SIZE
  return max(DEFAULT_BATCH_SIZE, num_rows // _TARGET_ELEMENTS)


def parse_args(  # noqa: PLR0915 — one flat list of flags; splitting it hides the CLI surface
    argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
  p = argparse.ArgumentParser(
      description="Synthetic Dataflow BigQuery — pipeline launcher")
  p.add_argument(
      "--ddl_uri",
      default="",
      help="gs:// or local path to _ddl.json — the OFFLINE "
      "FALLBACK only (ADR 0027 D2). Live extraction is "
      "authoritative at every launch: structure from "
      "--reference_table, description surfaces "
      "(llm_prompt_constraint + relational contract) "
      "overlaid from --landing_table — the SOURCE "
      "table's descriptions are stripped, never used. "
      "The pin (extract it from the LANDING table) is "
      "consumed only when live extraction fails "
      "(air-gap, BQ outage); staleness is reported "
      "(ddl_pin_drift/ddl_pin_fresh).")
  p.add_argument(
      "--reference_table",
      required=True,
      help="FQN of source table for live SELECT reference rows")
  p.add_argument("--reference_rows_limit", type=int, default=10_000)
  p.add_argument(
      "--landing_table",
      required=True,
      help="BQ table for synthetic rows (project.dataset.table). "
      "Accepts a comma-separated list for multi-table "
      "launches (ADR 0029 scenarios): each table runs "
      "sequentially with a suffixed run_id.")
  p.add_argument(
      "--dlq_table", required=True, help="BQ DLQ table (project.dataset.table)")
  p.add_argument(
      "--write_disposition",
      default="append",
      choices=["append", "overwrite"],
      help="Landing-table write mode. append = WRITE_APPEND "
      "(default, today's behavior); overwrite = "
      "WRITE_TRUNCATE (FILE_LOADS-compatible). DLQ, "
      "validation_runs and rag_chunks always append.")
  p.add_argument(
      "--create_if_not_exists",
      default="false",
      help="true/1/yes: create the landing table on first "
      "write (CREATE_IF_NEEDED) carrying the landing "
      "schema derived in-pipeline from the DDL. Anything "
      "else: CREATE_NEVER (default). Quality/RAG tables "
      "are never auto-created. NOTE: the auto-created "
      "table only gets name/type/mode/description per "
      "column — parameterized constraints (STRING "
      "max_length, NUMERIC precision/scale, column "
      "default expressions) are NOT carried over, "
      "because the FILE_LOADS load-job API rejects them "
      "at runtime. Pre-provision the table out-of-band "
      "(e.g. `bq mk`/DDL) if those constraints matter.")
  p.add_argument("--num_rows", type=int, required=True)
  p.add_argument(
      "--autoscaling",
      default="auto",
      choices=list(_AUTOSCALING_MODES),
      help="auto (default) = a fleet sized by --initial_workers "
      "stays that size (autoscaling_algorithm=NONE), "
      "otherwise Dataflow's THROUGHPUT_BASED. fixed = "
      "same pin, and --initial_workers is required. "
      "throughput = always let Dataflow scale (the "
      "2026-09-07/08 multi runs lost ~4 min per job to "
      "mid-job scale-downs between the parent and child "
      "stages, ADR 0034 D9).")
  p.add_argument(
      "--initial_workers",
      default="",
      help="Initial Dataflow worker count (ADR 0034). Empty = "
      "Dataflow's own default: the 2026-08-29 R6 pair "
      "started on 2 workers and autoscaled to 4 only ~4 min "
      "into the first generate stage, so C_TABLE ran 8 min "
      "at a quarter of its steady-state rate. A scale run "
      "starts at its max_num_workers. Pinned from the "
      "launcher like disk_size_gb; an explicit Beam "
      "--num_workers on the launch wins.")
  p.add_argument(
      "--batch_size",
      type=int,
      default=DEFAULT_BATCH_SIZE,
      help="Rows per element. Left at the default, this scales "
      "with --num_rows toward ~1,000 elements (never below "
      "the default). Pass an explicit value to pin it.")
  p.add_argument("--similarity", type=float, default=0.5)
  p.add_argument(
      "--seed",
      default="",
      help="Explicit base RNG seed (int). Empty = derive per "
      "(run_id, batch_id) — never replays across runs "
      "because run_id is salted per trigger.")
  p.add_argument("--run_id", required=True)
  p.add_argument(
      "--identity_cols",
      default="",
      help="Comma-separated per-row-unique columns synthesized "
      "fresh each row (PK/UUID); never sampled from "
      "reference data")
  p.add_argument(
      "--pk_cols",
      default="",
      help="Comma-separated declared primary-key columns; "
      "duplicate PK tuples divert to the DLQ "
      "(rule_id=pk.duplicate, BLOCKER). Empty disables.")
  p.add_argument(
      "--engine",
      default="b1_rag",
      help="Engine name registered in ENGINE_REGISTRY")
  p.add_argument(
      "--model_uri",
      required=True,
      help="gs://<bucket>/synthetic/models/<family>/<model>/<version>/")
  p.add_argument(
      "--embedder_uri",
      default="",
      help="gs://<bucket>/synthetic/models/embedders/<model>/<version>/ "
      "for the B.1 RAG embedder (optional; empty → HashingEmbedder)")
  p.add_argument(
      "--build_rag_layer",
      nargs="?",
      const="true",
      default="",
      help="true/false (bare flag = true). Populate "
      "synthetic_rag.rag_chunks from this run's "
      "reference sample (skipped if the reference_digest "
      "is already present for this embedder id+version). "
      "String-valued so the Flex Template/DAG chain can "
      "pass --build_rag_layer=true.")
  p.add_argument(
      "--rag_chunks_table",
      default="",
      help="FQN of synthetic_rag.rag_chunks. Enables the "
      "read-instead-of-reembed path; with "
      "--build_rag_layer also enables population.")
  p.add_argument(
      "--build_pool_layer",
      nargs="?",
      const="true",
      default="",
      help="true/false (bare flag = true). Build free-text "
      "pools in their own branch and persist them to "
      "--freetext_pools_table (skipped if this "
      "reference_digest + model_uri is already present). "
      "Without it pools are inferred inside every worker "
      "process's setup().")
  p.add_argument(
      "--freetext_pools_table",
      default="",
      help="FQN of synthetic_rag.freetext_pools. Enables the "
      "read-instead-of-rebuild path; with "
      "--build_pool_layer also enables the build branch.")
  p.add_argument(
      "--uniqueness_mode",
      default="exact",
      choices=list(UNIQUENESS_MODES),
      help="exact = divert every duplicate to the DLQ behind "
      "ONE full-row shuffle barrier (default; PK/identity "
      "resolved from key-only groups, ADR 0034). "
      "exact_chained = the pre-ADR-0034 three-barrier "
      "chain (row digest -> PK -> identity), kept for "
      "A/B runs. streaming = land rows as they "
      "are generated and MEASURE the duplicate rate "
      "instead of removing it, so no GroupByKey barrier "
      "sits between generation and BigQuery. In streaming mode "
      "duplicate rows LAND — the run is still marked "
      "FAILED_BLOCKER, so re-run with "
      "--write_disposition=overwrite.")
  p.add_argument(
      "--fk_fanout_stats_table",
      default="",
      help="FQN of synthetic_data_quality.fk_fanout_stats (ADR 0036 cache of "
      "the SOURCE fan-out histogram + PK cells per driving edge). "
      "Empty = measure every launch, never cache.")
  p.add_argument(
      "--fk_candidate_cap",
      type=validate_candidate_cap,
      default=DEFAULT_FK_CANDIDATE_CAP,
      help="ADR 0037: Top-M candidates kept per SHARED value on a "
      "CONDITIONAL edge (an edge sharing columns with the "
      "driving edge — a diamond branch). A hot shared key "
      "never carries more than M parent candidates into a "
      "request; the engine wraps only when a key's fan-out "
      "outruns the list it was handed. Raising it widens the "
      "per-key choice and LOWERS keys_per_batch to keep a "
      "request under ~100k candidate values.")
  p.add_argument(
      "--driven_uniqueness_mode",
      default="streaming",
      choices=list(UNIQUENESS_MODES),
      help="Uniqueness mode for a DRIVEN child without identity columns "
      "(ADR 0036): its PK is unique by construction, so `streaming` "
      "measures duplicates without the landing-path barrier.")
  p.add_argument(
      "--on_model_conflict",
      default=ON_CONFLICT_ADJUST,
      choices=list(ON_MODEL_CONFLICT_MODES),
      help="What a MEASURED contradiction between the "
      "relationship model and the SOURCE does (ADR "
      "0038). adjust (default) = drop the declared `pk:` "
      "the full-source fan-out proves is not a key, "
      "announce it in the MODEL ADJUSTED banner, emit "
      "the effective model as YAML, and carry on — the "
      "landing table then reproduces the source's "
      "key-repeat share, and that table's pk.duplicate "
      "stops counting toward the BLOCKER gate. stop = "
      "refuse the launch, exactly as before ADR 0038. "
      "Model SELF-contradictions (unknown columns, two "
      "`drives: true` edges, an ambiguous role) and the "
      "ADR 0035 capacity gate stop under BOTH settings.")
  p.add_argument(
      "--pool_seed_strategy",
      default="centroid",
      choices=list(POOL_SEED_STRATEGIES),
      help="How the 8 free-text prompt seeds are chosen. "
      "centroid = control (densest region, today). "
      "kcenter = seeds span the column's modes. "
      "kcenter_rotate = re-seeded per ladder attempt "
      "(forfeits vLLM prefix caching by design).")
  p.add_argument(
      "--pool_pattern_guidance",
      nargs="?",
      const="true",
      default="",
      help="true/false (bare flag = true). Constrain "
      "identifier-ish free-text pool completions at "
      "decode time with a charset/length regex "
      "(vLLM structured-output items.pattern). Opt-in "
      "until T4 throughput is confirmed; the post-hoc "
      "format gate protects pools either way.")
  p.add_argument(
      "--freetext_expansion",
      default="identifiers",
      choices=["off", "identifiers", "all"],
      help="Shape-preserving expander for free-text columns "
      "(2026-08-05 spec C3). off = pool draws only "
      "(distinct capped at pool size). identifiers "
      "(default) = code-like columns expand from their "
      "observed shape mix. all = also mutate digit runs "
      "inside texty pool draws. Never adds an LLM call. "
      "Mode-by-mode panels, guarantees and trade-offs: "
      "docs/designs/2026-08-05-freetext-expansion-modes.md")
  p.add_argument(
      "--prompt_constraints",
      default="on",
      choices=["on", "off"],
      help="Attach per-column llm_prompt_constraint (parsed "
      "from column-description JSON) to pool prompts "
      "(spec C5). Prefix-cache-safe constant suffix.")
  p.add_argument(
      "--prompt_debug",
      default="off",
      choices=["off", "redacted", "full"],
      help="Log each built pool prompt as a "
      "freetext_pool_prompt milestone (ADR 0024). "
      "'redacted' elides seed exemplars; 'full' logs "
      "verbatim prompts at WARNING — reference values "
      "reach Dataflow logs, debug runs only.")
  p.add_argument(
      "--source_stats",
      default="sample",
      choices=["off", "sample", "exact"],
      help="Compute per-column source_table_stats from the "
      "reference sample (driver-side, zero DAG cost). "
      "exact adds ONE aggregate scan of the live table "
      "(HLL distinct, deciles, top-k; ADR 0022) and "
      "feeds exact distinct into free-text pool sizing. "
      "off disables entirely.")
  p.add_argument(
      "--source_stats_table",
      default="",
      help="BQ table for source_table_stats rows "
      "(project.dataset.table); empty skips the BQ write. "
      "Existing (table, digest) rows are never rewritten.")
  p.add_argument(
      "--source_stats_json",
      default="",
      help="gs:// or local path for the stats JSON artifact; "
      "empty skips it.")
  p.add_argument(
      "--generate_fk_relationships",
      default="true",
      help="true (default): declared relationships are "
      "honored — a launch expands to the table's whole "
      "FK component (parents first) and children sample "
      "landed parent keys; tables with no declared "
      "relationships behave exactly as false (zero "
      "friction). false: isolated generation — declared "
      "edges ignored LOUDLY, FK columns use marginals. "
      "ADR 0029.")
  p.add_argument(
      "--fk_parent_landing",
      default="",
      help="EXPERT OVERRIDE only (ADR 0029): parents are "
      "assumed landed in the --landing_table dataset and "
      "this derives automatically. Set it only when "
      "parents land in a DIFFERENT project.dataset.")
  p.add_argument(
      "--multi_table_mode",
      default="single_job",
      choices=["single_job", "sequential_jobs"],
      help="How a multi-table plan executes (ADR 0030). "
      "single_job (default): every planned table in ONE "
      "Dataflow job — one worker fleet, one vLLM "
      "ignition, in-DAG FK key handoff. sequential_jobs: "
      "one job per table, parents first (fallback / "
      "debugging).")
  p.add_argument(
      "--relationships_uri",
      default=DEFAULT_RELATIONSHIPS_URI,
      help="Where the relational models live (ADR 0032): a "
      "folder or a single YAML file, local or gs://. "
      "Default: the config/relationships folder packaged "
      "in the image. Point it at gs://... to change PK/FK "
      "without rebuilding — this is the ONLY source of "
      "relational truth; table descriptions are never "
      "read for it.")
  p.add_argument(
      "--validation_runs_table",
      default="",
      help="BQ table for the run-level summary row "
      "(project.dataset.table); empty skips the write")
  p.add_argument(
      "--env",
      default="dev",
      help="Environment tier selecting thresholds (dev|uat|prd)")
  p.add_argument(
      "--thresholds_uri",
      default="config/thresholds.yml",
      help="gs:// or local path to thresholds.yml")
  p.add_argument(
      "--client_type", default="vllm", choices=["vllm", "mlx", "fake"])
  p.add_argument(
      "--vllm_dtype",
      default="auto",
      choices=["auto", "float16", "bfloat16"],
      help="vLLM --dtype override. auto = checkpoint dtype "
      "(bf16 for Gemma/Qwen). float16 is REQUIRED on T4 "
      "for fp16-safe bf16 checkpoints (Qwen); refused for "
      "gemma-family models (fp16 Gemma emits empty output)")
  p.add_argument(
      "--vllm_max_model_len",
      default="8192",
      help="vLLM --max-model-len cap. Without it vLLM sizes the "
      "KV cache for the checkpoint's NATIVE context "
      "(Qwen3-2507: 262K → 36GiB KV, kills the T4 "
      "EngineCore at startup). 8192 fits every registry "
      "model/GPU pairing and dwarfs the synthesis prompts. "
      "Empty = no cap (native context).")
  args, beam_args = p.parse_known_args(argv)
  if parse_bool_flag(args.build_rag_layer) and not args.rag_chunks_table:
    p.error("--build_rag_layer requires --rag_chunks_table")
  if parse_bool_flag(args.build_pool_layer) and not args.freetext_pools_table:
    p.error("--build_pool_layer requires --freetext_pools_table")
  return args, beam_args


def resolve_thresholds(thresholds_uri: str, env: str) -> Thresholds:
  """Load thresholds.yml; fall back to a permissive gate if unavailable."""
  try:
    with FileSystems.open(thresholds_uri) as f:
      data = yaml.safe_load(f.read())
    return Thresholds.from_mapping(data, env)
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.warning(
        "Could not load thresholds from %s (%s); using permissive gate",
        thresholds_uri,
        e,
    )
    return Thresholds(env=env, blocker_failure_ratio=1.0)


def resolve_num_workers(value: str | int | None) -> int | None:
  """``--initial_workers`` → an int, or None for "leave Dataflow's default".
    Anything that is not a positive integer is a launch error, loudly."""
  text = str(value if value is not None else "").strip()
  if not text:
    return None
  if not text.isdigit() or int(text) <= 0:
    raise ValueError(
        f"initial_workers / num_workers must be a positive integer or "
        f"empty, got {value!r}")
  return int(text)


def resolve_rag_embed_device(model_client) -> str:  # pylint: disable=unused-argument
  """Where the RAG population embeds run: the worker GPU, on every
    topology. The 2026-09-08 cold run moved them to CPU under `multi` and
    starved the model pull (177 s) and the vLLM engine init (598 s)
    instead; the contention is bounded by `rag_embed_shards` and by the
    pool branch waiting for the population (ADR 0034 D8)."""
  return "auto"


_AUTOSCALING_MODES = ("auto", "throughput", "fixed")


def resolve_autoscaling(mode: str, num_workers: int | None) -> str | None:
  """``--autoscaling`` → the Beam ``autoscaling_algorithm`` to pin, or
    None to leave Dataflow's default (THROUGHPUT_BASED).

    ``auto`` (default) pins ``NONE`` exactly when ``initial_workers`` is
    given: a fleet you sized by hand should stay that size. The 2026-09-07
    and 09-08 multi runs dropped to 1-2 workers during the parent's
    load / FK-pool phase and re-provisioned VMs for the child stage —
    A_TABLE ran its first 3-8 minutes short-handed (ADR 0034 D9).
    ``fixed`` requires ``initial_workers``; ``throughput`` keeps scaling.
    """
  text = str(mode or "auto").strip().lower()
  if text not in _AUTOSCALING_MODES:
    raise ValueError(
        f"autoscaling must be one of {_AUTOSCALING_MODES}, got {mode!r}")
  if text == "throughput":
    return None
  if text == "fixed" and num_workers is None:
    raise ValueError("autoscaling=fixed needs initial_workers (the size of the "
                     "fixed fleet)")
  return "NONE" if num_workers is not None else None


def resolve_cross_process(runner: str, experiments: list[str] | None) -> bool:
  """True when this Dataflow launch runs the SDK harness as MULTIPLE
    processes per worker (Runner v2's default) — i.e. the vLLM spawn
    window must be exclusive across processes, not just threads
    (ADR 0034). `no_use_multiple_sdk_containers` (RUN_PLAYBOOK §3) pins
    one process per worker; DirectRunner is always one process."""
  if runner != "DataflowRunner":
    return False
  return "no_use_multiple_sdk_containers" not in {
      str(e).strip() for e in (experiments or [])
  }


def build_model_client(
    client_type: str,
    model_uri: str,
    vllm_dtype: str = "auto",
    vllm_max_model_len: str = "8192",
    cross_process: bool = False,
) -> ModelClient:
  """Lazy factory — avoids importing vLLM / MLX on machines that don't have them.

    ``cross_process`` (ADR 0034) tells the vLLM client that sibling SDK
    PROCESSES on the worker will race it for the one GPU — see
    `resolve_cross_process`."""
  if client_type == "fake":
    from sdfb_beam.handlers.fake_client import FakeModelClient
    # Empty pool — caller is expected to override for any real smoke test.
    return FakeModelClient(reference_pool=[{}])
  if client_type == "vllm":
    from sdfb_beam.handlers.vllm_client import VLLMModelClient
    # "auto" = vLLM picks the checkpoint dtype. float16 = explicit
    # downcast so fp16-safe bf16 checkpoints (Qwen) run on T4/SM 7.5;
    # the client's init guard still refuses fp16 for gemma-family.
    kwargs = {} if vllm_dtype == "auto" else {"dtype": vllm_dtype}
    # Cap the context so the KV cache fits the GPU; without this vLLM
    # allocates for the checkpoint's native max_position_embeddings
    # (Qwen3-2507: 262K → 36GiB KV vs ~5GiB free on a T4) and the
    # EngineCore exits 1 at startup. Empty = no cap.
    max_len = str(vllm_max_model_len).strip()
    if max_len:
      if not max_len.isdigit() or int(max_len) <= 0:
        raise ValueError(f"vllm_max_model_len must be a positive integer or "
                         f"empty, got {vllm_max_model_len!r}")
      kwargs["max-model-len"] = max_len
    return VLLMModelClient(
        model_uri=model_uri,
        vllm_server_kwargs=kwargs,
        cross_process=cross_process,
    )
  if client_type == "mlx":
    from sdfb_beam.handlers.mlx_client import MLXModelClient
    return MLXModelClient(model_uri=model_uri)
  raise ValueError(f"Unknown client_type: {client_type}")


def load_ddl(ddl_uri: str) -> TableSchema:
  """Load `_ddl.json` from gs:// or local; transparent via Beam FileSystems."""
  with FileSystems.open(ddl_uri) as f:
    return TableSchema.model_validate(json.loads(f.read()))


def resolve_table_schema(ddl_uri: str,
                         reference_table: str,
                         landing_table: str = "") -> TableSchema:
  """The generation schema — :func:`resolve_schemas` without the
    landing schema beside it. Every caller that only steers generation
    uses this; the ADR 0037 NULL policy needs the landing modes too."""
  return resolve_schemas(ddl_uri, reference_table, landing_table)[0]


def resolve_schemas(
    ddl_uri: str,
    reference_table: str,
    landing_table: str = "") -> tuple[TableSchema, TableSchema | None]:
  """``(generation schema, LANDING schema or None)``.

    Live INFORMATION_SCHEMA extraction is AUTHORITATIVE at every launch,
    and generation-steering metadata comes from the TARGET table only
    (ADR 0027 D2, 2026-08-21).

    Two rules from the 2026-08-21 four-run cycle:

    1. **Live-first.** The schema is fetched from the bqClient every
       launch — the cycle consumed a stale ``--ddl_uri`` pin and silently
       dropped every constraint edit (zero `prompt_constraints_found`
       across four jobs). ``--ddl_uri`` demotes to the OFFLINE FALLBACK
       (air-gapped launcher, BQ outage); a corrupt pin in offline mode
       still raises — the operator's declared fallback is broken.
    2. **Target-only steering metadata.** Structure (columns/types/modes)
       mirrors the SOURCE table, but the description surfaces — the
       `llm_prompt_constraint` clauses and the `{"sdfb":1,…}` contract —
       are overlaid from the LANDING (synthetic/target) table, which the
       synthetic-data team owns and Terraforms. The source (lake) table's
       descriptions are another team's prose and must NEVER steer
       generation: with the target unreachable they are STRIPPED, not
       inherited. The offline pin is the one exception — it is the
       operator's declared fallback, extracted from the landing table per
       the propagation runbook, so its descriptions stand when the target
       is also unreachable.

    The landing ``TableSchema`` travels back out because its column
    MODES — not the source's — decide a conditional edge's NULL policy
    (ADR 0037 design §4 ruling B). It is ``None`` when the landing table
    is absent or unreachable (a ``--create_if_not_exists`` first run, a
    permissions gap); the caller then falls back to the source's modes
    and says so (:func:`nullability_schema`).
    """
  live_error: Exception | None = None
  if reference_table:
    try:
      schema = extract_table_schema(reference_table)
    except Exception as exc:  # any live failure → offline fallback  # pylint: disable=broad-exception-caught
      live_error = exc
    else:
      log_milestone(
          "ddl_live_extracted",
          table=reference_table,
          columns=len(schema.columns),
      )
      schema, target = _overlay_target_metadata(
          schema, landing_table, strip_on_missing=True)
      if ddl_uri:
        _check_ddl_pin_staleness(schema, ddl_uri)
      return schema, target

  if ddl_uri:
    if live_error is not None:
      logger.warning(
          "Live DDL extraction from %s failed (%s); using --ddl_uri "
          "offline fallback %s",
          reference_table,
          type(live_error).__name__,
          ddl_uri,
      )
      log_milestone(
          "ddl_live_extract_failed",
          level=logging.WARNING,
          table=reference_table,
          error=type(live_error).__name__,
          note="using --ddl_uri offline fallback — constraints/contract "
          "are as-of the pin's extraction, not the live deployment",
      )
    schema = load_ddl(ddl_uri)
    log_milestone(
        "ddl_loaded_from_uri",
        uri=ddl_uri,
        fallback=live_error is not None,
    )
    return _overlay_target_metadata(
        schema, landing_table, strip_on_missing=False)

  if live_error is not None:
    raise live_error
  raise ValueError(
      "resolve_table_schema needs --reference_table (live extraction) "
      "or --ddl_uri (offline fallback)")


def _overlay_target_metadata(
    base: TableSchema, landing_table: str, *,
    strip_on_missing: bool) -> tuple[TableSchema, TableSchema | None]:
  """``(base with the TARGET's description surfaces, the TARGET)``.

    `strip_on_missing=True` (live-source base): the source's descriptions
    must never survive, so an unreachable target strips them to empty.
    `strip_on_missing=False` (offline pin base): the pin is the
    operator's declared fallback and its descriptions stand.

    The target is returned as well — it is fetched here exactly once,
    and ADR 0037 needs its column MODES (not its descriptions) for the
    conditional NULL policy. `None` = the landing table was unreachable.
    """
  target: TableSchema | None = None
  if landing_table:
    try:
      target = extract_table_schema(landing_table)
    except Exception as exc:  # pylint: disable=broad-exception-caught
      log_milestone(
          "target_metadata_unavailable",
          level=logging.WARNING,
          table=landing_table,
          error=type(exc).__name__,
          note="no constraints/contract from the target this run; "
          "source descriptions are never used as a substitute",
      )
  if target is None:
    if not strip_on_missing:
      return base, None
    return _with_descriptions(base, "", {}), None
  col_desc = {c.name: (c.description or "") for c in target.columns}
  schema = _with_descriptions(base, target.table_info.description or "",
                              col_desc)
  log_milestone(
      "target_metadata_overlaid",
      table=landing_table,
      constraint_columns=len(_constraint_clauses(schema)),
  )
  return schema, target


def _with_descriptions(schema: TableSchema, table_description: str,
                       col_desc: dict[str, str]) -> TableSchema:
  """A copy of `schema` whose description surfaces are exactly the given
    ones — absent columns get empty, never the base's leftovers."""
  return schema.model_copy(
      update={
          "table_info":
              schema.table_info.model_copy(
                  update={"description": table_description}),
          "columns": [
              c.model_copy(update={"description": col_desc.get(c.name, "")})
              for c in schema.columns
          ],
      })


def _check_ddl_pin_staleness(live: TableSchema, ddl_uri: str) -> None:
  """Say aloud when the ``--ddl_uri`` pin disagrees with the LIVE schema
    on generation-steering metadata.

    Live already won this launch — the check protects the NEXT offline
    day: a stale pin would silently drop the constraint edits again the
    moment INFORMATION_SCHEMA becomes unreachable (exactly the
    2026-08-21 four-run failure class, then with the pin authoritative).
    Best-effort: an unloadable pin is a WARNING here, never fatal.
    """
  try:
    pinned = load_ddl(ddl_uri)
  except Exception as exc:  # pylint: disable=broad-exception-caught
    log_milestone(
        "ddl_pin_check_error",
        level=logging.WARNING,
        uri=ddl_uri,
        error=type(exc).__name__,
        note="pin unusable as an offline fallback",
    )
    return
  pinned_clauses = _constraint_clauses(pinned)
  live_clauses = _constraint_clauses(live)
  drifted = sorted(
      col for col in set(pinned_clauses) | set(live_clauses)
      if pinned_clauses.get(col, "") != live_clauses.get(col, ""))
  table_drift = ((pinned.table_info.description or "")
                 != (live.table_info.description or ""))
  if drifted or table_drift:
    log_milestone(
        "ddl_pin_drift",
        level=logging.WARNING,
        uri=ddl_uri,
        columns=",".join(drifted) or "-",
        table_description_drift=table_drift,
        fix="re-run scripts/extract_ddl.py so the offline fallback "
        "matches the live deployment",
    )
  else:
    log_milestone(
        "ddl_pin_fresh",
        uri=ddl_uri,
        constraint_columns=len(live_clauses),
    )


def _constraint_clauses(schema: TableSchema) -> dict[str, str]:
  """column → parsed `llm_prompt_constraint` clause (unparseable → the
    raw description, so a broken edit still reads as drift)."""
  out: dict[str, str] = {}
  for col in schema.columns:
    try:
      clause = parse_llm_prompt_constraint(col.description, column=col.name)
    except Exception:  # pylint: disable=broad-exception-caught
      clause = col.description or ""
    if clause:
      out[col.name] = clause
  return out


def resolve_engine_strictness(client_type: str) -> bool:
  """True for real-LLM client types (``vllm`` on Dataflow/L4, ``mlx`` on
    the M4 DirectRunner) — a failed generation must be loud (strict
    free-text fallback, BLOCKER gate fails the job), not silently degrade
    into memorized reference data. Only the deterministic ``fake`` client
    (CPU smoke run, produces fake data regardless) stays lenient."""
  return client_type != "fake"


# Flex Template parameters are strings; this is the accepted truthy set for
# string-valued boolean flags threaded through the template/DAG chain.
_TRUTHY_FLAG_VALUES = frozenset({"true", "1", "yes"})


def parse_bool_flag(value: str) -> bool:
  """Normalize a string-valued boolean Flex-Template parameter."""
  return str(value).strip().lower() in _TRUTHY_FLAG_VALUES


def resolve_fk_mode(generate_fk_relationships: bool,
                    fk_parent_landing: str) -> tuple[str, str]:
  """``(effective fk_parent_landing, mode)`` for one table's run
    (ADR 0029 rev B — minimal-input scenarios).

    Isolated (`--generate_fk_relationships=false`): FK pools are simply
    off — no sentinel value, no preflight refusal; the launcher already
    warned loudly. Relational: the value is whatever the plan derived
    (the landing table's own dataset unless overridden)."""
  if not generate_fk_relationships:
    return "", "isolated"
  return fk_parent_landing, "relational"


def derive_fk_parent_landing(landing_table: str) -> str:
  """``project.dataset`` of the landing table — parents land in the
    SAME dataset, so the old --fk_parent_landing input is derivable and
    no longer a user concern (ADR 0029 rev B)."""
  return landing_table.rsplit(".", 1)[0]


def derive_source_fqn(landing_table: str, reference_table: str) -> str:
  """A sibling's source FQN: the reference table's dataset + the
    sibling's table name (the repo's same-name convention)."""
  src_dataset = reference_table.rsplit(".", 1)[0]
  return f"{src_dataset}.{landing_table.rsplit('.', 1)[-1]}"


def resolve_driven_uniqueness_mode(flag: str,
                                   *,
                                   driven: bool,
                                   identity_cols: tuple,
                                   adjusted: bool = False) -> str | None:
  """None = not driven (keep --uniqueness_mode); identity columns keep
    `exact` (ADR 0036): an identity column is fresh-generated per row, so
    the child's PK is not unique by construction the way a pure fan-out
    key is.

    ``adjusted`` (ADR 0038) overrides both: a table whose declared `pk:`
    the SOURCE disproved is SUPPOSED to land repeated keys, and `exact`
    would divert exactly those rows to the DLQ. `streaming` measures
    `pk.duplicate` on a digest-only branch and removes nothing — which
    is also what makes the landing key-repeat share comparable with the
    source's. The cost is that `identity.unique` is measured on no
    branch at all in `streaming` mode; identity values are synthesized
    per row from `(run_id, batch_id, row_index, column)`, and
    `row.duplicate` still covers engine replay, so the loss is a
    reporting one, not a guarantee."""
  if not driven:
    return None
  if adjusted:
    return MODE_STREAMING
  return "exact" if identity_cols else flag


def adjusted_fanout_payload(
    fanout: dict | None, adjustments: Sequence[ModelAdjustment]) -> dict | None:
  """The fan-out payload an ADJUSTED table generates from (ADR 0038).

    `resolve_fanout` measures BEFORE preflight, so its ``exact_cells``
    was decided against the DECLARED PK. Once that PK is dropped the
    cells key nothing, and an ``exact_cells`` plan is capped at
    ``min(k, capacity)`` by `joint_key_draw` — with no cells that is ONE
    row per parent key, i.e. the measured fan-out silently discarded and
    the exact opposite of reproducing the source.

    Everything else is left ALONE. The histogram is the measurement the
    table is sized from, and the cell table stays as measured: inexact
    cells are drawn WITH replacement from their weighted marginal, so
    the column keeps its source distribution instead of being narrowed
    to a per-key permutation.
    """
  if fanout is None or not adjustments:
    return fanout
  return {**fanout, "exact_cells": False}


def distinct_keys_landed(num_rows: int,
                         adjustments: Sequence[ModelAdjustment]) -> int:
  """How many DISTINCT key values this table hands its children (ADR
    0038 fix H3) — its rows, unless its own `pk:` was ADJUSTED away, in
    which case it lands the source's key repeats and holds
    ``rows x (1 - source_repeat_share)`` distinct ones.

    The composer already fans a child out over the parent's distinct keys
    (`parent_pk=()` arms `FanoutDistinct`); this is the number the
    child's row count must be DERIVED from so the request matches what
    the DAG can produce.
    """
  share = next(
      (a.source_repeat_share
       for a in adjustments
       if a.source_repeat_share is not None),
      None,
  )
  return landed_distinct_keys(num_rows, share)


def resolve_table_rows(landing_table: str, *, driven: bool,
                       derived_rows: int | None, launch_rows: int) -> int:
  """This table's ``num_rows`` (ADR 0036): a DRIVEN child's row count is
    ``derived_rows`` (from the measured source fan-out) — NEVER the
    launch-wide ``--num_rows``, which would silently size it wrong. A
    root/undriven table keeps ``launch_rows``. A driven child with no
    derivable count (missing histogram mass, or the driving parent's row
    count unknown) is a loud stop, not a silent fallback."""
  if not driven:
    return launch_rows
  if not derived_rows:
    raise SystemExit(
        f"[preflight P4] {landing_table}: this table is generated from "
        f"its parent's keys but its row count could not be derived "
        f"(derived_rows={derived_rows!r}; the source fan-out histogram "
        f"or the parent's row count is missing, or the source parent "
        f"has no children). A driven child never takes the launch-wide "
        f"--num_rows.")
  return derived_rows


def _cache_unavailable(landing_table: str, op: str, exc: Exception) -> None:
  """The fk_fanout_stats table is a convenience, never a prerequisite
    (2026-09-10 operator ask): a cache that cannot be read or written —
    the table does not exist, no permission, a transient error — warns
    and the launch measures without it."""
  log_milestone(
      "fk_fanout_cache_unavailable",
      level=logging.WARNING,
      table=landing_table,
      op=op,
      error=f"{type(exc).__name__}: {str(exc)[:160]}",
      note="fan-out measured without the cache; create "
      "synthetic_data_quality.fk_fanout_stats (optional) to cache it",
  )


def _cache_read(stats_store, landing_table: str, source_table: str, cols,
                sha: str):
  if not stats_store:
    return None
  try:
    return stats_store.get(source_table, cols, sha)
  except Exception as exc:  # any cache failure is non-fatal by design  # pylint: disable=broad-exception-caught
    _cache_unavailable(landing_table, "get", exc)
    return None


def _cache_write(stats_store, landing_table: str, source_table: str, cols,
                 sha: str, measured: dict) -> None:
  if not stats_store:
    return
  try:
    stats_store.put(source_table, cols, sha, measured)
  except Exception as exc:  # any cache failure is non-fatal by design  # pylint: disable=broad-exception-caught
    _cache_unavailable(landing_table, "put", exc)


def _edge_label(edge) -> str:
  """One FK edge as the launcher names it everywhere: ``(cols)->ref``
    — the same string a conditional edge answers to as its ``edge_id``,
    so the milestones and the payload keys read alike."""
  return conditional_edge_id(edge.cols, edge.ref)


def conditional_rest_of(registry: RelationshipRegistry, landing_table: str,
                        roles: Mapping) -> dict[str, tuple[str, ...]]:
  """``edge_id -> rest columns`` for every CONDITIONAL edge (ADR 0037).

    ONE builder for both readers — `resolve_fanout`'s cell measurement
    and `preflight`'s P4 — so they can never disagree about what a
    conditional edge supplies. ``edge_id`` is `conditional_edge_id`, the
    string `FkEdgeSpec.edge_id` and the request payload's ``matches``
    key already use."""
  return {
      conditional_edge_id(edge.cols, edge.ref):
          registry.edge_rest(landing_table, edge)
      for edge, role in roles.items()
      if role == "conditional"
  }


def effective_pk_of(registry: RelationshipRegistry, landing_table: str,
                    args) -> tuple[str, ...]:
  """The PK this run actually ENFORCES, resolved the way `preflight`
    resolves it: the relationship model's `pk:` (ADR 0032, the single
    source of truth), with `--pk_cols` filling the gap only for a table no
    model declares (`relations.pk or pk_cols`).

    It is the tuple `EnforceUniqueness` keys on and `pk.duplicate` is
    measured against, so every launcher decision that turns on "is this
    column a key member" reads THIS — never `TableSchema.primary_keys`,
    the BigQuery table constraint copied into `_ddl.json`, which is
    `None` on the canonical ADR 0032 setup (fix wave G1/G2)."""
  relations = registry.relations(landing_table)
  declared = tuple(relations.pk) if relations else ()
  if declared:
    return declared
  raw = str(getattr(args, "pk_cols", "") or "")
  return tuple(c.strip() for c in raw.split(",") if c.strip())


def resolve_fanout(
    registry: RelationshipRegistry,
    landing_table: str,
    source_table: str,
    *,
    in_set_names: set[str],
    reference_rows: list[dict],
    table_schema,
    stats_store,
    bq_client,
    effective_pk: tuple[str, ...] = (),
) -> tuple[dict | None, dict[FkEdge, str]]:
  """``(FanoutPlan payload or None, edge roles)`` for one table (ADR
    0036). Measures (or reads the cache) for the driving edge only when
    its parent generates in the same launch — an external or root table
    (no driving edge) is undriven, and the caller keeps ``args.num_rows``.
    ``RelationshipError`` (an ambiguous or unimplied edge set) propagates
    to the caller. The driving edge's columns are checked against the
    schema (P2) before any BigQuery call, and a measurement failure is
    reported as a preflight ``SystemExit`` (never an escaping traceback)
    so the relational runner's collect-then-fail loop can report it
    alongside every other table's stop."""
  roles = registry.edge_roles(landing_table)  # RelationshipError propagates
  driving = next((e for e, r in roles.items() if r == "driving"), None)
  if driving is None or driving.ref.rsplit(".", 1)[-1] not in in_set_names:
    return None, roles
  valid = {c.name for c in table_schema.columns}
  unknown = [c for c in driving.cols if c not in valid]
  if unknown:
    raise SystemExit(
        f"[preflight P2] {landing_table}: the driving edge names "
        f"unknown columns {unknown}. Fix the model file (or the table).")
  relations = registry.relations(landing_table)
  # The key this run ENFORCES, which is what preflight checks: the
  # model's `pk:`, or `--pk_cols` for a table no model keys. Reading
  # `relations.pk` alone left a --pk_cols table with NO measurement,
  # so P5 stood down for evidence that did not exist and P4 fell
  # through — worse than before ADR 0038 (2026-09-13).
  pk = tuple(effective_pk) or (tuple(relations.pk) if relations else ())
  profiles = profile_columns(table_schema,
                             reference_rows) if reference_rows else {}
  # The columns another edge fills are NOT measured as cells — the
  # same `edge_supplied_members` call P4 makes, so the measurement and
  # the check can never disagree (ADR 0037 ruling 12).
  cell_cols, exact = pk_cell_columns(
      pk,
      tuple(driving.cols),
      profiles,
      known=edge_supplied_members(
          pk, roles, conditional_rest_of(registry, landing_table, roles)).known,
  )
  source_parent = derive_source_fqn(
      parent_landing_fqn(driving.ref, derive_fk_parent_landing(landing_table)),
      source_table,
  )
  sha = registry.sha12()
  edge_label = _edge_label(driving)
  measured = _cache_read(stats_store, landing_table, source_table,
                         tuple(driving.cols), sha)
  source = "cache"
  if measured is None:
    try:
      measured = measure_fanout(
          source_child=source_table,
          child_cols=tuple(driving.cols),
          source_parent=source_parent,
          ref_cols=tuple(driving.ref_cols),
          cell_cols=cell_cols,
          client=bq_client,
          edge=edge_label,
      )
    except Exception as exc:
      raise SystemExit(
          f"[preflight] {landing_table}: fan-out measurement on "
          f"{source_table} failed: {type(exc).__name__}: {exc}") from exc
    source = "measured"
  log_fanout_measured(edge_label, measured, source=source)
  if resolve_pk_measurement(
      measured,
      pk,
      tuple(driving.cols),
      landing_table=landing_table,
      source_table=source_table,
      client=bq_client,
  ) or source == "measured":
    # Something in the payload is new — a fresh fan-out, a freshly
    # measured PK, or both — so the cache row is (re)written once,
    # holding BOTH measurements under the same key (ADR 0038 fix J).
    _cache_write(
        stats_store,
        landing_table,
        source_table,
        tuple(driving.cols),
        sha,
        measured,
    )
  return fanout_payload(measured, tuple(driving.cols), exact), roles


def resolve_pk_measurement(
    measured: dict,
    pk: tuple[str, ...],
    driving_cols: tuple[str, ...],
    *,
    landing_table: str,
    source_table: str,
    client,
) -> bool:
  """Fill ``measured["pk"]`` — the DECLARED PK, measured on the SOURCE
    child (ADR 0038 fix J). Returns True when a BigQuery scan was paid
    for, so the caller knows the cached payload has to be rewritten.

    Three ways in, cheapest first, and they are tried in that order:

    * a cached payload already measured exactly these columns — nothing
      to do (a model whose `pk:` changed changes the model sha, and with
      it the cache key, so a stale tuple can never be trusted into the
      check; one that does not match is ignored, not used);
    * the declared PK IS the driving edge — the histogram already groups
      the source child by those columns, so the measurement is read off
      it for free (`pk_measurement_from_histogram`);
    * otherwise one GROUP BY over the PK columns of the source child.

    A table with no declared `pk:` has nothing to measure — `P4` never
    checks a key that does not exist.
    """
  if not pk:
    return False
  cached = measured.get("pk")
  if cached and tuple(cached.get("cols") or ()) == pk:
    log_pk_measured(landing_table, cached, source="cache")
    return False
  if set(pk) == set(driving_cols):
    from_hist = pk_measurement_from_histogram(
        measured.get("histogram") or {}, pk)
    if from_hist is not None:
      measured["pk"] = from_hist
      log_pk_measured(landing_table, from_hist, source="histogram")
    return False
  try:
    measured["pk"] = measure_pk_uniqueness(
        source_child=source_table,
        pk_cols=pk,
        client=client,
    )
  except Exception as exc:
    raise SystemExit(f"[preflight] {landing_table}: declared PK measurement "
                     f"{list(pk)} on {source_table} failed: "
                     f"{type(exc).__name__}: {exc}") from exc
  log_pk_measured(landing_table, measured["pk"], source="measured")
  return True


def _mean_k(hist: Mapping[str, int]) -> float:
  """Mean children-per-parent from a fan-out histogram (string keys,
    ADR 0036 payload shape: ``{"0": n0, "1": n1, ...}``). 0.0 for an
    empty histogram — the caller floors ``keys_per_batch`` at 1 anyway."""
  parents = sum(hist.values())
  if not parents:
    return 0.0
  children = sum(int(k) * n for k, n in hist.items())
  return children / parents


def assert_fk_pools_nonempty(fks, fk_pools: dict, parent_landing: str) -> None:
  """Loud stop when an enforced FK edge loaded an EMPTY parent pool
    (ADR 0029 rev B): an empty parent means "not landed yet", and
    generating the child anyway would silently repeat the 2026-08-21
    false '0 orphans'. Scenario 2 orders parents first automatically;
    a direct child launch must land parents first."""
  missing = sorted(fk.ref for fk in fks if not fk_pools.get(fk.cols[0]))
  if missing:
    raise SystemExit(
        f"FK parents not landed (or empty) in {parent_landing}: "
        f"{missing}. Scenario 2 (--generate_fk_relationships=true on "
        f"the launch) generates parents first automatically; for a "
        f"manual child-only run, land the parents first.")


def parse_landing_tables(value: str) -> list[str]:
  """``--landing_table`` accepts one FQN or a comma-separated list."""
  return [t.strip() for t in value.split(",") if t.strip()]


@dataclass(frozen=True)
class TableRun:
  """One planned per-table generation inside a launch."""

  landing_table: str
  source_table: str
  run_id: str
  fk_parent_landing: str


@dataclass(frozen=True)
class LaunchPlan:
  scenario: str
  runs: tuple[TableRun, ...]
  warnings: tuple[str, ...] = field(default=())


def plan_launch(
    landing_tables: list[str],
    reference_table: str,
    generate_fk_relationships: bool,
    registry: RelationshipRegistry,
    run_id: str,
) -> LaunchPlan:
  """The launch scenarios, resolved to an ordered per-table plan.

    Two inputs decide everything (ADR 0029 rev B, now sourced from
    `config/relationships/` — ADR 0032): the landing table(s) and the
    flag. The registry answers the rest.

    1. one table + false  → itself only; any enforced edge is loudly
       ignored (referential integrity UNVERIFIED for that run).
    2. one table + true   → not in a model, or detached: identical to 1,
       zero friction. In a model: its whole ENABLED component, ordered
       parents-first. Disabling one table in the model file detaches it
       and everything that reached the rest only through it.
    3. many tables + false → each independently, in the given order —
       concurrent isolated generation, no relationships anywhere.
       many tables + true  → the union of their components. Rare and
       expensive, so it says so.
    """
  warnings: list[str] = []
  if generate_fk_relationships:
    by_name: dict[str, str] = {}
    for target in landing_tables:
      dataset = derive_fk_parent_landing(target)
      for name in registry.component(target):
        by_name.setdefault(name, f"{dataset}.{name}")
    ordered = [
        by_name[name] for name in registry.generation_order(tuple(by_name))
    ]
    expanded = [t for t in ordered if t not in landing_tables]
    if expanded:
      warnings.append(
          f"the relational model expanded this launch to {expanded} "
          f"(declared in config/relationships; parents generate first)")
    if len(landing_tables) > 1:
      warnings.append(f"{len(landing_tables)} targets WITH relationships: the "
                      f"launch covers {len(ordered)} tables in one job. Pass "
                      f"--generate_fk_relationships=false for independent "
                      f"concurrent generation instead.")
    relational = bool(expanded) or any(
        registry.enforced_edges(t) for t in ordered)
    scenario = ("relational_closure" if relational else
                ("isolated" if len(ordered) == 1 else "multi_independent"))
  else:
    ordered = list(landing_tables)
    ignored = [t for t in ordered if registry.enforced_edges(t)]
    if ignored:
      warnings.append(
          f"generate_fk_relationships=false ignores the enforced FK "
          f"edges declared on {ignored} — referential integrity "
          f"UNVERIFIED for this run")
    scenario = "isolated" if len(ordered) == 1 else "multi_isolated"

  multi = len(ordered) > 1
  runs = tuple(
      TableRun(
          landing_table=t,
          source_table=derive_source_fqn(t, reference_table),
          run_id=(
              f"{run_id}-{i:02d}-{t.rsplit('.', 1)[-1]}" if multi else run_id),
          fk_parent_landing=(
              derive_fk_parent_landing(t) if generate_fk_relationships and
              registry.enforced_edges(t) else ""),
      ) for i, t in enumerate(ordered))
  return LaunchPlan(scenario=scenario, runs=runs, warnings=tuple(warnings))


def log_relationship_model(
    table_fqn: str,
    registry: RelationshipRegistry,
    mode: str,
) -> None:
  """The run's relational truth, in one glanceable log entry.

    `relationship_model` carries the card (tables, PK, identity, every
    edge with enforced/documented/disabled state, generation waves, and
    the file it came from) — pipes and arrows only; the mermaid source
    is rendered by `scripts/relationships/card.py --mermaid`, never logged.
    `fk_generation_mode` stays as the one-line greppable state. Nothing here
    is inferred: it is the model file, rendered.
    """
  log_milestone("fk_generation_mode", table=table_fqn, mode=mode)
  relations = registry.relations(table_fqn)
  enforced = len(registry.enforced_edges(table_fqn))
  documented = sum(
      1 for e in (relations.fk if relations else ()) if not e.enforced)
  for rec in registry.derived_widenings():
    log_milestone(
        "fk_edge_widened",
        table=rec["table"],
        ref=rec["ref"],
        via=rec["via"],
        added=",".join(f"{c}->{r}" for c, r in rec["added"]),
        note="inherited columns pinned by a child that references the "
        "same columns in both tables (ADR 0036)",
    )
  # Pipes and arrows only (2026-09-10): no mermaid in any log; the
  # diagram comes from `scripts/relationships/card.py --mermaid`.
  log_milestone_text(
      "relationship_model",
      registry.card(table_fqn),
      level=(logging.WARNING
             if mode == "relational" and relations is not None and
             not enforced and documented else logging.INFO),
      table=table_fqn,
      model=getattr(registry.model_for(table_fqn), "model", "none"),
      enforced=enforced,
      documented=documented,
      enabled=registry.enabled(table_fqn),
      sha=registry.sha12(),
      mode=mode,
  )


def log_model_adjustments(adjustments: Sequence[ModelAdjustment],) -> None:
  """Announce every ADR 0038 adjustment, loudly and twice.

    The BANNER is one multi-line WARNING block in the relationship
    card's style — launcher-side, where multi-line is the readable
    shape. The MILESTONES are one greppable WARNING line per adjustment
    (``model_adjusted table= change= declared= measured= consequence=``),
    the form every worker-side surface and every log miner already
    reads. A run that generated against a model the operator did not
    write must be impossible to mistake for a clean one, so neither is
    optional and neither is INFO.
    """
  if not adjustments:
    return
  log_milestone_text(
      "model_adjustments",
      adjustment_banner(adjustments),
      level=logging.WARNING,
      count=len(adjustments),
      tables=",".join(a.table_name for a in adjustments),
  )
  for adjustment in adjustments:
    log_milestone(
        "model_adjusted",
        level=logging.WARNING,
        table=adjustment.table,
        change=adjustment.change,
        declared=adjustment.declared,
        measured=adjustment.measured,
        consequence=adjustment.consequence,
        source_repeat_share=(round(adjustment.source_repeat_share, 4)
                             if adjustment.source_repeat_share is not None else
                             ""),
    )


def adjusted_model_artifact_uri(options: PipelineOptions, run_id: str,
                                model: str) -> str:
  """Where the emitted effective model goes: beside the job's own
    staged artifacts (``--staging_location``, else ``--temp_location``),
    under ``model_adjustments/``. With neither — a DirectRunner laptop
    run — it lands in the working directory, which is where every other
    local artifact lands."""
  gco = options.view_as(GoogleCloudOptions)
  base = (gco.staging_location or gco.temp_location or "").rstrip("/")
  name = f"{sanitize_job_name('sdfb', run_id)}.{model}.yaml"
  return f"{base}/model_adjustments/{name}" if base else name


def emit_effective_model(
    registry: RelationshipRegistry,
    adjustments: Sequence[ModelAdjustment],
    options: PipelineOptions,
    run_id: str,
) -> list[str]:
  """Hand the operator back the model this run ACTUALLY generated with.

    One YAML document per model file that owns an adjusted table — the
    declared model with those tables' ``pk:`` removed and a comment
    naming the measurement that removed it — written beside the job's
    staged artifacts and ALSO logged verbatim. The log copy is the one
    that always survives: a run whose bucket the reader cannot reach
    still carries its effective model in `worker_logs.jsonl`.

    A write failure is announced and never fatal: the model is already
    in the log, and refusing to launch over an artifact would trade a
    generated dataset for a file.
    """
  written: list[str] = []
  for model, mine in adjusted_models(registry.models, adjustments):
    text = adjusted_model_yaml(model, mine)
    uri = adjusted_model_artifact_uri(options, run_id, model.model)
    try:
      with FileSystems.create(uri, mime_type="text/plain") as handle:
        handle.write(text.encode("utf-8"))
      written.append(uri)
    except Exception as exc:  # pylint: disable=broad-exception-caught
      log_milestone(
          "model_adjustment_model_unwritten",
          level=logging.WARNING,
          uri=uri,
          model=model.model,
          error=f"{type(exc).__name__}: {str(exc)[:160]}",
          note="the effective model is in the "
          "model_adjustment_model entry below; paste it into "
          "config/relationships/ by hand",
      )
      uri = "(not written)"
    log_milestone_text(
        "model_adjustment_model",
        text,
        level=logging.WARNING,
        model=model.model,
        source=model.source,
        uri=uri,
        tables=",".join(a.table_name for a in mine),
    )
  return written


def report_model_adjustments(
    specs: Sequence[TableSpec],
    registry: RelationshipRegistry,
    options: PipelineOptions,
    run_id: str,
) -> tuple[ModelAdjustment, ...]:
  """Every table's adjustments, announced ONCE for the whole launch.

    Collected here rather than logged inside `preflight` so the banner,
    the per-adjustment milestones and the emitted model sit together in
    the log, and so a launch that stops later (another table's preflight
    failed) never claims to have adjusted anything.
    """
  adjustments = tuple(a for s in specs for a in s.adjustments)
  if not adjustments:
    return ()
  log_model_adjustments(adjustments)
  emit_effective_model(registry, adjustments, options, run_id)
  return adjustments


def table_projections(
    specs: Sequence[TableSpec],
    *,
    launch_rows: int,
    rows_by_landing: Mapping[str, int] | None = None,
    keys_by_landing: Mapping[str, int] | None = None,
) -> tuple[TableProjection, ...]:
  """Each resolved spec's projected record count, in generation order
    (ADR 0039).

    Nothing is measured or re-derived here: the driving edge comes from
    the spec the composer will use (`mode="fanout"`), the histogram and
    the source parent count from the fan-out payload the preflight
    already paid for, the parent's landed rows and DISTINCT keys from the
    two dicts `_run_relational_job` fills in plan order — the very
    numbers `preflight._derived_rows` sized the child with.
    """
  rows_by_landing = rows_by_landing or {}
  keys_by_landing = keys_by_landing or {}
  projections: list[TableProjection] = []
  for spec in specs:
    driving = next((e for e in spec.parent_edges if e.mode == "fanout"), None)
    repeat_share = next(
        (a.source_repeat_share
         for a in spec.adjustments
         if a.source_repeat_share is not None),
        None,
    )
    if driving is None:
      projections.append(
          project_table(
              spec.config.landing_table,
              launch_rows=launch_rows,
              adjusted=bool(spec.adjustments),
              repeat_share=repeat_share,
          ))
      continue
    fanout = spec.config.fanout or {}
    parent_rows = rows_by_landing.get(driving.parent_landing)
    parent_keys = keys_by_landing.get(driving.parent_landing)
    projections.append(
        project_table(
            spec.config.landing_table,
            launch_rows=launch_rows,
            parent=(driving.parent_table or
                    driving.parent_landing.rsplit(".", 1)[-1]),
            edge=driving.edge_id,
            parent_rows=parent_rows,
            # `_derived_rows` multiplies the DISTINCT keys and falls
            # back to the rows — same expression, same order.
            parent_keys=parent_keys or parent_rows,
            histogram=fanout.get("histogram"),
            source_parent_tuples=fanout.get("parents"),
            adjusted=bool(spec.adjustments),
            repeat_share=repeat_share,
        ))
  return tuple(projections)


def report_row_projection(
    specs: Sequence[TableSpec],
    *,
    launch_rows: int,
    rows_by_landing: Mapping[str, int] | None = None,
    keys_by_landing: Mapping[str, int] | None = None,
) -> tuple[TableProjection, ...]:
  """How many rows this launch will generate, announced ONCE, before
    the graph is built (ADR 0039).

    The BLOCK is the operator surface — one multi-line entry in the
    relationship card's style, per table the count and the measurement
    it came from, then the total and any warning. The MILESTONES are the
    greppable half: one `row_projection_table` per table,
    `row_projection_total` for the launch, and one
    `row_projection_warning` per unsound measurement, so a run's
    structured record carries the projection even when nobody read the
    log. A clean launch logs the block and NO warning at all.
    """
  projections = table_projections(
      specs,
      launch_rows=launch_rows,
      rows_by_landing=rows_by_landing,
      keys_by_landing=keys_by_landing,
  )
  if not projections:
    return ()
  warnings = projection_warnings(projections, launch_rows=launch_rows)
  total = projected_total(projections)
  derivable = [p for p in projections if p.rows is not None]
  dominant = max(derivable, key=lambda p: p.rows or 0, default=None)
  dominant_share = ((dominant.rows or 0) /
                    total if dominant is not None and total else 0.0)
  log_milestone_text(
      "row_projection",
      projection_banner(
          projections, launch_rows=launch_rows, warnings=warnings),
      level=logging.WARNING if warnings else logging.INFO,
      tables=len(projections),
      rows=total,
      warnings=len(warnings),
      dominant=dominant.table_name if dominant else "",
      dominant_share=round(dominant_share, 4),
  )
  for spec, projection in zip(specs, projections, strict=True):
    log_milestone(
        "row_projection_table",
        table=projection.table,
        rows=projection.rows if projection.rows is not None else "",
        basis=("underivable" if projection.rows is None else
               ("driven" if projection.driven else "root")),
        parent=projection.parent,
        parent_keys=(projection.parent_keys
                     if projection.parent_keys is not None else ""),
        mean_fanout=(round(projection.mean_fanout, 4)
                     if projection.mean_fanout is not None else ""),
        share=round((projection.rows or 0) / total, 4) if total else 0.0,
        # The count the run will actually REQUEST. It is the same
        # number by construction (`resolve_table_rows` divides the
        # same product); printing both makes any drift between the
        # projection and the launch visible in one grep.
        requested=spec.config.num_rows,
        note=projection.undecided,
    )
  log_milestone(
      "row_projection_total",
      tables=len(projections),
      rows=total,
      dominant=dominant.table_name if dominant else "",
      dominant_share=round(dominant_share, 4),
      underivable=len(projections) - len(derivable),
  )
  for warning in warnings:
    log_milestone(
        "row_projection_warning",
        level=logging.WARNING,
        code=warning.code,
        table=warning.table,
        measurement=warning.measurement,
        consequence=warning.consequence,
    )
  return projections


def resolve_landing_dispositions(write_disposition: str,
                                 create_if_not_exists: bool) -> tuple[str, str]:
  """Landing-sink ``(write, create)`` dispositions (WS4 §6a/§6c).

    Landing ONLY — the DLQ, validation_runs and rag_chunks sinks stay
    WRITE_APPEND/CREATE_NEVER unconditionally (blast-radius rule)."""
  write = (
      BigQueryDisposition.WRITE_TRUNCATE
      if write_disposition == "overwrite" else BigQueryDisposition.WRITE_APPEND)
  create = (
      BigQueryDisposition.CREATE_IF_NEEDED
      if create_if_not_exists else BigQueryDisposition.CREATE_NEVER)
  return write, create


def sanitize_job_name(prefix: str, run_id: str) -> str:
  """Build a Dataflow-legal job name: ``[-a-z0-9]``, starts with a letter,
    ends alphanumeric, ≤63 chars. Airflow run_ids carry ``:`` / ``+`` / ``__``
    (e.g. ``scheduled__2026-05-20T00:00:00+00:00``) that are all illegal."""
  slug = re.sub(r"[^a-z0-9]+", "-", run_id.lower()).strip("-")
  name = f"{prefix}-{slug}" if slug else prefix
  return name[:63].rstrip("-")


def _pin_autoscaling(worker_options, autoscaling: str,
                     num_workers: int | None) -> None:
  """ADR 0034 D9: pin ``autoscaling_algorithm`` per ``--autoscaling``; an
    explicit Beam flag always wins."""
  algorithm = resolve_autoscaling(autoscaling, num_workers)
  if algorithm is None:
    return
  if worker_options.autoscaling_algorithm:
    logger.info(
        "Worker autoscaling_algorithm already set explicitly (%s); "
        "leaving as-is (autoscaling=%s ignored).",
        worker_options.autoscaling_algorithm,
        autoscaling,
    )
    return
  worker_options.autoscaling_algorithm = algorithm
  logger.info("Pinned autoscaling_algorithm to %s (ADR 0034 D9).", algorithm)


def configure_pipeline_options(
    options: PipelineOptions,
    runner: str,
    run_id: str,
    num_workers: int | None = None,
    autoscaling: str = "auto",
) -> None:
  """Set runner-dependent options.

    ``save_main_session`` lives on ``SetupOptions`` (NOT ``GoogleCloudOptions``);
    True only for DirectRunner ad-hoc runs — on Dataflow the image bakes in
    deps + source, so it just adds startup cost.

    The flex launcher already passes a valid ``--job_name`` (from the DAG's
    ``jobName``), so we only synthesize one — sanitized from ``run_id`` — when
    it's absent (e.g. a direct DataflowRunner launch).

    ``sdk_container_image`` pins Runner v2 *workers* to THIS image. The Flex
    Template's ``--image`` only sets the *launcher*; the worker harness image
    must be set separately (Flex Templates can't bake a default — see Google's
    "Configure Flex Templates"). The image bakes its own pushed coordinate into
    ``SDFB_SDK_CONTAINER_IMAGE`` (``docker/Dockerfile``) so the sha-free template
    + Composer DAG never have to carry it. Without this, workers boot the stock
    Beam SDK container (no ``sdfb_core``/``sdfb_beam``) and DoFn unpickling dies
    with ``ModuleNotFoundError: No module named 'sdfb_core'``. An explicit
    ``--sdk_container_image`` on the launch wins.

    ``num_workers`` (ADR 0034) pins the INITIAL worker count the same way:
    a scale run starts at its ceiling instead of waiting on the
    autoscaler; an explicit Beam ``--num_workers`` on the launch wins.
    """
  options.view_as(SetupOptions).save_main_session = runner == "DirectRunner"
  if runner == "DataflowRunner":
    gco = options.view_as(GoogleCloudOptions)
    if not gco.job_name:
      gco.job_name = sanitize_job_name("sdfb", run_id)
    worker_options = options.view_as(WorkerOptions)
    if not worker_options.max_cache_memory_usage_mb:
      worker_options.max_cache_memory_usage_mb = _DEFAULT_STATE_CACHE_MB
      logger.info(
          "Pinned worker max_cache_memory_usage_mb to %d MB (ADR 0034).",
          _DEFAULT_STATE_CACHE_MB)
    _pin_autoscaling(worker_options, autoscaling, num_workers)
    if num_workers is not None:
      if worker_options.num_workers:
        logger.info(
            "Worker num_workers already set explicitly (%d); leaving "
            "as-is (initial_workers=%d ignored).",
            worker_options.num_workers,
            num_workers,
        )
      else:
        worker_options.num_workers = num_workers
        logger.info("Pinned initial num_workers to %d (ADR 0034).", num_workers)
    if worker_options.disk_size_gb:
      logger.info(
          "Worker disk_size_gb already set explicitly (%d GB); leaving as-is.",
          worker_options.disk_size_gb)
    else:
      worker_options.disk_size_gb = _DEFAULT_WORKER_DISK_GB
      logger.info("Pinned worker disk_size_gb to %d GB.",
                  _DEFAULT_WORKER_DISK_GB)
    sdk_image = os.environ.get("SDFB_SDK_CONTAINER_IMAGE")
    if worker_options.sdk_container_image:
      logger.info(
          "Worker sdk_container_image already set explicitly (%s); leaving as-is "
          "(SDFB_SDK_CONTAINER_IMAGE=%s ignored).",
          worker_options.sdk_container_image,
          sdk_image or "<unset>",
      )
    elif sdk_image:
      worker_options.sdk_container_image = sdk_image
      logger.info(
          "Pinned worker sdk_container_image to %s (from SDFB_SDK_CONTAINER_IMAGE).",
          sdk_image)
    else:
      logger.warning(
          "Neither --sdk_container_image nor SDFB_SDK_CONTAINER_IMAGE is set; Dataflow "
          "Runner v2 workers will fall back to the stock Beam SDK container (no sdfb_core/"
          "sdfb_beam) and DoFn unpickling will fail. Bake it in docker/Dockerfile or pass "
          "--sdk_container_image explicitly.",)


def _resolve_table_fanout(
    args,
    table_schema,
    registry: RelationshipRegistry,
    in_set_names: set[str],
    reference_rows: list[dict],
    landing_schema=None,
) -> tuple[dict | None, dict]:
  """``(fanout payload, edge roles)`` for `_load_reference_and_preflight`
    (ADR 0036). Only attempted for an in-set relational launch: a
    single-table run has no in-job sibling that could drive it, so its
    own ``args.num_rows`` always stands and this is skipped entirely. An
    ambiguous or unimplied edge set (``RelationshipError`` from
    ``registry.edge_roles``) surfaces as the same collect-then-fail
    ``SystemExit`` every other P2 launch stop uses, so the relational
    runner's per-table loop reports it alongside the other tables.

    ADR 0037: a table that HAS a payload also carries its conditional
    edges and the Top-M candidate cap into it — the engine reads both
    off ``FanoutPlan``. A root (no payload) carries neither.
    ``landing_schema`` (from `resolve_schemas`) decides each conditional
    edge's ``nullable``; without it the source's modes stand in and each
    conditional edge logs `fk_nullable_from_source`."""
  if not (in_set_names and parse_bool_flag(args.generate_fk_relationships)):
    return None, {}
  stats_store = (
      BigQueryFanoutStatsStore(args.fk_fanout_stats_table) if getattr(
          args, "fk_fanout_stats_table", "") else None)
  try:
    payload, roles = resolve_fanout(
        registry,
        args.landing_table,
        args.reference_table,
        in_set_names=in_set_names,
        reference_rows=reference_rows,
        table_schema=table_schema,
        stats_store=stats_store,
        bq_client=None,
        effective_pk=effective_pk_of(registry, args.landing_table, args),
    )
  except RelationshipError as exc:
    raise SystemExit(f"[preflight P2] {args.landing_table}: {exc}") from exc
  if payload is not None:
    if landing_schema is None:
      _warn_nullability_from_source(args.landing_table, roles)
    payload["conditional"] = conditional_plan_entries(
        registry,
        args.landing_table,
        roles,
        nullability_schema(landing_schema, table_schema),
        table_schema,
        # The PK this run enforces — it decides both the NULL policy
        # (a key member can never NULL-fill) and which edges bound a
        # key's capacity (fix waves G1/G2).
        effective_pk=effective_pk_of(registry, args.landing_table, args),
    )
    # Fix wave F1 (reverting A3): `--fk_candidate_cap` is `M`, the
    # size of the Top-M candidate SAMPLE the composer keeps per JOIN
    # VALUE — one sample shared by every driving key carrying that
    # value, NOT a per-key allotment. Clamping it to the measured max
    # fan-out collapsed a 1:1 driving edge to M=1, leaving every child
    # on a shared value the identical candidate (a point mass, most of
    # the co-parent's rows never referenced). Request size is bounded
    # by `keys_per_batch` (`in_set_parent_edges`) instead.
    payload["candidate_cap"] = candidate_cap_of(args)
  return payload, roles


def _warn_nullability_from_source(landing_table: str, roles: Mapping) -> None:
  """One WARNING per conditional edge when the LANDING schema could not
    be read: its NULL policy is then decided by the SOURCE table's column
    modes, which are another team's and need not match the sink's. A
    landing column that is REQUIRED where the source is NULLABLE would
    fail the FILE_LOADS job at the very end of a run."""
  for edge, role in roles.items():
    if role != "conditional":
      continue
    log_milestone(
        "fk_nullable_from_source",
        level=logging.WARNING,
        table=landing_table,
        edge=_edge_label(edge),
        note="landing schema unreadable — this edge's NULL policy "
        "reads the SOURCE table's column modes",
    )


def _log_edge_role_warnings(landing_table: str, registry: RelationshipRegistry,
                            edge_roles: Mapping) -> None:
  """The two WARNING milestones of ADR 0037 (design §3 rule 4, §9).

    `fk_driving_edge_defaulted` — no `drives: true` and no ancestry
    between the candidate parents, so the FIRST DECLARED edge drives: a
    legal, reproducible launch the operator did not actually choose.
    `fk_edge_overlap_external` — an EXTERNAL parent's edge writes a child
    column another edge also writes: the driving edge (which overwrites
    it), another external edge, or any non-driving edge. The last writer
    keeps the column, so the loser's tuple need not exist in its parent.
    Nothing the operator can declare resolves it while the parent stays
    outside the launch — an external edge never becomes `implied`,
    `drives: true` is inert for it and an external parent has no
    `tables:` entry to disable — so every such pair is NAMED here
    (WARNING) rather than stopping the launch (fix wave F3; the
    cross-edge ownership stop keeps its teeth for in-model pairs).

    The overlap report does NOT depend on roles having been resolved (fix
    wave G3). `_resolve_table_fanout` returns `{}` for a non-relational
    launch, so the classic denormalised child — BOTH parents external,
    the very shape fix wave F3 stopped stopping — launched with no stop
    AND no signal: at run time the second pool overwrites the shared
    column and its rows divert as `fk.orphan`, potentially the whole run,
    with nothing in the launch log naming the overlap. Only the
    driving-edge WARNING needs a resolved role."""
  driving = next((e for e, r in edge_roles.items() if r == "driving"), None)
  if (driving is not None and
      registry.driving_choice(landing_table) == "first_declared"):
    log_milestone(
        "fk_driving_edge_defaulted",
        level=logging.WARNING,
        table=landing_table,
        edge=_edge_label(driving),
        hint="mark drives: true to choose",
    )
  for external, other, overlap in _external_overlaps(landing_table, registry,
                                                     edge_roles):
    log_milestone(
        "fk_edge_overlap_external",
        level=logging.WARNING,
        table=landing_table,
        edge=_edge_label(external),
        other=_edge_label(other),
        overlap=",".join(overlap),
        note="an external parent's edge writes the same child "
        "column(s) as another edge of this table; the last write "
        "wins, so the other edge's tuple may not exist in its "
        "parent. Bring the parent into the launch to resolve it",
    )


def _external_overlaps(landing_table: str, registry: RelationshipRegistry,
                       edge_roles: Mapping) -> tuple:
  """`registry.external_overlaps`, resolving the roles ITSELF when the
    caller has none — a single-table launch, or one with FK generation
    off (fix wave G3).

    A model whose roles cannot be resolved at all (the cross-edge
    ownership stop) is reported here, not raised: this is the WARNING
    path, and a relational launch has already surfaced that same
    `RelationshipError` as a `[preflight P2]` SystemExit through
    `resolve_fanout`."""
  try:
    return registry.external_overlaps(landing_table, edge_roles or None)
  except RelationshipError as exc:
    logger.warning(
        "edge roles for %s could not be resolved (%s) — the "
        "external-overlap check did not run",
        landing_table,
        exc,
    )
    return ()


def _load_reference_and_preflight(
    args,
    table_schema,
    registry: RelationshipRegistry,
    in_set_landing: frozenset[str] = frozenset(),
    landing_schema=None,
):
  """Eager reference read + relational preflight (ADR 0032): the
    table's relations come from `config/relationships/`, pk/identity
    default from them (CLI fills the gaps for tables no model declares),
    and unknown columns fail fast — all driver-side, before any graph
    exists. ADR 0036: resolves the driving edge's roles and, when its
    parent is in-set, its measured (or cached) source fan-out — carried
    into `preflight` so a driven child's row count derives from it."""
  logger.info("Loading reference rows from %s (limit=%d)", args.reference_table,
              args.reference_rows_limit)
  reference_rows = load_reference_rows(
      table=args.reference_table,
      limit=args.reference_rows_limit,
  )
  in_set_names = {t.rsplit(".", 1)[-1] for t in in_set_landing}
  # ADR 0035: an in-set parent lands at most its own resolved row
  # count. ADR 0036: a driven parent's count may itself be DERIVED
  # from its own parent — `rows_by_landing` (filled by
  # `_run_relational_job` in plan order) carries that forward instead
  # of the launch's flat `num_rows`. An external parent's count is
  # unknown here and stays at the sample cap (an upper bound — never
  # a false stop).
  rows_by_landing: Mapping[str, int] = getattr(args, "_rows_by_landing", {})
  # ADR 0038 fix H3: the DISTINCT keys each in-set parent lands, which
  # is what a driven child fans out over. Same carry-forward, filled by
  # `_run_relational_job` in plan order; it differs from the rows only
  # for a parent whose own `pk:` was ADJUSTED away.
  keys_by_landing: Mapping[str, int] = getattr(args,
                                               "_distinct_keys_by_landing", {})
  parent_landing = derive_fk_parent_landing(args.landing_table)
  in_set_edges = [
      fk for fk in registry.enforced_edges(args.landing_table)
      if fk.ref.rsplit(".", 1)[-1] in in_set_names
  ]
  fk_parent_rows = {
      fk.ref:
          rows_by_landing.get(
              parent_landing_fqn(fk.ref, parent_landing), args.num_rows)
      for fk in in_set_edges
  }
  fk_parent_distinct_keys = {
      fk.ref: keys for fk in in_set_edges if (keys := keys_by_landing.get(
          parent_landing_fqn(fk.ref, parent_landing)))
  }
  thresholds = resolve_thresholds(
      getattr(args, "thresholds_uri", "config/thresholds.yml"),
      getattr(args, "env", "dev"),
  )
  fanout, edge_roles = _resolve_table_fanout(
      args,
      table_schema,
      registry,
      in_set_names,
      reference_rows,
      landing_schema=landing_schema,
  )
  _log_edge_role_warnings(args.landing_table, registry, edge_roles)
  pf = preflight(
      table_schema,
      tuple(c.strip() for c in args.pk_cols.split(",") if c.strip()),
      tuple(c.strip() for c in args.identity_cols.split(",") if c.strip()),
      reference_rows,
      relations=registry.relations(args.landing_table),
      prompt_constraints_enabled=args.prompt_constraints == "on",
      # ADR 0028 P4: refuse a PK whose routed generator cannot cover
      # num_rows — before any graph exists. ADR 0035: FK-bound and
      # categorical members count too, against the run's gate.
      num_rows=args.num_rows,
      fk_parent_rows=fk_parent_rows,
      # Fix H3: a driven child is sized off its parent's DISTINCT
      # landed keys — an adjusted parent lands fewer than it has rows.
      fk_parent_distinct_keys=fk_parent_distinct_keys,
      blocker_failure_ratio=thresholds.blocker_failure_ratio,
      fanout=fanout,
      edge_roles=edge_roles,
      # The edges this launch DRAWS (both ends enabled, widened) — not
      # the declared ones, or a disabled parent bounds the PK (P4).
      enforced_fk=registry.enforced_edges(args.landing_table),
      # ADR 0037: the shared columns each conditional edge is joined
      # on, so `fk_edge_role` names them in the launch log.
      edge_overlaps={
          edge: registry.edge_overlap(args.landing_table, edge)
          for edge, role in edge_roles.items()
          if role == "conditional"
      },
      # ADR 0037 §6 — a conditional edge's `rest` and the Top-M cap
      # are per-key PK factors. An independent edge's key pool is one
      # too, but preflight sizes it itself, from the derived row count
      # only it knows (ruling 13), and returns it on the result.
      conditional_rest=conditional_rest_of(registry, args.landing_table,
                                           edge_roles),
      candidate_cap=candidate_cap_of(args),
      # ADR 0038 — what a MEASURED contradiction does. `adjust` (the
      # default) drops the disproved `pk:` and carries on; `stop` is
      # the pre-0038 refusal, message for message.
      on_model_conflict=getattr(args, "on_model_conflict", ON_CONFLICT_ADJUST),
  )
  # ADR 0038 — `resolve_fanout` decided `exact_cells` against the
  # DECLARED PK, above. With that PK dropped the cells key nothing, and
  # an exact plan would cap the fan-out instead of reproducing it.
  fanout = adjusted_fanout_payload(fanout, pf.adjustments)
  for warning in pf.warnings:
    logger.warning("preflight: %s", warning)
  # ADR 0029 rev B — FK activation derives, never asks: with the flag
  # on and enforced edges declared, parents live in the SAME landing
  # dataset (--fk_parent_landing stays as an expert override only).
  # An empty parent pool is a loud, actionable stop.
  fk_pools: dict = {}
  fk_key_pools: list[dict] = []
  enforced_fk = registry.enforced_edges(args.landing_table)
  external_fk = tuple(
      fk for fk in enforced_fk if fk.ref.rsplit(".", 1)[-1] not in in_set_names)
  if parse_bool_flag(args.generate_fk_relationships) and external_fk:
    parent_landing = args.fk_parent_landing or derive_fk_parent_landing(
        args.landing_table)
    fk_key_pools = load_fk_key_pools(external_fk, parent_landing)
    fk_pools = per_column_view(fk_key_pools)
    assert_fk_pools_nonempty(external_fk, fk_pools, parent_landing)
    args.fk_parent_landing = parent_landing  # ctx fk_edges read it
  elif parse_bool_flag(args.generate_fk_relationships) and enforced_fk:
    # All enforced edges resolve in-set (ADR 0030 single job): keys
    # arrive as side inputs; still derive for the fk_edges metadata.
    args.fk_parent_landing = args.fk_parent_landing or (
        derive_fk_parent_landing(args.landing_table))
  source_distinct = _emit_source_stats(args, table_schema, reference_rows, pf)
  return (
      reference_rows,
      pf,
      fk_pools,
      fk_key_pools,
      source_distinct,
      fanout,
      edge_roles,
  )


def _emit_source_stats(args, table_schema, reference_rows,
                       pf) -> dict[str, int]:
  """WS-B: one profiling pass over the already-loaded reference sample —
    milestone always, JSON artifact and BQ rows when configured. A digest
    that already has rows is skipped (pool-store exists() idiom).

    Returns per-column EXACT distinct counts when ``--source_stats=exact``
    ran (ADR 0022 — they feed free-text pool sizing via
    ``GenerationContext.source_distinct``); empty dict otherwise.
    """
  if args.source_stats == "off" or not reference_rows:
    return {}
  stats = profile_source_table(
      table_schema, reference_rows, relations=pf.relations)
  source_distinct: dict[str, int] = {}
  if args.source_stats == "exact":
    from sdfb_beam.io.exact_stats import compute_exact_stats

    # A failed exact pass degrades LOUDLY to sample-tier stats: the run
    # is still valid, just without exact pool sizing.
    try:
      stats = compute_exact_stats(args.reference_table, table_schema, stats)
    except Exception as exc:  # pylint: disable=broad-exception-caught
      log_milestone(
          "source_stats_exact_failed",
          level=logging.WARNING,
          table=args.reference_table,
          error=f"{type(exc).__name__}: {exc}",
      )
    else:
      source_distinct = {
          name: int(entry["distinct"])
          for name, entry in stats.items()
          if entry.get("stats_tier") == "exact"
      }
      log_milestone(
          "source_stats_exact",
          table=args.reference_table,
          columns=len(source_distinct),
      )
  sparse = sorted(stats.items(), key=lambda kv: -kv[1]["empty_fraction"])[:5]
  log_milestone(
      "source_table_stats",
      table=table_schema.fqn,
      columns=len(stats),
      sparsest=",".join(
          f"{name}:{entry['empty_fraction']:.2f}" for name, entry in sparse),
  )
  digest = compute_reference_digest(reference_rows)
  if args.source_stats_json:
    with FileSystems.create(args.source_stats_json) as fh:
      fh.write(json.dumps(stats, indent=2, default=str).encode())
  if args.source_stats_table:
    store = BigQuerySourceStatsStore(args.source_stats_table)
    # Skip on the tier actually ACHIEVED (a degraded exact run writes
    # sample-tier rows and stays retryable), never the requested one.
    achieved_tier = "exact" if source_distinct else "sample"
    if store.exists(
        table_schema.fqn,
        digest,
        profiler_version=PROFILER_VERSION,
        stats_tier=achieved_tier,
    ):
      log_milestone("source_stats_skipped", reference_digest=digest[:12])
    else:
      store.write_rows(stats_rows(table_schema.fqn, digest, args.run_id, stats))
      log_milestone(
          "source_stats_written",
          reference_digest=digest[:12],
          rows=len(stats),
      )
  return source_distinct


def warm_pools_trusted(pool_store, source_value_store, reference_digest: str,
                       model_uri: str) -> bool:
  """False ⇒ the persisted pools overlap the live source and were
    deleted for a clean rebuild; True ⇒ keep the warm path.

    2026-08-07 10M warm run: `exists()` was the only guard, so the
    memorized 2026-08-05 pools (33-99% verbatim source values) were
    replayed wholesale at 10M-row scale. Both failure paths keep the warm
    pools — LOUDLY — because a taint check must never kill a launch
    (pools are an optimisation) and an append-rebuild without a clean
    delete would leave stale rows racing the rebuilt ones in `fetch`.
    """
  try:
    overlap = pool_source_overlap(pool_store, source_value_store,
                                  reference_digest, model_uri)
  except Exception as exc:  # pylint: disable=broad-exception-caught
    log_milestone(
        "pool_taint_check_error",
        level=logging.WARNING,
        error=type(exc).__name__,
    )
    return True
  if not overlap:
    return True
  log_milestone(
      "pool_taint_rebuild",
      level=logging.WARNING,
      columns=len(overlap),
      # Counts only — reference values must never reach logs.
      overlap_counts={
          c: n for c, n in sorted(overlap.items())
      },
  )
  try:
    pool_store.delete(reference_digest, model_uri)
  except Exception as exc:  # pylint: disable=broad-exception-caught
    log_milestone(
        "pool_taint_delete_error",
        level=logging.ERROR,
        error=type(exc).__name__,
    )
    return True
  return False


def resolve_pool_layer(args, reference_rows: list[dict]) -> tuple:
  """(freetext_pools_store, source_value_store) for `build_pipeline`.

    (None, None) when the pool layer is off; (None, store) when the warm
    pools were verified clean (branch skipped); (pool_store, value_store)
    when the branch must build — cold store, or a warm store the taint
    preflight condemned.
    """
  if not (parse_bool_flag(args.build_pool_layer) and args.freetext_pools_table):
    return None, None
  digest = compute_reference_digest(reference_rows)
  pool_store = BigQueryFreeTextPoolStore(args.freetext_pools_table)
  # Full-domain novelty rejection for the build branch, and the
  # taint preflight for the warm path (2026-08-05/07 E2E findings).
  source_value_store = BigQuerySourceValueStore(args.reference_table)
  # A MISSING pool table must not surface as a cryptic NotFound out of
  # the driver — that is exactly how TEST_1 (2026-07-25 16:38) died on
  # a 404. The table is never auto-created (the CREATE_IF_NEEDED
  # blast-radius rule confines auto-create to the landing sink), so
  # say what to run. The READ path degrades silently and correctly on
  # its own; only an explicit --build_pool_layer reaches here.
  try:
    already_built = pool_store.exists(digest, args.model_uri)
  except Exception as exc:
    proj, ds, tbl = args.freetext_pools_table.split(".", 2)
    raise SystemExit(
        f"--build_pool_layer needs {args.freetext_pools_table}, which "
        f"could not be read ({type(exc).__name__}: {exc}).\n"
        f"Create it once:\n"
        f"  bq mk --table {proj}:{ds}.{tbl} "
        f"config/bq_schema/synthetic_rag/freetext_pools.schema.json\n"
        f"Or drop --build_pool_layer: pools are an optimisation, and "
        f"the run works without them (they are rebuilt per worker).") from exc
  if already_built:
    # exists() alone let the 2026-08-07 10M warm run replay the
    # memorized 2026-08-05 pools; a warm store must also prove it
    # holds no live source values before it is trusted.
    already_built = warm_pools_trusted(pool_store, source_value_store, digest,
                                       args.model_uri)
  if already_built:
    log_milestone(
        "pool_build_skipped",
        reference_digest=digest[:12],
        model_uri=args.model_uri,
    )
    return None, source_value_store
  # The branch writes the store itself (blocking load job inside
  # the DoFn) so the pipeline's AwaitFreeTextPools gate releases
  # Generate only once the rows are readable — a sibling
  # WriteToBigQuery sink raced Generate on the 2026-07-28/29 cold
  # runs and every pool was built twice.
  return pool_store, source_value_store


def main(argv: list[str] | None = None) -> int:
  logging.basicConfig(
      level=logging.INFO,
      format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
      force=True,
  )
  # First line of every launch: which build is this? (2026-08-21 cycle —
  # two same-day runs were indistinguishable by build from the logs.)
  log_build_info("launcher")
  args, beam_argv = parse_args(argv or sys.argv[1:])

  # ADR 0029 rev B — resolve the launch scenario BEFORE any per-table
  # work: minimal inputs (landing table(s) + one flag), everything
  # else derived. One milestone states the whole plan.
  targets = parse_landing_tables(args.landing_table)
  generate_fk = parse_bool_flag(args.generate_fk_relationships)
  # ADR 0032 — the relational model comes from config, not from
  # production metadata: no dataset scan, no table-description parse,
  # one file the operator can read and version.
  registry = load_relationship_registry(args.relationships_uri)
  plan = plan_launch(targets, args.reference_table, generate_fk, registry,
                     args.run_id)
  for warning in plan.warnings:
    logger.warning("launch plan: %s", warning)
  log_milestone(
      "launch_scenario",
      scenario=plan.scenario,
      tables=len(plan.runs),
      order=",".join(r.landing_table.rsplit(".", 1)[-1] for r in plan.runs),
      generate_fk_relationships=generate_fk,
  )
  # ONE human-readable block stating every enabled/disabled config of
  # this execution (ADR 0030): pasted _full_report.md +
  # worker_logs.jsonl answer "what was on?" without arg archaeology.
  log_milestone_pretty(
      "launch_config",
      {
          **{
              k: v
              for k, v in sorted(vars(args).items())
              if not k.startswith("_") and k != "model_client"
          },
          "resolved": {
              "scenario": plan.scenario,
              "generate_fk_relationships": generate_fk,
              "multi_table_mode": args.multi_table_mode,
              "tables_in_order": [r.landing_table for r in plan.runs],
              "run_ids": [r.run_id for r in plan.runs],
              "fk_parent_landing_derived":
                  [r.fk_parent_landing or "(none)" for r in plan.runs],
              "warnings": list(plan.warnings),
          },
      },
      scenario=plan.scenario,
  )
  for target in targets:
    log_relationship_model(
        target,
        registry,
        mode="relational" if generate_fk else "isolated",
    )
  if len(plan.runs) > 1 and args.multi_table_mode == "single_job":
    return _run_relational_job(plan, args, beam_argv, registry=registry)
  for i, run in enumerate(plan.runs):
    table_args = argparse.Namespace(**vars(args))
    # Underscored on purpose: an internal Namespace channel read back via
    # getattr(), never a CLI flag.
    table_args._multi_table_plan = len(plan.runs) > 1  # pylint: disable=protected-access
    table_args.landing_table = run.landing_table
    table_args.reference_table = run.source_table
    table_args.run_id = run.run_id
    table_args.fk_parent_landing = (
        args.fk_parent_landing or run.fk_parent_landing)
    # The --ddl_uri pin describes the FIRST target only; siblings
    # extract live (authoritative per ADR 0027 D2).
    if run.landing_table != targets[0]:
      table_args.ddl_uri = ""
    rc = _run_one_table(table_args, beam_argv, registry=registry)
    if rc != 0:
      logger.error(
          "table %s failed (rc=%d) — aborting the remaining %d "
          "planned tables (children never run without parents)",
          run.landing_table,
          rc,
          len(plan.runs) - i - 1,
      )
      return rc
  return 0


def nullability_schema(landing_schema, source_schema):
  """The schema whose column MODES decide a conditional edge's NULL
    policy: the LANDING table's (ADR 0037 design §4 ruling B) — it is
    the sink, and it is Terraformed independently of the lake table the
    generation schema mirrors. Falls back to the source schema when the
    landing table is absent or unreachable; `_resolve_table_fanout`
    names that fallback with one `fk_nullable_from_source` per
    conditional edge (review round 1, finding B3)."""
  return landing_schema if landing_schema is not None else source_schema


def _column_modes(schema, rest: tuple[str, ...]) -> list[str]:
  """Each ``rest`` column's mode in ``schema`` (``""`` when absent)."""
  if schema is None:
    return ["" for _ in rest]
  modes = {c.name: c.mode for c in schema.columns}
  return [modes.get(col, "") for col in rest]


def _enforced_pk(generation, effective_pk: tuple[str, ...]) -> set[str]:
  """The PK the nullability guard must read (fix wave G2).

    ``effective_pk`` is what the run ENFORCES — the relationship model's
    `pk:` (ADR 0032), i.e. what `preflight` resolves and what
    `EnforceUniqueness` keys on. ``TableSchema.primary_keys`` is the
    BigQuery table CONSTRAINT the extractor copies into `_ddl.json` (its
    own docstring: "useful context, never the source of truth"), and on
    the canonical ADR 0032 setup it is ``None`` — so reading it alone
    meant the guard NEVER fired: ADR 0037's own diamond (BOTTOM declares
    `pk: [T, L, R]`, `R` NULLABLE in both schemas) NULL-filled a declared
    key member, those rows landed and their repeats diverted as
    `pk.duplicate`. The two are UNIONED, never substituted: the model's
    `pk:` is what the run enforces, the DDL constraint is what
    `derive_record_model` validates against, and a NULL in EITHER is
    fatal — a column the constraint declares and the model omits would
    otherwise be NULL-filled and then dropped inside the engine's
    `except Exception: continue`, with no envelope and no milestone."""
  return set(effective_pk) | set(
      getattr(generation, "primary_keys", None) or ())


def _rest_is_nullable(
    rest: tuple[str, ...],
    table_schema,
    generation_schema=None,
    *,
    table: str = "",
    edge_id: str = "",
    warn: bool = True,
    effective_pk: tuple[str, ...] = (),
) -> bool:
  """ADR 0037 §4 ruling B: an unmatched key may land with NULLs only
    when EVERY ``rest`` column is NULLABLE — in BOTH schemas (fix wave
    A4). An empty ``rest`` (a pure existence filter, ``(K)->P`` driving
    and ``(K)->Q`` conditional) has nothing to write a NULL into, so it
    is never nullable — the key is dropped and counted instead.

    ``table_schema`` is the LANDING schema (the sink, resolved by
    :func:`nullability_schema`); ``generation_schema`` is the one the
    engines derive their record model from, whose modes mirror the
    SOURCE table. Landing alone is not enough: where landing said
    NULLABLE and generation said REQUIRED, the DoFn kept the unmatched
    key, the engine wrote ``None``, ``model_validate`` rejected the row
    and the engine's ``except Exception: continue`` discarded every one
    of that key's rows — silently, with no envelope, counter or
    milestone. When the two disagree the edge is treated as NON-nullable,
    so the key is dropped as a visible ``fk.unmatched``, and the
    disagreement is named once. Omitting ``generation_schema`` keeps the
    single-schema behaviour (both reads hit the same schema).

    A column MODE is not the whole contract (fix wave F2): the record
    model ``derive_record_model`` builds rejects ``None`` on every
    declared PK column whatever its mode says (``_make_pk_base`` — BQ
    allows a NULLABLE PK column), and the engines swallow that
    ``ValidationError`` the same silent way. On ADR 0037's own diamond
    the child PK's last member IS the co-parent's column, so a rest
    column in the PK is the default shape, not a corner: such an edge is
    NON-nullable however both schemas mode it, and the reason rides on
    the milestone.

    WHICH PK (fix wave G2): ``effective_pk`` — the PK this run ENFORCES,
    from the relationship model (`effective_pk_of`) — with the schema's
    ``primary_keys`` standing in only when the model declares none
    (:func:`_enforced_pk`)."""
  if table_schema is None or not rest:
    return False
  generation = table_schema if generation_schema is None else generation_schema
  landing_modes = _column_modes(table_schema, rest)
  generation_modes = _column_modes(generation, rest)
  landing_ok = all(mode == "NULLABLE" for mode in landing_modes)
  generation_ok = all(mode == "NULLABLE" for mode in generation_modes)
  declared_pk = tuple(
      col for col in rest if col in _enforced_pk(generation, effective_pk))
  reasons = []
  if declared_pk:
    reasons.append("declared_pk")
  if landing_ok != generation_ok:
    reasons.append("schema_mode_mismatch")
  if warn and reasons:
    log_milestone(
        "fk_nullable_schema_mismatch",
        level=logging.WARNING,
        table=table,
        edge=edge_id,
        landing=",".join(landing_modes),
        generation=",".join(generation_modes),
        reason=",".join(reasons),
        pk=",".join(declared_pk),
        note="this edge cannot NULL-fill an unmatched key: the "
        "landing and generation schemas disagree on its rest "
        "columns' modes, and/or a rest column sits in the PRIMARY KEY "
        "this run enforces (the relationship model's `pk:`, or the "
        "DDL constraint when the model declares none), which the "
        "record model refuses a NULL on whatever the mode says. "
        "Treated as "
        "NON-nullable, so the key diverts as a visible fk.unmatched "
        "instead of landing rows the record model would silently "
        "reject",
    )
  return landing_ok and generation_ok and not declared_pk


def conditional_plan_entries(
    registry: RelationshipRegistry,
    landing_table: str,
    roles: Mapping,
    table_schema,
    generation_schema=None,
    *,
    effective_pk: tuple[str, ...],
) -> list[dict]:
  """``FanoutPlan.conditional`` payload entries (ADR 0037): one per
    conditional edge, in ``enforced_edges`` order. ``id`` is the string
    the request payload, the plan and ``FkEdgeSpec.edge_id`` all agree
    on (`conditional_edge_id` — it carries the parent, so two edges from
    the same columns to different parents keep separate entries);
    ``cols`` are the child columns the engine writes from the drawn
    candidate (``rest``); ``nullable`` is the NULL policy for a key with
    no candidate; ``pk_member`` says whether ``rest`` supplies a member
    of ``effective_pk``.

    ``effective_pk`` is `effective_pk_of` — the PK this run enforces, the
    same tuple preflight P4 counts its factors against. It decides BOTH
    remaining fields (fix waves G1/G2): a key member can never NULL-fill,
    and ONLY a PK-supplying edge may multiply a key's capacity in
    `joint_key_draw`."""
  pk = set(effective_pk)
  entries: list[dict] = []
  for fk in registry.enforced_edges(landing_table):
    if roles.get(fk) != "conditional":
      continue
    rest = registry.edge_rest(landing_table, fk)
    edge_id = conditional_edge_id(fk.cols, fk.ref)
    entries.append({
        "id":
            edge_id,
        "cols":
            list(rest),
        "nullable":
            _rest_is_nullable(
                rest,
                table_schema,
                generation_schema,
                table=landing_table,
                edge_id=edge_id,
                effective_pk=effective_pk,
            ),
        # `edge_roles` calls an edge conditional for SHARING a
        # column with the driving edge, which says nothing about
        # whether its `rest` keys anything: a lookup edge whose
        # rest sits outside the PK distinguishes no child, so it
        # must not inflate the cap (fix wave G1).
        "pk_member":
            bool(pk & set(rest)),
    })
  return entries


def in_set_parent_edges(
    registry: RelationshipRegistry,
    landing_table: str,
    *,
    in_set_names: set[str],
    key_sample_caps: Mapping[tuple[str, ...], int],
    edge_roles: Mapping | None = None,
    keys_per_batch: int = 100,
    table_schema=None,
    generation_schema=None,
    candidate_cap: int = DEFAULT_FK_CANDIDATE_CAP,
    effective_pk: tuple[str, ...] = (),
) -> tuple[FkEdgeSpec, ...]:
  """This table's enforced edges whose parent generates in the same
    job, as composer specs. ``key_sample_caps`` (preflight P4, ADR 0035)
    sizes the parent key sample per edge inside the child's PK; other
    edges keep the composer's floor. ``edge_roles`` (ADR 0036/0037) sets
    each spec's ``mode`` through ``_EDGE_MODES``: the driving edge is
    ``"fanout"``, an edge satisfied by construction through it is
    ``"implied"``, an edge sharing columns with it is ``"conditional"``,
    and one sharing none stays the ADR 0030 ``"side_input"``.

    ``table_schema`` (the LANDING schema) decides each conditional
    edge's ``nullable``, together with ``effective_pk`` — the PK this run
    enforces (`effective_pk_of`), which no key member may NULL-fill (fix
    wave G2); the plan entries read the same two.
    ``candidate_cap`` is ``--fk_candidate_cap``.

    With conditional edges present ``keys_per_batch`` is LOWERED so one
    request never carries more than ~100k candidate values (design §7) —
    the cap itself is the operator's knob and is never touched. Once
    ``cap x edges`` passes that ceiling on its own the bound degenerates
    to one key per request and stops bounding anything, which is a
    `fk_candidate_request_unbounded` WARNING (fix wave G5)."""
  roles = edge_roles or {}
  edges = [
      fk for fk in registry.enforced_edges(landing_table)
      if fk.ref.rsplit(".", 1)[-1] in in_set_names
  ]
  n_conditional = sum(1 for fk in edges if roles.get(fk) == "conditional")
  if n_conditional:
    per_request = candidate_cap * n_conditional
    bound = _MAX_CONDITIONAL_VALUES_PER_REQUEST // per_request
    if bound < 1:
      # Fix wave G5: the bound has STOPPED bounding. The floor below
      # pins it at ONE key per request, and that one key still
      # carries `cap x edges` candidate tuples — past the ceiling,
      # with no ceiling on the cap itself (fix wave F1 reverted
      # clamping it: the clamp collapsed the co-parent choice to a
      # point mass). Nothing is repaired silently here; the knob
      # that did it is named instead.
      log_milestone(
          "fk_candidate_request_unbounded",
          level=logging.WARNING,
          table=landing_table,
          candidate_cap=candidate_cap,
          conditional_edges=n_conditional,
          tuples_per_request=per_request,
          ceiling=_MAX_CONDITIONAL_VALUES_PER_REQUEST,
          note="--fk_candidate_cap x conditional edges already "
          "exceeds the per-request candidate ceiling at ONE key per "
          "request, so keys_per_batch cannot bound the request any "
          "further. Lower --fk_candidate_cap: past the candidates a "
          "co-parent actually holds per shared value it buys no "
          "per-key variety, only request size.",
      )
    keys_per_batch = min(keys_per_batch, max(1, bound))
  specs = []
  for fk in edges:
    role = roles.get(fk, "")
    parent = registry.relations(fk.ref)
    specs.append(
        FkEdgeSpec(
            child_cols=tuple(fk.cols),
            ref_cols=tuple(fk.ref_cols),
            parent_landing=parent_landing_fqn(
                fk.ref, derive_fk_parent_landing(landing_table)),
            # The bare model name — `edge_id` is `(cols)->parent`, so
            # two edges to different parents never share an id.
            parent_table=fk.ref,
            # The parent PK lets the composer skip a Distinct shuffle
            # when the projected tuple already holds it (Task 5) —
            # harmless on every mode, worth millions of rows on a
            # conditional edge's co-parent.
            parent_pk=tuple(parent.pk) if parent else (),
            key_sample_cap=key_sample_caps.get(
                tuple(fk.cols), FK_KEY_SAMPLE_FLOOR),
            mode=_EDGE_MODES.get(role, "side_input"),
            keys_per_batch=keys_per_batch,
            overlap=(registry.edge_overlap(landing_table, fk)
                     if role == "conditional" else ()),
            candidate_cap=candidate_cap,
            nullable=_rest_is_nullable(
                registry.edge_rest(landing_table, fk),
                table_schema,
                generation_schema,
                # `conditional_plan_entries` already named any
                # disagreement; one WARNING per edge, not two.
                warn=False,
                effective_pk=effective_pk,
            ) if role == "conditional" else False,
        ))
  return tuple(specs)


def fk_edge_metadata(
    registry: RelationshipRegistry,
    landing_table: str,
    relations,
    fk_parent_landing: str,
    *,
    in_set_names: set[str],
    edge_roles: Mapping,
) -> tuple[dict, ...]:
  """``GenerationContext.fk_edges`` — one dict per DECLARED edge, the
    worker's ``relational_fk_edge`` milestone source (ADR 0037 §8).

    Every in-set enforced edge carries the DAG path it took (``mode``,
    and ``overlap`` for a conditional edge). An edge outside the launch
    (external parent) or not enforced carries no ``mode`` key at all and
    the worker renders today's ``mode=side_input`` default, so no
    existing log line moves.

    ``edge_roles`` is keyed on the WIDENED edges ``enforced_edges``
    returns (ADR 0036 rev 2), while ``relations.fk`` holds the declared
    ones. A declaration is resolved to its widened form by APPLYING the
    same widening (`enforced_edges` = `widened` over the raw enforced
    edges), never by guessing from a column prefix: a documented
    ``(T)->P`` next to an enforced ``(T,R)->P`` prefix-matched the
    driving edge and claimed `mode=fanout` for an edge the launch never
    draws (review round 1, finding B1)."""
  meta: dict[tuple[str, tuple[str, ...]], dict] = {}
  for fk in registry.enforced_edges(landing_table):
    role = edge_roles.get(fk)
    if role not in _EDGE_MODES or fk.ref.rsplit(".", 1)[-1] not in in_set_names:
      continue
    entry: dict = {"mode": _EDGE_MODES[role]}
    if role == "conditional":
      entry["overlap"] = list(registry.edge_overlap(landing_table, fk))
    meta[(fk.ref, tuple(fk.cols))] = entry

  def _for(fk) -> dict:
    """This DECLARED edge's DAG path, or `{}` — an edge the launch
        does not draw (documented-only, or an out-of-set parent) is
        never stamped, whatever its columns look like."""
    if not fk.enforced or fk.ref.rsplit(".", 1)[-1] not in in_set_names:
      return {}
    # The same widening `enforced_edges` applies, applied once more
    # to THIS declaration — the only way to name the widened edge a
    # declaration became without re-deriving the rule here.
    widened = registry.widened(landing_table, fk)
    return meta.get((widened.ref, tuple(widened.cols)), {})

  return tuple({
      "cols": list(fk.cols),
      "ref": fk.ref,
      "ref_cols": list(fk.ref_cols),
      "enforced": fk.enforced,
      "parent_landing": (parent_landing_fqn(fk.ref, fk_parent_landing)
                         if fk_parent_landing and fk.enforced else ""),
      **_for(fk),
  } for fk in (relations.fk if relations else ()))


def _prepare_table_spec(
    args,
    model_client,
    in_set_landing: frozenset[str] = frozenset(),
    registry: RelationshipRegistry | None = None,
) -> TableSpec:
  """Everything one table needs, driver-side: schema, preflight, FK
    pools (EXTERNAL parents only — in-set parents arrive as in-DAG side
    inputs, ADR 0030), stores, sinks, config. Shared by the
    single-table runner and the single-job relational runner."""
  table_schema, landing_schema = resolve_schemas(args.ddl_uri,
                                                 args.reference_table,
                                                 args.landing_table)
  # ADR 0037 §4 ruling B: the conditional NULL policy reads the SINK's
  # column modes. `table_schema`'s modes mirror the SOURCE (only the
  # description surfaces are overlaid from the landing table), so the
  # two are only the same object when the landing table is unreadable.
  nullable_schema = nullability_schema(landing_schema, table_schema)
  logger.info("Loaded schema for %s (%d columns)", table_schema.fqn,
              len(table_schema.columns))

  # ADR 0029 rev B: mode is descriptive here — activation derives
  # inside _load_reference_and_preflight from the table's relations.
  args.fk_parent_landing, fk_mode = resolve_fk_mode(
      parse_bool_flag(args.generate_fk_relationships),
      args.fk_parent_landing,
  )
  if fk_mode == "isolated":
    log_milestone(
        "fk_generation_disabled",
        level=logging.WARNING,
        table=args.reference_table,
        note="--generate_fk_relationships=false — any declared FK "
        "edges generate from marginals; referential integrity "
        "UNVERIFIED this run",
    )
  registry = registry or RelationshipRegistry()
  in_set_names = {t.rsplit(".", 1)[-1] for t in in_set_landing}
  (
      reference_rows,
      pf,
      fk_pools,
      fk_key_pools,
      source_distinct,
      fanout,
      edge_roles,
  ) = _load_reference_and_preflight(
      args,
      table_schema,
      registry,
      in_set_landing=in_set_landing,
      landing_schema=landing_schema,
  )
  log_relationship_model(args.landing_table, registry, mode=fk_mode)

  # ADR 0036 — a DRIVEN child's row count, batch size and uniqueness
  # mode all derive from the measured source fan-out instead of the
  # launch's flat --num_rows/--uniqueness_mode.
  driven = fanout is not None
  num_rows = resolve_table_rows(
      args.landing_table,
      driven=driven,
      derived_rows=pf.derived_rows,
      launch_rows=args.num_rows,
  )
  batch_size = resolve_batch_size(args.batch_size, num_rows)
  # ADR 0038 — an ADJUSTED table keeps its DECLARED PK for measurement
  # only: `pk.duplicate` is still counted against it (that is the
  # evidence the landing table copies the source's key repeats), it
  # simply stops keying, deduplicating and gating.
  adjusted = pf.adjustments
  pk_measure_columns = adjusted[0].declared_pk if adjusted else ()
  uniqueness_mode = (
      resolve_driven_uniqueness_mode(
          getattr(args, "driven_uniqueness_mode", "streaming"),
          driven=driven,
          identity_cols=tuple(pf.identity_cols),
          adjusted=bool(adjusted),
      ) or args.uniqueness_mode)

  thresholds = resolve_thresholds(args.thresholds_uri, args.env)
  logger.info("Thresholds (env=%s): blocker_failure_ratio=%.4f", thresholds.env,
              thresholds.blocker_failure_ratio)

  embedder_id, embedder_version = embedder_identity(args.embedder_uri)

  rag_chunks_sink = None
  if parse_bool_flag(args.build_rag_layer) and args.rag_chunks_table:
    digest = compute_reference_digest(reference_rows)
    store = BigQueryChunkStore(args.rag_chunks_table)
    if store.exists(digest, embedder_id, embedder_version):
      log_milestone(
          "rag_population_skipped",
          reference_digest=digest[:12],
          embedder_id=embedder_id,
      )
    else:
      rag_chunks_sink = WriteToBigQuery(
          table=args.rag_chunks_table,
          method=WriteToBigQuery.Method.FILE_LOADS,
          write_disposition=BigQueryDisposition.WRITE_APPEND,
          create_disposition=BigQueryDisposition.CREATE_NEVER,
      )

  freetext_pools_store, source_value_store = resolve_pool_layer(
      args, reference_rows)

  config = PipelineConfig(
      table_schema=table_schema,
      engine_name=args.engine,
      model_client=model_client,
      num_rows=num_rows,
      batch_size=batch_size,
      similarity=args.similarity,
      seed=int(args.seed) if str(args.seed).strip() else None,
      run_id=args.run_id,
      identity_columns=pf.identity_cols,
      # ADR 0038: the EFFECTIVE PK — empty on an adjusted table.
      pk_columns=pf.pk_cols,
      pk_measure_columns=pk_measure_columns,
      # `pk.duplicate` is expected on an adjusted table, so it must not
      # weigh on the BLOCKER gate — named in the summary row, never
      # hidden. Every other table's keeps blocking.
      gate_excluded_rules=("pk.duplicate",) if adjusted else (),
      source_repeat_share=(adjusted[0].source_repeat_share
                           if adjusted else None),
      strict_freetext=resolve_engine_strictness(args.client_type),
      model_uri=args.model_uri,
      embedder_uri=args.embedder_uri,
      reference_table=args.reference_table,
      landing_table=args.landing_table,
      thresholds=thresholds,
      # A fake-client (CPU smoke) run produces fake data, so failing the job
      # on the BLOCKER gate is meaningless — keep it informational (the
      # validation_runs row still records the status). Real engines gate.
      fail_on_blocker=resolve_engine_strictness(args.client_type),
      rag_chunks_table=args.rag_chunks_table,
      embedder_id=embedder_id,
      embedder_version=embedder_version,
      pool_pattern_guidance=parse_bool_flag(args.pool_pattern_guidance),
      freetext_pools_table=args.freetext_pools_table,
      pool_seed_strategy=validate_seed_strategy(args.pool_seed_strategy),
      uniqueness_mode=uniqueness_mode,
      fanout=fanout,
      rag_embed_device=resolve_rag_embed_device(model_client),
      freetext_expansion=args.freetext_expansion,
      prompt_constraints=args.prompt_constraints == "on",
      prompt_debug=args.prompt_debug,
      fk_pools=fk_pools,
      fk_key_pools=fk_key_pools,
      log_table_prefix=(args.landing_table.rsplit(".", 1)[-1] if getattr(
          args, "_multi_table_plan", False) or in_set_landing else ""),
      fk_edges=fk_edge_metadata(
          registry,
          args.landing_table,
          pf.relations,
          args.fk_parent_landing,
          in_set_names=in_set_names,
          edge_roles=edge_roles,
      ),
      # The card the launcher printed, carried verbatim to the workers
      # (ADR 0032): the model is resolved ONCE, driver-side, and both
      # logs show the same thing.
      # Pipes and arrows for the worker echo (ADR 0035 rev); the mermaid
      # fence lives in the launcher's own relationship_model entry.
      relationship_card=registry.card(args.landing_table),
      source_distinct=source_distinct,
      # ADR 0023 generate-path seam: B.2 builds pools lazily in workers.
      source_values_table=args.reference_table,
  )

  create_if_not_exists = parse_bool_flag(args.create_if_not_exists)
  landing_write, landing_create = resolve_landing_dispositions(
      args.write_disposition, create_if_not_exists)
  landing_kwargs: dict = {}
  if create_if_not_exists:
    # CREATE_IF_NEEDED must carry the target schema — derived
    # in-pipeline from the resolved TableSchema (WS4 §6c), never
    # hand-provisioned. Must be the load-safe projection: the
    # FILE_LOADS runtime path (vendored apitools `TableFieldSchema`)
    # rejects `maxLength`/`precision`/`scale`/`defaultValueExpression`
    # with an `AttributeError` at load-job time, even though Beam
    # accepts the fuller `derive_bq_schema` dict at graph construction
    # (WS4 final-review CRITICAL-1).
    landing_kwargs["schema"] = derive_bq_load_schema(table_schema)
  log_milestone(
      "landing_sink_config",
      write_disposition=landing_write,
      create_disposition=landing_create,
  )
  landing_sink = WriteToBigQuery(
      table=args.landing_table,
      method=WriteToBigQuery.Method.FILE_LOADS,
      write_disposition=landing_write,
      create_disposition=landing_create,
      **landing_kwargs,
  )
  dlq_sink = WriteToBigQuery(
      table=args.dlq_table,
      method=WriteToBigQuery.Method.FILE_LOADS,
      write_disposition=BigQueryDisposition.WRITE_APPEND,
      create_disposition=BigQueryDisposition.CREATE_NEVER,
  )
  validation_runs_sink = None
  if args.validation_runs_table:
    validation_runs_sink = WriteToBigQuery(
        table=args.validation_runs_table,
        method=WriteToBigQuery.Method.FILE_LOADS,
        write_disposition=BigQueryDisposition.WRITE_APPEND,
        create_disposition=BigQueryDisposition.CREATE_NEVER,
    )

  # In-set enforced edges: parents live in THIS job's spec list — no BQ
  # pool load; the composer wires the parent's landed keys as a side
  # input (ADR 0030). parent_pk is patched in by the relational runner.
  # ADR 0036: a driven child's fanout edge batches parent keys sized
  # to land ~batch_size children per element (the composer's floor of
  # 100 keys/batch otherwise).
  keys_per_batch = (
      min(
          _MAX_KEYS_PER_BATCH,
          max(1, round(batch_size / max(_mean_k(fanout["histogram"]), 1e-6))),
      ) if driven else 100)
  parent_edges = in_set_parent_edges(
      registry,
      args.landing_table,
      in_set_names=in_set_names,
      key_sample_caps=pf.fk_key_sample_caps,
      edge_roles=edge_roles,
      keys_per_batch=keys_per_batch,
      table_schema=nullable_schema,
      generation_schema=table_schema,
      # The payload holds the operator's `--fk_candidate_cap` (fix
      # wave F1), so the specs and the workers read ONE number.
      candidate_cap=(fanout or {}).get("candidate_cap", candidate_cap_of(args)),
      # The PK this run enforces — `pf.pk_cols`, which `effective_pk_of`
      # resolves by the identical rule (fix wave G2) but WITHOUT the
      # ADR 0038 adjustment: reading the registry here would hand the
      # composer a key the source disproved and preflight dropped.
      effective_pk=pf.pk_cols,
  )

  return TableSpec(
      config=config,
      reference_rows=reference_rows,
      landing_sink=landing_sink,
      dlq_sink=dlq_sink,
      validation_runs_sink=validation_runs_sink,
      rag_chunks_sink=rag_chunks_sink,
      freetext_pools_store=freetext_pools_store,
      source_value_store=(source_value_store
                          if freetext_pools_store is not None else None),
      parent_edges=parent_edges,
      adjustments=adjusted,
  )


def _run_one_table(
    args,
    beam_argv: list[str],
    registry: RelationshipRegistry | None = None,
) -> int:
  options = PipelineOptions(beam_argv)
  runner = options.view_as(StandardOptions).runner or "DataflowRunner"
  configure_pipeline_options(
      options,
      runner,
      args.run_id,
      num_workers=resolve_num_workers(getattr(args, "initial_workers", "")),
      autoscaling=getattr(args, "autoscaling", "auto"),
  )
  cross_process = resolve_cross_process(
      runner,
      options.view_as(DebugOptions).experiments)
  log_milestone(
      "sdk_container_topology",
      topology="multi" if cross_process else "single",
      runner=runner,
  )

  logger.info(
      "Building model client (client_type=%s, vllm_dtype=%s, "
      "vllm_max_model_len=%s)", args.client_type, args.vllm_dtype,
      args.vllm_max_model_len)
  model_client = build_model_client(
      args.client_type,
      args.model_uri,
      vllm_dtype=args.vllm_dtype,
      vllm_max_model_len=args.vllm_max_model_len,
      cross_process=cross_process,
  )
  spec = _prepare_table_spec(args, model_client, registry=registry)
  report_model_adjustments([spec], registry or RelationshipRegistry(), options,
                           args.run_id)
  # ADR 0039 — one root table, so the projection is `--num_rows` and
  # nothing else. It is printed anyway: the block is where an operator
  # reads what a launch will generate, and a single-table launch must
  # not be the one shape that stays silent.
  report_row_projection([spec], launch_rows=args.num_rows)

  with beam.Pipeline(options=options) as p:
    result = build_pipeline(
        p,
        reference_rows=spec.reference_rows,
        config=spec.config,
        landing_sink=spec.landing_sink,
        dlq_sink=spec.dlq_sink,
        validation_runs_sink=spec.validation_runs_sink,
        rag_chunks_sink=spec.rag_chunks_sink,
        freetext_pools_store=spec.freetext_pools_store,
        source_value_store=spec.source_value_store,
    )
    logger.info(
        "Pipeline launched: run_id=%s reference_digest=%s",
        result["run_id"],
        result["reference_digest"],
    )
  return 0


def _run_relational_job(
    plan,
    args,
    beam_argv: list[str],
    registry: RelationshipRegistry | None = None,
) -> int:
  """ADR 0030 — scenario 2/3 in ONE Dataflow job: every planned table's
    subgraph in one pipeline, parents-first, children fed by in-DAG
    parent-key side inputs. One worker fleet and one vLLM ignition serve
    all tables; the 911 s launch+boot (measured, ADR 0028 figures) is
    paid once instead of per table."""
  from dataclasses import replace as _dc_replace

  options = PipelineOptions(beam_argv)
  runner = options.view_as(StandardOptions).runner or "DataflowRunner"
  configure_pipeline_options(
      options,
      runner,
      args.run_id,
      num_workers=resolve_num_workers(getattr(args, "initial_workers", "")),
      autoscaling=getattr(args, "autoscaling", "auto"),
  )
  cross_process = resolve_cross_process(
      runner,
      options.view_as(DebugOptions).experiments)
  log_milestone(
      "sdk_container_topology",
      topology="multi" if cross_process else "single",
      runner=runner,
  )

  model_client = build_model_client(
      args.client_type,
      args.model_uri,
      vllm_dtype=args.vllm_dtype,
      vllm_max_model_len=args.vllm_max_model_len,
      cross_process=cross_process,
  )
  in_set = frozenset(r.landing_table for r in plan.runs)
  # The --ddl_uri pin describes the USER'S target table(s) — closure
  # siblings extract live only (2026-08-22 launch: the first PLANNED
  # table, a parent, wrongly inherited the target's pin and logged
  # spurious ddl_pin_drift).
  pin_owners = set(parse_landing_tables(args.landing_table))
  specs = []
  prep_failures: list[tuple[str, str]] = []
  # ADR 0036: a grandchild's derived row count depends on its parent's
  # RESOLVED count (itself possibly derived), not the launch's flat
  # --num_rows — plan order is parents-first, so filling this as each
  # spec lands carries it forward to the next table's preflight.
  rows_by_landing: dict[str, int] = {}
  # ADR 0038 fix H3: and the DISTINCT keys each one lands, which is
  # what its children fan out over — the same number as its rows unless
  # its `pk:` was adjusted away.
  distinct_keys_by_landing: dict[str, int] = {}
  for run in plan.runs:
    table_args = argparse.Namespace(**vars(args))
    table_args.landing_table = run.landing_table
    table_args.reference_table = run.source_table
    table_args.run_id = run.run_id
    table_args.fk_parent_landing = (
        args.fk_parent_landing or run.fk_parent_landing)
    # Underscored on purpose: an internal Namespace channel read back via
    # getattr(), never a CLI flag.
    # pylint: disable-next=protected-access
    table_args._rows_by_landing = rows_by_landing
    # pylint: disable-next=protected-access
    table_args._distinct_keys_by_landing = distinct_keys_by_landing
    if run.landing_table not in pin_owners:
      table_args.ddl_uri = ""  # pin describes the target only
    # Collect-then-fail (2026-08-22 launch lesson): one table's
    # preflight stop must not HIDE the remaining tables' constraint
    # reports and blockers — prep everything, abort once with all.
    try:
      specs.append(
          _prepare_table_spec(
              table_args,
              model_client,
              in_set_landing=in_set,
              registry=registry,
          ))
      rows_by_landing[specs[-1].config.landing_table] = (
          specs[-1].config.num_rows)
      distinct_keys_by_landing[specs[-1].config.landing_table] = (
          distinct_keys_landed(specs[-1].config.num_rows,
                               specs[-1].adjustments))
    except SystemExit as exc:
      logger.error("prep failed for %s: %s", run.landing_table, exc)
      prep_failures.append((run.landing_table, str(exc)))
  if prep_failures:
    summary = "\n".join(f"- {t}: {e}" for t, e in prep_failures)
    raise SystemExit(f"{len(prep_failures)} of {len(plan.runs)} planned tables "
                     f"failed driver-side preflight — nothing was launched:\n"
                     f"{summary}")
  # Patch each edge's parent_pk from the sibling spec so the composer
  # can skip the Distinct shuffle when ref tuple == parent PK.
  pk_by_landing = {
      s.config.landing_table: tuple(s.config.pk_columns) for s in specs
  }
  specs = [
      _dc_replace(
          s,
          parent_edges=tuple(
              _dc_replace(e, parent_pk=pk_by_landing.get(e.parent_landing, ()))
              for e in s.parent_edges),
      )
      for s in specs
  ]
  # ADR 0038 — say what this launch generated with, before it
  # generates anything: the banner, one milestone per adjustment, and
  # the effective model handed back as YAML.
  report_model_adjustments(specs, registry or RelationshipRegistry(), options,
                           args.run_id)
  # ADR 0039 — and say how many rows that model will GENERATE, per
  # table and in total, while there is still time to stop.
  report_row_projection(
      specs,
      launch_rows=args.num_rows,
      rows_by_landing=rows_by_landing,
      keys_by_landing=distinct_keys_by_landing,
  )
  total_edges = sum(len(s.parent_edges) for s in specs)
  log_milestone(
      "relational_single_job",
      tables=len(specs),
      order=",".join(s.config.landing_table.rsplit(".", 1)[-1] for s in specs),
      edges=total_edges,
      edges_detail=",".join(f"{s.config.landing_table.rsplit('.', 1)[-1]}:"
                            f"{len(s.parent_edges)}" for s in specs),
      # ADR 0036 — each table's RESOLVED row count (a driven child's
      # derived count, everyone else's --num_rows), so the launch card
      # answers "how many rows did each table land" without re-deriving
      # fan-out math from the log.
      rows_detail=",".join(
          f"{s.config.landing_table.rsplit('.', 1)[-1]}:{s.config.num_rows}"
          for s in specs),
  )
  if len(specs) > 1 and total_edges == 0:
    log_milestone(
        "relational_closure_no_enforced_edges",
        level=logging.WARNING,
        tables=len(specs),
        note="the model grouped these tables but ZERO enforced "
        "in-set FK edges resolved — every FK column generates from "
        "marginals this run. Read the relationship_model card: `..>` "
        "edges are documented-only (`enforced: false`) and a table "
        "marked [DISABLED] hands out no keys. Flip the flag in "
        "config/relationships to change it.",
    )
  with beam.Pipeline(options=options) as p:
    results = build_relational_pipeline(p, specs)
    logger.info(
        "Relational pipeline launched: %d tables, run_id=%s",
        len(results),
        args.run_id,
    )
  return 0


if __name__ == "__main__":
  sys.exit(main())
