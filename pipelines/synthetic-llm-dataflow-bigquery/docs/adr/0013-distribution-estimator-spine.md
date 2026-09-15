# ADR 0013 — Synthesis engines use an LLM-as-distribution-estimator spine, not per-row LLM generation

- **Status**: accepted (2026-05-21)
- **Figures**: the spine's fidelity primitives and the rejected per-row-LLM
  alternative (GReaT) are drawn in
  [`2026-08-05-source-table-stats.md`](../designs/2026-08-05-source-table-stats.md)
  (inverse-CDF concept set + appendix schematics); retrieval-side geometry in
  [`2026-07-25-rag-retrieval-geometry-roadmap.md`](../designs/2026-07-25-rag-retrieval-geometry-roadmap.md).

## Context

Both M1 synthesis engines (B.1 RAG §7, B.2 library §6) must produce up to ~1M schema-conformant rows per table on Dataflow L4 workers, cheaply, while staying within the reference data's observed probabilistic ranges.

The naive reading of "RAG engine" is *prompt the LLM per row*. Two independent 2026 literature dives agree this fails on both axes:

- **Cost**: per-row token-by-token generation (GReaT / REaLTabFormer) is ~9,500× slower than statistical sampling ([arXiv 2507.19334](https://arxiv.org/html/2507.19334v1)). 1M rows ≈ 200M output tokens ≈ hundreds of L4-GPU-hours — the cost the project exists to avoid.
- **Fidelity**: cell-by-cell LLM sampling follows linguistic token frequency, not data frequency, collapsing distributions toward uniform ([arXiv 2505.02659](https://arxiv.org/html/2505.02659v2)) — violating the "stay within observed ranges" requirement (e.g. a constant column must stay constant).

## Decision

Both engines use the **FASTGEN-family spine** ([arXiv 2507.15839](https://arxiv.org/pdf/2507.15839)): **the LLM runs O(1) times, not O(N)**. In `setup()` the LLM (or a fitted statistical library, for B.2) infers per-column distributions, classifies field types, and handles hard columns once over the reference data. `generate_batch(n)` then samples the bulk **vectorized in NumPy on CPU**. LLM guided-JSON decoding is reserved for genuinely generative free-text columns (bounded unique pool, sample-with-replacement).

- **B.1 RAG**: distribution model = retrieval-conditioned (FAISS over `bge-small` embeddings) + LLM-inferred marginals/conditionals.
- **B.2 library**: distribution model = fitted `sdgx` (Apache-2.0; SDV/GaussianCopula deferred pending BSL license sign-off) + LLM free-text patch.

How the spine grew into each engine: the [RAG layer design](../designs/2026-07-07-rag-layer-design.md) (B.1) and the [B.2 library choice](../../packages/sdfb-core/src/sdfb_core/engines/b2_library/SPIKE_LIBRARY_CHOICE.md).

## Consequences

- **Enables**: ~1M rows in minutes on a single L4 + CPU; fidelity preserved *by construction* (constants copied, numeric ranges clipped, categorical frequencies and simple joints sampled from the empirical/inferred distributions). GPU is used only on the small free-text path → far fewer GPU-hours. The O(N) sampling can optionally run **GPU-accelerated via cuDF/CuPy** (Apache-2.0) on the already-provisioned L4 (idle during the O(1) LLM phase) — a candidate path to **billions** of rows.
- **Costs**: more engine-side machinery (column profiling, conditional sampling, a fidelity helper) than naive prompting; the LLM's role narrows to distribution inference + free-text.
- **Forbids**: routing the bulk N rows through per-row LLM decoding. The vLLM guided-JSON path (ADR 0011) serves only the bounded free-text pool. The B.1/B.2 agent charters and the `engine-contract` / `reference-data` skills are updated to encode this spine (supersedes the earlier "prompt Gemma per row" framing in the B.1 charter).

## Related

- [ADR 0006](0006-generation-engine-abc.md) — the `GenerationEngine` ABC this spine implements (unchanged).
- [ADR 0011](0011-adopt-beam-vllm-model-handler.md) — the vLLM handler the free-text path uses.
- [`docs/designs/2026-07-07-rag-layer-design.md`](../designs/2026-07-07-rag-layer-design.md) — the B.1 retrieval layer built on this spine.
- `.claude/agents/b1-rag-engineer.md`, `.claude/agents/b2-library-engineer.md` — to be updated to this spine.
