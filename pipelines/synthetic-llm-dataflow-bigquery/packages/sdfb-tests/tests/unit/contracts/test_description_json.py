"""Embedded-JSON extraction from BQ descriptions (Task 1, 2026-08-05 plan)."""

import pytest
from sdfb_core.contracts.description_json import (
    DescriptionJsonError,
    extract_embedded_json,
)


def test_prose_only_returns_none():
  assert extract_embedded_json("Landing table for FX ops.", "sdfb") is None


def test_pure_json_object():
  assert extract_embedded_json('{"sdfb": 1, "pk": ["A"]}', "sdfb") == {
      "sdfb": 1,
      "pk": ["A"],
  }


def test_json_embedded_in_prose():
  text = 'FX ops table. {"sdfb": 1, "pk": ["A", "B"]} Owned by team X.'
  assert extract_embedded_json(text, "sdfb") == {"sdfb": 1, "pk": ["A", "B"]}


def test_nested_braces_stay_balanced():
  text = 'x {"sdfb": 1, "fk": [{"cols": ["A"], "ref": "d.t", "ref_cols": ["A"]}]} y'
  assert extract_embedded_json(text, "sdfb")["fk"][0]["cols"] == ["A"]


def test_object_without_marker_is_ignored():
  assert extract_embedded_json('{"note": "not ours"}', "sdfb") is None


def test_marker_in_string_of_other_object_is_not_a_match():
  # The marker must be a KEY of the parsed object, not a substring hit.
  assert extract_embedded_json('{"note": "mentions \\"sdfb\\" only"}',
                               "sdfb") is None


def test_braces_inside_strings_do_not_break_balance():
  text = '{"sdfb": 1, "pk": ["we{ird}name"]}'
  assert extract_embedded_json(text, "sdfb") == {
      "sdfb": 1,
      "pk": ["we{ird}name"]
  }


def test_malformed_with_marker_raises():
  with pytest.raises(DescriptionJsonError):
    extract_embedded_json('{"sdfb": 1, "pk": [BROKEN}', "sdfb")


def test_empty_and_none_safe():
  assert extract_embedded_json("", "sdfb") is None
  assert extract_embedded_json(None, "sdfb") is None
