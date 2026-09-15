"""Shape-mix-templated fallback pools (2026-08-05 B_TABLE R1 remediation).

The freetext crosscheck found 4/13 columns reproducing **0%** of observed
source shapes whenever the relaxed-template fallback engaged: COL_026's
`␣␣␣…AAAA999A` (17 leading spaces + fixed suffix) cannot even reach
`build_relaxed_shapes` — its whitespace guard rejects the whole column and
the fallback returns []. `build_shape_mix` already models such columns
(spaces stay literal positions), so `_shape_fallback_pool` must template
from the profile's shape mix when it is expandable, and only then fall
back to the length-bucket relaxation.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=missing-class-docstring,protected-access

from __future__ import annotations

import pytest
from sdfb_core.engines.b1_rag import B1RagEngine
from sdfb_core.engines.b1_rag.profile import ColumnKind, ColumnProfile
from sdfb_core.engines.text_shapes import (
    build_shape_mix,
    shape_mix_can_template,
)

# COL_026-style: fixed leading-space padding, a literal prefix, varying
# digits, a literal suffix — all values share one mask.
_PADDED = [" " * 17 + f"COMP{i:03d}X" for i in (101, 205, 340, 411, 502)]


class TestShapeMixCanTemplate:

  def test_literal_whitespace_padding_is_templateable(self) -> None:
    assert shape_mix_can_template(build_shape_mix(_PADDED)) is True

  def test_none_is_not_templateable(self) -> None:
    assert shape_mix_can_template(None) is False

  def test_prose_masks_are_not_templateable(self) -> None:
    # Distinct prose masks land in single-value all-literal buckets:
    # nothing varies, so there is nothing to template.
    mix = build_shape_mix(["hello world foo", "another prose line"])
    assert shape_mix_can_template(mix) is False

  def test_class_position_containing_space_disqualifies(self) -> None:
    # A CLASS entry carrying whitespace means variable padding —
    # prose-like, not a code. (Unreachable from `build_shape_mix`,
    # which buckets by mask; guards hand-built RelaxedShapes.)
    shapes = ((2, ("A", " X", "9", "9")),)
    assert shape_mix_can_template(shapes) is False


class TestShapeFallbackPoolUsesShapeMix:

  @pytest.fixture
  def padded_profile(self) -> ColumnProfile:
    return ColumnProfile(
        name="COL_026",
        bq_type="STRING",
        kind=ColumnKind.FREE_TEXT,
        nullable=True,
        null_fraction=0.0,
        observed_values=tuple(_PADDED),
        shape_mix=build_shape_mix(_PADDED),
        is_unique_valued=True,
    )

  def test_space_padded_column_gets_a_fallback_pool(
      self, padded_profile: ColumnProfile) -> None:
    engine = B1RagEngine()
    pool = engine._shape_fallback_pool(padded_profile, 20, exclude=set())
    assert len(pool) == 20
    observed = set(_PADDED)
    for v in pool:
      assert v.startswith(" " * 17 + "COMP"), v
      assert v.endswith("X") and v[21:24].isdigit(), v
      assert v not in observed
