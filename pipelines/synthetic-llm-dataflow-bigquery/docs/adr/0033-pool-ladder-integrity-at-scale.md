# ADR 0033 — Pool-ladder integrity at scale (transient retry, filter-sized targets, format-collapse exit, prose ceiling)

**Status:** ACCEPTED (2026-08-29) — laptop-proven (TDD, 1,332 tests, DirectRunner); the next cold relational launch is the acceptance gate
**Design:** [`docs/designs/2026-08-29-r6-scale-pool-ladder-integrity.md`](../designs/2026-08-29-r6-scale-pool-ladder-integrity.md)
**Evidence:** the R6 relational pair — 2026-08-25 (1M rows/table) and 2026-08-26 (10M rows/table), both `C_TABLE ◄═ A_TABLE` FK-enforced, PK 1.0, **0 orphans at 10M**
**Amends:** [ADR 0018](0018-parallel-batched-freetext-pools.md) (parallel ladders) · [ADR 0022](0022-stats-driven-generation.md) (pool-target tiers) · [ADR 0027](0027-verified-wave4-operational-integrity.md) (`llm_route_unused` semantics)
**Keeps:** [ADR 0020](0020-freetext-pools-as-persisted-artifact.md) · [ADR 0023](0023-source-domain-pool-rejection.md) · [ADR 0024](0024-structured-prompt-constraint-templates.md) · [ADR 0030](0030-single-job-relational-generation.md) / [ADR 0031](0031-joint-fk-key-draws.md)

## Context

The R6 pair is the first relational evidence at scale, and its headline
is clean: PK uniqueness 1.0 on 10M rows per table, referential integrity
verified by an independent full-table join (0 / 10,000,000 orphans),
0 row duplicates, 67/67 + 44/45 stats columns `ok`, no memorization flag.
Both E2E reports (`runs/<job>/_full_report.md`) say "no
BLOCKER/CRITICAL". The worker logs say five things the reports did not:

1. **A bundle failed and was retried in the 1M run.** Four pool ladders
   ran on a thread pool; thread 1's vLLM fit-wait window (6 × 20 s)
   expired 60 s before a sibling embedder released 3.6 GiB and thread 2's
   spawn succeeded. `_build_free_text_pools` collected every future and
   re-raised thread 1's `ModelLenUnfittableError` after the other three
   ladders had finished (564–685 s of T4 work each) — Dataflow re-embedded
   (155 s), re-fetched, and re-ran the missing 70 s ladder eight minutes
   later. The 10M run fit on the first measure (5,712 tokens) by a
   300 MiB margin: on a T4 this is a coin flip.
2. **The one starved pool was sized by the sample.** `A_COL_015` (95 %
   empty) carried 94 distinct values in the 10k reference sample and
   4,022 in the source filter the same setup fetched a moment later; the
   target stayed 94 because the Tier-2 exact count (ADR 0022,
   `--source_stats=exact`) was absent. The 10M recommender doc
   attributed this to "stagnation" — it was the target.
3. **One column burned three ~150 s rounds at 98 % rejection.**
   `A_COL_037` (fixed 31-char) parsed 393 values per run and the format
   gate refused 385/386 of them; its clause shipped a 28-char fictitious
   example the model echoed. The shape fallback then produced the pool.
4. **Prose had no ceiling.** `A_COL_019` is a 35-char fixed-width
   narrative field (source p95 == max == 35); the prose gate passes
   everything, so pool values ran to 50 (1M) and 62 (10M) characters.
5. **64 `llm_route_unused` WARNINGs in a job whose GPU had just built
   those pools** — one per generate-DoFn instance served from the store.
   The milestone meant "no LLM work anywhere"; store-warm is the designed
   steady state (ADR 0020).

## Decision

