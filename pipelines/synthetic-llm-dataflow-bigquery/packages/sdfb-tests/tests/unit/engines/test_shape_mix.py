"""Exact shape-mix builder + digit-run mutator (Task 3)."""

import random

from sdfb_core.engines.text_shapes import (
    build_shape_mix,
    mutate_digit_runs,
    sample_relaxed_identifier,
    shape_mix_is_identifier_like,
)


def test_shape_mix_groups_by_mask_and_weights():
  vals = ["AB12", "CD34", "EF56", "12-99", "34-56"]
  shapes = build_shape_mix(vals)
  assert shapes is not None
  weights = sorted(w for w, _ in shapes)  # pylint: disable=not-an-iterable  # asserted above
  assert weights == [2, 3]  # 3x 'AA99', 2x '99-99'


def test_shape_mix_keeps_singletons():
  shapes = build_shape_mix(["AB12", "9-9x"])
  assert shapes is not None
  assert len(shapes) == 2


def test_shape_mix_none_on_empty_input():
  assert build_shape_mix([]) is None
  assert build_shape_mix(["", ""]) is None


def test_shape_mix_top_k_keeps_heaviest():
  vals = ["A1"] * 5 + ["B2"] * 5 + ["c3", "d4-", "5e."]
  shapes = build_shape_mix(vals, top_k=1)
  assert shapes is not None
  assert len(shapes) == 1
  # The two-heavy masks share one 'A9' mask.
  assert shapes[0][0] == 10  # pylint: disable=unsubscriptable-object  # asserted above


def test_shape_mix_samples_in_observed_shapes():
  vals = ["U900001", "U900002", "QQXB064", "QQXC703"]
  shapes = build_shape_mix(vals)
  rng = random.Random(7)
  for _ in range(50):
    v = sample_relaxed_identifier(shapes, rng.randrange)
    assert len(v) == 7
    assert v[0].isupper()


def test_shape_mix_spaces_allowed_and_kept_literal():
  vals = ["AB 12", "CD 34"]
  shapes = build_shape_mix(vals)
  assert shapes is not None
  rng = random.Random(1)
  v = sample_relaxed_identifier(shapes, rng.randrange)
  assert v[2] == " "


def test_identifier_like_detection():
  assert shape_mix_is_identifier_like(build_shape_mix(["AB1234", "CD5678"]))
  assert not shape_mix_is_identifier_like(
      build_shape_mix(["SEG.DE CAMBIO 12", "ABONO CANON A 34"]))


def test_mutate_digit_runs_preserves_shape_and_prefix_zero():
  rng = random.Random(3)
  src = "TRF.EX-090000123 A 17"
  seen = set()
  for _ in range(20):
    v = mutate_digit_runs(src, rng.randrange)
    assert len(v) == len(src)
    assert v.startswith("TRF.EX-0")  # leading zero of the run preserved
    assert v[-2:].isdigit()
    seen.add(v)
  assert len(seen) > 10  # actually mutating
  assert src not in seen or len(seen) > 15  # not a no-op generator


def test_mutate_digit_runs_leaves_short_runs_and_text():
  rng = random.Random(5)
  assert mutate_digit_runs("A1B", rng.randrange) == "A1B"  # runs < 2 untouched
  assert mutate_digit_runs("NODIGITS", rng.randrange) == "NODIGITS"
