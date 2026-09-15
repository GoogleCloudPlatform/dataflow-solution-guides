"""Standalone RAG package (WS2 §4a).

Chunking, embedding, exact-local indexing, chunk storage, and retrieval —
shared by `B1RagEngine`, the `--build_rag_layer` population stage, and any
future consumer of `synthetic_rag.rag_chunks`. Pure Python; heavy deps
(torch, faiss, numpy) stay deferred inside the seams exactly as before.
"""

from sdfb_core.rag.chunking import (
    CHUNK_KIND_FREE_TEXT_COL,
    CHUNK_KIND_ROW_DOC,
    Chunk,
    chunk_row,
    compute_chunk_id,
    compute_row_digest,
)
from sdfb_core.rag.embedding import BgeEmbedder, Embedder, HashingEmbedder, embedder_identity
from sdfb_core.rag.index import ExactIPIndex, build_index
from sdfb_core.rag.retrieval import (
    centroid,
    retrieve_centroid_top_k,
    retrieve_column_exemplars,
)
from sdfb_core.rag.serialize import serialize_row, serialize_rows
from sdfb_core.rag.store import ChunkStore, InMemoryChunkStore

__all__ = [
    "CHUNK_KIND_FREE_TEXT_COL",
    "CHUNK_KIND_ROW_DOC",
    "BgeEmbedder",
    "Chunk",
    "ChunkStore",
    "Embedder",
    "ExactIPIndex",
    "HashingEmbedder",
    "InMemoryChunkStore",
    "build_index",
    "centroid",
    "chunk_row",
    "compute_chunk_id",
    "compute_row_digest",
    "embedder_identity",
    "retrieve_centroid_top_k",
    "retrieve_column_exemplars",
    "serialize_row",
    "serialize_rows",
]