**D1 — A transient client failure in one ladder thread is retried
in-process, never raised across finished siblings.** `ModelClientTransientError`
(new, `sdfb_core.engines.base`) marks "the client is not usable *yet*";
`ModelLenUnfittableError` subclasses it. `_build_free_text_pools` collects
every future, rebuilds each transient-failed column once, sequentially,
against the client its siblings just used (`freetext_pool_ladder_retried`
WARNING), and raises only if that second attempt fails. The error is never
swallowed into the lax exemplar fallback — a client that never answered is
not "the LLM yielded nothing". The client's fit-wait window grows from
6 to 12 attempts (220 s between first and last measure; 161 s was needed).

**D2 — The source filter's cardinality sizes the pool target when Tier-2
stats are absent.** `_pool_target` resolves `column_distinct` as exact
stats → source-filter size → sample distinct. The filter is fetched
before the target is sized (it was fetched anyway, right after).

**D3 — Two consecutive full-yield rounds with zero in-format values end
the ladder** (`freetext_pool_format_collapse` WARNING, `stagnated=True`,
existing shape fallback). Temperature cannot fix a structural length
mismatch; one escalation retry is kept.

**D4 — Clause examples are preflighted through the column's own format
gate** (`prompt_constraint_example_off_format` WARNING with `example_len`
and `gate_lengths`) before a round is spent. The example is kept — the
operator authored it — but the log says why the model will echo the
wrong length. Prose columns (no gate) are exempt.

**D5 — Prose candidates are clamped to a fixed-width source ceiling.**
`text_shapes.length_ceiling`: over substantive values, p95 == max with a
real spread below it (`max − p05 ≥ max(4, max/4)`) is a wall the
distribution ran into; candidates longer than it are truncated (the way
the source truncates), *before* the novelty check, and counted in
`freetext_pool_length_clamped`. Narrow bands and lone maxima are not
ceilings. Identifier-ish columns keep their length-bucket gate unchanged.

**D6 — Store-warm setups log `freetext_pools_warm` (INFO);
`llm_route_unused` (WARNING) keeps its ADR 0027 meaning** — no LLM-derived
pool at all in this setup (all expandable / typed / binary routes).

**D7 — Dataflow network-tag names are redaction identifiers.**
`scripts/e2e/redaction.py` maps every tag in `use_network_tags=` /
`use_network_tags_for_flex_templates=` to `NETWORK_TAG_n`; the 10M oss
bundle had leaked them through the `experiments` string.

## Consequences

- A lost fit race on a contended T4 costs one 70 s in-process ladder
  instead of a bundle retry (re-embed + store fetches + the ladder, ~8 min
  in the 1M run) — and can no longer discard finished sibling work.
- Sparse free-text columns get the pool their source cardinality
  supports (`A_COL_015`: 94 → 512) without the `--source_stats=exact`
  tier, which stays the authoritative Tier 2 when present.
- A column whose clause example is off-format costs one round (~150 s on
  T4) instead of three, and the operator is told in the worker log — the
  `llm_prompt_constraint_recommender` prompt and `DDL_CONTRACT_GUIDE`
  now require examples to sit in the column's observed length bucket.
- Fixed-width prose fields keep their width; free prose is untouched.
- Generate workers stop crying wolf: 64 WARNINGs become 64 INFO lines
  with the count of warm columns; a real "GPU idle" run still warns.
- What this ADR does **not** do: it does not move the generate stage off
  GPU workers (the 10M job used vLLM for ~9 of 93 minutes and billed 272
  GPU-minutes — the CPU/GPU split of the RUN_PLAYBOOK cost note remains
  the next architecture step); it does not raise `FREE_TEXT_POOL_MAX`
  (512 is a GPU-time decision, not a fidelity defect); it does not touch
  the FK-column marginal trade-off (`C_COL_007` warn at 10M, ADR 0031).
- Laptop-verified only. The acceptance gate is the next cold relational
  launch: `freetext_pool_ladder_retried` absent or followed by
  `freetext_pool_built` for the same column; `A_COL_015`-class
  `target=512`; `A_COL_037`-class `attempts=2` + `format_collapse`;
  `A_COL_019`-class `len_max == source max` in the crosscheck;
  `freetext_pools_warm` on every generate setup, zero `llm_route_unused`.
