# 0019 — RAG population scoped to consumers; CUDA embed with VRAM demote

Date: 2026-07-25
Status: accepted

## Context

The 2026-07-25 06:18 E2E (`--build_rag_layer=true`) spent 1,506.7 s —
60.8 % of a 2,479 s job — on the chunk-population branch, running the
whole time in parallel with generation. It embedded 33,610 chunks on CPU
(both T4s idle): 10,000 `row_doc` chunks of which only the first 1,024
can ever be read back (`B1RagEngine._vectors_from_store` is all-or-nothing
over the fingerprint-ordered `rows[:1024]` prefix), plus 23,610
`free_text_col` chunks embedded once per *occurrence* — duplicates of a
few thousand distinct values, of which the consumer
(`_column_seed_examples`) ultimately picks top-8 exemplars per column.
~90 % of the embedding work was unreadable-by-design or redundant.

## Decision

1. **Write only the read contract.** Population creates `row_doc` chunks
   for exactly `chunking.MAX_ROW_DOC_ROWS` (1024) rows — the engine's
   `_MAX_EMBED_ROWS` now aliases that constant, making write and read one
   contract — and `free_text_col` chunks for distinct `(column, value)`
   pairs (first-seen order over the fingerprint-ordered sample, capped
   `MAX_FREE_TEXT_VALUES_PER_COLUMN=1024`).
2. **Dedupe driver-side, embed Beam-side.** The distinct-value scan runs
   in the driver (`distinct_free_text_values` — the branch already starts
   from the in-memory `reference_rows`), while the heavy work keeps
   Beam's parallelism: `Flatten → Reshuffle("RagFanout") → BatchElements
   → EmbedChunksDoFn` fans embedding out across workers.
3. **Value-keyed chunk identity.** Deduped value chunks carry
   `row_digest = sha256({"column", "value"})` and `chunk_index=0`; their
   `chunk_id` is therefore stable across runs of the same digest,
   improving WRITE_APPEND re-run behavior. Consumers only read
   `chunk_text` / `embedding` / `metadata["column"]` — unaffected.
4. **CUDA when available, demoted before vLLM.** `BgeEmbedder` accepts
   `device="auto"` (CUDA if `torch.cuda.is_available()`), and both users
   — `EmbedChunksDoFn` (population) and the engine's in-setup 1024-row
   embed — call `demote_to_cpu()` (weights → CPU, `cuda.empty_cache()`)
   as soon as bulk embedding is done. vLLM sizes its KV-cache budget from
   free GPU memory at ignition; bge-small (~150 MB + activations) must
   not be resident by then. Later seed-example embeds are tiny and run on
   CPU.
5. **Branch stays in-job.** With the volume cut (~350–450 CPU-s worst
   case on CPU; seconds on CUDA), the concurrent branch finishes far
   inside the ~16-min generation setup and no longer extends wall clock —
   splitting population into its own job is deferred until a real need
   (e.g. >50k-row samples or GPU-less population workers).

## Consequences

- Population cost drops from ~3,100–3,500 CPU-s (1,506 s wall on 2
  workers) to ~1,300 row-doc-equivalent embeds (~350–450 CPU-s on CPU,
  seconds on T4) — it stops being the job's dominant stage entirely.
- The engine's own digest-miss embed (267 s CPU in every fresh-digest
  run) drops to seconds on GPU workers; `b1_embed_done` now logs a
  `device=` field so reports can verify which path ran.
- Existing chunk stores keep working: the read path never depended on
  row-doc chunks beyond the prefix or on per-occurrence value chunks.
  Re-seeding a digest with the new code writes a smaller, value-stable
  chunk set alongside any old rows (readers tolerate duplicates).
- Deferred: population-only GPU-less job/template; same-run chunk
  consumption (the engine's reuse check still runs at setup-time, before
  this run's own writes commit).

## Related

- ADR 0017 (custom RAG layer), ADR 0018 (batched/parallel/cached pool
  builds — the generation-side half of the 2026-07-24/25 performance
  campaign).
