# ADR 0005 — Live BQ SELECT for reference rows, not cached parquet

- **Status**: accepted (2026-05-18)

## Context

The synthesis pipeline needs a sample of real rows to ground the LLM (B.1 RAG retrieves from them; B.2 library-wrapper fits a tabular model on them; Mode B fidelity / drift baselines compare against them). Two architectural shapes:

1. **Live SELECT every run** — `SELECT * FROM t LIMIT N` at job start; rows materialized in the pipeline driver.
2. **Cached parquet snapshot** — one-time pull to `gs://.../reference/{table}/sample.parquet`, refreshed periodically.

Cached parquet gives deterministic, repeatable runs (essential for SDMetrics fidelity comparison) but adds operational overhead (refresh job, freshness SLA, snapshot management). Live SELECT is simpler but non-deterministic — two consecutive jobs see different reference rows.

## Decision

**Live SELECT every run** for M1. Reproducibility comes from a **`reference_digest`** — a SHA-256 over canonical-encoded reference rows, written to `synthetic_data_quality.validation_runs` so any run can be re-traced to the rows it actually saw.

## Consequences

- **Enables**: no caching infrastructure, no freshness SLA, no snapshot management. The pipeline runs against the latest data without operational tax.
- **Costs**: SDMetrics / Evidently fidelity scores (M2) become noisy because the baseline itself drifts between runs. Mode B reports must check `reference_digest` equality before comparing scores. CI determinism requires substituting a frozen fixture rather than the live source.
- **Forbids**: building Mode B fidelity tools that assume a stable baseline. Tools must read `reference_digest` first.

## Related

- `packages/sdfb-beam/src/sdfb_beam/io/bq_sources.py` — `load_reference_rows()`.
- `packages/sdfb-beam/src/sdfb_beam/io/digest.py` — `compute_reference_digest()`.
- `.claude/skills/reference-data.md` — implementation recipe.
- M2 candidate: add an opt-in `--reference_snapshot_uri` flag that bypasses live SELECT and pulls from cached parquet for deterministic reruns.
