"""Constraint samplers (ADR 0028 Tier P / Tier B).

Fixtures are the four clauses of the 2026-08-21 first PK+FK run
(`runs/2026-08-21_14_54_30-1966084111444777604`): the C2E
24-hex PK pattern, the UUIDv4 pattern, and the S1-prefixed 12-byte
opaque key whose binary fallback memorized 58 source values.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=missing-class-docstring

from __future__ import annotations

import random
import re

import pytest
from sdfb_core.engines.constraint_sampler import (
    ByteTemplateSampler,
    compile_pattern_sampler,
)

C2E_PATTERN = r"^(C2E[13][0-9A-F]{20}|7301[0-9A-F]{20})$"
UUID4_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class TestPatternSampler:

  def test_c2e_draws_fullmatch_the_pattern(self):
    s = compile_pattern_sampler(C2E_PATTERN)
    assert s is not None
    rng = random.Random(7)
    rx = re.compile(C2E_PATTERN)
    for _ in range(200):
      assert rx.fullmatch(s.sample(rng))

  def test_uuid4_draws_fullmatch_the_pattern(self):
    s = compile_pattern_sampler(UUID4_PATTERN)
    assert s is not None
    rng = random.Random(7)
    rx = re.compile(UUID4_PATTERN)
    for _ in range(200):
      assert rx.fullmatch(s.sample(rng))

  def test_c2e_capacity_is_exact(self):
    s = compile_pattern_sampler(C2E_PATTERN)
    assert s is not None
    # C2E[13]{20 hex} = 2*16**20, 7301{20 hex} = 16**20.
    assert s.capacity == 3 * 16**20

  def test_uuid4_capacity_covers_millions(self):
    s = compile_pattern_sampler(UUID4_PATTERN)
    assert s is not None
    assert s.capacity == 4 * 16**30

  def test_same_seed_reproduces_the_sequence(self):
    s = compile_pattern_sampler(C2E_PATTERN)
    assert s is not None
    a = [s.sample(random.Random(42)) for _ in range(1)]
    b = [s.sample(random.Random(42)) for _ in range(1)]
    assert a == b
    r1, r2 = random.Random(1), random.Random(1)
    assert [s.sample(r1) for _ in range(20)
           ] == [s.sample(r2) for _ in range(20)]

  @pytest.mark.parametrize(
      "pattern",
      [
          r"^a+$",  # unbounded quantifier: capacity infinite
          r"^[^a]{3}$",  # negated class
          r"^(ab)\1$",  # backreference
          r"^a(?=b)b$",  # lookahead
          r"abc",  # unanchored: substring semantics unclear
      ],
  )
  def test_unsupported_patterns_return_none(self, pattern):
    assert compile_pattern_sampler(pattern) is None

  def test_family_shares_steer_prefix_mass(self):
    families = (("C2E3", 0.57), ("C2E1", 0.42), ("7301", 0.007))
    s = compile_pattern_sampler(C2E_PATTERN, families=families)
    assert s is not None
    rng = random.Random(3)
    n = 4000
    draws = [s.sample(rng) for _ in range(n)]
    rx = re.compile(C2E_PATTERN)
    assert all(rx.fullmatch(d) for d in draws)
    share_c2e3 = sum(d.startswith("C2E3") for d in draws) / n
    share_7301 = sum(d.startswith("7301") for d in draws) / n
    # Uniform-over-language would give 7301 a 1/3 share; the family
    # weights must pull it to ~0.7%.
    assert share_c2e3 == pytest.approx(0.57, abs=0.05)
    assert share_7301 == pytest.approx(0.007, abs=0.01)

  def test_sample_unique_rejects_forbidden_and_duplicates(self):
    s = compile_pattern_sampler(C2E_PATTERN)
    assert s is not None
    rng = random.Random(11)
    forbidden = frozenset(s.sample(random.Random(11)) for _ in range(5))
    out = s.sample_unique(rng, 2000, forbidden=forbidden)
    assert len(out) == 2000
    assert len(set(out)) == 2000
    assert not set(out) & forbidden


class TestByteTemplateSampler:

  def test_prefix_and_exact_length(self):
    s = ByteTemplateSampler(prefix="A1®±", length=12)
    rng = random.Random(5)
    for _ in range(100):
      v = s.sample(rng)
      assert v.startswith("A1®±")
      assert len(v) == 12

  def test_never_emits_forbidden_values(self):
    s = ByteTemplateSampler(prefix="A1®±", length=12)
    forbidden = frozenset(s.sample(random.Random(5)) for _ in range(10))
    rng = random.Random(5)
    out = s.sample_unique(rng, 500, forbidden=forbidden)
    assert len(out) == 500
    assert not set(out) & forbidden

  def test_tails_are_not_printable_ascii_only(self):
    # The clause: "never fall back to printable-ASCII-only". Across a
    # batch, tails must mix in bytes outside 0x20-0x7E.
    s = ByteTemplateSampler(prefix="A1®±", length=12)
    rng = random.Random(5)
    tails = [s.sample(rng)[4:] for _ in range(200)]
    assert any(any(not (0x20 <= ord(ch) <= 0x7E) for ch in t) for t in tails)

  def test_capacity_covers_millions(self):
    s = ByteTemplateSampler(prefix="A1®±", length=12)
    # 8 free tail positions over a >=255-codepoint alphabet.
    assert s.capacity >= 255**8

  def test_prefix_longer_than_length_raises(self):
    with pytest.raises(ValueError):
      ByteTemplateSampler(prefix="TOOLONGPREFIX", length=4)
