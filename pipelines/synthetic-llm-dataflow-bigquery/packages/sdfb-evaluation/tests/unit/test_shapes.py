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
"""Tests for `sdfb_evaluation.stats.shapes` (Task 9).

Includes a parity block pinning `shape_of`/`collapse` outputs identical to
`packages/sdfb-tests/tests/unit/scripts/test_freetext_crosscheck.py`'s cases
— this module is a VERBATIM, independent port of
`scripts/e2e/freetext_crosscheck.py`'s shape-mining functions (never an
import: `test_independence.py` forbids reaching outside this package).

Design: docs/designs/2026-07-07-evaluation-framework-design.md

Design: docs/DESIGN.md §5 Fidelity
(ADR 0026).
"""

from __future__ import annotations

import pytest

from sdfb_evaluation.stats import shapes as s

# ---------------------------------------------------------------------------
# shape_of / collapse — brief's own cases
# ---------------------------------------------------------------------------


def test_shape_of_masks_character_classes():
  assert s.shape_of("Ab 12-x") == "Aa␣99-a"


def test_collapse_compresses_runs():
  assert s.collapse("AAAA99") == "A+9+"


# ---------------------------------------------------------------------------
# shape_of / collapse — parity with
# packages/sdfb-tests/tests/unit/scripts/test_freetext_crosscheck.py
# ---------------------------------------------------------------------------


def test_shape_of_parity_with_crosscheck():
  # test_shape_of_masks_charclasses in test_freetext_crosscheck.py
  assert s.shape_of("AB-12 x") == "AA-99␣a"


def test_collapse_parity_with_crosscheck():
  # test_collapse_compresses_runs in test_freetext_crosscheck.py
  assert s.collapse(s.shape_of("2026-01-15")) == "9+-9+-9+"
  assert s.collapse(s.shape_of("AB")) == "A+"


def test_shape_of_empty_string_is_empty():
  assert s.shape_of("") == ""


def test_collapse_singleton_runs_are_not_marked():
  # A run of length 1 in "9Aa␣" stays literal (no "+"); punctuation is
  # always emitted literally regardless of run length.
  assert s.collapse("A9a") == "A9a"
  assert s.collapse("--") == "--"


# ---------------------------------------------------------------------------
# char_class_presence / char_class_fractions / char_class_l1
# ---------------------------------------------------------------------------


def test_char_class_presence_matches_crosscheck_charclasses():
  assert s.char_class_presence("Ab 12-x") == {
      "digit": True,
      "upper": True,
      "lower": True,
      "space": True,
      "punct": True,
  }
  assert s.char_class_presence("abc") == {
      "digit": False,
      "upper": False,
      "lower": True,
      "space": False,
      "punct": False,
  }


def test_char_class_fractions_over_non_empty_values():
  fractions = s.char_class_fractions(["abc", "ABC", "123", "", None])
  assert fractions == {
      "digit": pytest.approx(1 / 3),
      "upper": pytest.approx(1 / 3),
      "lower": pytest.approx(1 / 3),
      "space": pytest.approx(0.0),
      "punct": pytest.approx(0.0),
  }


def test_char_class_fractions_all_blank_is_all_zero():
  assert s.char_class_fractions(["", None]) == {
      "digit": 0.0,
      "upper": 0.0,
      "lower": 0.0,
      "space": 0.0,
      "punct": 0.0,
  }


def test_char_class_l1_identical_profiles_is_zero():
  profile = {
      "digit": 0.4,
      "upper": 0.1,
      "lower": 0.9,
      "space": 0.2,
      "punct": 0.3
  }
  assert s.char_class_l1(profile, dict(profile)) == pytest.approx(0.0)


def test_char_class_l1_missing_keys_count_as_zero():
  assert s.char_class_l1({"digit": 1.0}, {}) == pytest.approx(1.0)
  assert s.char_class_l1({}, {"punct": 0.5}) == pytest.approx(0.5)


def test_char_class_l1_sums_absolute_deltas_over_five_classes():
  src = {"digit": 0.2, "upper": 0.0, "lower": 1.0, "space": 0.0, "punct": 0.0}
  syn = {"digit": 0.0, "upper": 0.5, "lower": 1.0, "space": 0.0, "punct": 0.1}
  assert s.char_class_l1(src, syn) == pytest.approx(0.2 + 0.5 + 0.0 + 0.0 + 0.1)


