"""Collapsed-mask candidate gate for shape-rigid whitespace columns (§4c).

2026-08-09 B_TABLE R1, COL_038: the LLM normalized the source's literal
three-space run to one space and the pool ladder accepted all 512 of them —
the relaxed-shapes gate is inactive for any column containing whitespace
(`build_relaxed_shapes` → None), so nothing checked format at all. The gate
now falls back to collapsed-mask membership when the shape mix can template:
digit/letter run lengths may vary (novelty stays possible), whitespace runs
and punctuation must match an observed mask exactly.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,unused-argument

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

from sdfb_core.engines.b1_rag.engine import _pool_llm_yield
from sdfb_core.engines.b1_rag.profile import ColumnKind, ColumnProfile
from sdfb_core.engines.text_shapes import build_shape_mix

# COL_038-class: fixed 5-letter head, digit, THREE literal spaces, 2-letter
# code, digit run, letter tail.
_PADDED = tuple(f"CQZWD{i % 10}   CS{i:010d}B" for i in range(60))


def _padded_profile() -> ColumnProfile:
  return ColumnProfile(
      name="REF_CODE",
      bq_type="STRING",
      kind=ColumnKind.FREE_TEXT,
      nullable=False,
      null_fraction=0.0,
      observed_values=_PADDED,
      text_examples=_PADDED[:2],
      shape_mix=build_shape_mix(list(_PADDED)),
  )


class _SpaceCollapsingClient:
  """Mimics the R1 failure: mostly whitespace-normalized values."""

  def generate_json(self, prompt, json_schema, **kw):
    return [{
        "values": [
            "CQZWD7 CS9111111111B",  # single space — the R1 failure mode
            "CQZWD8 DN9222222222B",  # single space
            "CQZWD9   DN9333333333B",  # correct triple space, novel
            "CQZWD1CS9444444444B",  # dropped spaces entirely
        ]
    }]


def test_gate_rejects_normalized_whitespace_keeps_true_shape():
  y = _pool_llm_yield(
      _SpaceCollapsingClient(),
      "p",
      {},
      _padded_profile(),
      ["CQZWD0   CS0000000000B"],
      target=8,
  )
  assert y.pool == ["CQZWD9   DN9333333333B"]
  assert y.format_rejected >= 3


def test_gate_stays_off_for_word_diverse_prose():
  # Genuinely free prose: word counts AND word lengths vary, so masks are
  # near-singleton buckets, the mix cannot template, the gate stays off.
  import random as _r

  rng = _r.Random(4)
  vocab = [
      "outage",
      "glitch",
      "failure",
      "spike",
      "lag",
      "crash",
      "incident",
      "router",
      "db",
      "saturation",
      "flap",
      "link",
  ]
  vals = tuple(" ".join(rng.choice(vocab)
                        for _ in range(rng.randrange(3, 9))) + f" ticket {i}"
               for i in range(60))
  prof = ColumnProfile(
      name="notes",
      bq_type="STRING",
      kind=ColumnKind.FREE_TEXT,
      nullable=False,
      null_fraction=0.0,
      observed_values=vals,
      text_examples=vals[:2],
      shape_mix=build_shape_mix(list(vals)),
  )

  class _ProseClient:

    def generate_json(self, prompt, json_schema, **kw):
      return [{"values": ["a fresh synthetic outage note entirely new"]}]

  y = _pool_llm_yield(_ProseClient(), "p", {}, prof, [vals[0]], target=4)
  assert "a fresh synthetic outage note entirely new" in y.pool


def test_gate_reactivates_for_digit_columns_with_diverse_distinct_tail():
  # 2026-08-11 B_TABLE R1, COL_015: 98% of source ROWS are digit codes
  # (some with a literal leading space), but the DISTINCT set is dominated
  # by a diverse alpha tail — the distinct-weighted top-8 mix was mostly
  # all-literal singleton buckets, `shape_mix_can_template` failed its
  # class-position minimum, and the gate went dark (format_rejected=0
  # while 'REEMBOLSOS T'-class values filled the 94-value pool). With the
  # row-mass mix the digit buckets carry their true weight and the gate
  # comes back.
  from sdfb_core.contracts import TableSchema
  from sdfb_core.engines.b1_rag.engine import _format_gate
  from sdfb_core.engines.b1_rag.profile import profile_columns

  words = [
      "GARANTIA",
      "TRASPASO",
      "LIQUIDACION",
      "COMISION",
      "REINTEGRO",
      "PRESTAMO",
      "AMORTIZA",
      "RETENCION",
  ]
  rows = (
      # 25 distinct 7-digit codes, 40 rows each — 1000 rows, ONE mask.
      [{
          "c": f"{1000000 + i % 25}"
      } for i in range(1000)]
      # 20 distinct ␣+10-digit codes, 40 rows each — 800 rows, ONE mask.
      + [{
          "c": f" {2000000000 + i % 20}"
      } for i in range(800)]
      # 8 alpha mask families x 26 distinct single rows: by DISTINCT
      # count each family (26) outweighs both digit buckets (25/20), so
      # the pre-fix top-8 mix was all single-class-position alpha.
      + [{
          "c": f"{w} {chr(ord('A') + i)}"
      } for w in words for i in range(26)])
  prof = profile_columns(
      TableSchema.model_validate({
          "table_info": {
              "table_id": "p.d.t"
          },
          "schema": [{
              "name": "c",
              "type": "STRING",
              "mode": "NULLABLE"
          }],
      }),
      rows,
  )["c"]
  gate = _format_gate(prof)
  assert gate("1592637")  # novel 7-digit — in-mask
  assert gate(" 2999999999")  # novel padded 10-digit — in-mask
  assert not gate("TOTAL REEMB")  # hallucinated alpha shape — rejected
