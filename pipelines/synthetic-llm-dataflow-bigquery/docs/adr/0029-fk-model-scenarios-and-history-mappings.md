# ADR 0029 — FK-model generation scenarios, visual relational logging, history mappings

**Status:** ACCEPTED (2026-08-22, rev B same day) — Stage 1 implemented (TDD); rev B simplifies the launch UX after owner feedback (minimal inputs, derived activation); Stage 2 (single-job multi-table DAG) deferred to its own ADR after run evidence
**Design:** [`docs/designs/2026-08-22-fk-model-generation-scenarios.md`](../designs/2026-08-22-fk-model-generation-scenarios.md)
**Depends on:** [ADR 0021](0021-relational-contract-in-descriptions.md) · [ADR 0028](0028-constraint-router-relational-plan.md)
**Scope note:** opens M2 multi-table territory on the owner's direction (CLAUDE.md constraint 5); Stage 1 stays on ADR 0021's parent-first mechanism.

## Context

The first PK+FK run (ADR 0028 context) showed a declared FK silently
inactive; the production-shaped target is a 6-table relational model
(`docs/assets/fk_relationship_example.{png,tf}`, not published /
gitignored: composite PK/FKs, one
join key absent from every DDL) plus dozens of unrelated tables to
follow. Three gaps: (1) no user-facing switch between relational and
isolated generation, and no log surface showing the relationship model a
run actually resolved; (2) multi-table orchestration was
sequential-only, with the FK graph logic private to `run_tableset.py`
and blind to landing-dataset-style refs; (3) report redaction invented
fresh `COL_NNN` aliases per job, so cross-run comparisons and diagrams
could not stay consistent, and every report re-derived the relationship
drawing from scratch.

## Decision

**D1 (rev B) — two inputs describe every scenario**: `landing_table`
(one FQN or a comma-separated list) + `generate_fk_relationships`
(default `true`); everything else derives. The scenarios:

1. *one table + `false`* — that table only; declared enforced edges
   ignored LOUDLY (`fk_generation_disabled` + plan warning).
2. *one table + `true`* — no declared relationships in its component ⇒
   behaves exactly like 1, zero friction; relationships ⇒ the launcher
   expands the launch to the table's whole connected component
   (informational edges count for GROUPING, never for ORDER) and runs
   it parents-first, per-table run_ids suffixed, one
   `launch_scenario` milestone stating the plan.
3. *many tables + `false`* — each independently, given order (the
   dozens-of-unrelated-tables path). With `true`: union of components,
   deduped, ordered.

**`fk_parent_landing` is no longer a user input**: parents land in the
`landing_table`'s own dataset, so it derives
(`derive_fk_parent_landing`); the CLI/Param stays as an EXPERT override
for cross-dataset parents only. The P6 preflight refusal and the
`skip` sentinel are REMOVED — activation is verified where pools load
(`assert_fk_pools_nonempty`: an unlanded/empty parent is a loud,
actionable stop naming the missing refs). Component membership comes
from a landing-dataset contract scan (offline injectable via
`--fk_contracts_json`); a scan failure degrades loudly
(`fk_discovery_unavailable`) to single-target planning, never blocking
a run that used to work.

**D2 — one FK-model definition.** `sdfb_core/contracts/fk_model.py`
owns the graph (nodes, edges, external parents, parents-first levels,
`model_sha12`, mermaid renderer); `run_tableset`, the launcher and the
workers all render from it. Ref resolution falls back to unique
table-name match (contracts name refs by the LANDING dataset; sets list
SOURCE FQNs — the prior suffix-only match silently dropped such edges).

**D3 — informational FK edges.** `ForeignKey.informational: true`
declares a relationship whose join key is absent from the DDL
(JOIN_KEY-class): drawn dashed in every diagram, excluded from P2/P3/P6,
FK pools, orphan rules, and wave ordering. Visibility without
unenforceable promises.

**D4 — the model is visible in Dataflow logs as mermaid.**
`fk_model_pretty` (via `log_milestone_text`: greppable header carrying
`model_sha12`, pasteable `flowchart` body) launcher-side after preflight
and worker-side next to `relational_e2e`, under the once-per-plan guard.

**D5 — waves, not just order.** `run_tableset.py` plans parents-first
waves from the model; `--max-parallel` runs a wave's FK-independent
tables concurrently (bounded by quota), waves stay sequential, first
failure aborts the remainder. `--emit-trigger-configs` writes the
ordered Airflow confs for the Composer path (the DAG itself stays
single-table). The set's model lands as
`runs/fk_models/<sha12>.mmd`.

**D6 — one persistent alias registry replaces per-job `mapping.json`.**
`runs/history_mappings_replacement.json` (LOCAL-ONLY decode
key; the tree is gitignored): prefixes `A…Z, AA, …` in first-arrival
order, columns `<PREFIX>_COL_NNN` in DDL order, aliases immutable,
out-of-DDL fields `retained`. `scripts/e2e/history_mappings.py` owns
assignment; `e2e_bundle_export.py --history-mappings` presets the
redaction mapping from it (presets win over role naming) and stops
writing `mapping.json`. Reports embed `fk_models/<sha>.mmd` verbatim
when the sha matches (prompt §5.5) — diagrams are recycled, never
redrawn.

## Alternatives rejected

- **Single-job multi-table DAG now**: the biggest boot-amortization win
  (911 s boot vs 353 s generation, measured), but a major DAG-shape
  change with zero run evidence; ADR 0021 chose per-table jobs for
  failure isolation. Staged: design sketch in the doc (§5), own ADR
  after Stage-1 evidence.
- **Enforcing JOIN_KEY-style edges via synthesized join columns**: the
  column does not exist in the schema; inventing it would change the
  landing contract. Documentation-only edges cover the need.
- **Per-model mapping files (one registry per FK model)**: unrelated
  tables arrive continuously; a single first-arrival sequence avoids
  prefix collisions across models and keeps "dozens of new tables"
  append-only.
- **Rendering diagrams as PNGs in logs**: Cloud Logging is text;
  mermaid source is diffable, greppable, and pasteable — and the report
  layer already renders mermaid.

## Consequences

- Every run states its relational mode and shows its resolved model;
  "0 orphans" can no longer be misread when FK generation was off.
- The 6-table model runs as 3 waves (`[A,C] → [B,D,E] → [F]`) with up
  to 3 parallel jobs mid-wave; trigger-conf emission gives Composer launches the
  same order without new Composer machinery.
- Report aliases become stable across all future runs; the exporter's
  role-based `PK_COL`/`ID_COL` naming is legacy (still used without the
  registry).
- New surfaces tested laptop-side: fk_model graph/mermaid/levels,
  informational enforcement skips, flag resolution, waves/abort,
  trigger-conf emission, registry assignment/rollover/adopt, preset
  redaction.
- Stage 2 (side-input parent keys, one boot per model) is the recorded
  follow-up, gated on Stage-1 run measurements.
