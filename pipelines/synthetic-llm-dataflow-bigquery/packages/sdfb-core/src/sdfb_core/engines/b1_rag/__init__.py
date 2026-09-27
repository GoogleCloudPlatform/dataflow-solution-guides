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
"""B.1 RAG synthesis engine (M1 §7).

Retrieval-augmented, distribution-estimator engine: embed the reference
rows (Embedder seam → bge-small-en-v1.5 in prod, deterministic fake in
tests), index them with FAISS `IndexFlatIP`, profile columns, infer
per-column distributions once via the `ModelClient`, then vectorized-sample
the bulk and patch free-text from a bounded LLM pool.

Design: ADR 0013 (distribution-estimator spine) and
`docs/designs/2026-07-07-rag-layer-design.md` (the retrieval layer).

Importing this subpackage registers the engine under "b1_rag" so the Beam
DAG can resolve it by string name (see `sdfb_core.engines.get_engine`).

Pure-Python: `transformers` / `torch` / `faiss` / `numpy` are imported
lazily inside the seams, so `import sdfb_core.engines.b1_rag` works on a
laptop with no extras and with `HF_HUB_OFFLINE=1` set.

Design: docs/DESIGN.md §2 Engines
(ADR 0013).
"""

from sdfb_core.engines import register_engine
from sdfb_core.engines.b1_rag.embedder import (
    BgeEmbedder,
    Embedder,
    HashingEmbedder,
)
from sdfb_core.engines.b1_rag.engine import B1RagEngine
from sdfb_core.engines.b1_rag.profile import ColumnKind, ColumnProfile

register_engine("b1_rag", B1RagEngine)

__all__ = [
    "B1RagEngine",
    "BgeEmbedder",
    "ColumnKind",
    "ColumnProfile",
    "Embedder",
    "HashingEmbedder",
]
