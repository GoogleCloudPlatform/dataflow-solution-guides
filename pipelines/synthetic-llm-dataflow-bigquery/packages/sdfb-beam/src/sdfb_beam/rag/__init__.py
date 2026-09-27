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
"""Beam/GCP side of the RAG layer (WS2 §4b): the BigQuery-backed
`ChunkStore` and the `--build_rag_layer` population stage."""

from sdfb_beam.rag.population import (
    ChunkReferenceRowsDoFn,
    EmbedChunksDoFn,
    chunk_to_bq_row,
)
from sdfb_beam.rag.store import BigQueryChunkStore

__all__ = [
    "BigQueryChunkStore",
    "ChunkReferenceRowsDoFn",
    "EmbedChunksDoFn",
    "chunk_to_bq_row",
]
