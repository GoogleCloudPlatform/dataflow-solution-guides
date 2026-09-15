"""Import-path shim — GReaT serialization moved to `sdfb_core.rag.serialize`
(WS2 §4a). Import from `sdfb_core.rag` in new code."""

from sdfb_core.rag.serialize import serialize_row, serialize_rows

__all__ = ["serialize_row", "serialize_rows"]
