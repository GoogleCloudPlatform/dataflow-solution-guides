# ADR 0026 — Measurement first, then mask integrity (wave 4: crosscheck sampling, mask tail bucket, numeric source scrub)

**Status:** ACCEPTED (2026-08-20)
**Design:** [`docs/designs/2026-08-20-measurement-and-mask-integrity-wave4.md`](../designs/2026-08-20-measurement-and-mask-integrity-wave4.md)
**Figures:** `scripts/doc/make_wave4_figures.py` (2 evidence + 2 concept)
**Amends:** [ADR 0025](0025-marginal-fidelity-by-construction.md) (E5 evidence weighting; the numeric copy-ratio watch item)

## Context

The 2026-08-20 R1 cold pair (`…05_49_25-7855…` A_TABLE, `…06_28_11-7047…`
B_TABLE, 1M rows each) verified ADR 0025's numeric/categorical fixes
(112/112 columns `decile_ks` ok across both tables; B_TABLE raised zero
`memorization_flags`). Reading the residual findings column-by-column
against each bundle's own exact full-table aggregates split them in two:

1. **Measurement artifacts.** The crosscheck sampled with
   `RAND() < @p LIMIT @lim` (p oversampled 3×) — a storage-order prefix,
   not a sample. Every "mass-flattened/inverted" column (COL_054, COL_053,
   COL_024, COL_034, COL_015's "spurious alpha mass") matched the exact
   full-table marginal within 2 pp. The 2026-08-20 report's backlog #3
   (COL_015 cross-column contamination) is **retracted** — `'REEMBOLSOS T'`
   is a genuine dominant COL_015 source value re-emitted by `head_values`
   at its observed share. Wave 3's row-mass fixes were working; part of
   ADR 0025's §4 evidence was this same artifact.
2. **Four real defects**, one CRITICAL:
   - **COL_009 (INT64, 34,622 source-distinct): substantive copy 0.52.**
     Inverse-CDF interpolation in a dense integer band rounds onto real
     rare values — the risk ADR 0025 explicitly left "watch on R2; extend
     the numeric carve-out only with measured evidence". This run is that
     evidence.
   - **Mask-entropy collapse**: the 1024-mask cap confined draws to
     lexicographically tie-broken (digit-skewed) survivors and renormalized
     their mass by 1/recall (COL_064 recall 0.005; COL_001 0.456 with top
     masks inflated ×2.2–2.4).
   - **D1 (wave-3 regression)**: `_identifier_draw` concatenated the full
     source domain onto the row-weighted evidence — distinct values
     out-voted the sample (dominant mask 89.8% → 8.2% in reproduction) and
     skewed the coverage pivot.
   - **D2 + kind leak**: whole-draw novelty retries migrated mass out of
     saturated mask families (COL_026 0.890 → 0.838), and letter-only
     positions inside `A–F` bound to the hex class, emitting digits 62.5%
     of the time (COL_038's invented `'6C'`/`'F4'` codes).

## Decision

**D1 — the measurement is fixed before the sampler is judged.**
`freetext_crosscheck.py` samples both sides with
`ORDER BY FARM_FINGERPRINT(TO_JSON_STRING(t)) LIMIT n` (deterministic,
storage-order-free — the pattern `e2e_fetch_samples.py` /
`source_synthetic_stats_diff.py` already use, per
[BigQuery hash functions](https://cloud.google.com/bigquery/docs/reference/standard-sql/hash_functions)).
The diff gains `shape_mass_tv` — total-variation distance over the union
of shapes ([Gibbs & Su 2002](https://doi.org/10.1111/j.1751-5823.2002.tb00178.x))
— with a finding threshold and score weight, because presence-only
recall/precision score 1.0 on a fully inverted marginal. `missing_shapes`
falls back to the top missing shapes when everything sits under the 2%
floor, and the sub-floor tail is counted (`missing_shapes_below_floor`).

**D2 — mask tail bucket (Good–Turing).** `build_identifier_artifacts`
keeps the capped row-weighted mask table but moves the dropped mass plus
one unit per kept singleton mask into a tail bucket — the singleton count
is [Good's (1953)](https://doi.org/10.1093/biomet/40.3-4.237) estimator of
unseen-mask mass. Tail draws synthesize per position from observed
character frequencies (evidence = tail rows + domain-only values), so
near-unique-mask columns (UUID v4) keep ~unique masks, literal prefixes
and version/variant nibbles by construction, and rigid-mask columns
(no singletons, nothing dropped) get no tail at all. Count ties break on
crc32, not the mask string — the lexicographic order preferred
digit-front-loaded masks (`'-' < '9' < 'a'`).

**D3 — domain is support, never weight.** The ADR 0023 source domain
travels to `build_identifier_artifacts` as a separate `domain` argument:
it feeds novelty rejection, alphabets/positional evidence and the tail's
support; mask weights and the coverage denominator come from sample rows
only. (Amends ADR 0025 E5, which concatenated it into the evidence rows.)

**D4 — mask-stable novelty retries.** The bucket/mask is drawn once; only
the fill redraws on a collision (8 attempts). A keyspace so saturated
that every fill collides accepts the collision: its values are
k-anonymous by pigeonhole
([Sweeney 2002](https://doi.org/10.1142/S0218488502001648)) and mask mass
beats forced novelty there. `_class_for` additionally refuses any
character class that introduces a kind (digit/upper/lower) the position
never showed.

**D5 — numeric source scrub.** Integral NUMERIC columns whose sample
distinct count clears the memorization floor (>100) fetch their full
domain through the ADR 0023 store (`numeric_source_filter` milestone,
same loud `_absent`/`_error` degradation) and scrub draws whose rounded
value hits a rare source value: ≤2 inverse-CDF redraw rounds, then a ±8
nudge walk to the nearest non-source integer; residuals are kept and
logged (`numeric_source_rejected collisions= nudged= unresolved=`).
Multi-knot values (sample frequency ≥ 2) stay exact — k-anonymous enum
mass, the numeric twin of FREE_TEXT `head_values`.

**D6 — pools are only built where they are drawn from.** Columns whose
draw path is shape-mix expansion skip the LLM ladder entirely
(`freetext_pool_skipped_expandable` per column); the skip predicate IS
the draw predicate (`_draws_from_expansion`), so divergence is
impossible. Columns carrying an `llm_prompt_constraint` never expand —
the constraint's enforcement vehicle is the pool prompt + guided
`pattern`, which expansion silently bypassed.

**D7 — `freetext.copy_fraction` exempts numeric domains.** Dense-integer
collisions are domain-size effects, not pool memorization (the rule's
name says freetext); rows stay visible, tagged `exempt: numeric_domain`,
and the CRITICAL channel remains `memorization_flags` (substantive ≥ 0.3,
k-anon floor). Mirrored in `config/thresholds.yml`.

## Alternatives rejected

- **Raise the 1024 cap** instead of a tail bucket: memory grows with the
  domain and the renormalization defect merely moves; a 210k-mask column
  (COL_064 over its full domain) has no usable cap.
- **`route:"llm"` the UUID/identifier columns** (the run's Step-8
  stop-gap): correct output via guided decoding, but pays an LLM call per
  pool value for columns the mask machinery can now serve for free; the
  schema constraint stays as a belt-and-braces option, not the fix.
- **Reject ALL numeric source collisions** (no multi-knot exemption):
  destroys head fidelity on skewed columns (top numeric identifiers ARE
  re-emitted enum mass) and pushes `top1_delta`/entropy off their ADR 0025
  guarantees.
- **Skip expandable pools without the constraint carve-out**: a `format`
  clause would stay decorative; the carve-out makes the R1-c constraint
  edits (COL_015/COL_019) actually reach the tail draws.

## Consequences

- **B.1 identifier columns** get mask-marginal fidelity beyond the cap
  and guaranteed novelty against the full domain, at the cost of novel
  (but positionally faithful) masks on near-unique-mask columns — the
  crosscheck's shape recall/precision are no longer meaningful there;
  `shape_mass_tv` is the metric to read.
- **COL_009-class columns** trade ≤ a few integer units of marginal
  precision (nudges) for the CRITICAL privacy fix; enum knots unchanged.
- **Cold pool builds shrink** on tables with many expandable columns
  (B_TABLE spent 53% of wall time in PoolTrigger); warm-store semantics
  unchanged (skipped columns simply have no stored pool).
- **B.2 parity debt**: the tail bucket, kind preservation and mask-stable
  retries ride the shared `text_shapes` primitives, so B.2 inherits them;
  the numeric scrub and pool-skip are B.1-side — B.2's twins land with the
  R5 wave (same seam, `FreeTextHook`).
- **Bundle metrics change shape**: crosscheck adds `shape_mass_tv` +
  `missing_shapes_below_floor`; the probe adds `pool_ladder` and the
  `numeric_domain` exemption tag. `make_release_report.py` consumers read
  new keys opportunistically (absent = old bundle).
- Deferred, on record: pool/chunk store rows are still not scoped by
  `source_fqn` (two tables sharing a `reference_digest` could cross-read;
  only reachable via the degenerate empty-sample digest), and store-served
  pools are not re-validated by `_format_gate`. Neither fired in any run;
  both are hardening candidates for the next wave.
