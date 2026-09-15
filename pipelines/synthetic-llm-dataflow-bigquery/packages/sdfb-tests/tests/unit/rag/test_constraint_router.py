"""Constraint router — Tier P/B columns bypass the LLM pool (ADR 0028).

The 2026-08-21 run: the declared PK drew from a 512-cap constrained pool
(999 488 pk.duplicate), and the binary column's fallback copied 58
verbatim source values against its own privacy note. Routed columns
sample their clause's value space directly — CPU, unbounded, source-
rejecting — and the ladder never runs for them.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=invalid-name,missing-class-docstring,unused-argument,unused-variable,use-implicit-booleaness-not-comparison

from __future__ import annotations

import logging
import re

from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationContext
from sdfb_core.engines.b1_rag import B1RagEngine, HashingEmbedder
from sdfb_core.engines.base import GenerationConfig

C2E_PATTERN = "^(C2E[13][0-9A-F]{20}|7301[0-9A-F]{20})$"
_PK_DESC = ('{"llm_prompt_constraint": {"route": "llm", '
            '"pattern": "' + C2E_PATTERN.replace("\\", "\\\\") + '", '
            '"format": "24 uppercase hex"}}')
_BIN_DESC = ('{"llm_prompt_constraint": {"route": "llm", "length": 12, '
             '"prefix": "A1®±", "format": "opaque internal system key"}}')
_PROSE_DESC = ('{"llm_prompt_constraint": {"route": "llm", '
               '"charset": "rotate across 5-letter and 4-digit codes"}}')


class _RecordingClient:
  """Fresh in-format values; records every prompt it is asked."""

  def __init__(self) -> None:
    self.prompts: list[str] = []
    self._counter = 0

  def generate_json(self, *, prompt: str, n: int = 1, **kwargs):
    self.prompts.append(prompt)
    out = []
    for _ in range(n):
      values = []
      for _ in range(32):
        self._counter += 1
        values.append(f"CODE{self._counter:05d}")
      out.append({"values": values})
    return out


def _c2e_value(i: int) -> str:
  return f"C2E3{i:020X}"


def _bin_value(i: int) -> str:
  # chr(0x8D) is C1 — trips is_binary_class on every value.
  return f"A1®±{i:03d}xyz."


def _schema(cols: list[tuple[str, str]]) -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.routed"
      },
      "schema": [{
          "name": name,
          "type": "STRING",
          "mode": "REQUIRED",
          "description": desc,
      } for name, desc in cols],
  })


def _ctx(cols, rows, **update) -> GenerationContext:
  return GenerationContext(
      table_schema=_schema(cols),
      reference_rows=rows,
      reference_digest="router-digest",
      pipeline_run_id="router-run",
      **update,
  )


def _setup(cols, rows, caplog, **update):
  client = _RecordingClient()
  engine = B1RagEngine(embedder=HashingEmbedder())
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    engine.setup(client, _ctx(cols, rows, **update))
  return engine, client


class TestTierPattern:

  def _rows(self):
    return [{"KEY": _c2e_value(i)} for i in range(80)]

  def test_pattern_column_never_reaches_the_llm(self, caplog) -> None:
    _engine, client = _setup([("KEY", _PK_DESC)], self._rows(), caplog)
    assert client.prompts == []
    text = "\n".join(r.message for r in caplog.records)
    assert "name=constraint_sampler_active" in text
    assert "route=pattern" in text

  def test_draws_match_pattern_and_exceed_the_pool_cap(self, caplog) -> None:
    engine, _ = _setup([("KEY", _PK_DESC)], self._rows(), caplog)
    out = list(engine.generate_batch(2000, GenerationConfig(seed=1)))
    rx = re.compile(C2E_PATTERN)
    values = [r.model_dump()["KEY"] for r in out]
    assert all(rx.fullmatch(v) for v in values)
    # Unbounded: distinct far beyond _FREE_TEXT_POOL_MAX (512).
    assert len(set(values)) > 512

  def test_draws_reject_observed_source_values(self, caplog) -> None:
    rows = self._rows()
    engine, _ = _setup([("KEY", _PK_DESC)], rows, caplog)
    out = list(engine.generate_batch(500, GenerationConfig(seed=2)))
    source = {r["KEY"] for r in rows}
    assert not source & {r.model_dump()["KEY"] for r in out}

  def test_pk_column_is_unique_across_batches(self, caplog) -> None:
    engine, _ = _setup([("KEY", _PK_DESC)],
                       self._rows(),
                       caplog,
                       pk_columns=["KEY"])
    a = [
        r.model_dump()["KEY"]
        for r in engine.generate_batch(800, GenerationConfig(seed=3))
    ]
    b = [
        r.model_dump()["KEY"]
        for r in engine.generate_batch(800, GenerationConfig(seed=4))
    ]
    assert len(set(a) | set(b)) == 1600


class TestTierByteTemplate:

  def _rows(self):
    return [{"BLOB": _bin_value(i)} for i in range(80)]

  def test_binary_column_uses_byte_template_not_fallback(self, caplog) -> None:
    _engine, client = _setup([("BLOB", _BIN_DESC)], self._rows(), caplog)
    assert client.prompts == []
    text = "\n".join(r.message for r in caplog.records)
    assert "name=freetext_pool_byte_template" in text
    assert "name=freetext_pool_binary_fallback" not in text

  def test_draws_carry_prefix_and_length_and_never_source(self, caplog) -> None:
    rows = self._rows()
    engine, _ = _setup([("BLOB", _BIN_DESC)], rows, caplog)
    out = list(engine.generate_batch(400, GenerationConfig(seed=5)))
    values = [r.model_dump()["BLOB"] for r in out]
    assert all(v.startswith("A1®±") for v in values)
    assert all(len(v) == 12 for v in values)
    assert not {r["BLOB"] for r in rows} & set(values)


class TestUnroutedConstraintKeepsPool:

  def test_prose_clause_still_builds_the_llm_pool(self, caplog) -> None:
    rows = [{"CODE": f"W{i:04d}"} for i in range(80)]
    _engine, client = _setup([("CODE", _PROSE_DESC)], rows, caplog)
    # No pattern, not binary: the ladder remains the enforcement
    # vehicle (ADR 0026 behavior unchanged for Tier S/L).
    assert client.prompts
    text = "\n".join(r.message for r in caplog.records)
    assert "name=constraint_sampler_active" not in text
