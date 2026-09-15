# ADR 0031 — Referential integrity by construction: joint FK key draws

**Status:** ACCEPTED (2026-08-23) — DirectRunner-proven (TDD, 1290+ tests); the R7-FK Dataflow pair is the acceptance gate
**Design:** [`docs/designs/2026-08-23-referential-integrity-joint-fk-draws.md`](../designs/2026-08-23-referential-integrity-joint-fk-draws.md)
**Depends on:** [ADR 0030](0030-single-job-relational-generation.md) (in-DAG parent-key handoff) · [ADR 0029](0029-fk-model-scenarios-and-history-mappings.md) (informational edges) · [ADR 0021](0021-relational-contract-in-descriptions.md) (the contract) · [ADR 0025](0025-marginal-fidelity-by-construction.md)
**Supersedes:** the per-column FK pool semantics of ADR 0021 D-pools and ADR 0030 D2 (both recorded composite independence as a v1 limitation)

## Context

The first two-table single-job run
(`2026-08-23_05_12_06-966936752936039329`) landed 1M rows per table with
0 DLQ, unique PKs on both tables, every numeric decile-KS inside the
ADR 0025 gate — and **817,627 of 1,000,000 child rows (81.8%)
referencing a `(COL_005,COL_006,COL_008)` tuple that does not exist in
the parent**, while reporting `status=PASSED`.

Two causes, independent of each other:

1. The declared edge carried `"informational": true`. Per ADR 0029 that
   means display-only, so nothing was ever going to be enforced — yet
   the closure still grouped both tables into one 49-minute job on the
   strength of that edge, and the only signal was a WARNING emitted
   after the graph was built.
2. Even enforced, it would have been worse. FK pools were **per-column**
   and drawn independently, so a composite edge assembles combinations
   the parent never held. From the run's own cardinalities the hit rate
   is at most `1,000,000 / (1 × 1,664 × 22,345) = 2.69%` — i.e. ≥97.3%
   orphans, against the 81.8% the unenforced marginal draw produced.

No rule anywhere scored referential integrity, so neither cause was
visible to the gate.

## Decision

**D1 — the tuple is the unit of the draw.** FK pools carry whole parent
key TUPLES end to end: `GenerationContext.fk_key_pools` =
`[{"cols": [...], "keys": [(v, …), …]}, …]`, produced either by the
in-DAG side input (same-job parent, ADR 0030) or the driver-side BQ read
(`io/fk_pools.load_fk_key_pools`, already-landed parent). Both engines
draw one tuple per row per edge and transpose it onto its columns
(`sdfb_core/engines/fk_keys.py`, shared by B.1 and B.2). Per-column
views survive only as plan/preflight metadata. Orphans become
structurally impossible, at every edge width.

