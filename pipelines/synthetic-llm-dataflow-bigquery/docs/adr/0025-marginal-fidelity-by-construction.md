# ADR 0025 — Marginal fidelity by construction (B.1 inverse-CDF, empirical categoricals, positional alphabets, row-mass shape weighting)

**Status:** ACCEPTED (2026-08-20)
**Design:** [`docs/designs/2026-08-20-marginal-fidelity-wave3.md`](../designs/2026-08-20-marginal-fidelity-wave3.md)
**Figures:** `scripts/doc/make_marginal_fidelity_figures.py` (2 evidence + 2 concept)

## Context

The 2026-08-11 WS8 R1 cold-baseline pair (A_TABLE 67 columns / B_TABLE 45
columns, 1M rows each, pipeline-default `similarity=0.5`) was read
column-by-column. Every failing column reduced to five B.1 sampler defects:

1. **Numeric**: the anchored+uniform **value-average**
   (`s·(anchored+jitter) + (1−s)·uniform`) is a convolution — 22 columns at
   decile-KS 0.40–0.90, and banded domains broke structurally (COL_009-class:
   every source value opens `40…`, synthetic landed mid-range).
2. **Categorical**: the similarity blend flattened every skewed enum toward
   uniform at the default (entropy gaps to −0.82; A_TABLE COL_004-class:
   source ~all one currency, synthetic near-uniform over 37).
3. **Identifier mask fills** drew from column-wide class alphabets — fixed
   positional literals (`C2E3` prefixes, RFC 4122 v4 version/variant
   nibbles) were unreachable (COL_001 shape recall 0.38, COL_064 shape
   precision 0.10).
4. **Shape weighting counted distinct values, not rows** — row-mass
   marginals inverted wherever a heavy repeated head met a diverse tail
   (COL_054 digit-delta +0.22; B_TABLE COL_024 92% `99`-mask vs source 32%;
   COL_015 48% hallucinated alpha mass) — and head values were
   double-counted because `_with_head_values` already re-emits them.
5. **Identifier evidence was sample-bound**: the mask table and the novelty
   set saw only the ≤10k reference sample of a 52k-distinct source domain.

