#  Copyright 2026 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
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
