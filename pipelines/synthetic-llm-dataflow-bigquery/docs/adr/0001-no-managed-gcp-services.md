# ADR 0001 — No managed GCP services in the serving path

- **Status**: accepted (2026-05-18)
- **Decider**: project owner

## Context

The brief positions this project as an OSS-grade reference implementation that any company on Dataflow + BigQuery can adopt. The natural reach for "we need an LLM in a pipeline" is Vertex AI Model Garden / endpoints. Similarly, "we need data quality scans" naturally pulls in Dataplex DQ + Looker Studio dashboards.

But many adopters do not have those services provisioned — by policy (common in regulated industries such as banking) or by cost. Building on top of them would lock the deliverable out of those organizations.

## Decision

The LLM serving path runs **entirely inside Apache Beam DoFns** via `apache_beam.ml.inference.RunInference` with a custom `ModelHandler` on Dataflow GPU workers. Validation results land in BigQuery tables and GCS artifacts only; no Dataplex DQ scans, no Looker Studio dashboards. No external LLM APIs (GPT / Claude / Grok / Deepseek) in the runtime path.

## Consequences

- **Enables**: portability across GCP projects that lack managed AI / DQ services. Full ownership of the model serving stack — we control quantization, batch sizing, structured-output enforcement.
- **Costs**: we must build and maintain the GPU container ([ADR 0015](0015-worker-image-via-artifact-registry.md)), pull weights ourselves (see `docs/MODEL_LAYOUT.md`), and design our own validation reporting schema (`synthetic_data_quality.*` tables).
- **Forbids**: future design proposals that route LLM calls through Vertex, push DQ checks to Dataplex, or visualize in Looker Studio. If those become genuinely required, this ADR must be superseded explicitly.

## Related

- [ADR 0006](0006-generation-engine-abc.md) on how the `ModelClient` Protocol formalizes this isolation.
