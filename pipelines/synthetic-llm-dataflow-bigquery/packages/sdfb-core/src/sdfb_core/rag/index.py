"""Exact top-k vector index for exemplar retrieval.

The production path is a FAISS ``IndexFlatIP`` over L2-normalized vectors,
which makes inner product == cosine similarity. Flat (brute-force) is the
right choice below ~50k vectors: it is *exact*, needs no training, and has
no IVF nondeterminism — so the same query returns the same neighbors every
time, satisfying the "deterministic top-k" acceptance criterion. (Single-
threaded search is forced for bit-stable ordering.)

Index cardinality is small by design: at most the 10k-row reference sample
(never the full source table; see `sdfb_beam.rag.population`), and in the
B.1 engine's in-worker index at most `_MAX_EMBED_ROWS` (1024) of that
sample — which is why flat/exact stays comfortably inside its sweet spot.

`faiss` is imported lazily. When it is not installed (bare laptop / contract
tests), a pure-Python exact inner-product fallback is used — same results,
just slower. The fallback keeps `sdfb-core` importable with no extras.

REFs:
  - FAISS IndexFlatIP: https://github.com/facebookresearch/faiss/wiki
  - spec §3 step 2: "Flat beats IVF below ~50k vectors, avoids IVF nondeterminism"
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import math


def build_index(vectors: list[list[float]], dim: int) -> ExactIPIndex:
  """Build an exact inner-product index over `vectors` (will be normalized).

    Tries FAISS; falls back to a pure-Python implementation. Both expose the
    same `search(query, k)` contract and return identical neighbor sets.
    """
  try:
    import faiss  # lazy: optional [embedding] extra, never at module scope
  except ImportError:
    return _PyExactIPIndex(vectors, dim)
  return _FaissFlatIPIndex(vectors, dim, faiss)


class ExactIPIndex:
  """Common interface for the two backends."""

  def search(self, query: list[float], k: int) -> list[int]:
    """Return the row indices of the top-k nearest neighbors of `query`,
        ordered by descending cosine similarity. Ties broken by ascending
        index for determinism."""
    raise NotImplementedError

  def release(self) -> None:
    """Drop the underlying index / arrays (called from teardown())."""
    raise NotImplementedError


def _normalize(vec: list[float]) -> list[float]:
  norm = math.sqrt(sum(v * v for v in vec))
  if norm == 0.0:
    return list(vec)
  return [v / norm for v in vec]


class _PyExactIPIndex(ExactIPIndex):
  """Pure-Python exact cosine search. Deterministic; no deps."""

  def __init__(self, vectors: list[list[float]], dim: int) -> None:
    self._dim = dim
    self._vectors: list[list[float]] | None = [_normalize(v) for v in vectors]

  def search(self, query: list[float], k: int) -> list[int]:
    if self._vectors is None:
      raise RuntimeError("index released")
    if not self._vectors or k <= 0:
      return []
    q = _normalize(query)
    scored = [(sum(a * b
                   for a, b in zip(vec, q, strict=False)), idx)
              for idx, vec in enumerate(self._vectors)]
    # Sort by descending score, then ascending index (stable tie-break).
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [idx for _, idx in scored[:k]]

  def release(self) -> None:
    self._vectors = None


class _FaissFlatIPIndex(ExactIPIndex):
  """FAISS `IndexFlatIP` over normalized vectors. Single-threaded search."""

  def __init__(self, vectors: list[list[float]], dim: int, faiss) -> None:
    import contextlib

    import numpy as np

    self._faiss = faiss
    self._np = np
    # Single-threaded → bit-stable neighbor ordering across runs.
    with contextlib.suppress(Exception):  # older faiss builds lack the call
      faiss.omp_set_num_threads(1)
    self._index = faiss.IndexFlatIP(dim)
    if vectors:
      mat = np.asarray(vectors, dtype="float32")
      faiss.normalize_L2(mat)
      self._index.add(mat)
    self._dim = dim

  def search(self, query: list[float], k: int) -> list[int]:
    if self._index is None:
      raise RuntimeError("index released")
    if self._index.ntotal == 0 or k <= 0:
      return []
    np = self._np
    q = np.asarray([query], dtype="float32")
    self._faiss.normalize_L2(q)
    k = min(k, self._index.ntotal)
    _, ids = self._index.search(q, k)
    return [int(i) for i in ids[0] if i >= 0]

  def release(self) -> None:
    self._index = None


__all__ = ["ExactIPIndex", "build_index"]
