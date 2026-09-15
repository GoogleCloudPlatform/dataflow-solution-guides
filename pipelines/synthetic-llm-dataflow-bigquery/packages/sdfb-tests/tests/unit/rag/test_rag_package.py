"""WS2 §4a: the standalone `sdfb_core.rag` package.

Embedding / index / serialization move out of `engines/b1_rag/` so future
consumers (population stage, downstream apps) import them without touching
engine code. Old import paths stay alive as shims — the identity checks
pin that both paths resolve to the SAME objects, not divergent copies.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel

from __future__ import annotations


def test_rag_package_exports_moved_seams():
  from sdfb_core.rag import (
      BgeEmbedder,
      Embedder,
      ExactIPIndex,
      HashingEmbedder,
      build_index,
      serialize_row,
      serialize_rows,
  )

  assert isinstance(HashingEmbedder(dim=16), Embedder)
  idx = build_index(HashingEmbedder(dim=16).embed(["a", "b"]), 16)
  assert isinstance(idx, ExactIPIndex) or hasattr(idx, "search")
  assert serialize_row({"a": 1}, ["a"]) == "a is 1"
  assert serialize_rows([{"a": None}], ["a"]) == ["a is null"]
  assert BgeEmbedder is not None


def test_old_b1_rag_import_paths_are_shims_not_copies():
  from sdfb_core.engines.b1_rag import embedder as old_embedder
  from sdfb_core.engines.b1_rag import index as old_index
  from sdfb_core.engines.b1_rag import serialize as old_serialize
  from sdfb_core.rag import embedding, index, serialize

  assert old_embedder.HashingEmbedder is embedding.HashingEmbedder
  assert old_embedder.BgeEmbedder is embedding.BgeEmbedder
  assert old_embedder.Embedder is embedding.Embedder
  assert old_index.build_index is index.build_index
  assert old_serialize.serialize_row is serialize.serialize_row


def test_rag_package_imports_without_heavy_deps():
  # Heavy deps stay deferred inside the seams: constructing the
  # dependency-free implementations must not require torch/faiss.
  # (No sys.modules assertion — the shared pytest process imports
  # apache_beam elsewhere, so module-absence checks are order-dependent.)
  from sdfb_core.rag import HashingEmbedder, build_index

  emb = HashingEmbedder(dim=8)
  idx = build_index(emb.embed(["x"]), 8)
  assert idx.search(emb.embed(["x"])[0], 1) == [0]
  idx.release()
