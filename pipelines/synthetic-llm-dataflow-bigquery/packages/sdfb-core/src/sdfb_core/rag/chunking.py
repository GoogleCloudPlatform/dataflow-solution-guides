"""Chunk construction for `synthetic_rag.rag_chunks` (WS2 §4a).

`row_doc` chunk_text reuses `serialize_row()` byte-identically — that is
what makes the generation-time read a safe substitute for re-embedding
(same text in, same vector out, embedder pinned by id+version).

`row_digest` uses the same per-row canonical encoding as
`sdfb_beam.io.digest.compute_reference_digest` (json.dumps sort_keys
default=str → SHA-256) so the two provenance hashes stay comparable.
Pure stdlib.

Chunking input is the driver-loaded reference *sample* (default 10k rows,
deterministically fingerprint-ordered), never the full source table — see
`sdfb_beam.rag.population` for the scope rationale (provenance digest,
distribution-inference purpose, embed cost, bounded privacy surface).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sdfb_core.rag.serialize import serialize_row

if TYPE_CHECKING:  # pragma: no cover - typing only
  from collections.abc import Sequence

CHUNK_KIND_ROW_DOC = "row_doc"
CHUNK_KIND_FREE_TEXT_COL = "free_text_col"

# Population scope (2026-07-25 06:18 E2E postmortem). The only consumer of
# row_doc vectors is B1RagEngine._vectors_from_store, which reads EXACTLY
# the first MAX_ROW_DOC_ROWS fingerprint-ordered reference rows
# (all-or-nothing); embedding more is unreadable by design. The engine's
# _MAX_EMBED_ROWS aliases this constant — one source of truth for the
# write/read contract.
MAX_ROW_DOC_ROWS = 1024
# free_text_col chunks dedupe to distinct (column, value); a pathological
# near-unique column stays bounded here (consumers pick top-k=8 exemplars).
MAX_FREE_TEXT_VALUES_PER_COLUMN = 1024


@dataclass(frozen=True)
class Chunk:
  """One `rag_chunks` row. `embedding` is None until the embed stage."""

  chunk_id: str
  source_fqn: str
  row_digest: str
  reference_digest: str
  chunk_index: int
  chunk_kind: str
  chunk_text: str
  embedder_id: str
  embedder_version: str
  source_pk: dict | None = None
  embedding: list[float] | None = None
  metadata: dict = field(default_factory=dict)


def compute_row_digest(row: dict) -> str:
  """SHA-256 of the canonical-encoded row — content identity independent
    of chunking, comparable across runs and reference pulls."""
  return hashlib.sha256(
      json.dumps(row, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def compute_chunk_id(source_fqn: str, row_digest: str, chunk_index: int) -> str:
  """Deterministic chunk primary key (blake2b-256; dedupes retries)."""
  return hashlib.blake2b(
      f"{source_fqn}:{row_digest}:{chunk_index}".encode(),
      digest_size=32).hexdigest()


def chunk_row(
    row: dict,
    *,
    column_order: Sequence[str],
    free_text_columns: Sequence[str],
    source_fqn: str,
    reference_digest: str,
    embedder_id: str,
    embedder_version: str,
    pk_columns: Sequence[str] = (),
) -> list[Chunk]:
  """`row_doc` chunk (index 0) + one `free_text_col` chunk per non-null
    free-text column value, indices 1..k in declared column order."""
  row_digest = compute_row_digest(row)
  source_pk = {c: row.get(c) for c in pk_columns} if pk_columns else None

  def _chunk(index: int, kind: str, text: str, metadata: dict) -> Chunk:
    return Chunk(
        chunk_id=compute_chunk_id(source_fqn, row_digest, index),
        source_fqn=source_fqn,
        row_digest=row_digest,
        reference_digest=reference_digest,
        chunk_index=index,
        chunk_kind=kind,
        chunk_text=text,
        embedder_id=embedder_id,
        embedder_version=embedder_version,
        source_pk=source_pk,
        metadata=metadata,
    )

  chunks = [_chunk(0, CHUNK_KIND_ROW_DOC, serialize_row(row, column_order), {})]
  index = 1
  free_text = set(free_text_columns)
  for name in column_order:
    if name not in free_text:
      continue
    value = row.get(name)
    if value in (None, ""):
      continue
    chunks.append(
        _chunk(index, CHUNK_KIND_FREE_TEXT_COL, str(value), {"column": name}))
    index += 1
  return chunks


def distinct_free_text_values(
    rows: list[dict],
    free_text_columns: Sequence[str],
    cap: int = MAX_FREE_TEXT_VALUES_PER_COLUMN,
) -> dict[str, list[str]]:
  """First-seen distinct non-empty values per free-text column, capped.

    Driver-side dedupe for the population branch: the 2026-07-25 E2E
    embedded 23,610 per-occurrence value chunks where the distinct value
    count was a fraction of that — identical strings re-embedded per row.
    First-seen over the fingerprint-ordered sample keeps the pick
    deterministic."""
  out: dict[str, list[str]] = {c: [] for c in free_text_columns}
  seen: dict[str, set[str]] = {c: set() for c in free_text_columns}
  for row in rows:
    for column in free_text_columns:
      if len(out[column]) >= cap:
        continue
      value = row.get(column)
      if value in (None, ""):
        continue
      text = str(value)
      if text in seen[column]:
        continue
      seen[column].add(text)
      out[column].append(text)
  return out


def chunk_free_text_value(
    column: str,
    value: str,
    *,
    source_fqn: str,
    reference_digest: str,
    embedder_id: str,
    embedder_version: str,
) -> Chunk:
  """One deduped free_text_col `Chunk` for a distinct (column, value).

    Identity is VALUE-keyed (digest of {"column","value"}), not row-keyed:
    the consumer (`_fetch_free_text_chunks`) reads only chunk_text /
    embedding / metadata["column"], so per-row provenance bought nothing
    but duplicate embeds. chunk_index is fixed 0 — identity is carried by
    the value digest."""
  value_digest = compute_row_digest({"column": column, "value": value})
  return Chunk(
      chunk_id=compute_chunk_id(source_fqn, value_digest, 0),
      source_fqn=source_fqn,
      row_digest=value_digest,
      reference_digest=reference_digest,
      chunk_index=0,
      chunk_kind=CHUNK_KIND_FREE_TEXT_COL,
      chunk_text=value,
      embedder_id=embedder_id,
      embedder_version=embedder_version,
      source_pk=None,
      metadata={"column": column},
  )


__all__ = [
    "CHUNK_KIND_FREE_TEXT_COL",
    "CHUNK_KIND_ROW_DOC",
    "MAX_FREE_TEXT_VALUES_PER_COLUMN",
    "MAX_ROW_DOC_ROWS",
    "Chunk",
    "chunk_free_text_value",
    "chunk_row",
    "compute_chunk_id",
    "compute_row_digest",
    "distinct_free_text_values",
]
