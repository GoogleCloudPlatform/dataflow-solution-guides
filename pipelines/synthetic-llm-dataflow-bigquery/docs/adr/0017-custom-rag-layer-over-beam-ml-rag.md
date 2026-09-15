# ADR 0017 — Custom RAG layer instead of `apache_beam.ml.rag`

- **Status**: accepted (2026-07-24)
- **Figures**: the retrieval geometry this layer implements (centroid top-k
  cones, k-center coverage, conditioning trade-offs) is drawn in
  [`2026-07-25-rag-retrieval-geometry-roadmap.md`](../designs/2026-07-25-rag-retrieval-geometry-roadmap.md).

## Context

Beam ships a first-party RAG toolkit, [`apache_beam.ml.rag`](https://beam.apache.org/releases/pydoc/current/apache_beam.ml.rag.html)
(chunking → embedding → vector-store ingestion → enrichment), and it is
"Beam ready": `LangChainChunker`, `HuggingfaceTextEmbeddings` /
`VertexAITextEmbeddings` under `MLTransform`, writers for BigQuery vector
search, AlloyDB, Spanner, Milvus, Qdrant, and typed `Chunk`/`Embedding`
carriers. A fair question is why WS2 built `sdfb_core.rag.*` +
`sdfb_beam.rag.*` by hand instead of adopting it.

## Decision

Keep the custom RAG layer. `apache_beam.ml.rag` is built for a different
problem shape — document RAG over a text corpus feeding a vector store /
LLM enrichment — and adopting it would violate two hard constraints and
re-introduce exactly the coupling our seams exist to prevent:

1. **Its embedding handlers violate the self-hosting contract.**
   `HuggingfaceTextEmbeddings` wraps `sentence-transformers` and resolves a
   `model_name` through Hugging Face Hub machinery — forbidden at runtime
   (hard constraint #2, [ADR 0001](0001-no-managed-gcp-services.md));
   `VertexAITextEmbeddings` is Vertex in the serving path (hard constraint
   #1). Using the package therefore means writing a custom offline
   `EmbeddingsManager` anyway — at which point the "free" part of the
   toolkit is gone, and what remains is `MLTransform`'s artifact-location
   lifecycle wrapped around the same `BgeEmbedder` we already have.

2. **Our chunks are structured provenance records, not split documents.**
   `LangChainChunker` splits free text. Our chunks are canonical
   GReaT-style row serializations that must stay **byte-identical** to the
   engine's serialization (that identity is what lets generation reuse
   stored vectors instead of re-embedding), and each chunk carries
   provenance the ml.rag `Chunk` type does not model: `row_digest`,
   `reference_digest`, source PK, `embedder_id`/`embedder_version`, chunk
   kind. The `rag_chunks` BQ schema is preflight-checked DDL
   (deploy step 9), not a writer-owned default schema.

3. **Retrieval shape is inverted.** ml.rag's read side is per-element
   enrichment against a vector database service. B.1 retrieves **once per
   worker in `setup()`** (centroid top-k exemplars into an in-worker FAISS
   flat index, ≤1024 vectors) and then samples distributions vectorized —
   per ADR 0013 there is deliberately no per-row retrieval on the hot
   path, so a per-element enrichment transform has nothing to attach to.

4. **Import direction.** Engines live in `sdfb-core` and must stay
   Beam-free (strict `sdfb-beam` → `sdfb-core` dependency direction).
   Chunking/embedding/retrieval types shared with ml.rag would drag
   `apache_beam` into the engine layer; our own pure-stdlib `Chunk` and
   `Embedder` Protocol keep the laptop test story intact.

The Beam-side surface we do need — a DoFn with a setup()-built embedder,
`BatchElements`, a BQ sink — is ~150 lines (`sdfb_beam/rag/population.py`)
sharing one warm-pull and one lifecycle pattern with the generation path.
That is smaller than the adapter layer ml.rag would require.

## Scope note (what gets chunked/embedded/indexed)

The RAG layer processes the **driver-loaded reference sample** (default
10k rows, `--reference_rows_limit`, deterministically
FARM_FINGERPRINT-ordered — [ADR 0005](0005-live-select-reference-data.md)),
never the full source table: the sample *is* the provenance unit
(`reference_digest`), distribution inference needs representativeness
rather than exhaustiveness, CPU embedding is the wall-clock bottleneck on
T4-class workers, and every embedded row is bounded privacy exposure.
Details in `sdfb_beam/rag/population.py`.

## Consequences

- We own chunking/embedding/index code and its tests (today: pure-stdlib +
  optional faiss/transformers extras, 600-test laptop suite covers it).
- Revisit at M2+ if requirements change shape: streaming ingestion, a
  managed vector store (AlloyDB/BQ vector search) instead of the
  `rag_chunks` table, or per-element retrieval — those are the cases
  `apache_beam.ml.rag` is actually built for. An offline
  `EmbeddingsManager` contribution upstream would also change the calculus
  for constraint #1 above.
