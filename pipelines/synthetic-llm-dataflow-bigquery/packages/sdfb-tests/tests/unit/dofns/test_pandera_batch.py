"""Unit tests for `PanderaValidateBatchDoFn`. Covers M1 §8 acceptance #2
at the per-batch line of defense — specifically duplicate-PK detection
which is structurally impossible to catch at the per-record stage."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=use-implicit-booleaness-not-comparison

from __future__ import annotations

import apache_beam as beam
from sdfb_beam.dofns.pandera_batch import PanderaValidateBatchDoFn


def test_passes_valid_batch(customers_schema, customers_reference):
  do_fn = PanderaValidateBatchDoFn(table_schema=customers_schema)
  do_fn.setup()
  out = list(do_fn.process(customers_reference[:5]))
  assert len(out) == 5
  assert not any(isinstance(x, beam.pvalue.TaggedOutput) for x in out)


def test_empty_batch_is_noop(customers_schema):
  do_fn = PanderaValidateBatchDoFn(table_schema=customers_schema)
  do_fn.setup()
  out = list(do_fn.process([]))
  assert out == []


def test_duplicate_pk_routes_to_dlq(customers_schema, customers_reference):
  """Two rows with the same `customer_id` — PK uniqueness violation."""
  do_fn = PanderaValidateBatchDoFn(table_schema=customers_schema)
  do_fn.setup()
  batch = [customers_reference[0], dict(customers_reference[0])]  # same PK
  out = list(do_fn.process(batch))

  tagged = [x for x in out if isinstance(x, beam.pvalue.TaggedOutput)]
  assert tagged, "duplicate PK should route something to DLQ"
  for t in tagged:
    assert t.tag == "invalid"
    assert t.value["error_type"] == "pandera"
    assert t.value["rule_id"] == "schema.batch"
    assert t.value["stage"] == "pre_write"
    assert "failure_count" in t.value["error_detail"]
