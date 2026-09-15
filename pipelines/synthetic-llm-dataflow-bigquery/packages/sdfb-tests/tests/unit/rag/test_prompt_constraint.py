"""llm_prompt_constraint: DDL description → profile → pool prompt (Task 10)."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,protected-access,unused-argument

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

import numpy as np
from sdfb_core.contracts.schema import TableSchema
from sdfb_core.engines.b1_rag.engine import _build_pool_prompt
from sdfb_core.engines.b1_rag.profile import profile_columns
from sdfb_core.engines.b2_library.fidelity import profile_column
from sdfb_core.engines.b2_library.freetext import FreeTextHook
from sdfb_core.engines.base import GenerationConfig

_DESC = (
    'Reference column. {"llm_prompt_constraint": "uppercase SWIFT-style refs"}')


def _schema(desc: str) -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t"
      },
      "schema": [{
          "name": "NOTES",
          "type": "STRING",
          "mode": "NULLABLE",
          "description": desc,
      }],
  })


def _prose_rows() -> list[dict]:
  return [{
      "NOTES":
          f"LONG DESCRIPTIVE PROSE ENTRY NUMBER {i:03d} ABOUT NOTHING AT ALL {i}"
  } for i in range(60)]


def test_b1_profile_carries_constraint():
  prof = profile_columns(_schema(_DESC), _prose_rows())["NOTES"]
  assert prof.llm_prompt_constraint == "uppercase SWIFT-style refs"


def test_b1_profile_without_constraint_is_empty():
  prof = profile_columns(_schema("plain prose"), _prose_rows())["NOTES"]
  assert prof.llm_prompt_constraint == ""


def test_b2_profile_carries_constraint():
  field = _schema(_DESC).columns[0]
  prof = profile_column(field, _prose_rows())
  assert prof.llm_prompt_constraint == "uppercase SWIFT-style refs"


def test_pool_prompt_regression_pin_without_constraint():
  prompt = _build_pool_prompt("C", 32, ["x"])
  assert prompt == (
      "You generate synthetic tabular data. First identify the exact "
      "format of these example values for the column 'C' "
      "(e.g. UUID, hexadecimal identifier, numeric code, date, "
      "timestamp, natural-language text), then generate "
      "32 NEW, distinct, fictitious values in "
      "exactly that format. Never copy an example verbatim. "
      "Examples: ['x']. Return JSON {\"values\": [...]}.")


def test_pool_prompt_appends_constraint_constantly():
  a = _build_pool_prompt("C", 32, ["x"], constraint="abc")
  b = _build_pool_prompt("C", 32, ["x"], constraint="abc")
  assert a.endswith(" Column constraint: abc.")
  assert a == b  # byte-identical across attempts (prefix-cache pin)


class _RecordingPoolClient:

  def __init__(self):
    self.prompts: list[str] = []

  def generate_json(self, prompt, json_schema, **kwargs):
    self.prompts.append(prompt)
    return [{"values": [f"GENVAL{i:04d}X{i}" for i in range(32)]}]


def _b2_sample(desc: str, constraints_on: bool):
  field = _schema(desc).columns[0]
  prof = profile_column(field, _prose_rows())
  client = _RecordingPoolClient()
  hook = FreeTextHook(client)
  cfg = GenerationConfig(
      seed=3,
      engine_specific={
          "freetext_expansion": "off",
          "prompt_constraints": constraints_on,
      },
  )
  hook.sample(prof, 5, cfg, np.random.default_rng(3))
  return client.prompts


def test_b2_pool_prompt_carries_constraint_when_enabled():
  prompts = _b2_sample(_DESC, constraints_on=True)
  assert prompts and all("Column constraint: uppercase" in p for p in prompts)


def test_b2_pool_prompt_clean_when_disabled():
  prompts = _b2_sample(_DESC, constraints_on=False)
  assert prompts and all("Column constraint" not in p for p in prompts)
  assert all("characters long" not in p for p in prompts)


def test_length_hint_bands_and_gates():
  from sdfb_core.engines.text_shapes import length_hint

  varied = [f"{'x' * (10 + (i % 40))}" for i in range(50)]
  hint = length_hint(varied)
  assert "characters long" in hint and "median" in hint
  assert length_hint(["abc"] * 50) == ""  # fixed width → shape owns it
  assert length_hint(["ab", "abcd"]) == ""  # below min_samples


def test_b2_pool_prompt_carries_length_hint():
  """Measured length band rides the same prompt_constraints gate and is
    APPENDED (per-column constant suffix, vLLM prefix-cache-safe)."""
  prompts = _b2_sample(_DESC, constraints_on=True)
  assert prompts and all("characters long (median" in p for p in prompts)
  for p in prompts:
    assert p.index("You generate synthetic") < p.index("characters long")


def test_b1_column_constraint_joins_ddl_and_length():
  from sdfb_core.engines.b1_rag.engine import B1RagEngine

  prof = profile_columns(_schema(_DESC), _prose_rows())["NOTES"]
  engine = B1RagEngine.__new__(B1RagEngine)
  engine._ctx = type("Ctx", (), {"prompt_constraints": True})()
  joined = engine._column_constraint(prof)
  assert joined.startswith("uppercase SWIFT-style refs")
  assert "characters long" in joined
  engine._ctx = type("Ctx", (), {"prompt_constraints": False})()
  assert engine._column_constraint(prof) == ""
