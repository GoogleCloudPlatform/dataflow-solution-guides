"""Lock-free `strptime` replacement for the profiler's temporal formats.

The 2026-08-06 10M E2E stalled 24 generate bundles for 120-908 s inside
`datetime.strptime`: every call takes CPython's global
`_strptime._cache_lock`, and with more distinct formats in flight than the
5-slot TimeRE cache holds, all 16 DoFn threads serialize on regex
recompilation. This module parses the known digit-and-separator formats by
direct scanning — no locks, no regexes — and defers to `strptime` only for
directives it does not model.

Accept/reject semantics mirror `strptime` exactly for the supported
grammar: numeric fields take a 1-2 digit run (`%Y` exactly 4, `%f` 1-6,
right-padded), literals must match byte-for-byte, and trailing input
rejects. That equivalence holds because every supported format separates
numeric directives with non-digit literals — `_tokenize` refuses formats
that break the property, sending them to the fallback instead.
"""

from __future__ import annotations

from datetime import UTC, datetime

# The single route into the locked stdlib path — tests poison this to prove
# the fast path handled a call.
_strptime_fallback = datetime.strptime

# Directive → (min_digits, max_digits). Fields absent here (e.g. %j, %z)
# push the whole format to the fallback.
_FIELDS: dict[str, tuple[int, int]] = {
    "Y": (4, 4),
    "m": (1, 2),
    "d": (1, 2),
    "H": (1, 2),
    "M": (1, 2),
    "S": (1, 2),
    "f": (1, 6),
}

# fmt → tuple of ("lit", text) | ("field", directive), or None when the
# format is outside the scanner's grammar. Plain dict: GIL-atomic get/set,
# a racing duplicate compute is benign.
_TOKEN_CACHE: dict[str, tuple[tuple[str, str], ...] | None] = {}


def _tokenize(fmt: str) -> tuple[tuple[str, str], ...] | None:
  tokens: list[tuple[str, str]] = []
  i = 0
  prev_field = False
  while i < len(fmt):
    ch = fmt[i]
    if ch == "%":
      if i + 1 >= len(fmt):
        return None
      directive = fmt[i + 1]
      if directive not in _FIELDS:
        return None  # includes %% — the fallback owns it
      if prev_field:
        return None  # adjacent numeric fields are ambiguous
      tokens.append(("field", directive))
      prev_field = True
      i += 2
      continue
    if ch.isdigit():
      return None  # a digit literal would blur field boundaries
    tokens.append(("lit", ch))
    prev_field = False
    i += 1
  return tuple(tokens)


def _scan(s: str, tokens: tuple[tuple[str, str], ...]) -> dict[str, int] | None:
  """Captured fields, or None when the string cannot match the format
    under `strptime`'s grammar either (a definite reject)."""
  fields: dict[str, int] = {}
  pos = 0
  for kind, payload in tokens:
    if kind == "lit":
      if pos >= len(s) or s[pos] != payload:
        return None
      pos += 1
      continue
    lo, hi = _FIELDS[payload]
    run = 0
    while pos + run < len(s) and run < hi and s[pos + run].isdigit():
      run += 1
    if run < lo:
      return None
    digits = s[pos:pos + run]
    pos += run
    if pos < len(s) and s[pos].isdigit():
      # Digits beyond the field's width can only face a non-digit
      # literal next (the grammar guarantees it) — definite reject,
      # exactly as strptime's regex would backtrack and fail.
      return None
    if payload == "f":
      fields[payload] = int(digits.ljust(6, "0"))
    else:
      fields[payload] = int(digits)
  if pos != len(s):
    return None  # unconverted data remains
  return fields


def parse_temporal_string(s: str, fmt: str) -> datetime:
  """`datetime.strptime(s, fmt)` without the global parser lock.

    Raises `ValueError` on the same inputs `strptime` rejects; unknown
    directives defer to `strptime` itself.
    """
  if fmt in _TOKEN_CACHE:
    tokens = _TOKEN_CACHE[fmt]
  else:
    tokens = _tokenize(fmt)
    _TOKEN_CACHE[fmt] = tokens
  if tokens is None:
    return _strptime_fallback(s, fmt)
  fields = _scan(s, tokens)
  if fields is None:
    raise ValueError(f"time data {s!r} does not match format {fmt!r}")
  # datetime() range-checks month/day/hour/minute/second — the same
  # ValueError strptime raises for e.g. 2026-02-30.
  return datetime(
      fields.get("Y", 1900),
      fields.get("m", 1),
      fields.get("d", 1),
      fields.get("H", 0),
      fields.get("M", 0),
      fields.get("S", 0),
      fields.get("f", 0),
  )


__all__ = ["UTC", "parse_temporal_string"]
