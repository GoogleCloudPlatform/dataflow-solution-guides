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
"""Retrieval picks do not depend on the interpreter's `sum()`.

CPython 3.12 made the builtin `sum()` of floats a compensated sum. The
pure-Python retrieval summed with it in three places, so on inputs whose
candidates tie (or nearly tie) the last bits of a distance decided the
pick, and the pick changed with the interpreter:

    site                          near-tie below            3.11    3.12+
    ────────────────────────────  ────────────────────────  ──────  ──────
    retrieval._sq_dist            8 x 4 grid, k = 4         item 5  item 0
    index._normalize              [-0.6, -0.1, 0.8]         (last bits)
    index._PyExactIPIndex.search  a vector and 3 x itself   idx 3   idx 4

Every expectation here is what CPython 3.11 computed with the code as it
was, which is also what the plain left-to-right sums of the GUI's
TypeScript mirror (`gui/packages/stats/src/retrieval.ts`) compute. The
sums are now explicit loops; these tests fail if one goes back to the
builtin on an interpreter that compensates.
"""

# Test module: white-box access is intentional.
# pylint: disable=protected-access

from __future__ import annotations

import pytest
from sdfb_core.rag.embedding import HashingEmbedder
from sdfb_core.rag.index import _normalize, _PyExactIPIndex
from sdfb_core.rag.retrieval import (
    _sq_dist,
    retrieve_centroid_top_k,
    retrieve_kcenter_k,
    select_seed_examples,
)

# Values on a 0.1 grid: inexact in binary, with many equal true distances.
_GRID = [
    [-0.7, -0.9, 0.9, 0.0],
    [0.4, 0.3, 0.7, 0.2],
    [-0.5, -0.3, -0.1, 0.5],
    [-0.9, -0.2, 0.5, -0.9],
    [-0.6, -0.7, 0.6, 0.4],
    [-0.9, 0.7, 0.4, 0.2],
    [-0.8, -0.3, -0.8, 0.9],
    [0.2, -0.4, -0.3, 0.2],
]

# Rows 3 and 4 point the same way (row 3 is three times row 4), so their
# cosine to any query is one number and the lower index must win.
_SCALED = [
    [-0.2, -0.7, -0.6],
    [-0.1, 0.3, 0.6],
    [0.9, -0.7, 0.8],
    [0.6, 0.6, -0.9],
    [0.2, 0.2, -0.3],
    [-0.1, -0.2, -0.1],
]

_TEXTS = [
    "alpha beta", "beta gamma", "Calle Mayor 12", "calle mayor 12 3B",
    "ACME S.L.", "acme sl", "N/A", "", "x y z", "lorem ipsum dolor sit amet",
    "Av. Diagonal 640", "avda diagonal 640", "0001-AB", "0002-AB", "0003-CD",
    "pending review", "review pending", "closed", "CLOSED", "re-opened"
]


def test_sq_dist_is_a_plain_left_to_right_sum():
  """Both distances are 1.5 in exact arithmetic. Summed left to right in
    binary the second lands one unit in the last place above the first, and
    that unit is what the k-center walk below turns into a pick."""
  assert _sq_dist(_GRID[0], _GRID[3]).hex() == "0x1.8000000000000p+0"
  assert _sq_dist(_GRID[5], _GRID[2]).hex() == "0x1.8000000000001p+0"


def test_normalize_matches_python_3_11_to_the_last_bit():
  assert [v.hex() for v in _normalize([-0.6, -0.1, 0.8])] == [
      "-0x1.31ace8a44e87ep-1", "-0x1.9791363068b53p-4", "0x1.9791363068b53p-1"
  ]
  assert _normalize([0.0, 0.0]) == [0.0, 0.0]


def test_kcenter_near_tie_picks_what_python_3_11_picked():
  items = list(range(len(_GRID)))
  assert retrieve_kcenter_k(_GRID, items, 4) == [2, 3, 1, 5]


def test_kcenter_rotate_near_tie_picks_what_python_3_11_picked():
  items = list(range(len(_GRID)))
  assert retrieve_kcenter_k(_GRID, items, 4, start=2) == [2, 3, 1, 5]


def test_exact_index_keeps_the_lower_index_of_two_parallel_vectors():
  index = _PyExactIPIndex(_SCALED, 3)
  try:
    got = retrieve_centroid_top_k(index, _SCALED, list(range(len(_SCALED))), 2)
  finally:
    index.release()
  assert got == [2, 3]


@pytest.mark.parametrize("strategy, attempt, expected", [
    ("centroid", 0, [
        "pending review", "review pending", "re-opened", "Av. Diagonal 640",
        "lorem ipsum dolor sit amet"
    ]),
    ("kcenter", 0, ["pending review", "Calle Mayor 12", "N/A", "", "x y z"]),
    ("kcenter_rotate", 0, ["alpha beta", "Calle Mayor 12", "N/A", "", "x y z"]),
    ("kcenter_rotate", 1,
     ["acme sl", "Calle Mayor 12", "N/A", "x y z", "avda diagonal 640"]),
])
def test_hashed_text_seeds_are_the_ones_python_3_11_chose(
    strategy, attempt, expected):
  """Hashed token vectors are sparse, so whole groups of texts sit at one
    true distance from a seed: the commonest near-tie there is."""
  vectors = HashingEmbedder(dim=64).embed(_TEXTS)
  assert select_seed_examples(
      vectors, _TEXTS, 5, strategy=strategy, attempt=attempt) == expected
