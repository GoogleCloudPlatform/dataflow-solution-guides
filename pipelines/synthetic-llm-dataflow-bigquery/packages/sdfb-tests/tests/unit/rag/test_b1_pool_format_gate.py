"""Format-plausibility gate on LLM pool values (2026-07-25 10:52 E2E).

That run's COL_053 pool accepted LLM hallucinations that were
"novel" but format-junk: an echo of the COLUMN NAME from the prompt
('COL_053_CARR_67J8K'), an echo of the prompt's format-instruction
examples ('UUID-1a2b3c4d-…'), and low-entropy filler. The only acceptance
test was novelty (v not in observed). For identifier-ish columns (a
relaxed template exists), values must now also match an observed length
bucket, stay within the observed charset, and never contain the column
name. Prose columns (no template) skip the gate entirely.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,missing-class-docstring,unused-argument

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

from sdfb_core.contracts import TableSchema
from sdfb_core.engines.b1_rag.engine import B1RagEngine, _pool_llm_yield
from sdfb_core.engines.b1_rag.profile import ColumnKind, ColumnProfile
from sdfb_core.engines.base import GenerationContext
from sdfb_core.observability import parse_milestone

# Mixed-length identifier values (defeats the strict detector, relaxed
# templates exist): CHG + 6 digits (9 chars) and CHG + 13 digits (16 chars).
_ID_VALUES = tuple([f"CHG{i:06d}" for i in range(40)] +
                   [f"CHG{i:013d}" for i in range(40)])


def _id_profile() -> ColumnProfile:
  return ColumnProfile(
      name="COL_053",
      bq_type="STRING",
      kind=ColumnKind.FREE_TEXT,
      nullable=False,
      null_fraction=0.0,
      observed_values=_ID_VALUES,
      text_examples=_ID_VALUES[:2],
  )


def _prose_profile() -> ColumnProfile:
  vals = tuple(f"customer reported outage number {i}" for i in range(60))
  return ColumnProfile(
      name="notes",
      bq_type="STRING",
      kind=ColumnKind.FREE_TEXT,
      nullable=False,
      null_fraction=0.0,
      observed_values=vals,
      text_examples=vals[:2],
  )


class _HallucinatingClient:
  """Yields a fixed mix of in-format novel values and format junk."""

  def __init__(self):
    self.call_count = 0

  def generate_json(self,
                    prompt,
                    json_schema,
                    *,
                    max_tokens=2048,
                    temperature=0.7,
                    n=1,
                    seed=None,
                    top_p=None,
                    top_k=None):
    self.call_count += 1
    return [
        {
            "values": [
                "CHG900001",  # in-format (9-char bucket)
                "CHG9000000000002",  # in-format (16-char bucket)
                "UUID-1a2b3c4d-5e6f-7d8c",  # prompt-example echo
                "COL_053_CARR_67J8K",  # column-name echo
                "CHG666666",  # in-format (9-char bucket)
                "col_053-77",  # column-name echo, lowercase
                "CHG12345678",  # wrong length (11) -> reject
            ]
        } for _ in range(n)
    ]


def test_gate_rejects_hallucinations_keeps_in_format():
  y = _pool_llm_yield(
      _HallucinatingClient(),
      "p",
      {},
      _id_profile(),
      ["CHG000001"],
      target=64,
  )
  assert "CHG900001" in y.pool
  assert "CHG9000000000002" in y.pool
  assert "CHG666666" in y.pool
  for junk in ("UUID-1a2b3c4d-5e6f-7d8c", "COL_053_CARR_67J8K", "col_053-77",
               "CHG12345678"):
    assert junk not in y.pool, junk
  assert y.format_rejected > 0


def test_gate_skips_prose_columns():

  class _ProseClient:

    def generate_json(self, prompt, json_schema, **kw):
      return [{"values": ["a fresh synthetic outage note entirely new"]}]

  y = _pool_llm_yield(_ProseClient(), "p", {}, _prose_profile(), [], target=8)
  assert "a fresh synthetic outage note entirely new" in y.pool
  assert y.format_rejected == 0


def test_format_rejected_surfaces_in_undersized_milestone(caplog):
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.t"
      },
      "schema": [{
          "name": "COL_053",
          "type": "STRING",
          "mode": "REQUIRED"
      }],
  })
  ctx = GenerationContext(
      table_schema=schema,
      reference_rows=[{
          "COL_053": v
      } for v in _ID_VALUES],
      pipeline_run_id="run-gate-1",
      num_rows=500,
      # Ladder-mechanics test: expansion off forces the pool path
      # (wave 4 skips ladders for expandable columns).
      freetext_expansion="off",
  )
  engine = B1RagEngine()
  with caplog.at_level("WARNING"):
    engine.setup(_HallucinatingClient(), ctx)
  milestones = [
      m for m in (parse_milestone(r.getMessage()) for r in caplog.records) if m
  ]
  reported = [
      m for m in milestones
      if m["name"] in ("freetext_pool_undersized",
                       "freetext_pool_stagnated") and "format_rejected" in m
  ]
  assert reported, "format_rejected must surface in pool-health milestones"
  assert any(int(m["format_rejected"]) > 0 for m in reported)


# --- pattern-guided decoding (opt-in; layer 2 of the hallucination fix) ----


def test_relaxed_shapes_pattern_matches_format_rejects_junk():
  import re

  from sdfb_core.engines.text_shapes import (
      build_relaxed_shapes,
      relaxed_shapes_pattern,
  )

  shapes = build_relaxed_shapes(list(_ID_VALUES))
  assert shapes is not None
  pattern = re.compile(relaxed_shapes_pattern(shapes))
  assert pattern.fullmatch("CHG900001")  # 9-char bucket
  assert pattern.fullmatch("CHG9000000000002")  # 16-char bucket
  assert not pattern.fullmatch("UUID-1a2b3c4d-5e6f-7d8c")
  assert not pattern.fullmatch("COL_053_CARR_67J8K")
  assert not pattern.fullmatch("CHG12345678")  # wrong length


class _SchemaRecordingClient:

  def __init__(self):
    self.schemas: list[dict] = []

  def generate_json(self,
                    prompt,
                    json_schema,
                    *,
                    max_tokens=2048,
                    temperature=0.7,
                    n=1,
                    seed=None,
                    top_p=None,
                    top_k=None):
    self.schemas.append(json_schema)
    return [{"values": ["CHG900001"]} for _ in range(n)]


def _gate_ctx(**overrides) -> GenerationContext:
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.t"
      },
      "schema": [{
          "name": "COL_053",
          "type": "STRING",
          "mode": "REQUIRED"
      }],
  })
  defaults = dict(
      table_schema=schema,
      reference_rows=[{
          "COL_053": v
      } for v in _ID_VALUES],
      pipeline_run_id="run-pattern-1",
      num_rows=8,
      freetext_expansion="off",
  )
  defaults.update(overrides)
  return GenerationContext(**defaults)


def test_pattern_guidance_off_by_default():
  client = _SchemaRecordingClient()
  B1RagEngine().setup(client, _gate_ctx())
  assert client.schemas
  for s in client.schemas:
    assert "pattern" not in s["properties"]["values"]["items"]


def test_pattern_guidance_adds_items_pattern_when_enabled():
  client = _SchemaRecordingClient()
  B1RagEngine().setup(client, _gate_ctx(pool_pattern_guidance=True))
  assert client.schemas
  item_schema = client.schemas[0]["properties"]["values"]["items"]
  assert "pattern" in item_schema
  import re

  assert re.compile(item_schema["pattern"]).fullmatch("CHG900001")


# --- per-column freetext_pool_built milestone (observability audit) --------


def test_clean_pool_build_logs_per_column_milestone_with_counts(caplog):
  """A pool that fills to target cleanly used to hide its format
    rejections (format_rejected only rode the stagnated/undersized/fallback
    milestones). Every column build now emits freetext_pool_built with
    counts, per-ladder seconds, and whether decode-pattern guidance was
    active."""
  client = _SchemaRecordingClient()
  with caplog.at_level("INFO"):
    B1RagEngine().setup(client, _gate_ctx(pool_pattern_guidance=True))
  built = [
      m for m in (parse_milestone(r.getMessage()) for r in caplog.records)
      if m and m["name"] == "freetext_pool_built"
  ]
  assert built, "every free-text column must emit freetext_pool_built"
  m = built[0]
  assert m["column"] == "COL_053"
  assert int(m["pool_size"]) >= 1
  assert "format_rejected" in m
  assert m["pattern_guided"] == "True"
  assert float(m["seconds"]) >= 0.0


def test_pool_built_milestone_reports_pattern_guided_false_by_default(caplog):
  client = _SchemaRecordingClient()
  with caplog.at_level("INFO"):
    B1RagEngine().setup(client, _gate_ctx())
  built = [
      m for m in (parse_milestone(r.getMessage()) for r in caplog.records)
      if m and m["name"] == "freetext_pool_built"
  ]
  assert built and built[0]["pattern_guided"] == "False"


# ---------------------------------------------------------------------------
# 2026-08-25/26 R6 A_COL_037: 385/393 and 386/393 parsed values were
# format-rejected (28-char values into a fixed 31-char bucket) over THREE
# rounds (~150 s each on T4) before the stagnation break released the
# column to the shape fallback. Two consecutive full-yield rounds with ZERO
# in-format values is a structural mismatch that temperature cannot fix.
# ---------------------------------------------------------------------------


class _WrongLengthClient:
  """Every value parses and every value has an unobserved length (11)."""

  def __init__(self):
    self.call_count = 0

  def generate_json(self, prompt, json_schema, *, n=1, **kw):
    self.call_count += 1
    out = []
    for c in range(max(1, n)):
      base = (self.call_count * 10 + c) * 100
      out.append({"values": [f"CHG{base + i:08d}" for i in range(32)]})
    return out


def test_ladder_exits_after_two_fully_rejected_rounds(caplog):
  import logging

  client = _WrongLengthClient()
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    y = _pool_llm_yield(
        client,
        "p",
        {},
        _id_profile(),
        ["CHG000001"],
        target=512,
    )
  assert client.call_count == 2, "one escalation retry, then out"
  assert y.pool == []
  assert y.parsed > 0 and y.format_rejected == y.parsed
  assert y.stagnated is True  # the caller's fallback path, unchanged
  text = "\n".join(r.message for r in caplog.records)
  assert "name=freetext_pool_format_collapse" in text
  assert "column=COL_053" in text


def test_partially_rejected_rounds_keep_the_ladder_running():
  # The existing hallucinating client yields 3 in-format values per
  # round — a partial yield is progress, not collapse.
  client = _HallucinatingClient()
  y = _pool_llm_yield(
      client,
      "p",
      {},
      _id_profile(),
      ["CHG000001"],
      target=64,
  )
  assert client.call_count > 2
  assert y.pool


# ---------------------------------------------------------------------------
# The same column's clause shipped a 28-char fictitious example for a
# fixed 31-char column: the model echoed the example's length (8 verbatim
# echoes, 385 rejects). The engine can see this BEFORE spending a round:
# the example fails the column's own format gate.
# ---------------------------------------------------------------------------


def test_off_format_constraint_example_is_flagged_before_the_ladder(caplog):
  import logging

  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.t"
      },
      "schema": [{
          "name":
              "COL_053",
          "type":
              "STRING",
          "mode":
              "REQUIRED",
          "description": ('{"llm_prompt_constraint": {"format": "change ref",'
                          ' "examples": ["CHG1234567890", "CHG777777"]}}'),
      }],
  })
  ctx = GenerationContext(
      table_schema=schema,
      reference_rows=[{
          "COL_053": v
      } for v in _ID_VALUES],
      pipeline_run_id="run-example-preflight",
      reference_digest="d-example-preflight",
      num_rows=64,
      freetext_expansion="off",
  )
  engine = B1RagEngine()
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    engine.setup(_HallucinatingClient(), ctx)
  milestones = [
      m for m in (parse_milestone(r.getMessage()) for r in caplog.records) if m
  ]
  flagged = [
      m for m in milestones
      if m["name"] == "prompt_constraint_example_off_format"
  ]
  # Only the 13-char example is off-format; CHG777777 (9) sits in a bucket.
  assert len(flagged) == 1
  assert flagged[0]["column"] == "COL_053"
  assert flagged[0]["example_len"] == "13"
  assert flagged[0]["gate_lengths"] == "9,16"
  engine.teardown()


# ---------------------------------------------------------------------------
# 2026-08-26 R6 A_COL_019: source narratives are cut at 35 characters
# (len_p50 34, len_p95 == len_max 35 — a fixed-width field); the pool's
# LLM values ran to 62 (p95 42). The prose gate passes everything, so the
# prompt's length band was advisory only. A fixed-width ceiling is
# enforced the way the source enforces it: by truncation.
# ---------------------------------------------------------------------------


def _fixed_width_prose_profile(width: int = 35) -> ColumnProfile:
  long_text = "TRANSFER TO BENEFICIARY ACCOUNT NUMBER REFERENCE"
  vals = tuple([f"{long_text} {i}"[:width]
                for i in range(30)]  # cut at the width
               + [f"NOMINA {i}" for i in range(30)]  # short, free length
              )
  return ColumnProfile(
      name="narrative",
      bq_type="STRING",
      kind=ColumnKind.FREE_TEXT,
      nullable=False,
      null_fraction=0.0,
      observed_values=vals,
      text_examples=vals[:2],
  )


def test_prose_candidates_are_clamped_to_a_fixed_width_source_ceiling(caplog):
  import logging

  long_value = "TRF.EX-095098765 A SOFIA MORALES MONTIEL DE LA VEGA"  # 51
  short_value = "ABONO A UNA CUENTA AJENA NUEVO"  # 30

  class _Client:

    def generate_json(self, prompt, json_schema, **kw):
      return [{"values": [long_value, short_value]}]

  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    y = _pool_llm_yield(
        _Client(),
        "p",
        {},
        _fixed_width_prose_profile(35),
        [],
        target=8,
    )
  assert long_value[:35] in y.pool
  assert long_value not in y.pool
  assert short_value in y.pool
  assert y.format_rejected == 0  # clamped, never rejected
  text = "\n".join(r.message for r in caplog.records)
  assert "name=freetext_pool_length_clamped" in text
  assert "column=narrative" in text
  assert "max_len=35" in text


def _free_length_prose_profile() -> ColumnProfile:
  # Lengths 15..54 spread evenly, plus ONE outlier at 80: p95 < max.
  vals = (
      *(f"note {'x' * (10 + i % 40)}" for i in range(60)),
      "note " + "y" * 75,
  )
  return ColumnProfile(
      name="remarks",
      bq_type="STRING",
      kind=ColumnKind.FREE_TEXT,
      nullable=False,
      null_fraction=0.0,
      observed_values=vals,
      text_examples=vals[:2],
  )


def test_free_length_prose_is_never_clamped():
  # A lone maximum (p95 < max) is not a wall: no ceiling exists.
  long_value = "a very long synthetic outage note that keeps going and going on"

  class _Client:

    def generate_json(self, prompt, json_schema, **kw):
      return [{"values": [long_value]}]

  y = _pool_llm_yield(
      _Client(), "p", {}, _free_length_prose_profile(), [], target=8)
  assert long_value in y.pool


def test_narrow_length_band_is_not_a_ceiling():
  # The gate file's prose fixture: every value is 33-34 chars. A band
  # that narrow is a width the shape template carries, not a wall the
  # length distribution ran into — the LLM's longer value passes whole.
  from sdfb_core.engines.text_shapes import length_ceiling

  assert length_ceiling(_prose_profile().observed_values) is None
  assert length_ceiling(_fixed_width_prose_profile(35).observed_values) == 35


# ---------------------------------------------------------------------------
# ADR 0034 — the fixed-width ceiling also applies to MASK-gated columns.
#
# 2026-08-29 R6 (cold): A_COL_019 was gated by collapsed masks
# (`prompt_constraint_example_off_format ... gate_lengths=mask`), so the
# ADR 0033 prose ceiling never engaged and pool values ran to 48 chars
# against a 35-char source (p95 42 vs 35). Masks collapse letter runs, so
# they cannot see length — the ceiling must clamp BEFORE the mask check,
# exactly as it does for prose.
# ---------------------------------------------------------------------------
def _mask_gated_fixed_width_profile(width: int = 35) -> ColumnProfile:
  from sdfb_core.engines.text_shapes import build_shape_mix

  names = [
      "MARGARITA",
      "VALENTINO",
      "SEBASTIAN",
      "ALEJANDRA",
      "FRANCISCO",
      "GUADALUPE",
      "ESPERANZA",
      "CRISTOBAL",
      "MAXIMILIA",
      "ANASTASIA",
  ]
  long_vals = [
      f"TRANSFERENCIA A FAVOR DE {names[i % len(names)]} LOPEZ GARCIA"[:width]
      for i in range(30)
  ]
  short_vals = [f"NOMINA {i}" for i in range(30)]
  vals = tuple(long_vals + short_vals)
  return ColumnProfile(
      name="concept",
      bq_type="STRING",
      kind=ColumnKind.FREE_TEXT,
      nullable=False,
      null_fraction=0.0,
      observed_values=vals,
      text_examples=vals[:2],
      shape_mix=build_shape_mix(vals),
  )


def test_mask_gated_candidates_are_clamped_to_the_fixed_width_ceiling(caplog):
  import logging

  from sdfb_core.engines.b1_rag.engine import _format_gate

  prof = _mask_gated_fixed_width_profile(35)
  gate = _format_gate(prof)
  assert not gate.prose, "fixture must be mask-gated, not prose"

  long_value = "TRANSFERENCIA A FAVOR DE CATALINAS RUIZ MORALES"  # 47
  short_value = "NOMINA 77"

  class _Client:

    def generate_json(self, prompt, json_schema, **kw):
      return [{"values": [long_value, short_value]}]

  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    y = _pool_llm_yield(_Client(), "p", {}, prof, [], target=8)
  assert long_value[:35] in y.pool
  assert long_value not in y.pool
  assert short_value in y.pool
  assert y.format_rejected == 0
  text = "\n".join(r.message for r in caplog.records)
  assert "name=freetext_pool_length_clamped" in text
  assert "max_len=35" in text
