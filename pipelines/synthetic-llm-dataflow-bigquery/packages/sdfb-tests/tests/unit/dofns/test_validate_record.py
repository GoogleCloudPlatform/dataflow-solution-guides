"""Unit tests for `ValidateRecordDoFn`. Covers M1 §8 acceptance #2
(DLQ populated when synthetic records intentionally violate constraints)
at the per-record line of defense."""

from __future__ import annotations

import apache_beam as beam
from sdfb_beam.dofns.validate_record import ValidateRecordDoFn


def test_passes_valid_record_unchanged(customers_schema, customers_reference):
  do_fn = ValidateRecordDoFn(table_schema=customers_schema)
  do_fn.setup()
  record = customers_reference[0]
  out = list(do_fn.process(record))
  assert out == [record]


def test_routes_wrong_type_to_invalid_tag(customers_schema):
  do_fn = ValidateRecordDoFn(table_schema=customers_schema)
  do_fn.setup()
  bad = {
      "customer_id": "not-an-int",
      "email": "x@example.com",
      "signup_at": "2026-01-01T00:00:00Z",
      "tier": "FREE",
  }
  out = list(do_fn.process(bad))
  assert len(out) == 1
  tagged = out[0]
  assert isinstance(tagged, beam.pvalue.TaggedOutput)
  assert tagged.tag == "invalid"
  assert tagged.value["error_type"] == "pydantic"
  assert tagged.value["rule_id"] == "schema.types"
  assert tagged.value["stage"] == "pre_write"
  assert tagged.value["raw_record"] == bad
  assert isinstance(tagged.value["error_detail"], list)


def test_routes_missing_required_to_invalid_tag(customers_schema,
                                                customers_reference):
  do_fn = ValidateRecordDoFn(table_schema=customers_schema)
  do_fn.setup()
  bad = dict(customers_reference[0])
  bad.pop("customer_id")  # REQUIRED column
  out = list(do_fn.process(bad))
  assert len(out) == 1
  assert isinstance(out[0], beam.pvalue.TaggedOutput)
  assert out[0].tag == "invalid"


def test_routes_extra_field_to_invalid_tag(customers_schema,
                                           customers_reference):
  """`extra='forbid'` on `GeneratedRecord` — extra fields must DLQ."""
  do_fn = ValidateRecordDoFn(table_schema=customers_schema)
  do_fn.setup()
  bad = dict(customers_reference[0])
  bad["unexpected"] = "stray field"
  out = list(do_fn.process(bad))
  assert len(out) == 1
  assert isinstance(out[0], beam.pvalue.TaggedOutput)
