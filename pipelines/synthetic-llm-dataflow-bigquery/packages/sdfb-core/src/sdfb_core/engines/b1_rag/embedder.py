"""Import-path shim — the embedder seam moved to `sdfb_core.rag.embedding`
(WS2 §4a). Import from `sdfb_core.rag` in new code."""

from sdfb_core.rag.embedding import BgeEmbedder, Embedder, HashingEmbedder

__all__ = ["BgeEmbedder", "Embedder", "HashingEmbedder"]
