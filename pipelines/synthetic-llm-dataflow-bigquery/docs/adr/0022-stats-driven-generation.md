# ADR 0022 — source_table_stats as a bounded generation input (tiered stats, inverse-CDF sampling, measured prompt hints)

**Status:** ACCEPTED (2026-08-05)
**Design:** [`docs/designs/2026-08-05-source-table-stats.md`](../designs/2026-08-05-source-table-stats.md)
**Figures:** concept set (entropy/skew, inverse-CDF, epoch deciles, null patterns) regenerable via `scripts/doc/make_source_stats_figures.py` — the design doc carries them with two-audience captions and code links.

## Context

WS-B (ADR 0021's sibling workstream) landed `synthetic_rag.source_table_stats`
as a *human/drift* view: one profiling pass over the eager reference sample,
persisted append-only, engines re-profiling per worker from the same rows.
Three quality findings pushed it further:

1. **Pool starvation** — the 2026-08-05 five-run postmortem (§11 of
   `docs/designs/2026-07-27-ws6-pipeline-shape.md`) measured synthetic
   free-text `distinct == pool size` while the 10k reference sample capped a
   column's observable distinct at 95 vs 4k–146k in source. The sample
   *cannot* see true cardinality.
2. **Flattened marginals** — B.2 sampled numerics/temporals
   `uniform(min, max)`: any skewed source column (amounts, burst-heavy
   timestamps) landed uniform, exactly what SDV's KSComplement-style
   marginal tests punish.
3. **No skew stat existed** — ADR 0021's FK plan says "frequency-weighted
   when WS-B stats show a skewed copy pattern"; nothing computed skew.

Hard constraints stand: no Dataplex/managed profiling, engines pure-Python,
workers must not read BigQuery.

## Decision

1. **Stats stay tiered, workers stay stats-table-agnostic.**
   - *Tier 1 (`--source_stats=sample`, default)* profiles the reference
     sample driver-side. v2 adds per-column Shannon entropy +
     normalized entropy + `top1_share` (the skew stat ADR 0021 was
     missing — [Shannon 1948](https://ieeexplore.ieee.org/document/6773024)),
     numeric mean/stddev/deciles, temporal ISO range + day-of-week /
     month / hour mixes + `future_fraction`, and a `__table__` pseudo-column
     with the row-level **null-pattern mix** (which columns are null
     *together* — the one joint stat cheap enough for M1).
   - *Tier 2 (`--source_stats=exact`)* is ONE aggregate `SELECT` over the
     live table (`sdfb_beam/io/exact_stats.py`), all approximate aggregates
     in a single scan: `APPROX_COUNT_DISTINCT`
     ([HyperLogLog++, Heule/Nunkesser/Hall 2013](https://research.google.com/pubs/archive/40671.pdf),
     [BQ HLL functions](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/hll_functions)),
     `APPROX_QUANTILES`, `APPROX_TOP_COUNT`
     ([BQ approximate aggregation](https://cloud.google.com/bigquery/docs/reference/standard-sql/approximate_aggregate_functions)).
     A failed exact pass degrades loudly to Tier 1 (milestone
     `source_stats_exact_failed`), never kills the run.
2. **Engine consumers are contained and profile-mediated** — every consumer
   reads the worker-side `ColumnProfile` or a `GenerationContext` field the
   driver populated; no worker BQ reads:
   - *B.2 numeric + temporal*: 11-point empirical decile vector on the
     profile; samplers **inverse-transform** uniform draws through it
     (inverse transform sampling,
     [Devroye 1986, ch. II](http://luc.devroye.org/rnbookindex.html)).
     Skewed marginals survive; every draw stays novel and in-range.
   - *B.1 pool sizing*: `GenerationContext.source_distinct` (Tier-2 exact
     counts) lifts `_pool_target` past the sample's under-estimate, still
     capped by `_FREE_TEXT_POOL_MAX` (GPU cost is linear in pool size).
   - *Both engines' pool prompts*: `text_shapes.length_hint()` appends the
     measured p05–p95 length band as a per-column **constant suffix** —
     after the shared instruction prefix, so vLLM
     [automatic prefix caching](https://docs.vllm.ai/en/stable/design/prefix_caching/)
     keeps serving the cached prefix (ADR 0018's byte-identical-prefix rule).
3. **Provenance is part of the skip key.** The stats table gains
   `sample_rows`, `stats_tier`, `profiler_version` columns;
   `exists()` filters on version + *achieved* tier. The reference digest
   hashes rows, not profiler code — without the version a profiler upgrade
   skips forever; without the tier a sample row blocks the exact pass.
4. **Privacy gate on literals.** `top_values`/`APPROX_TOP_COUNT` literals are
   persisted only for enum-routed columns (distinct ≤ 50). Higher-cardinality
   columns get shapes/entropy/lengths — never values. Rationale: LLM
   training-data extraction and the repo's own T4 exemplar leak show
   literal high-cardinality values are quasi-identifiers
   ([Carlini et al. 2021](https://arxiv.org/abs/2012.07805)).

## Alternatives rejected

- **Dataplex data profiling** — banned (CLAUDE.md hard constraint 3); the
  single-SELECT pattern keeps profiling SQL-native and cost-visible.
- **LLM-side joint generation (GReaT-style autoregressive rows,
  [Borisov et al., ICLR 2023](https://arxiv.org/abs/2210.06280))** — one
  LLM call per row violates the ADR 0013 distribution-estimator spine
  (LLM infers distributions once; bulk sampling is vectorized CPU).
- **Copulas / CTGAN-style learned joint structure
  ([Xu et al., NeurIPS 2019](https://arxiv.org/abs/1907.00503))** for the
  independence gap — `SdgxBackend` already owns learned-joint when enabled;
  hand-rolled correlation modeling in the empirical path is M2. The
  null-pattern mix is the only M1 joint stat (measured, not modeled).
- **Workers reading the stats table** — a BQ dependency in `setup()` per
  worker for values the driver already has; violates the WS-B invariant.

## Consequences

- `distinct` in the stats table changes meaning with `stats_tier` — readers
  MUST group by tier (that is why it is a real column, not JSON-only).
- The append-only table accretes one row-set per (digest, tier, profiler
  version); storage is trivial, and cross-run drift queries key on
  `sample_rows` to stay comparable.
- Validation gets free per-column fidelity oracles: entropy delta and decile
  overlap between a source-tier and a landing-table profile are the
  SDMetrics [KSComplement](https://docs.sdv.dev/sdmetrics/metrics/quality-metrics/kscomplement)
  / [TVComplement](https://docs.sdv.dev/sdmetrics/metrics/quality-metrics/tvcomplement)
  / [CategoryCoverage](https://docs.sdv.dev/sdmetrics/data-metrics/quality/categorycoverage)
  shapes without importing SDV (mode collapse shows as an entropy gap even
  when the distinct count looks healthy).
- Deferred consciously (M2+): weekday/hour-preserving temporal bucket
  sampling (the mixes are already measured), correlation/functional-
  dependency stats, exact temporal deciles in Tier 2, JSON/GEOGRAPHY/BYTES
  columns in the exact pass.
