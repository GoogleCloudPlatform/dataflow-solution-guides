"""`--build_rag_layer` population branch (WS2 §4b.1; 2026-07-07 design §3).

Chunk reference rows → embed → `rag_chunks` BQ rows. The embed stage is a
DoFn with a setup()-built embedder (the same GCS warm-pull + local-only
loading as `GenerateRecordsDoFn`) rather than a RunInference ModelHandler
— one embedding code path, one lifecycle pattern; the rewritten RAG
design doc records this delta from the 2026-07-07 diagram.

**Scope — the consumers' read contract, never the full sample or table.**
Since 2026-07-25 (ADR 0019) the branch writes `row_doc` chunks only for the
first `MAX_ROW_DOC_ROWS` (1024) fingerprint-ordered rows — exactly the
all-or-nothing prefix `B1RagEngine._vectors_from_store` reads — and
`free_text_col` chunks deduped to distinct (column, value) pairs (the
2026-07-25 06:18 E2E embedded 33,610 chunks, ~90 % unreadable-by-design or
duplicates, for 1,506 s / 60.8 % of wall clock). The input is the
driver-loaded reference sample (`load_reference_rows`,
`--reference_rows_limit`, default 10k, deterministic FARM_FINGERPRINT
ordering — see `sdfb_beam.io.bq_sources`), NOT every row of the source
table. Deliberate, for four reasons:

  1. Provenance: `reference_digest` is computed over the driver-held rows
     before graph construction; the chunk set must be exactly that row set
     or the digest stops describing what was embedded.
  2. Purpose: the chunks condition per-column distribution inference and
     exemplar retrieval (ADR 0013 — estimate once, sample vectorized);
     a representative sample carries that signal, an exhaustive copy adds
     rows, cost, and no new distributional information.
  3. Cost/wall-clock: CPU-embedding is the T4/n1 bottleneck (the
     2026-07-16 run spent 26-92 min per bundle embedding 10k rows);
     full-table embedding scales that by the table's row count.
  4. Privacy surface: every embedded row is source data reproduced into
     `rag_chunks` and worker memory. After the 2026-07-10 exemplar-leak
     postmortem, the exposure is kept bounded to the sampled subset.

The in-worker retrieval index narrows further still — B.1's setup embeds
at most `_MAX_EMBED_ROWS` (1024) of the sample (see
`sdfb_core.engines.b1_rag.engine`).
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import json
from datetime import UTC, datetime

import apache_beam as beam
from sdfb_core.rag.chunking import Chunk, chunk_row


class ChunkReferenceRowsDoFn(beam.DoFn):
  """One reference row → its `Chunk`s (no embeddings yet). Pure."""

  def __init__(
      self,
      *,
      source_fqn: str,
      reference_digest: str,
      column_order: list[str],
      free_text_columns: list[str],
      pk_columns: list[str],
      embedder_id: str,
      embedder_version: str,
  ) -> None:
    super().__init__()
    self.source_fqn = source_fqn
    self.reference_digest = reference_digest
    self.column_order = column_order
    self.free_text_columns = free_text_columns
    self.pk_columns = pk_columns
    self.embedder_id = embedder_id
    self.embedder_version = embedder_version

  # pylint: disable-next=arguments-renamed  # Beam passes the element positionally
  def process(self, row: dict):
    yield from chunk_row(
        row,
        column_order=self.column_order,
        free_text_columns=self.free_text_columns,
        source_fqn=self.source_fqn,
        reference_digest=self.reference_digest,
        embedder_id=self.embedder_id,
        embedder_version=self.embedder_version,
        pk_columns=self.pk_columns,
    )


class EmbedChunksDoFn(beam.DoFn):
  """Batch of `Chunk`s → `rag_chunks` BQ row dicts with embeddings.

    The embedder is built ONCE per worker in setup() (heavy), matching
    the engine-in-setup rule from `.claude/skills/beam-dofn.md`.
    """

  def __init__(self, embedder_uri: str, device: str = "auto") -> None:
    super().__init__()
    self.embedder_uri = embedder_uri
    self.device = device
    self._embedder = None

  def setup(self):
    uri = self.embedder_uri
    if uri.startswith("gs://"):
      # Import from the localization module itself — importing the old
      # re-export off dofns.generate is what killed the 2026-07-28 R1
      # rerun (ImportError on every RagEmbedChunks bundle after the
      # constant moved to dofns.localize in d2e1711).
      from sdfb_beam.dofns.localize import EMBEDDER_LOCAL_DIR
      from sdfb_beam.gcs import localize_gcs_prefix

      uri = localize_gcs_prefix(uri, EMBEDDER_LOCAL_DIR)
    if uri:
      from sdfb_core.rag.embedding import BgeEmbedder

      # "auto" = the worker's GPU when present (2026-07-25 06:18 E2E:
      # both T4s idle while 33,610 chunks embedded on CPU for 25 min).
      # teardown() demotes so vLLM ignition never contends for VRAM.
      self._embedder = BgeEmbedder(uri, device=self.device)
    else:
      from sdfb_core.rag.embedding import HashingEmbedder

      self._embedder = HashingEmbedder(dim=384)

  # pylint: disable-next=arguments-renamed  # Beam passes the element positionally
  def process(self, chunks: list[Chunk]):
    vectors = self._embedder.embed([c.chunk_text for c in chunks])
    created_at = datetime.now(UTC).isoformat()
    for chunk, vector in zip(chunks, vectors, strict=True):
      yield chunk_to_bq_row(chunk, vector, created_at)

  def teardown(self):
    # Release VRAM before anything else (vLLM) sizes its budget.
    demote = getattr(self._embedder, "demote_to_cpu", None)
    if callable(demote):
      # False positive: pylint does not narrow `demote` on callable().
      demote()  # pylint: disable=not-callable
    self._embedder = None


def chunk_to_bq_row(chunk: Chunk, embedding: list[float],
                    created_at: str) -> dict:
  """`Chunk` + embedding → the `rag_chunks` row dict (JSON cols encoded)."""
  return {
      "chunk_id":
          chunk.chunk_id,
      "source_fqn":
          chunk.source_fqn,
      "source_pk":
          None if chunk.source_pk is None else json.dumps(
              chunk.source_pk, sort_keys=True, default=str),
      "row_digest":
          chunk.row_digest,
      "reference_digest":
          chunk.reference_digest,
      "chunk_index":
          chunk.chunk_index,
      "chunk_kind":
          chunk.chunk_kind,
      "chunk_text":
          chunk.chunk_text,
      "embedder_id":
          chunk.embedder_id,
      "embedder_version":
          chunk.embedder_version,
      "embedding": [float(v) for v in embedding],
      "metadata":
          json.dumps(chunk.metadata, sort_keys=True, default=str),
      "created_at":
          created_at,
  }


__all__ = ["ChunkReferenceRowsDoFn", "EmbedChunksDoFn", "chunk_to_bq_row"]
