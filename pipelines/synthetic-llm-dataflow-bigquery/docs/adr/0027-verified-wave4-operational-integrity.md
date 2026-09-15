# ADR 0027 — Wave 4 verified; operational integrity (build stamp, DDL-pin drift guard, source-side k-anonymity, nudge-first scrub, head TV, binary fast-path)

**Status:** ACCEPTED (2026-08-21)
**Design:** [`docs/designs/2026-08-20-measurement-and-mask-integrity-wave4.md`](../designs/2026-08-20-measurement-and-mask-integrity-wave4.md) §6 (verification)
**Figures:** `scripts/doc/make_wave4_verification_figures.py` (2 evidence)
**Amends:** [ADR 0026](0026-measurement-first-mask-integrity.md) (scrub order, k-anon source, TV metric)

## Context

The 2026-08-20/21 four-run cycle (two wave-4 cold pairs on A_TABLE +
B_TABLE, 1M rows each; runs 3–4 with `llm_prompt_constraint`
recommendations applied to BigQuery metadata) measured every ADR 0026
acceptance criterion — and its own report mis-read three of them, which
is itself the finding:

1. **Wave 4 verified.** COL_009 substantive copy 0.522 → 0.253,
   *identical on both cold runs* (0.2531 / 0.2538) — the report called it
   "sampling variance, not a fix" because nothing in the logs said which
   build a job ran. COL_064's shape plateau collapsed 0.25% → 0.045% per
   shape and COL_001's top-mask share hit source parity (0.485 vs 0.475)
   — the report called both "still broken" from recall, the metric ADR
   0026 already retired for that class. False `copy_fraction` BLOCKERs:
   16 → 2.
2. **The constraint edits never reached any launch.** Zero
   `prompt_constraints_found` milestones in all four jobs: the launches
   consume a pinned `--ddl_uri` artifact extracted before the BigQuery
   description edits (WS4 §6b precedence, working as designed) — and
   nothing said so.
3. **The scrub's residual anatomy** (worker telemetry,
   `numeric_source_rejected`): COL_009 ≈547 collisions/batch, 380
   resolved by redraw, ~142 unresolved (dense neighborhoods). The
   measured 0.253 sits above the ~0.14 telemetry residual because of
   PSEUDO multi-knots — values seen ≥2× in the 10k sample that are still
   rare in the full source (at ~21× subsampling, sample-freq 2 does not
   imply source-freq ≥ 10). And redraw-first REDISTRIBUTES rejected mass:
   COL_047 (a version-number enum with 197 sparse-neighborhood
   collisions/batch, 190 redrawn) paid decile-KS 0.038 → 0.166 for a
   privacy gain its k-anonymity floor mostly didn't need.
4. **A B_TABLE cold run never ignited vLLM** — all 13 tracked free-text
   columns resolved `expandable` (deterministic ADR 0026 E5 behavior,
   mis-filed as "run-to-run non-determinism"), billing ~28 idle
   GPU-minutes; A_TABLE's one remaining ladder column is the binary
   COL_048, burning 8.6 min of LLM calls that are format-rejected en
   masse.
5. **`shape_mass_tv` saturates on near-unique-mask columns** (COL_064:
   0.956) — two ~unique-mask sets are disjoint even for a perfect
   generator; the raw TV would rank the healthiest identifier columns
   worst.
6. **The oss redaction never covered the Dataflow environment dump** —
   staging buckets, KMS key ring, subnetwork projects and network tags
   leaked verbatim into every bundle, and one bundle skipped column
   redaction entirely.

## Decision

**D1 — every log surface names its build.** `SDFB_BUILD_COMMIT` is baked
into the image (`docker/Dockerfile` `GIT_COMMIT_ARG`; CI and the
personal Cloud Build pass `git rev-parse --short HEAD`) and stamped as a
`build_info` milestone from the launcher (`run_pipeline.main`) and every
worker (`GenerateRecordsDoFn.setup`). Interpreters compare builds before
comparing runs.

**D2 — live schema is authoritative at every launch, and steering
metadata comes from the TARGET table only** *(strengthened twice
2026-08-21, same day: the initial cut kept pin-wins and only warned —
the operator's requirements are that BigQuery metadata edits ALWAYS
take effect without a re-extraction step, and that the
`llm_prompt_constraint` / relational contract are declared on the
SYNTHETIC (landing) table the team owns, never read off the source
table)*. `resolve_table_schema` extracts the schema from live
`INFORMATION_SCHEMA` (the bqClient) on every launch. Structure
(columns/types/modes) mirrors the SOURCE table; the description
surfaces are then **overlaid from the LANDING table**
(`target_metadata_overlaid` / `target_metadata_unavailable`): the
source (lake) table's descriptions are another team's prose and are
STRIPPED when the target is unreachable, never inherited. `--ddl_uri`
demotes to the OFFLINE FALLBACK, used only when live source extraction
fails (`ddl_live_extract_failed`; a corrupt pin in offline mode still
raises); as the operator's declared artifact — extracted from the
LANDING table per the runbook — its descriptions stand when the target
is also unreachable, and a reachable target still overrides them. Pin
staleness vs the effective schema is reported (`ddl_pin_drift` /
`ddl_pin_fresh` / `ddl_pin_check_error`). WS4 §6b's pin-wins precedence
is superseded.

