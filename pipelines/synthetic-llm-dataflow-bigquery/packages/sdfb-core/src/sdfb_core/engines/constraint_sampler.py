"""Programmatic constraint samplers (ADR 0028, Tiers P and B).

A clause whose ``pattern`` is machine-checkable defines its value space
exactly — sampling that space with a seeded RNG gives format conformance
and uniqueness by construction, at CPU cost, for any ``num_rows``
(2026-08-21 run: the LLM pool route capped the declared PK at 512 values
and DLQ'd 999 488 rows). Binary-class columns (Tier B) get a
prefix + random-tail template instead of the source-copying fallback
their privacy notes forbid.

Engine-agnostic and dependency-free: both engines may import this;
nothing here touches Beam, profiles, or the LLM client.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any

# The stdlib regex parser (``re._parser``, 3.11+). Private but stable —
# the same seam Hypothesis' `from_regex` builds on; guarded so a future
# rename degrades to "no pattern support" instead of an import crash.
try:  # pragma: no cover - import guard
  _PARSER: Any = re._parser  # type: ignore[attr-defined]  # pylint: disable=protected-access
except AttributeError:  # pragma: no cover - future-python guard
  _PARSER = None

_MAXREPEAT = getattr(_PARSER, "MAXREPEAT", None)
# Bounded rejection sampling: a family prefix reachable with probability
# p fails all tries with (1-p)^N — for the worst intended case (p ~ 1/3,
# ADR 0028 C2E families) that is ~1e-36.
_FAMILY_MAX_TRIES = 200
_UNIQUE_MAX_EXTRA_TRIES = 1000

_DIGIT_INTERVALS: tuple[tuple[int, int], ...] = ((ord("0"), ord("9")),)
# ^…$ needs at least the two anchor ops around the body.
_MIN_ANCHORED_OPS = 2


class UnsupportedPatternError(Exception):
  """Internal: pattern uses a construct outside the sampled subset."""


@dataclass(frozen=True)
class _Choice:
  """A single position drawn from codepoint intervals."""

  intervals: tuple[tuple[int, int], ...]

  @property
  def count(self) -> int:
    return sum(hi - lo + 1 for lo, hi in self.intervals)

  def draw(self, rng: random.Random) -> str:
    r = rng.randrange(self.count)
    for lo, hi in self.intervals:
      span = hi - lo + 1
      if r < span:
        return chr(lo + r)
      r -= span
    raise AssertionError("interval walk out of range")


def _parse(pattern: str) -> Any:
  if _PARSER is None:
    raise UnsupportedPatternError("stdlib regex parser unavailable")
  try:
    return _PARSER.parse(pattern)
  except re.error as exc:
    raise UnsupportedPatternError(str(exc)) from exc


def _strip_anchors(seq: Any) -> list[Any]:
  """Require ^…$ anchoring; return the inner op sequence.

    An unanchored clause pattern has substring semantics — sampling it as
    a whole value would silently widen the format, so it is unsupported
    rather than guessed at.
    """
  ops = list(seq)
  if len(ops) < _MIN_ANCHORED_OPS:
    raise UnsupportedPatternError("pattern too short to be anchored")
  (op0, arg0), (opn, argn) = ops[0], ops[-1]
  if not (str(op0) == "AT" and "BEGINNING" in str(arg0) and str(opn) == "AT" and
          "END" in str(argn)):
    raise UnsupportedPatternError("pattern must be ^…$-anchored")
  return ops[1:-1]


def _in_choice(items: Any) -> _Choice:
  intervals: list[tuple[int, int]] = []
  for op, arg in items:
    name = str(op)
    if name == "LITERAL":
      intervals.append((arg, arg))
    elif name == "RANGE":
      intervals.append((arg[0], arg[1]))
    elif name == "CATEGORY" and "DIGIT" in str(arg) and "NOT" not in str(arg):
      intervals.extend(_DIGIT_INTERVALS)
    else:
      raise UnsupportedPatternError(f"class item {name} unsupported")
  return _Choice(intervals=tuple(intervals))


class _Node:
  """Sampled regex node: capacity + seeded draw."""

  capacity: int

  def draw(self, rng: random.Random) -> str:
    raise NotImplementedError


class _Lit(_Node):

  def __init__(self, ch: str) -> None:
    self.ch = ch
    self.capacity = 1

  def draw(self, rng: random.Random) -> str:
    return self.ch


class _Set(_Node):

  def __init__(self, choice: _Choice) -> None:
    self.choice = choice
    self.capacity = choice.count
    if self.capacity == 0:
      raise UnsupportedPatternError("empty character class")

  def draw(self, rng: random.Random) -> str:
    return self.choice.draw(rng)


class _Seq(_Node):

  def __init__(self, parts: list[_Node]) -> None:
    self.parts = parts
    cap = 1
    for p in parts:
      cap *= p.capacity
    self.capacity = cap

  def draw(self, rng: random.Random) -> str:
    return "".join(p.draw(rng) for p in self.parts)


class _Branch(_Node):
  """Alternation, capacity-weighted so draws are uniform over the
    language (modulo alternatives that generate overlapping strings —
    capacity is then an upper bound, acceptable for identifiers)."""

  def __init__(self, alts: list[_Node]) -> None:
    self.alts = alts
    self.capacity = sum(a.capacity for a in alts)

  def draw(self, rng: random.Random) -> str:
    r = rng.randrange(self.capacity)
    for a in self.alts:
      if r < a.capacity:
        return a.draw(rng)
      r -= a.capacity
    raise AssertionError("branch walk out of range")


class _Repeat(_Node):
  """Bounded repetition; the count is capacity-weighted per length so
    the node stays uniform over its language."""

  def __init__(self, lo: int, hi: int, sub: _Node) -> None:
    self.lo, self.hi, self.sub = lo, hi, sub
    self.capacity = sum(sub.capacity**k for k in range(lo, hi + 1))

  def draw(self, rng: random.Random) -> str:
    r = rng.randrange(self.capacity)
    for k in range(self.lo, self.hi + 1):
      cap_k = self.sub.capacity**k
      if r < cap_k:
        return "".join(self.sub.draw(rng) for _ in range(k))
      r -= cap_k
    raise AssertionError("repeat walk out of range")


def _build(ops: list[Any]) -> _Node:
  parts: list[_Node] = []
  for op, arg in ops:
    name = str(op)
    if name == "LITERAL":
      parts.append(_Lit(chr(arg)))
    elif name == "IN":
      parts.append(_Set(_in_choice(arg)))
    elif name == "MAX_REPEAT":
      lo, hi, sub = arg
      if hi is _MAXREPEAT or int(hi) == getattr(_MAXREPEAT, "real", 2**32):
        raise UnsupportedPatternError("unbounded repetition")
      parts.append(_Repeat(int(lo), int(hi), _build(list(sub))))
    elif name == "SUBPATTERN":
      _, add_flags, del_flags, sub = arg
      if add_flags or del_flags:
        raise UnsupportedPatternError("inline flags unsupported")
      parts.append(_build(list(sub)))
    elif name == "BRANCH":
      _, alts = arg
      parts.append(_Branch([_build(list(a)) for a in alts]))
    else:
      raise UnsupportedPatternError(f"op {name} unsupported")
  return _Seq(parts)


class PatternSampler:
  """Seeded sampler over a ^…$-anchored pattern's exact language.

    ``families`` — optional ``((prefix, share), …)`` — reweights draws
    by value prefix via bounded rejection (ADR 0028 D5: the structured
    replacement for prose percentages). Shares are normalized.
    """

  def __init__(
      self,
      pattern: str,
      node: _Node,
      families: tuple[tuple[str, float], ...] = (),
  ) -> None:
    self.pattern = pattern
    self._node = node
    self._rx = re.compile(pattern)
    total = sum(share for _, share in families) or 1.0
    self._families = tuple((p, s / total) for p, s in families)

  @property
  def capacity(self) -> int:
    return self._node.capacity

  def sample(self, rng: random.Random) -> str:
    if not self._families:
      return self._node.draw(rng)
    r = rng.random()
    acc = 0.0
    prefix = self._families[-1][0]
    for fam_prefix, share in self._families:
      acc += share
      if r < acc:
        prefix = fam_prefix
        break
    for _ in range(_FAMILY_MAX_TRIES):
      v = self._node.draw(rng)
      if v.startswith(prefix):
        return v
    raise ValueError(f"family prefix {prefix!r} unreachable under pattern "
                     f"{self.pattern!r} after {_FAMILY_MAX_TRIES} tries")

  def sample_unique(
      self,
      rng: random.Random,
      n: int,
      forbidden: frozenset[str] = frozenset(),
  ) -> list[str]:
    if self.capacity < n:
      raise ValueError(f"capacity {self.capacity} < requested unique {n}")
    return _sample_unique(self.sample, rng, n, forbidden)


class ByteTemplateSampler:
  """Tier B: literal prefix + length-pinned random tail (ADR 0028 D4).

    The tail draws each position from codepoints 0x01-0xFF — printable
    and non-printable mixed, per the binary clauses' own wording — and
    NEVER from observed source values: the replaced fallback copied 58
    verbatim source keys although the clause said "never copied
    (privacy-sensitive)".
    """

  _ALPHABET_LO, _ALPHABET_HI = 0x01, 0xFF

  def __init__(self, prefix: str, length: int) -> None:
    if len(prefix) > length:
      raise ValueError(f"prefix {prefix!r} longer than pinned length {length}")
    self.prefix = prefix
    self.length = length
    self._tail_len = length - len(prefix)

  @property
  def capacity(self) -> int:
    span = self._ALPHABET_HI - self._ALPHABET_LO + 1
    return int(span**self._tail_len)

  def sample(self, rng: random.Random) -> str:
    tail = "".join(
        chr(rng.randrange(self._ALPHABET_LO, self._ALPHABET_HI + 1))
        for _ in range(self._tail_len))
    return self.prefix + tail

  def sample_unique(
      self,
      rng: random.Random,
      n: int,
      forbidden: frozenset[str] = frozenset(),
  ) -> list[str]:
    if self.capacity < n:
      raise ValueError(f"capacity {self.capacity} < requested unique {n}")
    return _sample_unique(self.sample, rng, n, forbidden)


def _sample_unique(
    draw: Any,
    rng: random.Random,
    n: int,
    forbidden: frozenset[str],
) -> list[str]:
  out: list[str] = []
  seen: set[str] = set()
  budget = 20 * n + _UNIQUE_MAX_EXTRA_TRIES
  while len(out) < n and budget:
    budget -= 1
    v = draw(rng)
    if v in seen or v in forbidden:
      continue
    seen.add(v)
    out.append(v)
  if len(out) < n:
    raise RuntimeError(f"unique sampling stalled at {len(out)}/{n} "
                       f"(forbidden={len(forbidden)})")
  return out


def compile_pattern_sampler(
    pattern: str,
    families: tuple[tuple[str, float], ...] = (),
) -> PatternSampler | None:
  """A :class:`PatternSampler` for ``pattern``, or None when the
    pattern falls outside the supported subset (anchored literals,
    character classes, ``\\d``, bounded repetition, groups,
    alternation). None means "not Tier P" — the caller keeps its
    existing route; nothing raises on the hot path."""
  if not pattern:
    return None
  try:
    node = _build(_strip_anchors(_parse(pattern)))
  except UnsupportedPatternError:
    return None
  if node.capacity < 1:
    return None
  return PatternSampler(pattern, node, families=families)


__all__ = [
    "ByteTemplateSampler",
    "PatternSampler",
    "compile_pattern_sampler",
]
