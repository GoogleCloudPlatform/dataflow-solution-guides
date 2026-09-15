"""Tests for `MLXModelClient._extract_first_json_object`.

These exercise only the static JSON-extraction helpers, which never import
`mlx_lm` (the heavy imports are deferred to `setup()` / `generate_json()`).
So they run on the laptop + CI without the `[mlx]` extra — no `@pytest.mark.gpu`.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=protected-access

from __future__ import annotations

import pytest
from sdfb_beam.handlers.mlx_client import MLXModelClient

extract = MLXModelClient._extract_first_json_object


def test_plain_object():
  assert extract('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_strips_markdown_fence():
  text = 'Here you go:\n```json\n{"a": 1}\n```\nDone.'
  assert extract(text) == {"a": 1}


def test_ignores_braces_inside_string_values():
  # The naive brace-counter would mis-balance on `{x}` inside the value.
  assert extract('{"note": "set {x} to 3", "n": 2}') == {
      "note": "set {x} to 3",
      "n": 2
  }


def test_tolerates_trailing_comma():
  assert extract('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}


def test_skips_leading_prose():
  assert extract('Sure! Here it is: {"a": 1}') == {"a": 1}


def test_handles_nested_objects_and_arrays():
  assert extract('{"a": {"b": 1}, "c": [1, 2, {"d": 3}]}') == {
      "a": {
          "b": 1
      },
      "c": [1, 2, {
          "d": 3
      }],
  }


def test_truncated_output_returns_none():
  # Output cut off mid-object (e.g. hit max_tokens) — no balanced close.
  assert extract('{"a": 1, "b": "unclosed') is None


def test_no_json_returns_none():
  assert extract("I cannot help with that.") is None


def test_first_valid_object_wins_over_earlier_garbage():
  # A `{` that opens a non-parseable span, then a valid object after it.
  text = '{not json at all : } then {"ok": true}'
  assert extract(text) == {"ok": True}


@pytest.mark.parametrize("empty", ["", "   ", "no braces here"])
def test_empty_and_bracketless(empty):
  assert extract(empty) is None
