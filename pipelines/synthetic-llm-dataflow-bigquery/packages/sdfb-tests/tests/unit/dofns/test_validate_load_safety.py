"""Load-safety gate in record validation (2026-08-22 single-job run).

B_TABLE/WriteLanding died with `reason: invalid, Rows: 1; errors: 1`:
ONE bad row aborts the whole FILE_LOADS landing job. Values that BQ
rejects at load time — strings over the column's parameterized
max_length, non-finite floats (json.dumps emits bare NaN = invalid
JSON) — must divert to the DLQ per row instead of killing the table.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import math

from sdfb_beam.dofns.validate_record import ValidateRecordDoFn
from sdfb_core.contracts import TableSchema

_SCHEMA = TableSchema.model_validate({
    "table_info": {
        "table_id": "p.d.t"
    },
    "schema": [
        {
            "name": "CODE",
            "type": "STRING",
            "mode": "REQUIRED",
            "max_length": 5
        },
        {
            "name": "AMT",
            "type": "FLOAT64",
            "mode": "NULLABLE"
        },
    ],
})


def _run(record):
  dofn = ValidateRecordDoFn(table_schema=_SCHEMA)
  dofn.setup()
  return list(dofn.process(record))


def _tags(outputs):
  import apache_beam as beam

  return [
      o.value["rule_id"]
      for o in outputs
      if isinstance(o, beam.pvalue.TaggedOutput)
  ]


def test_valid_record_passes():
  out = _run({"CODE": "AB123", "AMT": 1.5})
  assert out == [{"CODE": "AB123", "AMT": 1.5}]


def test_overlong_string_already_caught_by_pydantic():
  # The derived record model carries max_length (StringConstraints) —
  # line 1 of defense owns this class; pinned so it never regresses.
  out = _run({"CODE": "TOOLONG99", "AMT": 1.0})
  assert _tags(out) == ["schema.types"]


def test_nan_float_diverts_to_dlq():
  out = _run({"CODE": "OK123", "AMT": math.nan})
  assert _tags(out) == ["schema.non_finite"]


def test_infinity_diverts_to_dlq():
  out = _run({"CODE": "OK123", "AMT": math.inf})
  assert _tags(out) == ["schema.non_finite"]


def test_none_and_short_values_untouched():
  out = _run({"CODE": "A", "AMT": None})
  assert out == [{"CODE": "A", "AMT": None}]
