"""Structured constraint → profile → engine wiring (ADR 0024, §3a/§3b).

Covers: `route: "llm"` forcing STRING columns onto the LLM free-text route,
the rendered clause + pattern/length/examples landing on the profile, the
derived length hint suppression, guided-decoding pattern override, and the
non-STRING unsupported warning.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,missing-class-docstring,protected-access,unused-argument

from __future__ import annotations

import logging

from sdfb_core.contracts.schema import TableSchema
from sdfb_core.engines.b1_rag.engine import B1RagEngine
from sdfb_core.engines.b1_rag.profile import ColumnKind, profile_columns
from sdfb_core.engines.b2_library.fidelity import (
    ColumnKind as B2ColumnKind,)
from sdfb_core.engines.b2_library.fidelity import (
    profile_column,)
from sdfb_core.engines.base import GenerationContext


def _schema(col_type: str, desc: str) -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t"
      },
      "schema": [{
          "name": "COL",
          "type": col_type,
          "mode": "NULLABLE",
          "description": desc,
      }],
  })


_ROUTE_LLM = '{"llm_prompt_constraint": {"format": "3-letter code", "route": "llm"}}'


class TestB1Routing:

  def test_route_llm_forces_freetext_on_categorical_string(self) -> None:
    rows = [{"COL": v} for v in ["AAA", "BBB", "CCC"] * 10]
    prof = profile_columns(_schema("STRING", _ROUTE_LLM), rows)["COL"]
    assert prof.kind is ColumnKind.FREE_TEXT
    assert prof.text_examples  # LLM branch, seeded
    assert prof.llm_prompt_constraint == "format=3-letter code"

  def test_route_llm_overrides_identifier_detection(self) -> None:
    rows = [{"COL": f"REF{i:08d}"} for i in range(60)]
    prof = profile_columns(_schema("STRING", _ROUTE_LLM), rows)["COL"]
    assert prof.kind is ColumnKind.FREE_TEXT
    assert prof.identifier_shape is None  # forced off the template route

  def test_route_llm_on_constant_string(self) -> None:
    rows = [{"COL": "K"} for _ in range(30)]
    prof = profile_columns(_schema("STRING", _ROUTE_LLM), rows)["COL"]
    assert prof.kind is ColumnKind.FREE_TEXT

  def test_route_llm_unsupported_on_numeric_warns(self, caplog) -> None:
    rows = [{"COL": i} for i in range(30)]
    with caplog.at_level(logging.WARNING):
      prof = profile_columns(_schema("INT64", _ROUTE_LLM), rows)["COL"]
    assert prof.kind is ColumnKind.NUMERIC
    assert "prompt_constraint_route_unsupported" in caplog.text


class TestB1ConstraintFields:
  _DESC = ('{"llm_prompt_constraint": {"format": "hex id", "length": 8, '
           '"pattern": "^[0-9A-F]{8}$", "examples": ["AB12CD34"]}}')

  def _profile(self):
    rows = [{
        "COL": f"prose value number {i} entirely different {i}"
    } for i in range(60)]
    return profile_columns(_schema("STRING", self._DESC), rows)["COL"]

  def test_clause_pattern_and_examples_land_on_profile(self) -> None:
    prof = self._profile()
    assert prof.llm_prompt_constraint == (
        "format=hex id; length=8; pattern=^[0-9A-F]{8}$; "
        "fictitious examples=['AB12CD34']")
    assert prof.constraint_pattern == "^[0-9A-F]{8}$"
    assert prof.constraint_sets_length is True
    assert prof.constraint_examples == ("AB12CD34",)

  def test_constraint_length_suppresses_derived_hint(self) -> None:
    prof = self._profile()
    engine = B1RagEngine()
    clause = engine._column_constraint(prof)
    assert "Most values are" not in clause
    assert clause.startswith("format=hex id")

  def test_constraint_pattern_reaches_guided_decoding(self) -> None:
    prof = self._profile()
    recorded: list[dict] = []

    class _Recorder:

      def generate_json(self, prompt, json_schema, **kw):
        recorded.append(json_schema)
        return [{"values": ["AB99EF01"]}]

    engine = B1RagEngine()
    engine._client = _Recorder()
    engine._ctx = GenerationContext(
        table_schema=_schema("STRING", self._DESC),
        reference_rows=[],
        reference_digest="d1",
    )
    engine._infer_free_text_pool(prof, ["seed one"], target=4)
    items = recorded[0]["properties"]["values"]["items"]
    assert items["pattern"] == "^[0-9A-F]{8}$"

  def test_constraint_examples_never_enter_the_pool(self) -> None:
    # Observed values share the constraint's 8-hex format, so the format
    # gate passes both candidates — only the fictitious-example echo
    # must be rejected.
    rows = [{"COL": f"{(i * 2654435761) % 16**8:08X}"} for i in range(60)]
    prof = profile_columns(_schema("STRING", self._DESC), rows)["COL"]

    class _EchoingClient:

      def generate_json(self, prompt, json_schema, **kw):
        return [{"values": ["AB12CD34", "99FE00AA"]}]

    engine = B1RagEngine()
    engine._client = _EchoingClient()
    engine._ctx = GenerationContext(
        table_schema=_schema("STRING", self._DESC),
        reference_rows=[],
        reference_digest="d1",
    )
    pool = engine._infer_free_text_pool(prof, ["seed one"], target=2)
    assert "AB12CD34" not in pool
    assert "99FE00AA" in pool


class TestB2Routing:

  def test_route_llm_forces_freetext_with_clause(self) -> None:
    field = _schema("STRING", _ROUTE_LLM).columns[0]
    rows = [{"COL": v} for v in ["AAA", "BBB", "CCC"] * 10]
    prof = profile_column(field, rows)
    assert prof.kind is B2ColumnKind.FREE_TEXT
    assert prof.identifier_shape is None
    assert prof.llm_prompt_constraint == "format=3-letter code"

  def test_pattern_and_length_reach_the_pool_call(self) -> None:
    from sdfb_core.engines.b2_library.freetext import FreeTextHook
    from sdfb_core.engines.base import GenerationConfig

    desc = ('{"llm_prompt_constraint": {"format": "hex id", "length": 8, '
            '"pattern": "^[0-9A-F]{8}$"}}')
    field = _schema("STRING", desc).columns[0]
    rows = [{"COL": f"{(i * 2654435761) % 16**8:08X}"} for i in range(60)]
    prof = profile_column(field, rows)
    recorded: list[tuple[str, dict]] = []

    class _Recorder:

      def generate_json(self, prompt, json_schema, **kw):
        recorded.append((prompt, json_schema))
        return [{"values": ["AB99EF01"]}]

    hook = FreeTextHook(_Recorder(), pool_size=1)
    hook._generate_pool(prof, GenerationConfig(seed=3))
    prompt, schema = recorded[0]
    assert schema["properties"]["values"]["items"]["pattern"] == "^[0-9A-F]{8}$"
    assert "Most values are" not in prompt  # user length pins it
    assert "format=hex id" in prompt
