"""Shared string-shape detection (`sdfb_core.engines.text_shapes`).

The 2026-07-17 E2E runs showed both engines routing *shaped* STRING columns
to the LLM free-text pool, where they can only fail:

  - date-shaped strings (B.1 COL_044/COL_034/COL_061): any plausible
    generated date collides with the dense source keyspace → copy_ratio
    flags without memorization;
  - fixed-alphabet identifiers (B.2 COL_001, 24-char upper-hex): the model
    echoes the shown exemplars verbatim on every attempt → novel=0, DLQ.

Detection is shared, stdlib-only: temporal-shaped strings re-route to the
range sampler, identifier-shaped strings to a format-preserving generator.
"""

from __future__ import annotations

import random
import re
from datetime import datetime

from sdfb_core.engines.text_shapes import (
    build_relaxed_shapes,
    detect_identifier_shape,
    detect_temporal_format,
    sample_identifier,
    sample_relaxed_identifier,
)

# ---------------------------------------------------------------------------
# detect_temporal_format
# ---------------------------------------------------------------------------


def test_detects_iso_dates():
  values = [f"2024-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 8)]
  assert detect_temporal_format(values) == "%Y-%m-%d"


def test_detects_iso_datetimes_with_t_separator():
  values = [
      f"2024-06-01T{h:02d}:{m:02d}:33" for h in range(24) for m in (5, 25)
  ]
  assert detect_temporal_format(values) == "%Y-%m-%dT%H:%M:%S"


def test_detects_space_separated_datetimes_with_fraction():
  values = [
      f"2024-06-01 10:{m:02d}:33.{u:06d}" for m in range(30) for u in (1, 999)
  ]
  assert detect_temporal_format(values) == "%Y-%m-%d %H:%M:%S.%f"


def test_rejects_mixed_formats():
  values = ["2024-01-01", "2024-06-01T10:00:00", "2024-01-02"]
  assert detect_temporal_format(values) is None


def test_rejects_prose():
  values = ["The export job hangs.", "Password reset never arrives."]
  assert detect_temporal_format(values) is None


def test_rejects_empty_and_single():
  assert detect_temporal_format([]) is None
  assert detect_temporal_format(["2024-01-01"]) is None


def test_rendering_round_trips():
  values = ["2023-11-05", "2024-02-29", "2022-01-31"]
  fmt = detect_temporal_format(values)
  assert fmt is not None
  for v in values:
    assert datetime.strptime(v, fmt).strftime(fmt) == v


# ---------------------------------------------------------------------------
# detect_identifier_shape / sample_identifier
# ---------------------------------------------------------------------------


def _hex_upper_ids(n: int, length: int = 24, seed: int = 7) -> list[str]:
  rng = random.Random(seed)
  return [
      "".join(rng.choice("0123456789ABCDEF")
              for _ in range(length))
      for _ in range(n)
  ]


def test_detects_fixed_length_upper_hex():
  shape = detect_identifier_shape(_hex_upper_ids(80))
  assert shape is not None
  pick = random.Random(3).randrange
  for _ in range(20):
    assert re.fullmatch(r"[0-9A-F]{24}", sample_identifier(shape, pick))


def test_detects_uuid_shape_with_literal_hyphens():
  rng = random.Random(11)
  values = [
      f"{rng.getrandbits(32):08x}-{rng.getrandbits(16):04x}"
      f"-4{rng.getrandbits(12):03x}-a{rng.getrandbits(12):03x}"
      f"-{rng.getrandbits(48):012x}" for _ in range(60)
  ]
  shape = detect_identifier_shape(values)
  assert shape is not None
  pick = random.Random(5).randrange
  for _ in range(20):
    out = sample_identifier(shape, pick)
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-a[0-9a-f]{3}-[0-9a-f]{12}", out)


def test_preserves_literal_prefix():
  values = [f"INV-{i:06d}" for i in range(3000, 3100)]
  shape = detect_identifier_shape(values)
  assert shape is not None
  pick = random.Random(1).randrange
  for _ in range(20):
    assert re.fullmatch(r"INV-\d{6}", sample_identifier(shape, pick))


def test_rejects_varying_lengths():
  assert detect_identifier_shape(["AB12", "AB123", "AB1234"] * 30) is None


def test_rejects_prose_with_spaces():
  values = [f"Ticket about issue {i:04d}" for i in range(100)]
  assert detect_identifier_shape(values) is None


def test_rejects_short_strings():
  # Below the minimum length, "identifier" vs enum-code is ambiguous.
  assert detect_identifier_shape([f"{i:04X}" for i in range(100)]) is None


def test_rejects_empty():
  assert detect_identifier_shape([]) is None
  assert detect_identifier_shape(["ABCDEF12ABCDEF12"]) is None


def test_sample_identifier_is_deterministic_for_a_seeded_pick():
  shape = detect_identifier_shape(_hex_upper_ids(80))
  a = [sample_identifier(shape, random.Random(9).randrange) for _ in range(1)]
  b = [sample_identifier(shape, random.Random(9).randrange) for _ in range(1)]
  assert a == b


# ---------------------------------------------------------------------------
# build_relaxed_shapes / sample_relaxed_identifier — the last-resort template
# for LLM-echo-saturated free-text columns (2026-07-22 b2 E2E: COL_052,
# 96/96 prompt echoes over all escalation attempts → whole run FAILED).
# The strict detector rejects mixed lengths / short codes / punctuation
# variation; the relaxed builder buckets by length and keeps per-position
# observed character sets instead of disqualifying.
# ---------------------------------------------------------------------------


def test_relaxed_accepts_mixed_lengths_strict_detector_rejects():
  values = [f"USR{i}" for i in range(5, 250)]  # lengths 4..6, mixed
  assert detect_identifier_shape(values) is None
  shapes = build_relaxed_shapes(values)
  assert shapes is not None
  pick = random.Random(3).randrange
  for _ in range(30):
    out = sample_relaxed_identifier(shapes, pick)
    assert re.fullmatch(r"USR\d{1,3}", out)


def test_relaxed_accepts_short_codes():
  values = [f"U{i:03d}" for i in range(120)]  # length 4 < strict minimum 8
  assert detect_identifier_shape(values) is None
  shapes = build_relaxed_shapes(values)
  assert shapes is not None
  pick = random.Random(5).randrange
  assert re.fullmatch(r"U\d{3}", sample_relaxed_identifier(shapes, pick))


def test_relaxed_keeps_observed_set_for_unclassifiable_positions():
  # Punctuation variation at one position defeats the strict char classes.
  values = [f"AA{p}{i:05d}" for i in range(60) for p in "._-"]
  assert detect_identifier_shape(values) is None
  shapes = build_relaxed_shapes(values)
  assert shapes is not None
  pick = random.Random(7).randrange
  for _ in range(30):
    assert re.fullmatch(r"AA[._-]\d{5}",
                        sample_relaxed_identifier(shapes, pick))


def test_relaxed_rejects_prose_and_tiny_input():
  assert build_relaxed_shapes([f"Ticket about issue {i}" for i in range(50)
                              ]) is None
  assert build_relaxed_shapes([]) is None
  assert build_relaxed_shapes(["LONESOME_VALUE_01"]) is None


def test_relaxed_skips_single_value_length_buckets():
  # A one-value bucket is all-literal — it can only regenerate that exact
  # observed value, which the novelty filter would always reject.
  values = [f"GRP{i:04d}" for i in range(80)] + ["ODDLENGTHONE"]
  shapes = build_relaxed_shapes(values)
  assert shapes is not None
  # 12-char bucket dropped. (pylint infers `tuple([]) or None`; asserted above.)
  assert all(len(shape) == 7 for _, shape in shapes)  # pylint: disable=not-an-iterable


def test_relaxed_sampling_is_deterministic_and_weighted():
  values = [f"A{i:03d}" for i in range(90)] + [f"BB{i:04d}" for i in range(10)]
  shapes = build_relaxed_shapes(values)
  a = [
      sample_relaxed_identifier(shapes,
                                random.Random(9).randrange) for _ in range(20)
  ]
  b = [
      sample_relaxed_identifier(shapes,
                                random.Random(9).randrange) for _ in range(20)
  ]
  assert a == b
  # 90:10 length weighting → the short bucket dominates a seeded sample.
  lengths = [len(v) for v in a]
  assert lengths.count(4) > lengths.count(6)
