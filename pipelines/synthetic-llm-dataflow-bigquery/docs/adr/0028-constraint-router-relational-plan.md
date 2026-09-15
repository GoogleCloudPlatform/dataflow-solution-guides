# ADR 0028 — Constraint router (program-first sampling) + relational contract in the generation plan

**Status:** ACCEPTED (2026-08-22) — laptop implementation landed (TDD,
1173 tests); R1-c re-run acceptance pending (design doc §6)
**Design:** [`docs/designs/2026-08-22-constraint-router-scale.md`](../designs/2026-08-22-constraint-router-scale.md)
**Figures:** `scripts/doc/make_constraint_router_figures.py` (2 evidence)
**Amends:** [ADR 0026](0026-measurement-first-mask-integrity.md) (constraint-wins-over-expandable, narrowed) · [ADR 0027](0027-verified-wave4-operational-integrity.md) (D6 binary fast-path, fallback replaced)
**Depends on:** [ADR 0021](0021-relational-contract-in-descriptions.md) · [ADR 0024](0024-structured-prompt-constraint-templates.md) · [ADR 0018](0018-parallel-batched-freetext-pools.md)

## Context

The first PK+FK run (`2026-08-21_14_54_30-1966084111444777604`,
B_TABLE, 1M rows) went FAILED_BLOCKER by construction, and its
telemetry generalizes to the fleet-scale question (thousands of tables,
thousands of constrained columns):

1. **PK on a capped pool.** The declared PK is `freetext_llm_pool` with
   an `llm_prompt_constraint`; constrained columns never expand
   (ADR 0026, `engine.py::_draws_from_expansion`) and pools cap at
   `_FREE_TEXT_POOL_MAX = 512`. 1M rows over 512 unique keys ⇒
   999 488 `pk.duplicate` DLQ rows and a BlockerGate raise — discovered
   37 minutes and 1 586 GPU-s after launch. `GenerationContext` carries
   `fk_pools` but no PK: the engine cannot know it is generating a key.
2. **FK declared, silently inactive.** The contract declares a 3-column
   FK, but without `--fk_parent_landing` the launcher skips
   `load_fk_pools` with no log line (`run_pipeline.py:682`). Zero
   `fk.orphan` evaluations — referential integrity unverified, not
   passed.
3. **The binary fallback violates its own clause.** COL_047's clause
   says "never copied from a seed value (privacy-sensitive)"; the
   ADR 0027 D6 fast-path filled its pool from the 19 815-value source
   domain — `copy_ratio_substantive = 1.0` on every landed row.
4. **No clause in the run needed an LLM.** The two pattern clauses
   (24-hex key, UUIDv4) define 1e24–1e36 value spaces and were
   grammar-enforced (format_rejected = 0); the LLM's contribution was
   *negative* — patterned, low-entropy "random" hex with landed
   duplicates — at 80–227 T4-seconds per 512 values. The prose clause
   leaked 12 rejects. All 8 generate workers logged `llm_route_unused`.

## Decision

