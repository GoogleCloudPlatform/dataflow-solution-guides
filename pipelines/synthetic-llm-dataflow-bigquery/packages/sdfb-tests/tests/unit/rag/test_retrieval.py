"""WS2 §4a/§4b: centroid top-k retrieval, generic over items, plus the
per-column value retrieval Phase A uses for column-relevant exemplars."""

from __future__ import annotations

from sdfb_core.rag.embedding import HashingEmbedder
from sdfb_core.rag.index import build_index
from sdfb_core.rag.retrieval import (
    centroid,
    retrieve_centroid_top_k,
    retrieve_column_exemplars,
)


def test_centroid_is_componentwise_mean():
  assert centroid([[0.0, 2.0], [2.0, 0.0]]) == [1.0, 1.0]


def test_retrieve_centroid_top_k_returns_items_deterministically():
  emb = HashingEmbedder(dim=32)
  rows = [{
      "i": i,
      "text": t
  } for i, t in enumerate(["alpha", "beta", "alpha b"])]
  vectors = emb.embed([r["text"] for r in rows])
  index = build_index(vectors, emb.dim)
  a = retrieve_centroid_top_k(index, vectors, rows, 2)
  b = retrieve_centroid_top_k(index, vectors, rows, 2)
  assert a == b
  assert len(a) == 2 and all(r in rows for r in a)
  index.release()


def test_retrieve_column_exemplars_returns_subset_of_values():
  emb = HashingEmbedder(dim=32)
  values = [f"note {i}" for i in range(10)]
  out = retrieve_column_exemplars(values, emb, 4)
  assert len(out) == 4
  assert set(out) <= set(values)
  assert retrieve_column_exemplars(values, emb, 4) == out  # deterministic


def test_retrieve_column_exemplars_short_input():
  emb = HashingEmbedder(dim=32)
  assert retrieve_column_exemplars([], emb, 4) == []
  assert retrieve_column_exemplars(["only"], emb, 4) == ["only"]