**D3 — the scrub keep-set comes from the source.** The optional
`SourceValueStore.fetch_frequent(column, min_count)` (BigQuery: one
`GROUP BY … HAVING COUNT(*) >= 10`, same cap/cache discipline) supplies
the k-anonymous enum mass; the sample multi-knot heuristic remains only
as the no-store fallback. This closes the pseudo-multi-knot gap between
the probe's substantive metric and the scrub's rejection set — the two
now share one definition of "enum mass".

**D4 — nudge-first scrub.** Colliding draws walk ±24 to the nearest
non-source integer FIRST (in-quantile, marginal-preserving); inverse-CDF
redraws serve only saturated neighborhoods, with one final nudge after.
Telemetry gains `redrawn=` so the order stays observable.

**D5 — `shape_head_tv` carries the mass claim.** TV grouped as {each
named shape with source mass ≥ the 2% floor} + {everything else}:
COL_024-class inversions on named shapes stay visible; near-unique-mask
columns stay quiet. Findings, the column score and the crosscheck's
executive-summary table key on it; the raw `shape_mass_tv` stays
reported for dense-shape columns.

**D6 — no LLM work, say so; binary columns skip the ladder.**
`llm_route_unused` (WARNING) fires when a setup ran zero LLM ladders —
the operator's cue to rerun CPU-only (the playbook cost note). Columns
whose substantive values are ≥50% control-character-bearing
(`is_binary_class`, C0/C1 minus tab/newline — accented text never
trips it) route straight to the shape-fallback template pool
(`freetext_pool_binary_fallback`); an LLM cannot usefully emit control
bytes.

**D7 — infra identifiers never leave the probe.** `_job_params` masks
buckets (`gs://REDACTED_BUCKET/…`), registry paths (image basename
kept — it carries the build id), service accounts, and drops
KMS/subnetwork/network-tag values wholesale at collection time; the
bundles in `runs/` were scrubbed retroactively and the
mis-redacted bundle re-mapped to COL_XXX names (mapping derived from the
schema-ordered gcp annexes, 67/67 invariant match).

## Alternatives rejected

- **Auto-switch to a CPU worker pool when the plan has no LLM columns**:
  worker pools are fixed at launch; a mid-job switch does not exist. The
  WARNING plus a playbook recipe is the honest version.
- **Keeping pin-wins precedence with a drift WARNING** (this ADR's first
  cut): a warning still requires an operator to read it before the
  constraints work — the propagation failure class survives one
  inattentive launch. Live-first removes the class; the pin keeps its
  one honest job (offline fallback).
- **Making the pin-staleness check fatal**: air-gapped launchers cannot
  reach `INFORMATION_SCHEMA`; staleness of a fallback is an operator
  signal, not a crash.
- **Estimating source frequencies from the sample** (scale sample counts
  by N/n): at 21× subsampling the estimator is dominated by the tail's
  Poisson noise in exactly the frequency band that matters (2–9); one
  GROUP BY against the source is exact and already inside the ADR 0023
  cost envelope.
- **Dropping the raw `shape_mass_tv`**: dense-shape columns (every shape
  named) still read best on the ungrouped number; it stays in the diff.

## Consequences

- COL_009-class residual after v2 ≈ the dense-neighborhood floor
  (~0.14 by telemetry) — below it needs range extension, which trades
  marginal truth for privacy; on record as out of scope.
- COL_047-class columns keep their scrub but stop paying for it in
  decile-KS (nudges are in-quantile); the wave-4 `numeric.decile_ks`
  numbers of 0.166/0.177 should return to the ≤0.1 class next cycle.
- Every next-run checklist starts with `build_info` and, for pinned
  launches, `ddl_pin_fresh` — the constraint-propagation class of silent
  no-op (this cycle's runs 3–4) can no longer pass unnoticed.
- B.2 parity debt now covers: numeric scrub v2, binary fast-path,
  `llm_route_unused` (R5 wave, same seams).
- The E2E interpreter prompt gains the corrected reads: judge identifier
  columns by `shape_head_tv`; `llm_route_unused` +
  `freetext_pool_skipped_expandable` mean by-design GPU idleness, not a
  lifecycle failure; compare `build_info` before calling anything
  "non-deterministic".
