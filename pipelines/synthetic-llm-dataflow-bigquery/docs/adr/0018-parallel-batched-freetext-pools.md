# 0018 — Batched, parallel, cached free-text pool builds (B.1)

Date: 2026-07-25
Status: accepted
Figures: pool-build cost anatomy and seed strategies in
[`2026-07-26-ws5-generation-throughput.md`](../designs/2026-07-26-ws5-generation-throughput.md);
the prefix-cache prompt anatomy this ADR's byte-identical-prefix rule
protects is drawn in
[`2026-08-05-source-table-stats.md`](../designs/2026-08-05-source-table-stats.md).

## Context

WS2 §4b.2 scaled B.1's free-text pool target from 32 to
`min(num_rows, distinct, 512)`, which multiplied sequential vLLM calls per
column by up to 32x (20-48 s/call on T4/float16). The 2026-07-24 12:46 E2E
spent 1552 s (49% of wall clock) building 3 pools; the 16:35 run tripled
its setup because one echo-saturated column (`COL_053`) raised
`FreeTextEmptyYieldError` out of `DoFn.setup()` twice, and Dataflow's
silent bundle retries rebuilt every sibling pool from scratch — invisible
in `validation_runs`.

## Decision

1. **Batched sampling**: each pool HTTP round trip requests
   `n=_POOL_PARALLEL_CHOICES=4` independent array-completions (vLLM
   parallel sampling, unseeded) and de-dupes across choices; the attempt
   budget scales down by the same factor. Requests for one column keep a
   byte-identical prompt across attempts (only sampling params vary).
2. **Column-parallel ladders**: independent per-column ladders run on a
   `ThreadPoolExecutor` (≤4 workers) — vLLM continuous batching absorbs
   the concurrency. Embedder work (seed-example retrieval) completes
   sequentially first: HF fast tokenizers are not thread-safe.
3. **Shape fallback parity + top-up**: B.1 adopts B.2's relaxed-template
   fallback for copy-saturated builds and additionally tops up undersized
   pools with verified-novel template values, so identifier-ish columns
   can no longer fail `setup()` or land 31-distinct pools.
4. **Process-level pool cache**: completed pools cache on
   `(reference_digest, model_uri, column, target)` and survive bundle
   retries within a worker process (precedent: `_SERVER_REFS` vLLM server
   reuse). All columns complete before a strict failure re-raises.
5. **Retry observability**: re-entrant `setup()` after an in-process
   failure emits `dofn_setup_retry` (WARNING) and increments the
   `generation/setup_retries` Beam counter.

## Consequences

- Worst-case pool build for 3 columns drops from ~26 min sequential to
  roughly the slowest single column's scaled ladder (~3-7 min expected on
  T4); a retried setup pays only the failed column.
- `n=4` with up to 4 concurrent ladders means up to ~16 in-flight
  sequences; vLLM queues what the T4's KV cache cannot admit — graceful
  degradation, no config change needed.
- New milestones (`freetext_pool_shape_topup`, `freetext_pool_cache_hit`,
  `dofn_setup_retry`) are additive; existing probe-mined names unchanged.
- RAG embed/index phases are deliberately untouched: the persisted
  chunk-store read path (ADR 0017) already reduced re-embedding to a
  ~29 s fetch (vs 283 s fresh embed, verified 2026-07-24 16:35), and the
  1024-vector flat index builds in 0.2 s.

## Compatibility with planned explorations

- **LMCache** (KV-cache layer for vLLM): a server-side deployment change
  only — enable via vLLM connector flags/env in `docker/Dockerfile` and
  `VLLMModelClient`'s server spawn args. No engine change: the pool
  builder already keeps prompts prefix-stable per column, which is the
  property prefix/KV reuse (vLLM APC today, LMCache later) rewards. The
  `ModelClient` Protocol stays transport-agnostic.
- **turbovec** (TurboQuant ANN index, Rust/Python): the swap point is the
  `sdfb_core.rag.index.build_index()` / `ExactIPIndex` seam
  (`search(query, k)` / `release()`), which nothing outside
  `rag/index.py` bypasses. Caveats for a future adapter: turbovec is
  quantized/approximate — the M1 acceptance criterion is *deterministic
  exact* top-k at ≤10k vectors, so FAISS `IndexFlatIP` stays the default;
  gate any turbovec backend behind explicit config and revisit at the
  >50k-vector scale where ANN pays. Vectors must be float32 numpy 2-D
  (our seam's `list[list[float]]` converts losslessly).
