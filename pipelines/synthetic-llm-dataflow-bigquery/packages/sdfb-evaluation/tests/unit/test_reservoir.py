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
"""Tests for `sdfb_evaluation.sampling.reservoir` (Task 15; fix round 1 adds
the O(1) duplicate path, exact multiplicity (Ruling R34), the R35 tie-break
and numpy-integer coercion coverage).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import itertools
import warnings
from collections import Counter

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sdfb_evaluation.sampling.reservoir import BottomK
from sdfb_evaluation.sampling.reservoir import priority

# ---------------------------------------------------------------------------
# priority()
# ---------------------------------------------------------------------------


def test_priority_is_deterministic_uint64():
  a = priority("salt-a", 42)
  b = priority("salt-a", 42)
  assert a == b
  assert 0 <= a < 2**64


def test_priority_different_salt_gives_different_value():
  a = priority("salt-a", 42)
  b = priority("salt-b", 42)
  assert a != b


def test_priority_different_key_gives_different_value():
  a = priority("salt", 1)
  b = priority("salt", 2)
  assert a != b


# ---------------------------------------------------------------------------
# BottomK — basic behaviour
# ---------------------------------------------------------------------------


def test_bottomk_keeps_the_k_smallest_priorities():
  bk = BottomK(3)
  for key in range(10):
    bk.add(priority("s", key), key, payload=f"row-{key}")
  extracted = bk.extract()
  assert len(extracted) == 3
  # The 3 keys kept must be exactly the 3 with the smallest priority.
  ranked = sorted(range(10), key=lambda key: (priority("s", key), key))
  expected = [f"row-{key}" for key in ranked[:3]]
  assert extracted == expected


def test_bottomk_never_exceeds_k_after_any_add():
  bk = BottomK(5)
  for key in range(200):
    bk.add(priority("s", key), key, payload=key)
    assert len(bk.extract()) <= 5


def test_bottomk_extract_sorted_by_priority_then_key():
  bk = BottomK(4)
  for key in range(50):
    bk.add(priority("sort-check", key), key, payload=key)
  extracted_keys = bk.extract()
  priorities = [priority("sort-check", key) for key in extracted_keys]
  assert priorities == sorted(priorities)


def test_bottomk_k_zero_always_empty():
  bk = BottomK(0)
  for key in range(20):
    bk.add(priority("s", key), key, payload=key)
  assert bk.extract() == []


def test_bottomk_rejects_negative_k():
  with pytest.raises(ValueError):
    BottomK(-1)


def test_ties_on_equal_priority_broken_by_key_both_insertion_orders():
  p = 12345
  bk1 = BottomK(1)
  bk1.add(p, 100, "A")
  bk1.add(p, 50, "B")  # smaller key must win when priority ties
  bk2 = BottomK(1)
  bk2.add(p, 50, "B")
  bk2.add(p, 100, "A")
  assert bk1.extract() == ["B"]
  assert bk2.extract() == ["B"]


# ---------------------------------------------------------------------------
# merge — purity, associativity, commutativity, same-k requirement
# ---------------------------------------------------------------------------


def test_merge_is_pure():
  a, b = BottomK(3), BottomK(3)
  for key in range(5):
    a.add(priority("s", key), key, key)
  for key in range(5, 10):
    b.add(priority("s", key), key, key)
  a_before, b_before = a.extract(), b.extract()
  merged = a.merge(b)
  assert a.extract() == a_before
  assert b.extract() == b_before
  assert merged.extract() != []


def test_merge_matches_single_pass_bottomk():
  keys = list(range(30))
  bk_a, bk_b, bk_all = BottomK(6), BottomK(6), BottomK(6)
  for key in keys[:15]:
    p = priority("merge-check", key)
    bk_a.add(p, key, key)
    bk_all.add(p, key, key)
  for key in keys[15:]:
    p = priority("merge-check", key)
    bk_b.add(p, key, key)
    bk_all.add(p, key, key)
  merged = bk_a.merge(bk_b)
  assert merged.extract() == bk_all.extract()


def test_merge_is_commutative():
  a, b = BottomK(4), BottomK(4)
  for key in range(20):
    p = priority("commute", key)
    (a if key % 2 == 0 else b).add(p, key, key)
  assert a.merge(b).extract() == b.merge(a).extract()


def test_merge_different_k_raises_value_error():
  small, large = BottomK(2), BottomK(5)
  for key in range(10):
    small.add(priority("s", key), key, key)
    large.add(priority("s", key), key, key)
  with pytest.raises(ValueError):
    small.merge(large)
  with pytest.raises(ValueError):
    large.merge(small)


def test_duplicate_key_across_merge_operands_is_commutative():
  key = 7
  p = priority("dup-merge", key)
  a = BottomK(3)
  a.add(p, key, "payload-A")
  b = BottomK(3)
  b.add(p, key, "payload-B")
  assert a.merge(b).extract_with_counts() == b.merge(a).extract_with_counts()
  # The two operands each saw the key once, so the merged multiplicity is 2.
  assert a.merge(b).extract_with_counts()[0][1] == 2


# ---------------------------------------------------------------------------
# Hypothesis: order/tree independence, and the k-bound
# ---------------------------------------------------------------------------


@settings(max_examples=40)
@given(
    st.lists(
        st.integers(min_value=0, max_value=999),
        min_size=0,
        max_size=60,
        unique=True).flatmap(st.permutations),
    st.integers(min_value=0, max_value=20),
)
def test_bottomk_result_independent_of_insertion_order(order, k):
  salt = "order-independence"

  baseline = BottomK(k)
  for key in sorted(order):
    baseline.add(priority(salt, key), key, key)

  permuted = BottomK(k)
  for key in order:
    permuted.add(priority(salt, key), key, key)

  assert baseline.extract_with_counts() == permuted.extract_with_counts()
  assert len(permuted.extract()) <= k


@settings(max_examples=30)
@given(
    st.lists(
        st.integers(min_value=0, max_value=999),
        min_size=0,
        max_size=90,
        unique=True),
    st.integers(min_value=0, max_value=15),
)
def test_bottomk_result_independent_of_merge_tree_shape(keys, k):
  salt = "merge-tree"
  entries = [(priority(salt, key), key, key) for key in keys]

  def build(items) -> BottomK:
    bk = BottomK(k)
    for prio, key, payload in items:
      bk.add(prio, key, payload)
    return bk

  # Tree A: one flat pass.
  flat = build(entries)

  # Tree B: split into thirds, build each, merge pairwise in a different
  # associativity than a naive left-fold.
  third = max(1, len(entries) // 3 or 1)
  chunks = [entries[i:i + third] for i in range(0, len(entries), third)] or [[]]
  parts = [build(chunk) for chunk in chunks]
  # Fold right-to-left instead of left-to-right, and pair up first.
  while len(parts) > 1:
    paired = []
    it = iter(parts)
    for left, right in itertools.zip_longest(it, it):
      paired.append(left.merge(right) if right is not None else left)
    parts = paired
  tree = parts[0]

  assert flat.extract() == tree.extract()
  assert len(flat.extract()) <= k


@settings(max_examples=40)
@given(
    st.lists(st.integers(min_value=0, max_value=50), min_size=0, max_size=150),
    st.integers(min_value=0, max_value=10),
)
def test_bottomk_counts_match_true_multiplicity_under_any_merge_tree(keys, k):
  """Ruling R34: a retained key's count equals its true occurrence count in
  the input, regardless of insertion order or merge-tree shape. `keys` is
  NOT `unique=True`, so repeats are common in this property.
  """
  salt = "multiplicity"
  true_counts = Counter(keys)

  flat = BottomK(k)
  for key in keys:
    flat.add(priority(salt, key), key, key)

  third = max(1, len(keys) // 3 or 1)
  chunks = [keys[i:i + third] for i in range(0, len(keys), third)] or [[]]

  def build(chunk) -> BottomK:
    bk = BottomK(k)
    for key in chunk:
      bk.add(priority(salt, key), key, key)
    return bk

  parts = [build(chunk) for chunk in chunks]
  while len(parts) > 1:
    paired = []
    it = iter(parts)
    for left, right in itertools.zip_longest(it, it):
      paired.append(left.merge(right) if right is not None else left)
    parts = paired
  tree = parts[0]

  assert flat.extract_with_counts() == tree.extract_with_counts()
  for payload, count in flat.extract_with_counts():
    assert count == true_counts[payload]


# ---------------------------------------------------------------------------
# Uniformity and salt sensitivity
# ---------------------------------------------------------------------------


def test_bottomk_uniform_across_key_id_deciles():
  n_keys, k, deciles = 20_000, 1000, 10
  bk = BottomK(k)
  for key in range(n_keys):
    bk.add(priority("uniformity", key), key, key)
  kept_keys = bk.extract()
  assert len(kept_keys) == k

  bucket_size = n_keys // deciles
  counts = [0] * deciles
  for key in kept_keys:
    counts[min(key // bucket_size, deciles - 1)] += 1

  expected = k // deciles  # 100
  for count in counts:
    assert abs(count - expected) <= 35, counts


def test_different_salt_gives_a_different_sample_not_just_a_different_priority(
):
  keys = range(500)
  bk_a, bk_b = BottomK(20), BottomK(20)
  for key in keys:
    bk_a.add(priority("salt-a", key), key, key)
    bk_b.add(priority("salt-b", key), key, key)
  assert set(bk_a.extract()) != set(bk_b.extract())


# ---------------------------------------------------------------------------
# Duplicate keys: resolution, capacity, and the R35 tie-break
# ---------------------------------------------------------------------------


def test_duplicate_key_keeps_one_payload_deterministically():
  salt = "dup"
  key = 7
  p = priority(salt, key)

  order_a = BottomK(3)
  order_a.add(p, key, "payload-A")
  order_a.add(p, key, "payload-B")

  order_b = BottomK(3)
  order_b.add(p, key, "payload-B")
  order_b.add(p, key, "payload-A")

  assert order_a.extract() == order_b.extract()
  assert len(order_a.extract()) == 1


def test_duplicate_key_does_not_consume_extra_capacity():
  salt = "dup-capacity"
  bk = BottomK(2)
  p = priority(salt, 1)
  bk.add(p, 1, "first")
  bk.add(p, 1, "second")
  bk.add(priority(salt, 2), 2, "other")
  assert len(bk.extract()) == 2


def test_duplicate_key_increments_count():
  salt = "dup-count"
  p = priority(salt, 1)
  bk = BottomK(2)
  bk.add(p, 1, "a")
  bk.add(p, 1, "b")
  bk.add(p, 1, "c")
  [(_, count)] = bk.extract_with_counts()
  assert count == 3


def test_tie_break_falls_back_to_repr_for_incomparable_mapping_keys():
  # A payload that is a Mapping with mixed str/int keys makes
  # `json.dumps(..., sort_keys=True)` raise TypeError while trying to sort
  # the keys — this is the realistic path that exercises the repr fallback
  # (Ruling R35). The winner must still be symmetric/order-independent.
  salt = "tie-break-fallback"
  key = 1
  p = priority(salt, key)
  payload_a = {"a": 1, 2: "b"}
  payload_b = {"a": 2, 2: "c"}

  order_a = BottomK(1)
  order_a.add(p, key, payload_a)
  order_a.add(p, key, payload_b)

  order_b = BottomK(1)
  order_b.add(p, key, payload_b)
  order_b.add(p, key, payload_a)

  assert order_a.extract() == order_b.extract()
  winner = order_a.extract()[0]
  assert winner in (payload_a, payload_b)
  assert winner == (
      payload_a if repr(payload_a) < repr(payload_b) else payload_b)


def test_tie_break_prefers_canonical_json_for_mapping_payloads():
  # Two Mappings that are canonically equal (same values, different key
  # order / insertion order) must tie-break identically regardless of
  # which literal dict object arrives first.
  salt = "canonical-tie"
  key = 1
  p = priority(salt, key)
  same_content_reordered = {"z": 1, "a": 2}
  other = {"a": 9, "z": 9}

  order_a = BottomK(1)
  order_a.add(p, key, same_content_reordered)
  order_a.add(p, key, other)

  order_b = BottomK(1)
  order_b.add(p, key, other)
  order_b.add(p, key, same_content_reordered)

  assert order_a.extract() == order_b.extract()


# ---------------------------------------------------------------------------
# numpy integer coercion
# ---------------------------------------------------------------------------


def test_numpy_uint64_keys_do_not_wrap_or_warn_and_tie_break_correctly():
  big_a = np.uint64(2**63 + 5)
  big_b = np.uint64(2**63 + 3)
  bk = BottomK(1)
  with warnings.catch_warnings():
    warnings.simplefilter("error")
    bk.add(np.uint64(100), big_a, "A")
    bk.add(np.uint64(100), big_b, "B")  # equal priority -> smaller key wins
  assert bk.extract() == ["B"]  # int(big_b) < int(big_a)


def test_numpy_uint64_priority_values_do_not_wrap_or_warn():
  bk = BottomK(1)
  with warnings.catch_warnings():
    warnings.simplefilter("error")
    bk.add(np.uint64(2**63 + 100), 1, "A")
    bk.add(np.uint64(2**63 + 1), 2, "B")  # strictly smaller priority
  assert bk.extract() == ["B"]
