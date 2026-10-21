#  Copyright 2026 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""A deterministic, mergeable bottom-k sampler: `priority` assigns every
key a pseudo-random `uint64` rank from a salt, and `BottomK` keeps the `k`
smallest `(priority, key)` pairs seen so far, together with each retained
key's exact multiplicity (Ruling R34).

This is coordinated/bottom-k sampling (Cohen, 1997; Cohen & Kaplan, 2007):
because `priority` is a pure function of `(salt, key)`, the same key always
gets the same rank everywhere it is computed — on any worker, in any run,
with the same salt — so two samples drawn this way agree on every key they
share (Task 24 relies on exactly this: sampling synthetic rows by a row-hash
key so a row that also exists in the source draws the same priority as its
source twin). `BottomK` is also mergeable: the true bottom-k of a union of
streams is always contained in the union of each stream's own bottom-k (an
item outside a stream's local bottom-k already has `k` smaller items from
that stream alone, so it cannot be in the global bottom-k either), which is
what makes `merge` exact, pure and independent of how work was partitioned.

`BottomK` maintains its own indexed binary max-heap on `(priority, key)`
(rather than delegating to `heapq`) plus a `key -> heap index` map kept
correct through every swap, so that re-adding an already-held key — the
common case once a table has duplicate/memorized rows, since `priority` is
a pure function of the key and so always recomputes the SAME priority for
it — resolves in O(1) instead of an O(k) scan for the held entry.

Design: docs/designs/2026-07-07-evaluation-framework-design.md

References:
  Cohen, E. (1997), "Size-Estimation Framework with Applications to
    Transitive Closure and Reachability" (bottom-k / min-hash sampling).
  Cohen, E., Kaplan, H. (2007), "Summarizing Data using Bottom-k Sketches"
    (mergeability of bottom-k sketches).
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import blake2b
from typing import Any

from sdfb_evaluation.canonical import canonical_json

# A held entry as a mutable 4-list `[priority, key, payload, count]` rather
# than a tuple or a small class: `_resolve_duplicate`'s common O(1) path
# mutates `payload`/`count` in place at a known heap index without
# disturbing heap order (which depends only on `priority`/`key`), and a
# plain list avoids both the attribute-lookup cost and the boilerplate of a
# `__slots__` class for four fields.


def priority(salt: str, key: int) -> int:
  """`key`'s pseudo-random `uint64` rank under `salt`.

  `int.from_bytes(blake2b(f"{salt}:{key}".encode(), digest_size=8).digest(),
  "big")`: a pure function of `(salt, key)` alone, so it is reproducible
  across workers and runs, and different for a different `salt` or `key`
  with overwhelming probability (a 64-bit hash). `BottomK` uses this rank,
  never `key` itself or insertion order, to decide who stays — and, because
  it is pure, `BottomK.add` sees the SAME priority every time the same key
  recurs, which is what makes O(1) duplicate resolution and exact
  multiplicity tracking (Ruling R34) both possible.
  """
  digest = blake2b(f"{salt}:{key}".encode(), digest_size=8).digest()
  return int.from_bytes(digest, "big")


def _tie_rank(payload: Any) -> str:
  """A canonical, order-independent tie-break string for two payloads under
  the identical key (Ruling R35; see `BottomK._resolve_duplicate`).

  Prefers `canonical.canonical_json`, wrapping a non-`Mapping` payload as
  its own single-column "row" (`canonical_json({"payload": payload},
  ["payload"])`) so any payload type reaches the same canonicalisation
  `evaluation_row_flags` uses for real rows — a pure function of the
  payload's own value, never of which `add`/`merge` call happened to see it
  first, so choosing the smaller rank is symmetric:
  `resolve(a, b) == resolve(b, a)` for any two payload values, which is
  what keeps duplicate-key resolution (and therefore `merge`)
  order-independent. Falls back to `repr(payload)` only if `canonical_json`
  raises `TypeError` (e.g. a `Mapping` payload with mutually-incomparable
  key types, which `json.dumps(..., sort_keys=True)` cannot sort) — `repr`
  is not itself canonical across equal-but-differently-represented values,
  but it is still a pure, order-independent function of the payload.
  """
  try:
    if isinstance(payload, Mapping):
      return canonical_json(payload, sorted(payload, key=str))
    return canonical_json({"payload": payload}, ["payload"])
  except TypeError:
    return repr(payload)


