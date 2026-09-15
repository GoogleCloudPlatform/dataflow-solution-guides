"""Tolerant extraction of a JSON contract embedded in a BQ description.

Descriptions are shared, human-edited real estate: prose before/after the
object is expected. The scanner walks brace-balanced candidates (string- and
escape-aware) and returns the first that parses AND carries the marker key.
A candidate that merely *mentions* the marker but does not parse raises —
a half-written contract silently dropped is worse than a loud stop
(2026-08-05 spec, WS-A parsing rule 2).
"""

from __future__ import annotations

import json


class DescriptionJsonError(ValueError):
  """A description contains a marked-but-unparseable JSON object."""


def _candidates(text: str):
  """Yield brace-balanced ``{...}`` slices, ignoring braces inside strings."""
  depth, start, in_str, esc = 0, -1, False, False
  for i, ch in enumerate(text):
    if in_str:
      if esc:
        esc = False
      elif ch == "\\":
        esc = True
      elif ch == '"':
        in_str = False
      continue
    if ch == '"':
      in_str = True
    elif ch == "{":
      if depth == 0:
        start = i
      depth += 1
    elif ch == "}" and depth:
      depth -= 1
      if depth == 0:
        yield text[start:i + 1]


def extract_embedded_json(text: str | None, marker_key: str) -> dict | None:
  """First embedded JSON object of ``text`` carrying ``marker_key``.

    Returns None when no balanced object mentions the marker; raises
    :class:`DescriptionJsonError` when one mentions it but does not parse.
    An object that parses without the marker as a KEY is not a match.
    """
  for cand in _candidates(text or ""):
    if f'"{marker_key}"' not in cand:
      continue
    try:
      obj = json.loads(cand)
    except json.JSONDecodeError as exc:
      raise DescriptionJsonError(
          f"description contains a {marker_key!r}-marked JSON object "
          f"that does not parse: {exc}") from exc
    if isinstance(obj, dict) and marker_key in obj:
      return obj
  return None


__all__ = ["DescriptionJsonError", "extract_embedded_json"]