**D2 — the draw is weighted to the child's own marginals, by fitting.**
Uniform-over-keys would buy integrity by destroying the child's column
distributions; the naive product of the child's per-column shares,
restricted and renormalized, distorts them (a value held in many parent
tuples collects mass from all of them). Weights come from **iterative
proportional fitting** ([Deming & Stephan 1940](https://doi.org/10.1214/aoms/1177731829))
over the parent's key tuples until each column's induced marginal
matches the child's observed marginal — the I-projection of the child's
distribution onto the parent's key set. Per-column targets carry a
**Good–Turing floor** ([Good 1953](https://doi.org/10.1093/biomet/40.3-4.237))
so parent values the child sample never showed stay reachable.
Concept-figure measurement: total variation to target 0.477 (uniform) →
0.013 (fitted).

**D3 — NULL FK tuples are preserved, not filled.** A NULL reference
means "no parent" (SQL MATCH SIMPLE), so the child's observed
null-tuple rate is reproduced, all-or-nothing per tuple, and such rows
are exempt from the orphan rule. NULL-bearing PARENT keys are dropped
from the pool on both paths (SQL equality never matches NULL, so they
are not referenceable). v1 forced `null_fraction=0.0` on FK columns —
that was a marginal defect as well as a semantic one.

**D4 — `fk.orphan` is a BLOCKER rule, checked in-DAG.**
`EnforceFkIntegrityDoFn` sits between Generate and ValidateRecord for
any table with enforced edges, holds the same key set the child sampled
from, and diverts non-members to the DLQ (`rule_id: fk.orphan`,
`config/thresholds.yml`, threshold 0, dimension `consistency`). It can
only fire on a generator regression — which is exactly its job: it makes
"0 orphans" a MEASURED per-run fact in `validation_runs` instead of a
claim about the code. Tables with no enforced edge keep their DAG shape.

**D5 — the launch says what it will enforce, before the GPU spends.**
`fk_enforcement_summary` (`sdfb_core/contracts/fk_enforcement.py`,
logged by the launcher) states enforced vs informational counts per
table and, for each informational edge whose columns exist on BOTH
sides, prints `ENFORCEABLE` plus the exact contract edit. It logs at
WARNING when a launch enforces nothing it could have enforced. The
enforceability test is derived, never guessed: a parent whose schema
this launch did not resolve is reported as unknown, and a genuine
DDL-absent join key (the ADR 0029 use case) is reported as correctly
display-only. Landing-dataset discovery now captures column sets from
the same `get_table` call it already made for descriptions.

**D6 — the 100k key cap is announced** (sized per edge since [ADR 0035](0035-pk-capacity-fk-bound-members.md))**.** `fk_key_pool_capped` (WARNING)
fires when a parent's distinct key count reaches the side-input sample
cap, naming the consequence: the child references a uniform sample of
parents, so its FK distinct count cannot exceed the cap. No silent
truncation.

## Alternatives considered

- **Post-hoc repair join.** Generate freely, then join the child against
  the parent and rewrite non-matching keys. Costs a full shuffle of the
  child (hundreds of millions of rows), and "repair" is just the joint
  draw done later and more expensively.
- **Rejection sampling against the key set.** Draw per column, resample
  on miss. At 2.7% acceptance this is ~37 draws per row, and it still
  distorts the marginals in an uncontrolled direction.
- **Uniform joint draw** (no fitting). Simplest correct-on-integrity
  option; rejected because it regresses the ADR 0025 marginals the
  previous three waves earned — measured TV 0.477 vs 0.013 on the
  concept case.
- **Raising the side-input cap.** A 5M-tuple broadcast is hundreds of MB
  per worker. The scaling answer is a co-partitioned shuffle join, kept
  as the recorded escape hatch (design §6) until a run needs it.
- **Refusing to group tables that share only informational edges.**
  Would have saved this run's parent generation, but reverses ADR 0029's
  deliberate choice that "launching any member means the whole model".
  D5 makes the trade-off visible instead of reversing it.

## Consequences

- Composite FK edges are now first-class: `ref_cols` need not be the
  parent's full PK — any projection works, because the pool takes
  `DISTINCT` over exactly those columns. The 2026-08-23 edge (3 of the
  parent's 5 PK columns) is enforceable as declared, once the
  `informational` flag is removed.
- **Operator action required for the next run**: the real (non-example) contract on
  the child table must drop `"informational": true` from its FK entry.
  Until it does, the run behaves exactly as before — and now says so at
  launch, loudly.
- FK columns bypass the per-column samplers entirely (they were being
  sampled and then overwritten); they also no longer reach the free-text
  ladder, which is a small GPU saving on identifier-shaped FK columns.
- Cost measured on a 100k-key, 3-column pool: 4.7 s fit once per engine
  setup, 1.1 s of draw per 1M rows. Invisible beside a ~400 s pool build.
- A run whose FK pool is empty still raises loudly (ADR 0030 D2
  behaviour, now per edge).
- The DLQ gains a fifth failure class; `validation_runs.dlq_by_rule` may
  now carry `fk.orphan`, and the blocker ratio counts it.
- Deferred (unchanged by this ADR): Tier S constraint routing, B.2
  draw-path constraint routing, per-child cancellation under shared
  fate, and the co-partitioned join for parents beyond the 100k cap.