**D1 — Route each constrained column to the cheapest sufficient
generator, launcher-side.** From parsed clause fields alone: supported
`pattern` ⇒ **Tier P** seeded automaton-walk sampler (prior art:
[exrex](https://github.com/asciimoo/exrex); in-repo prior art:
[Hypothesis `from_regex`](https://hypothesis.readthedocs.io/en/latest/data.html#hypothesis.strategies.from_regex));
binary payload ⇒ **Tier B** byte template (literal prefix +
length-pinned RNG tail); charset/shape prose ⇒ **Tier S** small LLM
seed pool (64) + shape expansion *(deferred — prose clauses keep the
current pool-ladder behavior until R-evidence justifies the change)*;
semantic prose ⇒ **Tier L**, the existing RAG ladder. The route is recorded per column in
`generation_plan` and decided before any worker exists. UUID columns
sample per [RFC 9562](https://www.rfc-editor.org/rfc/rfc9562) §5.4.

**D2 — The relational contract reaches the generation plan.**
`GenerationContext` gains `pk_columns`; preflight verifies that every
PK column's routed generator has unique capacity ≥ `num_rows` and fails
at the launcher otherwise — the 2026-08-21 failure mode becomes a
pre-submission error. This narrows ADR 0026: a constraint still owns
format and still disables *unguided* expansion, but on Tier P/B the
sampler is the enforcement vehicle and cardinality is unbounded.

**D3 — A declared FK is resolved or loudly refused.** `contract.fk`
non-empty without `--fk_parent_landing` is a preflight error; the
explicit escape hatch (`--fk_parent_landing=skip`) emits a
`fk_declared_skipped` WARNING milestone. Silent inactivity is removed.

**D4 — Privacy clauses are enforced, not advisory.** Tier B replaces
the ADR 0027 D6 source-domain fallback: milestone
`freetext_pool_binary_fallback` → `freetext_pool_byte_template`; a
column whose clause carries a never-copy note may never be served
source values — if no template can be derived, preflight fails.

**D5 — Clause authoring standard (fleet scale).** Machine-first field
priority: `pattern` > structured `families` (numeric shares, replacing
prose percentages) > `charset` > `format` prose; `length` pinned when
fixed; clauses are content-addressed by `clause_sha12` and route
decisions/samplers/pools cache per `(clause_sha12, reference_digest)`.
Worked examples land in `DDL_CONTRACT_GUIDE.md`.

**D6 — vLLM feeding for the remaining Tier-S/L work.** Pool prompts are
reordered static-prefix-first (invariant instructions, then
column/constraint/seeds) so
[automatic prefix caching](https://docs.vllm.ai/en/latest/features/automatic_prefix_caching.html)
shares the preamble KV across columns and tables (measured hit rate
73→89% was intra-column only); all Tier-L ladders of a job submit
concurrently instead of sequentially; sampling params are pinned per
request (the served `generation_config.json` silently overrode
defaults); format enforcement stays with
[structured outputs](https://docs.vllm.ai/en/latest/features/structured_outputs.html)
/ xgrammar ([Dong et al. 2024, arXiv:2411.15100](https://arxiv.org/abs/2411.15100)),
prose steers style only. The launcher derives the GPU verdict from the
route table: a job with no Tier-S/L work submits CPU-only
(`llm_route_unused` becomes a prediction, not a post-mortem).

## Alternatives rejected

- **Expand constrained PK pools with digit-run mutation** (report §7
  option 1): keeps the LLM in the loop for values a grammar defines
  exactly; mutation of 512 seeds biases toward seed neighborhoods and
  cannot honor family shares; still burns the pool phase on the GPU.
- **Drop the constraint from PK columns** (report §7 option 2): loses
  the format contract entirely; the column regresses to shape-mix
  quality and the DDL stops documenting the key format.
- **LLM-generate keys with a uniqueness retry loop**: the run's own
  UUID output shows LLM low-entropy patterning; uniqueness pressure
  grows with `num_rows` while a seeded RNG pays O(1) per value.
- **Keep the FK flag optional and silent**: a declared contract whose
  enforcement depends on remembering a launch flag reproduces this
  run's false "0 orphans" reading at every scale.

## Consequences

- The 2026-08-21 blocker class (PK/pool capacity, silent FK) moves from
  a 37-minute GPU run to a launcher-side error; R1-c re-run criteria
  live in the design doc §6.
- GPU demand becomes proportional to *semantic* columns only; runs
  whose constrained columns are all mechanical (this table: 4 of 4)
  bill zero GPU for them.
- `copy_ratio_substantive` on binary columns drops 1.0 → ~0; the
  memorization CRITICAL tracked since ADR 0027 closes.
- New surface to test pure-Python: pattern-subset compiler + weighted
  sampler, byte-template sampler, preflight capacity/FK rules
  (laptop-side, no Beam).
- Engines gain a non-LLM generator family; the `GenerationEngine` seam
  is unchanged (samplers live in `sdfb-core`, DoFn wrappers untouched).
- **Implemented 2026-08-22 (laptop)**: `constraint_sampler.py` (Tier
  P/B), B.1 routing + draw path + PK emitted-set, preflight P4/P6,
  `families` clause key, and the once-per-plan pretty log entries
  (`generation_plan_pretty` + `relational_e2e`, both engines). **B.2
  draw-path routing is a follow-up** (its free-text pools build lazily
  per batch — same seam story as ADR 0023's R5 note); B.2 constrained
  columns keep pool behavior until then.

## Amendment (2026-08-25) — a routed column owns itself, including identity

Run `…-11759075672032343276` routed `B_COL_008` to the Tier-P sampler
(`constraint_sampler_active route=pattern capacity=3.63e+24`) and landed
UUIDv4s in every row: the column is also the table's `identity`, and
`apply_identity_columns` overwrote the router's output after generation.
The declared `pattern` was computed, then discarded.

- **A constraint-driven generator owns its column.** Engines expose
  `constrained_columns` (Tier P/B); the DoFn removes those from identity
  synthesis and logs `identity_constraint_owned`. Engines without a
  router (B.2, any stub) return the empty set and behave exactly as
  before.
- **Identity still means unique.** `_routed_emitted` now tracks identity
  columns as well as PK columns, so a routed identity column is unique
  per run — and the routed path already rejects the source domain
  (`_routed_forbidden`), which is the privacy property identity
  synthesis was introduced for in the first place. The milestone field
  `pk=` is now `unique=`, covering both.

