# ADR 0021 — Relational metadata as description-embedded JSON; parent-first multi-table

**Status:** SUPERSEDED (2026-08-24) by [ADR 0032](0032-relationships-as-config.md) — relationships moved to `config/relationships/*.yaml`; the `{"sdfb": 1, …}` table-description contract is no longer read by anything. Kept for the rationale: why the contract ever lived in descriptions (BQ constraints are unenforced, the Terraform module exposes no PK block).
**Was:** ACCEPTED (2026-08-05)
**Design:** the original planning spec is not published; the superseding design is [`docs/designs/2026-08-24-relationships-as-config.md`](../designs/2026-08-24-relationships-as-config.md)

## Context

The BigQuery Terraform module in use could not declare real PK/FK
constraints, and multi-table synthetic generation (ROADMAP M2) needs exactly
that metadata: which columns key a table, which reference which parents.
The table `description` is the one metadata channel Terraform owns end to
end. The extractor already parsed a legacy `PRIMARY KEY: a, b` line out of
descriptions, proving the channel works.

## Decision

1. **A versioned JSON contract lives in the table description**, marker key
   `"sdfb"`:
   `{"sdfb": 1, "pk": [...], "fk": [{"cols": [...], "ref": "dataset.table",
   "ref_cols": [...]}], "identity": [...]}`. Prose around the object is
   fine; Terraform composes it with `jsonencode` (never hand-written —
   BQ caps descriptions at 16,384 chars). Column descriptions carry
   per-column keys the same way (`llm_prompt_constraint` first).
2. **Parsing is tolerant to absence, loud to corruption**: no marker ⇒
   pipeline behaves exactly as before; a marked-but-invalid object fails
   the driver preflight (`cli/preflight.py`, checks P1-P5) before any graph
   is built. Explicit `--pk_cols`/`--identity_cols` always override the
   contract, logged as `relational_contract_overridden`.
3. **Multi-table v1 is parent-first sequential** (`scripts/run_tableset.py`):
   topo-sort the set by FK edges, run the proven single-table DAG per table,
   children sample FK columns from the parents' LANDED synthetic keys
   (`io/fk_pools.py` → `GenerationContext.fk_pools` → uniform categorical in
   both engines). No cross-table Beam graph, no new barriers, per-table
   failure isolation.

## Consequences

- **Enables:** 6-7-table runs without hand-curated flags; referential
  integrity by construction (FK values ⊆ parent keys); `source_table_stats`
  rows annotated with `is_pk`/`is_fk`; a single parser for future
  per-column metadata (temporal floors, enum whitelists).
- **Costs:** N Dataflow submissions per set (§11-measured: ~12.6 min warm
  per 1M-row table); composite FKs draw per-column, not per-tuple —
  cross-column co-occurrence is NOT guaranteed in v1 (recorded limitation,
  joint draws are the M2 follow-up); the level-parallel scheduler (spec
  Option 3) is deferred.
- **Forbids:** silently ignoring a malformed marked contract; auto-deriving
  FK edges from column-name heuristics; a mega-DAG bridging tables with
  side-input barriers (revisit only after WS7 per-column pool fan-out).
