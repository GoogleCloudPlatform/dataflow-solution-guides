"""build_plan_detail: per-column fidelity detail (Task 9)."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,use-implicit-booleaness-not-comparison

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from sdfb_core.contracts.schema import FieldSchema, TableSchema
from sdfb_core.engines.b1_rag.profile import profile_columns
from sdfb_core.engines.b2_library.fidelity import profile_column
from sdfb_core.engines.generation_plan import build_plan_detail


def _rows() -> list[dict]:
  rows = [{
      "NOTES": f"REF {i:04d} SETTLED PAYMENT ORDER",
      "FLAG": "Y"
  } for i in range(60)]
  rows += [{"NOTES": "", "FLAG": "N"} for _ in range(40)]
  return rows


def test_detail_from_b1_profiles():
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t"
      },
      "schema": [
          {
              "name": "NOTES",
              "type": "STRING",
              "mode": "NULLABLE"
          },
          {
              "name": "FLAG",
              "type": "STRING",
              "mode": "REQUIRED"
          },
      ],
  })
  detail = build_plan_detail(profile_columns(schema, _rows()))
  assert detail["NOTES"]["kind"] == "freetext_llm_pool"
  assert detail["NOTES"]["empty_fraction"] == 0.4
  assert detail["NOTES"]["shapes"] >= 1
  assert detail["NOTES"]["constraint"] is False
  assert detail["FLAG"]["kind"] == "categorical"


def test_detail_from_b2_profiles():
  fields = {
      name:
          FieldSchema.model_validate({
              "name": name,
              "type": "STRING",
              "mode": "NULLABLE"
          }) for name in ("NOTES", "FLAG")
  }
  profiles = {name: profile_column(f, _rows()) for name, f in fields.items()}
  detail = build_plan_detail(profiles)
  assert detail["NOTES"]["empty_fraction"] == 0.4
  assert set(detail) == {"NOTES", "FLAG"}


def test_detail_reports_expandability():
  # 2026-08-11 R1 postmortems reverse-engineered per column whether the
  # bulk draw came from the shape-mix expansion or the bounded pool —
  # the plan line now says it (`expandable`, the default-`identifiers`
  # expansion eligibility).
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t"
      },
      "schema": [
          {
              "name": "CODE",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "PROSE",
              "type": "STRING",
              "mode": "REQUIRED"
          },
      ],
  })
  import random

  rng = random.Random(2)
  words = ["outage", "spike", "crash", "router", "flap", "link", "poll"]
  rows = [{
      "CODE":
          f"U{i:06d}",
      "PROSE":
          " ".join(rng.choice(words)
                   for _ in range(rng.randrange(3, 9))) + f" ticket {i}",
  }
          for i in range(60)]
  detail = build_plan_detail(profile_columns(schema, rows))
  assert detail["CODE"]["expandable"] is True
  assert detail["PROSE"]["expandable"] is False


def test_constraints_detail_shows_the_fetched_clause():
  # Debugging "did my Terraform constraint reach the engine?" needed a
  # worker-log line carrying WHAT was fetched, not the boolean
  # `constraint: true` — the clause is config (Terraform/git-owned, never
  # real data per ADR 0024 §privacy), so it is loggable in full.
  from sdfb_core.engines.generation_plan import build_constraints_detail

  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t"
      },
      "schema": [
          {
              "name": "PLAIN",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name":
                  "REF",
              "type":
                  "STRING",
              "mode":
                  "NULLABLE",
              "description":
                  ('{"llm_prompt_constraint": {"format": "SWIFT-style ref",'
                   ' "pattern": "^[A-Z]{4}[0-9]{3}$", "length": 7,'
                   ' "examples": ["ABCD123"]}}'),
          },
      ],
  })
  rows = [{
      "PLAIN": f"note {i}",
      "REF": f"{'ABCD'[i % 4] * 4}{i:03d}"
  } for i in range(60)]
  detail = build_constraints_detail(profile_columns(schema, rows))
  assert set(detail) == {"REF"}
  entry = detail["REF"]
  assert "SWIFT-style ref" in entry["clause"]
  assert entry["chars"] == len(entry["clause"])
  assert len(entry["clause_sha12"]) == 12
  assert entry["pattern"] is True
  assert entry["sets_length"] is True
  assert entry["examples"] == 1


def test_constraints_detail_empty_without_constraints():
  from sdfb_core.engines.generation_plan import build_constraints_detail

  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t"
      },
      "schema": [{
          "name": "NOTES",
          "type": "STRING",
          "mode": "NULLABLE"
      }],
  })
  rows = [{"NOTES": f"free prose num {i}"} for i in range(60)]
  assert build_constraints_detail(profile_columns(schema, rows)) == {}


def test_constraints_detail_from_b2_profiles():
  from sdfb_core.engines.generation_plan import build_constraints_detail

  field = FieldSchema.model_validate({
      "name": "REF",
      "type": "STRING",
      "mode": "NULLABLE",
      "description": '{"llm_prompt_constraint": {"charset": "digits only"}}',
  })
  rows = [{"REF": f"{i:07d}"} for i in range(60)]
  detail = build_constraints_detail({"REF": profile_column(field, rows)})
  assert "digits only" in detail["REF"]["clause"]
