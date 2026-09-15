"""--prompt_debug: the built pool prompt as a milestone (ADR 0024 §3c).

Reference values are banned from logs, which is why prompts were previously
unloggable. `redacted` (the debug default) elides seed exemplars; `full` is
an explicit opt-in that logs verbatim prompts at WARNING; `off` (the
production default) logs nothing.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,protected-access,unused-argument

from __future__ import annotations

import logging

from sdfb_core.contracts.schema import TableSchema
from sdfb_core.engines.b1_rag.engine import B1RagEngine
from sdfb_core.engines.b1_rag.profile import profile_columns
from sdfb_core.engines.b2_library.fidelity import profile_column
from sdfb_core.engines.b2_library.freetext import FreeTextHook
from sdfb_core.engines.base import GenerationConfig, GenerationContext

_SEED = "SEEDVALUE123"


def _schema() -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t"
      },
      "schema": [{
          "name": "COL",
          "type": "STRING",
          "mode": "NULLABLE",
          "description": '{"llm_prompt_constraint": {"format": "hex id"}}',
      }],
  })


def _b1_profile():
  rows = [{"COL": f"{(i * 2654435761) % 16**8:08X}"} for i in range(60)]
  return profile_columns(_schema(), rows)["COL"]


class _Client:

  def generate_json(self, prompt, json_schema, **kw):
    return [{"values": ["AB99EF01"]}]


def _run_b1(mode: str, caplog) -> str:
  engine = B1RagEngine()
  engine._client = _Client()
  engine._ctx = GenerationContext(
      table_schema=_schema(),
      reference_rows=[],
      reference_digest="d1",
      prompt_debug=mode,
  )
  with caplog.at_level(logging.INFO):
    engine._infer_free_text_pool(_b1_profile(), [_SEED], target=1)
  return caplog.text


def test_context_default_is_off():
  ctx = GenerationContext(table_schema=_schema())
  assert ctx.prompt_debug == "off"


def test_b1_off_logs_no_prompt(caplog):
  text = _run_b1("off", caplog)
  assert "freetext_pool_prompt" not in text


def test_b1_redacted_elides_seeds_keeps_clause(caplog):
  text = _run_b1("redacted", caplog)
  assert "freetext_pool_prompt" in text
  assert _SEED not in text
  assert "seeds elided" in text
  assert "format=hex id" in text


def test_b1_full_logs_verbatim_at_warning(caplog):
  text = _run_b1("full", caplog)
  assert "freetext_pool_prompt" in text
  assert _SEED in text
  assert "WARNING" in text


def test_b2_redacted_elides_seeds_keeps_clause(caplog):
  field = _schema().columns[0]
  rows = [{"COL": f"{(i * 2654435761) % 16**8:08X}"} for i in range(60)]
  prof = profile_column(field, rows)
  hook = FreeTextHook(_Client(), pool_size=1)
  cfg = GenerationConfig(seed=3, engine_specific={"prompt_debug": "redacted"})
  with caplog.at_level(logging.INFO):
    hook._generate_pool(prof, cfg)
  assert "freetext_pool_prompt" in caplog.text
  assert "seeds elided" in caplog.text
  assert "format=hex id" in caplog.text


def test_generate_dofn_mirrors_prompt_debug():
  from sdfb_beam.dofns.generate import GenerateRecordsDoFn

  captured: list = []

  class _StubEngine:
    name = "stub"

    def generate_batch(self, n, cfg):
      captured.append(cfg)
      return iter(())

  dofn = GenerateRecordsDoFn(
      engine_name="b1_rag",
      model_client=object(),
      ctx=GenerationContext(table_schema=_schema(), prompt_debug="redacted"),
  )
  dofn._engine = _StubEngine()
  dofn._column_types = {}
  dofn._column_max_lengths = {}
  list(dofn.process({"batch_id": 0, "n": 4}))
  assert captured[0].engine_specific["prompt_debug"] == "redacted"
