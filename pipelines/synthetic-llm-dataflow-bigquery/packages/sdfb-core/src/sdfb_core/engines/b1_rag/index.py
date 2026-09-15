"""Import-path shim — the exact index moved to `sdfb_core.rag.index`
(WS2 §4a). Import from `sdfb_core.rag` in new code."""

from sdfb_core.rag.index import ExactIPIndex, build_index

__all__ = ["ExactIPIndex", "build_index"]