# ---------------------------------------------------------------------------
# shape_head_tv
# ---------------------------------------------------------------------------


def test_shape_head_tv_adr0026_inverted_mass_example():
  # ADR 0026: source masses {"AAA": 0.686, "999": 0.309, other tail} vs
  # synthetic {"AAA": 0.077, "999": 0.922} -> head TV > 0.6.
  src_mass = {"AAA": 0.686, "999": 0.309, "tail": 0.005}
  syn_mass = {"AAA": 0.077, "999": 0.922, "tail": 0.001}
  raw, head, head_shapes = s.shape_head_tv(src_mass, syn_mass)
  assert head > 0.6
  assert head_shapes == {"AAA", "999"}
  assert raw > 0.0


def test_shape_head_tv_disjoint_near_unique_tails():
  # Two disjoint near-unique mask sets (e.g. UUID-class columns): every
  # shape sits below the 2% floor, so head groups everything as "other"
  # (near 0 divergence) while raw (union, ungrouped) saturates near 1.
  src_mass = {f"s{i}": 0.01 for i in range(100)}
  syn_mass = {f"y{i}": 0.01 for i in range(100)}
  raw, head, head_shapes = s.shape_head_tv(src_mass, syn_mass)
  assert head_shapes == set()
  assert raw == pytest.approx(1.0)
  assert head == pytest.approx(0.0)


def test_shape_head_tv_identical_masses_is_zero():
  mass = {"9+-9+-9+": 0.6, "A+": 0.4}
  raw, head, head_shapes = s.shape_head_tv(mass, dict(mass))
  assert raw == pytest.approx(0.0)
  assert head == pytest.approx(0.0)
  assert head_shapes == {"9+-9+-9+", "A+"}


def test_shape_head_tv_custom_floor():
  src_mass = {"a": 0.05, "b": 0.95}
  syn_mass = {"a": 0.0, "b": 1.0}
  # With the default 0.02 floor "a" is in the head; with a 0.10 floor it
  # falls into the "other" bucket alongside nothing else.
  _, head_default, head_shapes_default = s.shape_head_tv(src_mass, syn_mass)
  _, head_wide, head_shapes_wide = s.shape_head_tv(
      src_mass, syn_mass, floor=0.10)
  assert head_shapes_default == {"a", "b"}
  assert head_shapes_wide == {"b"}
  assert head_default == pytest.approx(head_wide)


# CPython 3.12 made the builtin `sum()` of floats compensated, which moved
# the last bit of the values below. Both functions add their terms left to
# right, as 3.11 added them: these are the values 3.11 computed.
def test_char_class_l1_is_the_value_python_3_11_computed():
  src = {"digit": 0.1, "upper": 0.95, "lower": 0.0, "space": 0.75, "punct": 0.4}
  syn = {"digit": 0.7, "upper": 0.28, "lower": 0.24, "space": 0.9, "punct": 0.6}
  assert s.char_class_l1(src, syn).hex() == "0x1.dc28f5c28f5c3p+0"


def test_shape_head_tv_is_the_value_python_3_11_computed():
  """One head shape and four tail shapes, so the two "other" sums run over
  four masses each, in the mappings' own order."""
  src = {"AAAA": 0.94, "A9": 0.02, "AA9": 0.02, "A99": 0.01, "AA99": 0.01}
  syn = {"AAAA": 0.955, "A9": 0.015, "AA9": 0.005, "A99": 0.01, "AA99": 0.015}
  raw, head, head_shapes = s.shape_head_tv(src, syn, floor=0.5)
  assert head_shapes == {"AAAA"}
  assert head.hex() == "0x1.eb851eb851ebep-7"
  # `raw` runs over a set, whose order follows the process's hash seed: it
  # is the plain sum in that order, whatever the order is.
  total = 0.0
  for shape in set(src) | set(syn):
    total += abs(src.get(shape, 0.0) - syn.get(shape, 0.0))
  assert raw.hex() == (0.5 * total).hex()
