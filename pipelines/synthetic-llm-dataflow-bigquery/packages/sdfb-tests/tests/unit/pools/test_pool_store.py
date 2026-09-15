"""The free-text pool store seam (WS5 T4).

Deliberately a SIBLING of ChunkStore, not a chunk_kind of it: pools key on
model_uri (which LLM produced the values), chunks key on embedder_id /
embedder_version (which vector space). One fetch signature cannot answer
both questions honestly.
"""

from __future__ import annotations

from sdfb_core.pools import FreeTextPool, FreeTextPoolStore, InMemoryFreeTextPoolStore


def _pool(column: str,
          digest: str = "d1",
          model: str = "m1",
          **kw) -> FreeTextPool:
  return FreeTextPool(
      reference_digest=digest,
      model_uri=model,
      column=column,
      target=kw.pop("target", 8),
      values=kw.pop("values", ("a", "b")),
      stagnated=kw.pop("stagnated", False),
      attempts=kw.pop("attempts", 1),
  )


def test_in_memory_store_satisfies_the_protocol():
  assert isinstance(InMemoryFreeTextPoolStore(), FreeTextPoolStore)


def test_fetch_filters_by_digest_and_model():
  store = InMemoryFreeTextPoolStore(
      [_pool("a"),
       _pool("b", digest="other"),
       _pool("c", model="other")])
  assert [p.column for p in store.fetch("d1", "m1")] == ["a"]


def test_exists_is_false_for_unknown_digest():
  store = InMemoryFreeTextPoolStore([_pool("a")])
  assert store.exists("d1", "m1") is True
  assert store.exists("nope", "m1") is False


def test_exists_is_false_for_a_different_model():
  """The same reference sample under a different LLM is a different pool."""
  store = InMemoryFreeTextPoolStore([_pool("a")])
  assert store.exists("d1", "other-model") is False


def test_stagnation_is_recorded_not_rediscovered():
  """A pool that stagnated below target is stored AS stagnated, with the
    attempt count that proved it — so the next worker reads the conclusion
    instead of re-running the ladder (2026-07-26 E2E: 36x per column)."""
  store = InMemoryFreeTextPoolStore(
      [_pool("k", stagnated=True, attempts=12, values=tuple("abcdefgh"))])
  got = store.fetch("d1", "m1")[0]
  assert got.stagnated is True
  assert got.attempts == 12
  assert len(got.values) == 8


def test_add_extends_the_store():
  store = InMemoryFreeTextPoolStore()
  assert store.exists("d1", "m1") is False
  store.add([_pool("a")])
  assert store.exists("d1", "m1") is True


def test_pool_is_frozen_and_hashable():
  """Pools are values, not mutable state — they get compared and cached."""
  a, b = _pool("a"), _pool("a")
  assert a == b
  assert len({a, b}) == 1