ADR 0022 had deliberately assigned inverse-CDF to B.2 ("R5 is the acceptance
run") and left B.1 on the blend; the R1 pair shows the blend is not a
weaker-but-sound baseline — it is structurally wrong for banded and skewed
domains, at any similarity.

## Decision

1. **B.1 numeric = inverse transform sampling through the full sorted
   observed sample** (inverse transform sampling,
   [Devroye 1986, ch. II](http://luc.devroye.org/rnbookindex.html)) — the
   ADR 0022 primitive at sample resolution instead of 11 decile points.
   Draws interpolate between consecutive order statistics: in-range by
   construction, novel-by-interpolation, deciles reproduced at sample
   resolution. `b1_rag/_fidelity.py`.
2. **B.1 categorical = empirical frequency table, exactly** — ADR 0013's
   original contract, matching B.2. Sparsity categories keep their exact
   mass (wave-2 rule, unchanged).
3. **`similarity` stops being a fidelity dial.** It remains the
   retrieval-tightness / LLM-temperature knob (its B.1-RAG meaning); the
   "0 = pure random within schema" semantics for bulk samplers retire.
   Reproducing the reference marginal is the engine's contract, not a
   preference.
4. **Positional alphabets on every mask-table fill**
   (`text_shapes.py::positional_alphabets`): per length bucket (≥2 values),
   each class position draws from the characters observed at that position;
   singletons pin as literals. This fixes fixed prefixes and UUIDv4
   version/variant nibbles generically — no per-kind special cases.
5. **Row-mass shape weighting**: `build_shape_mix` input is rows minus head
   values (B.1 profile; heads are re-emitted separately at exact share),
   `build_mask_table` weights are row occurrences, engine `top_k` rises
   8→32, and the identifier mix-coverage pivot counts only *generative*
   buckets (≥1 class position) — an all-literal bucket can only regenerate
   its observed value verbatim.
6. **Identifier columns consume the ADR 0023 `source_value_store`**: full
   distinct domain fetched once per setup (`identifier_source_filter`
   milestone), feeding both the mask table (recall) and the rejection set
   (novelty against the whole keyspace). Draw artifacts are cached per
   column — domain-sized tables are too heavy to rebuild per batch.
7. **Gate the class that had no gate**: `numeric.decile_ks` becomes a
   post-run catalog rule (warn 0.2 / fail 0.4, `config/thresholds.yml`),
   `freetext.copy_fraction` exempts day-granularity temporal columns
   (tagged, visible, passing), and the crosscheck's `copy_fraction` gains
   the enum-reuse (k ≥ 10) carve-out so it can no longer contradict the
   probe's `copy_ratio_substantive`.

## Alternatives rejected

- **Keep B.1 on the blend until R5 (status quo)** — the blend's failure is
  structural (convolution + outlier-widened uniform), not a fidelity *tier*;
  keeping it makes the R5 A/B comparison "broken vs correct" instead of
  "empirical marginal vs learned joint", which is the comparison worth
  running.
- **11-point decile vector for B.1 (exact ADR 0022 parity)** — B.1 already
  carries the full observed sample on the profile; 11 points would spread up
  to 10% of mass across the outlier-to-band gap that full-sample
  interpolation bounds to ~1/n. B.2 keeps 11 points because its profile
  does not retain raw rows.
- **`similarity`-weighted mixture (probability `1−s` uniform)** — at the 0.5
  default half the mass would still be uniform: KS ≈ 0.45 on banded columns,
  i.e. still failing its own gate. A knob whose default violates the
  engine's contract is not a knob.
- **A `uuid_v4` column kind** (the R1 report's own first suggestion) —
  positional alphabets subsume it with zero per-kind code; the v4 nibble is
  just a pinned position.
- **Per-mask-bucket positional alphabets** (tighter than per-length) —
  memorization pressure rises sharply for small buckets while the
  per-length table already pins every structural literal observed; rejected
  as premature tightening.
- **Constraint-driven mask steering** (`llm_prompt_constraint.prefix` wired
  into the mask sampler) — auto-detection now carries the observed cases;
  adding a schema-side steering channel for the same effect is an
  abstraction without a driving case (CLAUDE.md anti-pattern).

## Consequences

- B.1 numeric/categorical draws change for every existing digest: seeded
  reproducibility holds per build, but cross-build value streams differ.
  R2+ comparisons against the R1 pair are before/after by design.
- Numeric copy-ratio metrics may rise on dense integer domains
  (interpolation rounds onto observed integers when adjacent gaps < 1);
  this is domain density, not memorization — the same carve-out family as
  enum reuse. Watch on R2; extend the numeric carve-out only with measured
  evidence.
- TEMPORAL stays on the blend + now−10y clamp on purpose: the interim
  temporal-age policy (`profile.py::_MAX_TEMPORAL_AGE_YEARS`) owns that
  distribution until per-column DDL contracts supersede it. Bringing
  inverse-CDF to temporal without lifting the copy-gate exemption first
  would redden day-collision metrics.
- B.2 keeps its distinct-weighted `shape_mix` this wave: its coverage pivot
  divides by the deduped pool, so mass and denominator must move together —
  the row plumbing lands with the R5/B.2 work (`b2_library/fidelity.py`
  carries the note).
- Generate workers now issue one `source_value_store.fetch_distinct` per
  identifier column in `setup()` (bounded by the store cap, same cost class
  as the pool-store reads); wholly absent stores keep today's behavior.
- The recommender and E2E-report prompts treat numeric KS, categorical
  entropy, identifier structure, and shape-share drift as **sampler-owned**
  classes: a post-ADR-0025 failure there is a code regression, never a
  schema-constraint recommendation.
