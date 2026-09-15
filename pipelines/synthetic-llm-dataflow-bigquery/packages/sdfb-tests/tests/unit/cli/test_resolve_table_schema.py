"""Live-first DDL resolution + target-only steering metadata (ADR 0027 D2).

Two rules, both from the 2026-08-21 four-run cycle:

1. **Live-first**: the schema is extracted from live `INFORMATION_SCHEMA`
   (the bqClient) at every launch — the cycle consumed a stale
   ``--ddl_uri`` pin and silently dropped every constraint edit. The pin
   demotes to the OFFLINE FALLBACK.
2. **Generation-steering metadata comes from the TARGET (landing) table
   only** — the `llm_prompt_constraint` column descriptions and the
   `{"sdfb":1,…}` table contract are declared on the synthetic table the
   team owns and Terraforms; the SOURCE (lake) table's descriptions are
   another team's prose and must NEVER steer generation. Structure
   (columns/types/modes) still mirrors the source.
"""

from __future__ import annotations

import json
import logging

import pytest
from sdfb_beam.cli import run_pipeline
from sdfb_core.contracts import FieldSchema, TableInfo, TableSchema

SRC = "p.lake.tbl"
TGT = "p.synthetic_data.tbl"

_CONSTRAINT = (
    '{"llm_prompt_constraint": {"format": "8-digit reference code"}}')
_OLD_CONSTRAINT = (
    '{"llm_prompt_constraint": {"format": "OLD, superseded clause"}}')


def _schema_with(description: str,
                 *,
                 table_description: str = "",
                 name: str = "COL_001") -> TableSchema:
  return TableSchema(
      table_info=TableInfo(
          table_id="proj.ds.tbl", description=table_description),
      columns=[
          FieldSchema(
              name=name,
              bq_type="STRING",
              mode="NULLABLE",
              description=description,
          )
      ],
  )


def _route(monkeypatch, mapping: dict) -> None:
  """extract_table_schema router: fqn → TableSchema | Exception."""

  def _extract(fqn: str) -> TableSchema:
    result = mapping[fqn]
    if isinstance(result, Exception):
      raise result
    return result

  monkeypatch.setattr(run_pipeline, "extract_table_schema", _extract)


def _milestones(caplog) -> str:
  return "\n".join(r.getMessage() for r in caplog.records)


def test_constraints_come_from_the_target_table_only(monkeypatch, caplog):
  """Source-side descriptions — even ones that LOOK like constraints —
    never steer generation; the landing table's do."""
  _route(
      monkeypatch,
      {
          SRC:
              _schema_with(
                  _OLD_CONSTRAINT, table_description="lake-team prose"),
          TGT:
              _schema_with(
                  _CONSTRAINT,
                  table_description='{"sdfb": 1, "pk": ["COL_001"]}',
              ),
      },
  )
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    got = run_pipeline.resolve_table_schema("", SRC, TGT)
  assert got.columns[0].description == _CONSTRAINT
  assert got.table_info.description == '{"sdfb": 1, "pk": ["COL_001"]}'
  assert "name=target_metadata_overlaid" in _milestones(caplog)


def test_source_descriptions_are_stripped_when_target_unreachable(
    monkeypatch, caplog):
  """First cold run: the landing table may not exist yet. No constraints
    then — but NEVER the source's descriptions instead."""
  _route(
      monkeypatch,
      {
          SRC:
              _schema_with(
                  _OLD_CONSTRAINT, table_description="lake-team prose"),
          TGT:
              RuntimeError("landing table not found"),
      },
  )
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    got = run_pipeline.resolve_table_schema("", SRC, TGT)
  assert got.columns[0].name == "COL_001"  # structure survives
  assert got.columns[0].description == ""
  assert got.table_info.description == ""
  assert "name=target_metadata_unavailable" in _milestones(caplog)


