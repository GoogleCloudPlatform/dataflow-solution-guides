"""Launcher-side constraint-vehicle visibility (ADR 0030 follow-up).

The 2026-08-22 launch log showed clauses but not their ROUTE nor
whether each clause actually drives generation (freetext pool / RAG /
Tier-P sampler) or only steers prompts if the column happens to be
free-text. `prompt_constraints_pretty` answers it per TABLE.COL, at
preflight — before any worker exists.
"""

from __future__ import annotations

import logging

from sdfb_beam.cli.preflight import preflight
from sdfb_core.contracts import TableSchema

_PATTERN = ('{"llm_prompt_constraint": {"route": "llm", '
            '"pattern": "^[0-9A-F]{16}$"}}')
_ENUM_NO_ROUTE = '{"llm_prompt_constraint": {"values": ["I", "O"]}}'
_ENUM_FORCED = (
    '{"llm_prompt_constraint": {"route": "llm", "values": ["I", "O"]}}')
_NUMERIC_CLAUSE = '{"llm_prompt_constraint": {"examples": ["20"]}}'


def _schema() -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.src.A_TABLE"
      },
      "schema": [
          {
              "name": "HEX_KEY",
              "type": "STRING",
              "mode": "REQUIRED",
              "description": _PATTERN
          },
          {
              "name": "DIRECTION",
              "type": "STRING",
              "mode": "REQUIRED",
              "description": _ENUM_NO_ROUTE
          },
          {
              "name": "FLAG",
              "type": "STRING",
              "mode": "REQUIRED",
              "description": _ENUM_FORCED
          },
          {
              "name": "BRANCH",
              "type": "INT64",
              "mode": "REQUIRED",
              "description": _NUMERIC_CLAUSE
          },
          {
              "name": "PLAIN",
              "type": "STRING",
              "mode": "REQUIRED"
          },
      ],
  })


def _rows(n: int = 60) -> list[dict]:
  return [{
      "HEX_KEY": f"{i:016X}",
      "DIRECTION": "I" if i % 2 else "O",
      "FLAG": "I" if i % 3 else "O",
      "BRANCH": 20,
      "PLAIN": f"note {i}",
  } for i in range(n)]


def test_vehicles_logged_per_table_and_column(caplog):
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    preflight(_schema(), (), (), _rows())
  text = caplog.text
  assert "name=prompt_constraints_pretty" in text
  # TABLE.COL-qualified keys, one entry per constrained column.
  assert '"A_TABLE.HEX_KEY"' in text
  assert '"A_TABLE.DIRECTION"' in text
  assert '"A_TABLE.BRANCH"' in text
  assert '"A_TABLE.PLAIN"' not in text  # unconstrained: absent
  # The route field is finally visible.
  assert '"route": "llm"' in text
  assert '"route": "auto"' in text


def test_vehicle_reflects_actual_generation_path(caplog):
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    preflight(_schema(), (), (), _rows())
  text = caplog.text
  # forced route:llm + samplable pattern -> Tier P, no LLM
  assert "pattern_sampler" in text
  # STRING enum WITHOUT route -> typed categorical route; clause is
  # steering-only and the log says how to force it.
  assert "typed route" in text
  assert "route" in text and "llm" in text
  # numeric clause: never a generation vehicle
  assert "non-STRING" in text
