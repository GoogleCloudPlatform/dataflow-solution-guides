# Architecture Decision Records

Durable record of significant decisions taken on synthetic-dataflow-bigquery. Each ADR captures one decision in immutable form: once recorded, an ADR is amended only with a "Superseded by …" note pointing at a successor, never silently rewritten.

## When to write an ADR

- Locking a technology choice that constrains future work (model family, registry, region).
- Rejecting a managed service or external dependency for non-obvious reasons.
- Establishing a project-wide convention that future contributors will need to understand.
- Reversing a previous decision (write a new ADR that supersedes the old one).

Do NOT write an ADR for:
- Implementation details documented in the code itself.
- Recipes that belong in `.claude/skills/`.
- Per-task notes — those live in `docs/ROADMAP.md` or the task tracker.

## Format

Each ADR is a single markdown file:

```
docs/adr/NNNN-short-kebab-title.md
```

with these sections:

- **Status** — `accepted` / `superseded by NNNN` / `proposed`.
- **Context** — what's the situation that forces a decision?
- **Decision** — the choice, in one sentence.
- **Consequences** — what this enables, what it costs, what it forbids.

Keep ADRs tight — half a page is plenty. Detail belongs in the code or in skills.

## Index

- [0001 — No managed GCP services in the serving path](0001-no-managed-gcp-services.md)
- [0002 — Gemma 4 family as the M1 model shortlist](0002-gemma-4-model-shortlist.md)
- [0003 — Private third-party container registry (withdrawn; superseded by 0015)](0003-jfrog-image-registry.md)
- [0004 — europe-west3 as the Dataflow region](0004-europe-west3-region.md)
- [0005 — Live BQ SELECT for reference rows, not cached parquet](0005-live-select-reference-data.md)
- [0006 — `GenerationEngine` ABC + `ModelClient` Protocol shape](0006-generation-engine-abc.md)
- [0007 — DRY across documentation and code](0007-dry-documentation-policy.md)
- [0008 — Container image builds happen in CI, not on developer laptops](0008-ci-driven-builds.md)
- [0009 — Single image for Flex Template launcher AND Dataflow workers](0009-single-flex-template-image.md)
- [0010 — M4 local smoke test via MLX (no DirectRunner-with-Docker)](0010-m4-local-smoke-mlx.md)
- [0011 — Adopt Beam's `VLLMCompletionsModelHandler` for §9](0011-adopt-beam-vllm-model-handler.md)
- [0012 — Network constraints for the GPU image build + Dataflow run (withdrawn)](0012-enterprise-image-build.md)
- [0013 — Synthesis engines use an LLM-as-distribution-estimator spine](0013-distribution-estimator-spine.md)
- [0014 — `VLLMModelClient` owns the vLLM OpenAI server (amends 0011)](0014-vllm-model-client-owns-server.md)
- [0015 — Dataflow worker image served via Artifact Registry (amends 0003)](0015-worker-image-via-artifact-registry.md)
- [0016 — Personal-project images build on Cloud Build (ADR 0008 carve-out)](0016-personal-gcp-cloud-build.md)
- [0017 — Custom RAG layer instead of `apache_beam.ml.rag`](0017-custom-rag-layer-over-beam-ml-rag.md)
- [0018 — Batched, parallel, cached free-text pool builds (B.1)](0018-parallel-batched-freetext-pools.md)
- [0019 — RAG population scoped to consumers; CUDA embed with VRAM demote](0019-rag-population-scoped-to-consumers.md)
- [0020 — Free-text pools are a persisted artifact, not per-worker work (amends 0018)](0020-freetext-pools-as-persisted-artifact.md)
- [0021 — Relational contract in BigQuery column descriptions](0021-relational-contract-in-descriptions.md)
- [0022 — source_table_stats as a bounded generation input](0022-stats-driven-generation.md)
- [0023 — Free-text pools reject against the full source domain; warm pools prove cleanliness](0023-source-domain-pool-rejection.md)
- [0024 — Structured prompt-constraint templates in column descriptions](0024-structured-prompt-constraint-templates.md)
- [0025 — Marginal fidelity by construction (B.1 inverse-CDF, positional alphabets, row-mass shapes)](0025-marginal-fidelity-by-construction.md)
- [0026 — Measurement first, then mask integrity (crosscheck sampling, mask tail bucket, numeric source scrub; amends 0025)](0026-measurement-first-mask-integrity.md)
- [0027 — Wave 4 verified; operational integrity (build stamp, DDL-pin drift guard, source-side k-anonymity, nudge-first scrub, head TV, binary fast-path; amends 0026)](0027-verified-wave4-operational-integrity.md)
- [0028 — Constraint router + relational plan (Tier P/B samplers, PK capacity preflight)](0028-constraint-router-relational-plan.md)
- [0029 — FK-model scenarios + history mappings (minimal-input launches, informational edges, alias registry)](0029-fk-model-scenarios-and-history-mappings.md)
- [0030 — Single-job relational generation (one Dataflow job, in-DAG FK key handoff)](0030-single-job-relational-generation.md)
- [0031 — Referential integrity by construction: joint FK key draws (IPF-fitted weights, fk.orphan gate; supersedes per-column pools)](0031-joint-fk-key-draws.md)
- [0032 — Relationships are config, not table descriptions (config/relationships/*.yaml; supersedes 0021)](0032-relationships-as-config.md)
- [0033 — Pool-ladder integrity at scale (transient-retry ladders, filter-sized targets, format-collapse exit, prose ceiling, warm-pool semantics; amends 0018/0022/0027)](0033-pool-ladder-integrity-at-scale.md)
- [0034 — Generation throughput: one dedup barrier, one engine per process, a fleet that starts full, lazy embedder, Storage-API domains, the multi-process SDK experiment (amends 0019/0030/0033)](0034-generation-throughput-single-barrier-shared-engines.md)
- [0035 — PK capacity counts FK-bound and categorical members; the FK key sample is sized by the child's PK (amends 0028/0030/0031)](0035-pk-capacity-fk-bound-members.md)
- [0036 — Parent-driven fan-out generation: children are generated from their parent's landed keys, not random draws (amends 0030/0031/0035)](0036-parent-driven-fanout-generation.md)
- [0037 — Multi-parent children: independent and conditional edges give every declared FK edge a role and a DAG path (amends 0036)](0037-multi-parent-children.md)
- [0038 — The source is the authority: a MEASURED model conflict adjusts the effective model and announces it; a self-contradiction still stops (`--on_model_conflict`; amends 0036/0037)](0038-measured-conflicts-adjust-the-model.md)
- [0039 — A launch projects its record counts, per table and in total, before the graph is built; four measured warnings say where that projection is unsound (extends 0036/0038)](0039-row-projection-before-the-graph.md)
- [0040 — Donated to the Dataflow Solution Guides; this repository stays the golden source (manifest-driven `/dsg-sync`, shared style and sensitive-content gates)](0040-dsg-donation-golden-source-sync.md)
