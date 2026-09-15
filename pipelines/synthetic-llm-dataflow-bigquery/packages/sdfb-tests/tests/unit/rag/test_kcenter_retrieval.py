"""Greedy k-center seeds span the modes; centroid top-k does not (WS5 T8).

The geometry roadmap names the flaw directly: "a centroid query lands in
the densest region; rows in rare modes are never exemplars." Retrieval runs
3x per worker setup and its only job is picking 8 prompt seeds, so showing
the LLM the 8 most-average values and asking for novel ones is a plausible
mechanism for the low novel-yield and long ladders measured on 2026-07-26.
"""

from __future__ import annotations

from sdfb_core.rag.index import build_index
from sdfb_core.rag.retrieval import retrieve_centroid_top_k, retrieve_kcenter_k


def _three_modes():
  """One dense mode plus two far, sparse ones — the realistic shape for a
    column with a dominant value class and a rare tail."""
  dense = [[1.0, 0.0, 0.0] for _ in range(20)]
  far_a = [[0.0, 1.0, 0.0] for _ in range(3)]
  far_b = [[0.0, 0.0, 1.0] for _ in range(2)]
  vectors = dense + far_a + far_b
  items = ([f"dense{i}" for i in range(20)] + [f"a{i}" for i in range(3)] +
           [f"b{i}" for i in range(2)])
  return vectors, items


def test_centroid_topk_stays_in_the_densest_mode():
  """The control arm's behavior, pinned so the comparison is honest."""
  vectors, items = _three_modes()
  index = build_index(vectors, 3)
  try:
    got = retrieve_centroid_top_k(index, vectors, items, 4)
  finally:
    index.release()
  assert all(g.startswith("dense") for g in got)


def test_kcenter_reaches_every_mode():
  vectors, items = _three_modes()
  got = retrieve_kcenter_k(vectors, items, 4)
  assert any(g.startswith("dense") for g in got)
  assert any(g.startswith("a") for g in got)
  assert any(g.startswith("b") for g in got)


def test_kcenter_is_deterministic():
  vectors, items = _three_modes()
  assert retrieve_kcenter_k(vectors, items,
                            5) == retrieve_kcenter_k(vectors, items, 5)


def test_kcenter_start_rotates_the_seed_set():
  """kcenter_rotate re-seeds per ladder attempt; a different start must
    give a different set."""
  vectors, items = _three_modes()
  assert retrieve_kcenter_k(
      vectors, items, 4, start=0) != retrieve_kcenter_k(
          vectors, items, 4, start=21)


def test_start_is_taken_modulo_the_population():
  """Callers derive `start` from an attempt counter, so it must wrap."""
  vectors, items = _three_modes()
  assert retrieve_kcenter_k(
      vectors, items, 3, start=len(items) + 2) == (
          retrieve_kcenter_k(vectors, items, 3, start=2))


def test_kcenter_returns_everything_when_k_exceeds_the_data():
  vectors, items = _three_modes()
  assert len(retrieve_kcenter_k(vectors, items, 999)) == len(items)


def test_kcenter_never_repeats_an_item():
  vectors, items = _three_modes()
  got = retrieve_kcenter_k(vectors, items, 8)
  assert len(got) == len(set(got)) == 8


def test_degenerate_inputs_are_safe():
  assert retrieve_kcenter_k([], [], 4) == []
  assert retrieve_kcenter_k([[1.0]], ["only"], 0) == []
  assert retrieve_kcenter_k([[1.0]], ["only"], 3) == ["only"]
