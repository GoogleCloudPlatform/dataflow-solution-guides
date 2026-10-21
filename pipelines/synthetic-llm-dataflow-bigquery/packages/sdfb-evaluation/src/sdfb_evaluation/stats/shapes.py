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
"""Shape masks and character-class profiles: the pure maths behind
`column.shape_head_tv` and `column.char_class_l1`.

`shape_of`, `collapse` and `char_class_presence` (originally `_charclasses`)
are VERBATIM ports of `scripts/e2e/freetext_crosscheck.py` — copied, never
imported, because this package stays independent of everything outside it
(`test_independence.py`, ADR 0041). `shape_head_tv` is likewise a verbatim
port of that script's `_shape_tv`, with its module-level `_SHAPE_MASS_MIN`
constant turned into an explicit `floor` parameter (default 0.02, the same
value) so a caller here does not need a second constant to stay in sync
with the script's.

`char_class_fractions` and `char_class_l1` are new: the first turns a batch
of raw values into the same `{class: fraction of non-empty values
containing it}` profile `freetext_crosscheck.py`'s `_profile_sample` builds
inline (for the Beam census pass, which needs it as a standalone,
mergeable-friendly step); the second is the L1 distance between two such
profiles.

Design: docs/designs/2026-07-07-evaluation-framework-design.md

References:
  ADR 0026 — measurement first, then mask integrity (the head/tail
    grouping `shape_head_tv` implements, and its inverted-mass example).
    docs/adr/0026-measurement-first-mask-integrity.md
  scripts/e2e/freetext_crosscheck.py — shape_of, collapse, _charclasses,
    _shape_tv (the functions this module ports).

Design: docs/DESIGN.md §5 Fidelity; §11 Evaluation (sdfb-evaluation)
(ADR 0026, 0041).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

# A shape must hold at least this share of source mass to keep its own bin
# in `shape_head_tv`'s default grouping — verbatim value of
# `freetext_crosscheck.py`'s `_SHAPE_MASS_MIN` (ADR 0026).
_DEFAULT_SHAPE_MASS_FLOOR = 0.02

_CHARACTER_CLASSES = ("digit", "upper", "lower", "space", "punct")


def shape_of(value: str) -> str:
  """Mask `value` to a character-class skeleton: digit -> `9`, uppercase ->
  `A`, lowercase -> `a`, whitespace -> `␣`, everything else kept literally
  (punctuation, symbols).

  VERBATIM port of `scripts/e2e/freetext_crosscheck.py`'s `shape_of`.
  """
  out = []
  for ch in value:
    if ch.isdigit():
      out.append("9")
    elif ch.isalpha() and ch.isupper():
      out.append("A")
    elif ch.isalpha():
      out.append("a")
    elif ch.isspace():
      out.append("␣")
    else:
      out.append(ch)
  return "".join(out)


def collapse(mask: str) -> str:
  """Run-length-collapse a `shape_of` mask: a run of 2+ repeated `9`/`A`/
  `a`/`␣` becomes `<char>+`; everything else (including single-character
  runs and any literal punctuation) is kept as-is.

  VERBATIM port of `scripts/e2e/freetext_crosscheck.py`'s `collapse`.
  """
  out, i, n = [], 0, len(mask)
  while i < n:
    ch = mask[i]
    j = i
    while j < n and mask[j] == ch:
      j += 1
    run_len = j - i
    if ch in "9Aa␣" and run_len > 1:
      out.append(f"{ch}+")
    else:
      out.append(mask[i:j])
    i = j
  return "".join(out)


def char_class_presence(value: str) -> dict[str, bool]:
  """Whether `value` contains at least one character of each of the five
  classes (digit, upper, lower, space, punct).

  VERBATIM port of `scripts/e2e/freetext_crosscheck.py`'s `_charclasses`.
  """
  return {
      "digit": any(c.isdigit() for c in value),
      "upper": any(c.isalpha() and c.isupper() for c in value),
      "lower": any(c.isalpha() and c.islower() for c in value),
      "space": any(c.isspace() for c in value),
      "punct": any(not c.isalnum() and not c.isspace() for c in value),
  }


def char_class_fractions(values: Iterable[str]) -> dict[str, float]:
  """For each of the five character classes, the fraction of `values`'
  non-empty, non-`None` entries that contain it.

  Mirrors the `charclass_fraction` computation inline in
  `freetext_crosscheck.py`'s `_profile_sample`, split out as its own
  reusable step (the Beam census builds this per bundle, then merges).
  All-blank (or empty) input reports every class at 0.0 rather than
  dividing by zero.
  """
  vals = [v for v in values if v is not None and v != ""]
  if not vals:
    return dict.fromkeys(_CHARACTER_CLASSES, 0.0)
  counts = dict.fromkeys(_CHARACTER_CLASSES, 0)
  for v in vals:
    for cls, present in char_class_presence(v).items():
      if present:
        counts[cls] += 1
  n = len(vals)
  return {cls: counts[cls] / n for cls in _CHARACTER_CLASSES}


def _plain_sum(values: Iterable[float]) -> float:
  """`values` added left to right. Not the builtin `sum()`: CPython 3.12
  made the `sum()` of floats compensated, which moves the last bit of a
  total, and a metric must not depend on the interpreter that computed it.
  """
  total = 0.0
  for value in values:
    total += value
  return total


def char_class_l1(
    src_frac: Mapping[str, float],
    syn_frac: Mapping[str, float],
) -> float:
  """`sum_k |syn_frac[k] - src_frac[k]|` over the five character classes; a
  class missing from either mapping counts as 0.
  """
  return _plain_sum(
      abs(syn_frac.get(cls, 0.0) - src_frac.get(cls, 0.0))
      for cls in _CHARACTER_CLASSES)


def shape_head_tv(
    src_mass: Mapping[str, float],
    syn_mass: Mapping[str, float],
    floor: float = _DEFAULT_SHAPE_MASS_FLOOR,
) -> tuple[float, float, set[str]]:
  """`(raw, head, head_shapes)`: two total-variation readings over shape
  mass marginals, plus the "head" shape set the second one grouped on.

  `raw` is plain TV over the union of shapes — exact, but it saturates near
  1.0 on near-unique-mask columns (e.g. identifiers), where two disjoint
  near-unique mask sets read as maximally divergent even for a perfect
  generator. `head` groups the distribution as {each shape with source mass
  >= `floor`} + {everything else pooled into one "other" bucket}, which
  keeps a mass INVERSION on a named shape visible (ADR 0026: a source split
  of 68.6%/30.9% between two named shapes rendered as 7.7%/92.2% synthetic
  reads as head TV > 0.6) while collapsing a near-unique tail into a single
  bucket that a perfect generator scores near 0 on.

  VERBATIM port of `scripts/e2e/freetext_crosscheck.py`'s `_shape_tv`, with
  its `_SHAPE_MASS_MIN` module constant turned into the `floor` parameter
  and its sums added left to right (`_plain_sum`).
  """
  raw = 0.5 * _plain_sum(
      abs(src_mass.get(s, 0.0) - syn_mass.get(s, 0.0))
      for s in set(src_mass) | set(syn_mass))
  head_shapes = {s for s, m in src_mass.items() if m >= floor}
  src_other = _plain_sum(m for s, m in src_mass.items() if s not in head_shapes)
  syn_other = _plain_sum(m for s, m in syn_mass.items() if s not in head_shapes)
  head = 0.5 * (
      _plain_sum(
          abs(src_mass.get(s, 0.0) - syn_mass.get(s, 0.0))
          for s in head_shapes) + abs(src_other - syn_other))
  return raw, head, head_shapes