def _coerce_int(value: Any) -> int:
  """`value` as a plain Python `int`.

  `priority`/`key` may arrive as numpy integer scalars (`np.uint64` row
  hashes from `canonical.linear_hash`, in particular) rather than plain
  `int`; numpy's fixed-width arithmetic can silently wrap or raise a
  `RuntimeWarning` on values that don't fit signed 64-bit range, so every
  `priority`/`key` this module compares or stores is coerced to Python's
  arbitrary-precision `int` immediately on entry, once, rather than trusted
  to behave like one from whatever type the caller passed in.
  """
  return int(value)


class BottomK:
  """The `k` smallest `(priority, key)` pairs seen by `add`/`merge`, each
  with its payload and its exact multiplicity, via an indexed max-heap on
  `(priority, key)` bounded to size `k` at all times.

  Ties on `priority` are broken by `key` (both are plain integers, so the
  order is total and needs no randomness); a `key` added more than once —
  which, since `priority` is a pure function of `(salt, key)`, normally
  means the same priority reaching `add` twice with two different payloads
  — resolves via `_resolve_duplicate` rather than either occupying its own
  slot or letting arrival order decide, and increments that key's held
  count (Ruling R34) rather than discarding the observation.

  `k` must be non-negative; `k = 0` is a valid, permanently empty sampler
  (every `add` is then a no-op).

  Exact multiplicity (Ruling R34): a key retained at the end was admitted
  on its very first `add` and never evicted afterwards, in every stream
  that ever fed this structure (directly or through a `merge`). Proof: the
  admission threshold — the current worst `(priority, key)` among the held
  entries once the heap is full — only ever falls (an `add` only replaces
  the worst entry with a strictly smaller candidate) or is undefined
  (heap not yet full, everything is admitted). So once a key is rejected
  or evicted at some threshold, it stays rejected at every later, stricter
  threshold; a key retained at the end can therefore never have been
  rejected or evicted along the way. That means every `add` for a
  currently-held key really is another sighting of the SAME key (not a
  competing candidate that happens to share a slot), so incrementing its
  count on each such `add` — and summing counts for a key held by both
  operands of a `merge` — gives its exact total multiplicity across the
  full input, not merely a lower bound.
  """

  def __init__(self, k: int) -> None:
    if k < 0:
      raise ValueError(f"k must be non-negative, got {k}")
    self._k = k
    # Indexed max-heap on (priority, key): heap[0] is always the WORST held
    # entry, the one evicted first when a smaller candidate arrives. Each
    # entry is `[priority, key, payload, count]`; `self._pos` maps every
    # currently-held key to its exact index in `self._heap`, kept correct
    # through every swap (`_swap`) so a duplicate key resolves in O(1)
    # (look up `self._pos[key]`) instead of scanning the heap.
    self._heap: list[list[Any]] = []
    self._pos: dict[int, int] = {}

  def __len__(self) -> int:
    return len(self._heap)

  # -- indexed max-heap: parent >= both children, by (priority, key) -----

  def _order(self, index: int) -> tuple[int, int]:
    entry = self._heap[index]
    return (entry[0], entry[1])

  def _swap(self, i: int, j: int) -> None:
    self._heap[i], self._heap[j] = self._heap[j], self._heap[i]
    self._pos[self._heap[i][1]] = i
    self._pos[self._heap[j][1]] = j

  def _sift_up(self, index: int) -> None:
    while index > 0:
      parent = (index - 1) // 2
      if self._order(index) <= self._order(parent):
        break
      self._swap(index, parent)
      index = parent

  def _sift_down(self, index: int) -> None:
    size = len(self._heap)
    while True:
      largest = index
      left, right = 2 * index + 1, 2 * index + 2
      if left < size and self._order(left) > self._order(largest):
        largest = left
      if right < size and self._order(right) > self._order(largest):
        largest = right
      if largest == index:
        return
      self._swap(index, largest)
      index = largest

  def _push(self, entry: list[Any]) -> None:
    self._heap.append(entry)
    index = len(self._heap) - 1
    self._pos[entry[1]] = index
    self._sift_up(index)

  def _replace_root(self, entry: list[Any]) -> None:
    del self._pos[self._heap[0][1]]
    self._heap[0] = entry
    self._pos[entry[1]] = 0
    self._sift_down(0)

  # -- admission -----------------------------------------------------------

  def _resolve_duplicate(self, index: int, prio: int, payload: Any,
                         count: int) -> None:
    """Fold another observation of an already-held `key` (at `self._heap`
    index `index`) into that entry, in O(1) for the expected case.

    Equal priority (`prio == entry[0]`) is the expected case, since
    `priority` is a pure function of `(salt, key)`: adds `count` to the
    held entry's multiplicity and, if `payload` ranks lower than the held
    one under `_tie_rank`, replaces it — a symmetric comparison, so which
    `add`/`merge` call happens to arrive "first" never changes the
    outcome. Only this equal-priority branch is the one Ruling R34's
    multiplicity proof and the O(1) duplicate-resolution requirement cover.

    A DIFFERING priority for the same key cannot happen through the public
    `priority()` function — two different priorities implies two different
    `salt`s or an inconsistent caller — so it is out of contract; this
    still resolves deterministically (the smaller `(priority, key)` wins,
    same rule `add` uses for any two distinct candidates), but the losing
    side's count is discarded rather than summed, since the two
    observations no longer describe one consistent stream.
    """
    entry = self._heap[index]
    existing_prio = entry[0]
    if prio == existing_prio:
      entry[3] += count
      if _tie_rank(payload) < _tie_rank(entry[2]):
        entry[2] = payload
      return
    if prio < existing_prio:
      entry[0], entry[2], entry[3] = prio, payload, count
      # The ordering key changed; at most one of sift-up/sift-down actually
      # moves the entry; the other is then a same-index no-op.
      self._sift_up(index)
      self._sift_down(index)
    # else: the held entry already has the smaller priority and wins;
    # the candidate, including its count, is discarded.

  def _admit(self, prio: int, key: int, payload: Any, count: int) -> None:
    """`add`'s and `merge`'s shared admission logic, parameterised by the
    multiplicity (`count`) the incoming observation already carries.
    """
    if self._k == 0:
      return
    index = self._pos.get(key)
    if index is not None:
      self._resolve_duplicate(index, prio, payload, count)
      return
    if len(self._heap) < self._k:
      self._push([prio, key, payload, count])
      return
    if (prio, key) < self._order(0):
      self._replace_root([prio, key, payload, count])
    # else: no better than the current worst held entry, and the sampler
    # is already full; discard it (and its count).

  def add(self, prio: int, key: int, payload: Any) -> None:
    """Offer `(prio, key, payload)`, one observation of `key`; it is kept
    iff it belongs among the `k` smallest `(priority, key)` pairs seen so
    far, and its multiplicity is tracked exactly (see the class docstring).
    """
    self._admit(_coerce_int(prio), _coerce_int(key), payload, 1)

  def merge(self, other: BottomK) -> BottomK:
    """A new `BottomK(k)` holding the `k` smallest `(priority, key)` pairs
    across `self` and `other` combined, each with its summed multiplicity.
    Pure: neither operand is modified.

    Exact, associative and commutative: `self`'s and `other`'s own kept
    entries already dominate everything each of them discarded (see the
    class docstring), so replaying only those (at most `2k`) entries, with
    their already-accumulated counts, through a fresh `BottomK._admit` — in
    either order, since admission selects purely by `(priority, key)`
    value, never arrival order — reconstructs the true bottom-k of the
    full union, counts included.

    Raises `ValueError` if `other`'s `k` differs from `self`'s: merging
    samplers built for different `k` has no well-defined result (a smaller
    `k` is not a subset of a larger one keyed the same way), and silently
    picking one side's `k` would make the result depend on merge
    order/direction.
    """
    other_k = other._k  # pylint: disable=protected-access
    if other_k != self._k:
      raise ValueError(
          f"cannot merge BottomK(k={self._k}) with BottomK(k={other_k}): "
          "both operands must share the same k")
    # `merged`/`other` are BottomK like `self`; replaying each one's held
    # entries into a fresh instance is this class's own merge logic, not
    # an outside caller reaching into private state.
    merged = BottomK(self._k)
    for prio, key, payload, count in self._heap:
      merged._admit(prio, key, payload, count)  # pylint: disable=protected-access
    for prio, key, payload, count in other._heap:  # pylint: disable=protected-access
      merged._admit(prio, key, payload, count)  # pylint: disable=protected-access
    return merged

  def extract_with_counts(self) -> list[tuple[Any, int]]:
    """`(payload, count)` for every held key, ordered by `(priority, key)`
    ascending; `count` is that key's exact multiplicity (Ruling R34).
    """
    ordered = sorted(self._heap, key=lambda entry: (entry[0], entry[1]))
    return [(entry[2], entry[3]) for entry in ordered]

  def extract(self) -> list[Any]:
    """The kept payloads, ordered by `(priority, key)` ascending."""
    return [payload for payload, _ in self.extract_with_counts()]
