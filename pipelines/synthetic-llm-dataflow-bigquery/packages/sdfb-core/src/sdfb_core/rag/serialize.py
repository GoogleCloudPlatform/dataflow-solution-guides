"""GReaT-style row → text serialization for embedding.

Each reference row is rendered as ``"col is value, col is value, …"`` in a
**deterministic column order** (the schema's declared order), so the same
row always produces the same string and therefore the same embedding. This
is the textual sentence representation from GReaT (arXiv 2210.06280) that
lets a text embedder place semantically-similar rows near each other.

Pure-Python, no deps — lives in `sdfb-core`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
  from collections.abc import Sequence


def serialize_row(row: dict, column_order: Sequence[str]) -> str:
  """Render one row as a GReaT-style sentence.

    `column_order` fixes the field order (use the schema's column order).
    Columns absent from the row are rendered as ``is null`` so two rows with
    the same present values but different missing keys still differ.
    """
  parts: list[str] = []
  for col in column_order:
    value = row.get(col)
    parts.append(f"{col} is {_render_value(value)}")
  return ", ".join(parts)


def serialize_rows(rows: Sequence[dict],
                   column_order: Sequence[str]) -> list[str]:
  """Serialize a batch of rows, preserving input order."""
  return [serialize_row(r, column_order) for r in rows]


def _render_value(value: object) -> str:
  if value is None:
    return "null"
  if isinstance(value, bool):
    # Render before int — bool is an int subclass.
    return "true" if value else "false"
  return str(value)


__all__ = ["serialize_row", "serialize_rows"]
