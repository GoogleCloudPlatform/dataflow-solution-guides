"""Structured prompt-constraint templates (ADR 0024, wave-2 design doc §3).

The `llm_prompt_constraint` description marker accepts an object as well as
the legacy string. One parse site, one deterministic renderer; the legacy
string form must render byte-identically to its own text (compat pin).
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,missing-class-docstring

import pytest
from sdfb_core.contracts.description_json import DescriptionJsonError
from sdfb_core.contracts.prompt_constraint import (
    PromptConstraint,
    parse_llm_prompt_constraint,
    parse_prompt_constraint,
    render_prompt_clause,
)


class TestParseLegacyString:

  def test_string_form_lands_in_notes(self) -> None:
    pc = parse_prompt_constraint(
        'x {"llm_prompt_constraint": "uppercase SWIFT-style refs"} y')
    assert pc is not None
    assert pc.notes == "uppercase SWIFT-style refs"
    assert pc.route == "auto"

  def test_unmarked_description_is_none(self) -> None:
    assert parse_prompt_constraint("plain prose") is None
    assert parse_prompt_constraint(None) is None

  def test_legacy_render_is_byte_identical_to_the_string(self) -> None:
    # The pre-wave-2 facade returned the normalized string itself; the
    # renderer must not decorate it (prompt regression pin).
    desc = '{"llm_prompt_constraint": "uppercase  SWIFT-style\\nrefs"}'
    assert parse_llm_prompt_constraint(desc) == "uppercase SWIFT-style refs"


class TestParseObjectForm:

  def test_object_form_parses_typed_keys(self) -> None:
    pc = parse_prompt_constraint(
        '{"llm_prompt_constraint": {"format": "24-char upper hex", '
        '"pattern": "^[0-9A-F]{24}$", "prefix": "C2E", "length": 24, '
        '"route": "llm"}}')
    assert pc is not None
    assert pc.format == "24-char upper hex"
    assert pc.pattern == "^[0-9A-F]{24}$"
    assert pc.prefix == "C2E"
    assert pc.length == (24, 24)
    assert pc.route == "llm"

  def test_length_band_form(self) -> None:
    pc = parse_prompt_constraint(
        '{"llm_prompt_constraint": {"length": [6, 12]}}')
    assert pc is not None
    assert pc.length == (6, 12)

  def test_unknown_keys_are_skipped_not_fatal(self) -> None:
    pc = parse_prompt_constraint(
        '{"llm_prompt_constraint": {"format": "x", "hologram": true}}')
    assert pc is not None
    assert pc.format == "x"

  def test_invalid_regex_raises_loudly(self) -> None:
    with pytest.raises(DescriptionJsonError):
      parse_prompt_constraint(
          '{"llm_prompt_constraint": {"pattern": "[unclosed"}}')

  def test_invalid_route_raises_loudly(self) -> None:
    with pytest.raises(DescriptionJsonError):
      parse_prompt_constraint('{"llm_prompt_constraint": {"route": "vertex"}}')

  def test_examples_and_values_are_capped(self) -> None:
    many = ", ".join(f'"v{i}"' for i in range(100))
    pc = parse_prompt_constraint(
        f'{{"llm_prompt_constraint": {{"examples": [{many}], '
        f'"values": [{many}]}}}}')
    assert pc is not None
    assert len(pc.examples) == 8
    assert len(pc.values) == 64


class TestRenderClause:

  def test_deterministic_compact_order(self) -> None:
    pc = PromptConstraint(
        format="DD.MM.YYYY date",
        length=(10, 10),
        charset="0-9.",
        pattern=r"^\d{2}\.\d{2}\.\d{4}$",
        examples=("28.08.2019",),
        notes="calendar date",
    )
    clause = render_prompt_clause(pc)
    assert clause == ("format=DD.MM.YYYY date; length=10; charset=0-9.; "
                      r"pattern=^\d{2}\.\d{2}\.\d{4}$; "
                      "fictitious examples=['28.08.2019']; calendar date")

  def test_values_render_as_allowed_values(self) -> None:
    clause = render_prompt_clause(PromptConstraint(values=("I", "O")))
    assert clause == "allowed values=['I', 'O']"

  def test_empty_constraint_renders_empty(self) -> None:
    assert render_prompt_clause(PromptConstraint()) == ""

  def test_clause_is_single_line_and_capped(self) -> None:
    clause = render_prompt_clause(PromptConstraint(notes="a\nb " + "x" * 600))
    assert "\n" not in clause
    assert len(clause) <= 500


class TestParseSiteNamesTheColumn:

  def test_unknown_keys_milestone_carries_the_column(self, caplog) -> None:
    # The WARNING existed but never said WHICH column carried the typo'd
    # key — useless for debugging a 67-column DDL.
    import logging

    with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
      parse_prompt_constraint(
          '{"llm_prompt_constraint": {"format": "x", "hologram": true}}',
          column="COL_REF",
      )
    text = "\n".join(r.message for r in caplog.records)
    assert "name=prompt_constraint_unknown_keys" in text
    assert "column=COL_REF" in text
    assert "hologram" in text

  def test_validation_error_names_the_column(self) -> None:
    with pytest.raises(DescriptionJsonError) as exc:
      parse_prompt_constraint(
          '{"llm_prompt_constraint": {"pattern": "[unclosed"}}',
          column="COL_REF",
      )
    assert "COL_REF" in str(exc.value)
