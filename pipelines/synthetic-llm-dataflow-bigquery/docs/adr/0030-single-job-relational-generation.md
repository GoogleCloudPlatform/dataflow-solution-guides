# ADR 0030 — Single-job relational generation: one Dataflow job, in-DAG FK key handoff

**Status:** ACCEPTED (2026-08-22) — DirectRunner-proven (TDD, 1240+ tests); Dataflow run evidence pending (the scenario-2 R-series is the acceptance gate)
**Design:** [`docs/designs/2026-08-22-fk-model-generation-scenarios.md`](../designs/2026-08-22-fk-model-generation-scenarios.md) §5 (now implemented)
**Depends on:** [ADR 0029](0029-fk-model-scenarios-and-history-mappings.md) (scenario planner) · [ADR 0021](0021-relational-contract-in-descriptions.md) · [ADR 0028](0028-constraint-router-relational-plan.md)
**Supersedes in part:** ADR 0029 D5's sequential-jobs execution — now the `sequential_jobs` fallback mode.

## Context

Scenario 2 (one landing table whose contract chains a relational model)
executed as N sequential Dataflow jobs, paying the measured **911 s
launch+boot against 353 s of generation** (ADR 0028 figures,
`make_constraint_router_figures.py::PHASES`) once PER TABLE — ~91 min of
pure overhead on the 6-table model — and igniting a fresh vLLM fleet
each time. The owner's directive: one Dataflow execution must carry the
whole model (and multi-table independent sets), at
dozens-to-hundreds-of-millions of rows, with generation, RAG chunking,
free-text pools, validation and DLQ all per table — and the logs must
make every configuration and every table's columns unambiguous.

## Decision

**D1 — one pipeline, N label-namespaced subgraphs.**
`build_relational_pipeline(p, [TableSpec…])` composes each table's FULL
existing subgraph (pool branch, RAG branch, generate, record/Pandera
validation, EnforceUniqueness, DLQ, BlockerGate, sinks) onto one Beam
pipeline, every transform label prefixed by the landing table name.
One worker fleet and ONE vLLM server per worker serve every table's
LLM work (`VLLMModelClient` is port-idempotent per worker; the ADR 0028
router already minimizes which columns need it).

**D2 — parent keys are an in-DAG side input.** A child's enforced FK
edge consumes its parent's LANDED (post-EnforceUniqueness) rows:
ref-column tuples → `Distinct` (skipped when the ref tuple IS the
parent PK — already unique) → `Sample.FixedSizeGlobally(100 000)`
(mirrors `io/fk_pools._DEFAULT_LIMIT`) → an `AsSingleton`
`{child_col: (values…)}` side input
([Beam side inputs](https://beam.apache.org/documentation/programming-guide/#side-inputs)).
The side input is also the ordering barrier: the runner cannot schedule
child generation before the parent's keys exist — parents-first without
`Wait.on` or BQ round-trips, referential integrity by construction.
Side inputs are process()-scoped, so the child's engine build defers to
the first bundle (`GenerateRecordsDoFn.expect_fk_side`; the
setup()-builds-engines rule is relaxed by exactly one hop, still
once-per-instance). An EMPTY parent pool raises loudly — a child never
silently degrades to marginals.

**D3 — routing.** `--multi_table_mode=single_job` (default; CLI +
Composer Param + Flex metadata) sends any multi-table plan — a
scenario-2 closure or independent scenario-3 sets — through
`_run_relational_job`; `sequential_jobs` keeps the ADR 0029 per-table
loop as the fallback. Driver prep is one shared seam
(`_prepare_table_spec`); in-set parents skip the BQ FK-pool load
entirely (their keys are in-DAG), external parents keep the ADR 0021
landed-read + loud empty check.

**D4 — execution visibility.** (a) `launch_config`: ONE indent-2 block
logging every arg plus the resolved plan (scenario, table order,
run_ids, derived FK activation, warnings) — pasted `_full_report.md` /
`worker_logs.jsonl` answer "what was enabled?" without arg
archaeology. (b) Every engine/DoFn milestone in a multi-table run
carries `table=<LANDING_NAME>` via a ContextVar scope (DoFn-set;
propagated into pool-ladder threads by executor initializer). (c)
Pretty payloads (`generation_plan_pretty`, `relational_e2e`) qualify
column keys as `<LANDING_NAME>.<col>` when multiple tables run —
mechanical oss/ replacements, zero ambiguity. (d) Per-table run_ids
(`<base>-NN-<table>`) keep DLQ and `validation_runs` rows attributable
with no schema change. `generation_plan` remains the per-table point of
truth for what generates (numeric / temporal / categorical /
shaped_identifier / freetext ± constraints), and
`prompt_constraints_found` stays once-per-table-per-column under the
existing once-guard.

## Alternatives rejected

- **Sequential jobs (status quo)**: N× the 911 s boot + N× vLLM
  ignition; kept only as the fallback mode.
- **`Wait.on` + BQ landed reads between stages in one job**: pays a
  write-then-read round-trip per edge and couples stages to load-job
  timing; the side input is strictly simpler and stronger.
- **Joint FK tuple side inputs**: the in-DAG sampler sees aligned
  tuples, so joint draws are now CHEAP to add — but engines draw
  per-column today (recorded v1 semantics, `io/fk_pools.py`); changing
  the draw math belongs to its own evidence-backed follow-up.
- **Per-table Dataflow templates fan-out from the launcher**
  (multi-job-from-one-launcher): unvalidated flex-launcher platform
  behavior; the single pipeline avoids the question entirely.

## Consequences

- One boot, one ignition, shared prefix cache across every table's
  pool builds; the wave parallelism of ADR 0029 becomes runner-level
  parallelism inside one job.
- **Shared fate**: any table's BlockerGate raise fails the whole job
  (FILE_LOADS commits are per-load-job — partial landings possible on
  failure, as today). Per-child cancellation needs Stage-3 work.
- Composite FKs keep per-column independence (v1); single-column edges
  are exact — the child samples only landed parent keys.
- DirectRunner-proven end to end (child FK containment asserted against
  disjoint reference values); T4/L4 Dataflow evidence is the acceptance
  gate before this becomes the recommended production path.
- `runs` interpreters gain three new anchors:
  `launch_config`, `relational_single_job`, and `table=`-tagged
  milestones.
- **Amends ADR 0028 P4** (2026-08-22 first single-job launch evidence):
  the PK capacity check is now TUPLE-product-aware and route-aware — a
  composite PK passes when any member's generator is unbounded, and a
  non-STRING member with a cosmetic clause (A_TABLE's numeric
  `A_COL_002`, `examples=['20']`) no longer reads as a 512-value
  pool; enum `values` clauses contribute their domain size. The
  2026-08-21 single-capped-PK failure mode still stops identically.
