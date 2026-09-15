"""Exact-local exemplar retrieval (WS2 §4a).

Centroid top-k moved out of `B1RagEngine._retrieve_exemplars`; the
per-column variant (Phase A §4b.3) retrieves representative VALUES of one
column so free-text exemplars are column-relevant instead of diluted
whole-row sentences. Always exact, local, deterministic — never BQ
VECTOR_SEARCH in the generation hot path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sdfb_core.rag.index import build_index

if TYPE_CHECKING:  # pragma: no cover - typing only
  from collections.abc import Sequence

  from sdfb_core.rag.embedding import Embedder
  from sdfb_core.rag.index import ExactIPIndex


def centroid(vectors: list[list[float]]) -> list[float]:
  """Component-wise mean, pure Python (no numpy hard-dep)."""
  dim = len(vectors[0])
  total = [0.0] * dim
  for vec in vectors:
    for j in range(dim):
      total[j] += vec[j]
  return [t / len(vectors) for t in total]


def retrieve_centroid_top_k(
    index: ExactIPIndex,
    vectors: list[list[float]],
    items: Sequence,
    k: int,
) -> list:
  """The k items nearest the vectors' centroid (densest region).

    `items[i]` must correspond to `vectors[i]`. Deterministic given the
    index (descending score, ascending-index tie-break)."""
  ids = index.search(centroid(vectors), k)
  return [items[i] for i in ids]


def retrieve_column_exemplars(values: Sequence[str], embedder: Embedder,
                              k: int) -> list[str]:
  """Top-k most-representative values of one column: embed the values,
    build a throwaway exact index, centroid top-k."""
  values = list(values)
  if not values:
    return []
  if len(values) <= k:
    return values
  vectors = embedder.embed(values)
  index = build_index(vectors, embedder.dim)
  try:
    return retrieve_centroid_top_k(index, vectors, values, k)
  finally:
    index.release()


def _sq_dist(a: Sequence[float], b: Sequence[float]) -> float:
  return sum((x - y) * (x - y) for x, y in zip(a, b, strict=True))


def retrieve_kcenter_k(
    vectors: list[list[float]],
    items: Sequence,
    k: int,
    start: int | None = None,
) -> list:
  """Greedy k-center: each pick is the item FARTHEST from everything
    already picked.

    `retrieve_centroid_top_k` lands entirely in the densest region by
    construction, so rare modes are never shown to the LLM — it is asked
    for novel values while looking at the most average ones. k-center
    trades typicality for coverage.

    `start` re-seeds the walk (the `kcenter_rotate` arm) and is taken
    modulo the population so callers can pass an attempt counter. Default
    starts at the medoid, so the dominant mode is still represented.

    Deterministic: ties break on the lower index.
    """
  items = list(items)
  if not items or k <= 0 or not vectors:
    return []
  if len(items) <= k:
    return items

  if start is None:
    c = centroid(vectors)
    start = min(range(len(vectors)), key=lambda i: _sq_dist(vectors[i], c))
  start %= len(vectors)

  chosen = [start]
  best = [_sq_dist(v, vectors[start]) for v in vectors]
  # -1 marks "already chosen". Without this the START could be re-picked
  # once every remaining candidate has collapsed to distance 0 (a column
  # with few DISTINCT values but many rows — exactly the stagnating shape).
  best[start] = -1.0
  for _ in range(k - 1):
    nxt = max(range(len(vectors)), key=lambda i: (best[i], -i))
    chosen.append(nxt)
    best[nxt] = -1.0  # never re-pick
    for i, v in enumerate(vectors):
      if best[i] < 0:
        continue
      d = _sq_dist(v, vectors[nxt])
      best[i] = min(best[i], d)
  return [items[i] for i in chosen]


def select_seed_examples(
    vectors: list[list[float]],
    texts: Sequence[str],
    k: int,
    strategy: str = "centroid",
    attempt: int = 0,
) -> list[str]:
  """The k prompt seeds for one ladder attempt, under `strategy` (WS5 §3).

    - ``centroid``       — today's behavior; the control arm.
    - ``kcenter``        — seeds span the column's modes, prompt prefix fixed.
    - ``kcenter_rotate`` — re-seeded per attempt, so successive calls show
      the LLM different regions. This forfeits vLLM prefix caching by
      design; that cost mattered when pools rebuilt 36x per job (ADR 0018)
      and is close to free now a pool is built once per digest (WS5 §2).

    An unrecognised strategy falls back to ``centroid`` rather than raising:
    the CLI validates up front, and a stale ctx must not kill a worker
    mid-run.
    """
  texts = list(texts)
  if not texts or k <= 0 or not vectors:
    return []
  if len(texts) <= k:
    return texts
  if strategy in ("kcenter", "kcenter_rotate"):
    start = (attempt * k) % len(texts) if strategy == "kcenter_rotate" else None
    return retrieve_kcenter_k(vectors, texts, k, start=start)
  index = build_index(vectors, len(vectors[0]))
  try:
    return retrieve_centroid_top_k(index, vectors, texts, k)
  finally:
    index.release()


__all__ = [
    "centroid",
    "retrieve_centroid_top_k",
    "retrieve_column_exemplars",
    "retrieve_kcenter_k",
    "select_seed_examples",
]