def test_live_failure_falls_back_to_the_pin_and_keeps_its_descriptions(
    monkeypatch, caplog):
  """OFFLINE mode (air-gap): the pin is the operator's declared fallback
    — extracted from the LANDING table per the propagation runbook — so
    its descriptions stand when the target is also unreachable."""
  _route(
      monkeypatch,
      {
          SRC: RuntimeError("no INFORMATION_SCHEMA"),
          TGT: RuntimeError("no INFORMATION_SCHEMA"),
      },
  )
  monkeypatch.setattr(run_pipeline, "load_ddl",
                      lambda uri: _schema_with(_CONSTRAINT))
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    got = run_pipeline.resolve_table_schema("gs://b/ddl.json", SRC, TGT)
  assert got.columns[0].description == _CONSTRAINT
  text = _milestones(caplog)
  assert "name=ddl_live_extract_failed" in text
  assert "name=ddl_loaded_from_uri" in text


def test_target_overlay_wins_over_the_pin_in_offline_source_mode(monkeypatch,):
  """Source unreachable but the landing table is: the live target
    metadata still overrides the pin's descriptions."""
  _route(
      monkeypatch,
      {
          SRC: RuntimeError("no INFORMATION_SCHEMA"),
          TGT: _schema_with(_CONSTRAINT),
      },
  )
  monkeypatch.setattr(run_pipeline, "load_ddl",
                      lambda uri: _schema_with(_OLD_CONSTRAINT))
  got = run_pipeline.resolve_table_schema("gs://b/ddl.json", SRC, TGT)
  assert got.columns[0].description == _CONSTRAINT


def test_live_failure_with_corrupt_pin_still_fails_loudly(monkeypatch):
  _route(monkeypatch, {SRC: RuntimeError("no INFORMATION_SCHEMA")})

  def _bad(uri):
    raise json.JSONDecodeError("Expecting value", "", 0)

  monkeypatch.setattr(run_pipeline, "load_ddl", _bad)
  with pytest.raises(json.JSONDecodeError):
    run_pipeline.resolve_table_schema("gs://b/corrupt.json", SRC, TGT)


def test_live_failure_without_pin_raises_the_live_error(monkeypatch):
  _route(monkeypatch, {SRC: RuntimeError("no INFORMATION_SCHEMA")})
  with pytest.raises(RuntimeError, match="no INFORMATION_SCHEMA"):
    run_pipeline.resolve_table_schema("", SRC, TGT)


def test_stale_pin_warns_against_the_effective_schema(monkeypatch, caplog):
  """The pin (offline fallback) is compared against what generation
    ACTUALLY uses — the target-overlaid schema."""
  _route(
      monkeypatch,
      {
          SRC: _schema_with("whatever the lake says"),
          TGT: _schema_with(_CONSTRAINT),
      },
  )
  monkeypatch.setattr(run_pipeline, "load_ddl",
                      lambda uri: _schema_with(_OLD_CONSTRAINT))
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    got = run_pipeline.resolve_table_schema("gs://b/ddl.json", SRC, TGT)
  assert got.columns[0].description == _CONSTRAINT
  text = _milestones(caplog)
  assert "name=ddl_pin_drift" in text
  assert "COL_001" in text


def test_fresh_pin_logs_the_fresh_milestone(monkeypatch, caplog):
  _route(
      monkeypatch,
      {
          SRC: _schema_with(""),
          TGT: _schema_with(_CONSTRAINT),
      },
  )
  monkeypatch.setattr(run_pipeline, "load_ddl",
                      lambda uri: _schema_with(_CONSTRAINT))
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    run_pipeline.resolve_table_schema("gs://b/ddl.json", SRC, TGT)
  text = _milestones(caplog)
  assert "name=ddl_pin_fresh" in text
  assert "name=ddl_pin_drift " not in text


def test_unusable_pin_is_a_warning_when_live_works(monkeypatch, caplog):
  _route(
      monkeypatch,
      {
          SRC: _schema_with(""),
          TGT: _schema_with(_CONSTRAINT)
      },
  )
  monkeypatch.setattr(
      run_pipeline,
      "load_ddl",
      lambda uri: (_ for _ in ()).throw(FileNotFoundError(uri)),
  )
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    got = run_pipeline.resolve_table_schema("gs://b/missing.json", SRC, TGT)
  assert got.columns[0].description == _CONSTRAINT
  assert "name=ddl_pin_check_error" in _milestones(caplog)
