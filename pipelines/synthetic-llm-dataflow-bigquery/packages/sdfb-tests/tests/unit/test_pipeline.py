"""Unit tests for `sdfb_beam.pipeline.build_pipeline` config validation.

Regression test for the misleading failure mode where a typo'd
`identity_columns` entry (not a real column on the target schema) sends
every-row-but-one to the DLQ as `identity.unique` (every row keys on
`str(None)` since `record.get(bogus_column)` is always `None`), which then
trips the BLOCKER gate with an error that never mentions the actual typo.
Failing fast with a clear `ValueError` at graph-construction time is much
cheaper to diagnose.
"""

from __future__ import annotations

import apache_beam as beam
import pytest
from apache_beam.options.pipeline_options import PipelineOptions
from sdfb_beam.io.local_sinks import WriteToJsonLines
from sdfb_beam.pipeline import PipelineConfig, _dlq_rule_weight, build_pipeline
from sdfb_tests.fakes import FakeModelClient  # also registers the "minimal" engine


def _config(schema, **overrides):
  defaults = dict(
      table_schema=schema,
      engine_name="minimal",
      model_client=FakeModelClient(reference_pool=[{}]),
      num_rows=5,
      batch_size=5,
      run_id="test-identity-validation",
  )
  defaults.update(overrides)
  return PipelineConfig(**defaults)


def test_build_pipeline_rejects_unknown_identity_column(tmp_path,
                                                        customers_schema):
  config = _config(customers_schema, identity_columns=("not_a_real_column",))

  options = PipelineOptions(["--runner=DirectRunner"])
  with pytest.raises(
      ValueError,
      match="not_a_real_column"), beam.Pipeline(options=options) as p:
    build_pipeline(
        p,
        reference_rows=[],
        config=config,
        landing_sink=WriteToJsonLines(str(tmp_path / "landing")),
        dlq_sink=WriteToJsonLines(str(tmp_path / "dlq")),
    )


def test_build_pipeline_error_lists_valid_columns(tmp_path, customers_schema):
  config = _config(customers_schema, identity_columns=("bogus",))

  options = PipelineOptions(["--runner=DirectRunner"])
  with pytest.raises(ValueError) as exc_info, beam.Pipeline(
      options=options) as p:
    build_pipeline(
        p,
        reference_rows=[],
        config=config,
        landing_sink=WriteToJsonLines(str(tmp_path / "landing")),
        dlq_sink=WriteToJsonLines(str(tmp_path / "dlq")),
    )
  assert "customer_id" in str(exc_info.value)


def test_build_pipeline_accepts_valid_identity_column(tmp_path,
                                                      customers_schema,
                                                      customers_reference):
  config = _config(
      customers_schema,
      identity_columns=("customer_id",),
      model_client=FakeModelClient(reference_pool=customers_reference),
  )

  options = PipelineOptions(["--runner=DirectRunner"])
  with beam.Pipeline(options=options) as p:
    result = build_pipeline(
        p,
        reference_rows=customers_reference,
        config=config,
        landing_sink=WriteToJsonLines(str(tmp_path / "landing")),
        dlq_sink=WriteToJsonLines(str(tmp_path / "dlq")),
    )
  assert result["run_id"] == "test-identity-validation"


def test_build_pipeline_rejects_unknown_pk_column(tmp_path, customers_schema):
  config = _config(customers_schema, pk_columns=("bogus",))

  options = PipelineOptions(["--runner=DirectRunner"])
  with pytest.raises(ValueError) as exc_info, beam.Pipeline(
      options=options) as p:
    build_pipeline(
        p,
        reference_rows=[],
        config=config,
        landing_sink=WriteToJsonLines(str(tmp_path / "landing")),
        dlq_sink=WriteToJsonLines(str(tmp_path / "dlq")),
    )
  assert "pk_columns" in str(exc_info.value)
  assert "bogus" in str(exc_info.value)


class TestDlqRuleWeight:
  """`_dlq_rule_weight` feeds the BLOCKER-gate rule counts (§12). A crashed
    batch loses its whole `n`-row batch, not one row — so `engine_failure`
    envelopes must weight by the lost batch size, not count as 1 like every
    other rule."""

  def test_engine_failure_weighted_by_batch_n(self):
    envelope = {
        "rule_id": "engine_failure",
        "raw_request": {
            "batch_id": 3,
            "n": 16
        }
    }
    assert _dlq_rule_weight(envelope) == ("engine_failure", 16)

  def test_engine_failure_missing_raw_request_defaults_to_one(self):
    envelope = {"rule_id": "engine_failure"}
    assert _dlq_rule_weight(envelope) == ("engine_failure", 1)

  def test_engine_failure_non_numeric_n_defaults_to_one(self):
    envelope = {"rule_id": "engine_failure", "raw_request": {"n": "oops"}}
    assert _dlq_rule_weight(envelope) == ("engine_failure", 1)

  def test_fk_unmatched_weighted_by_the_keys_expected_rows(self):
    """ADR 0037 §4 ruling B: a driving key dropped for having no
        conditional candidate loses every row it would have produced, so
        its envelope carries that share in `raw_request["n"]` — counting
        it as 1 would let a run that lost half its rows PASS the gate."""
    envelope = {
        "rule_id": "fk.unmatched",
        "error_type": "referential_integrity",
        "raw_request": {
            "batch_id": 3,
            "keys": [["t1", "l1"]],
            "n": 7
        },
    }
    assert _dlq_rule_weight(envelope) == ("fk.unmatched", 7)

  def test_fk_unmatched_without_a_weight_defaults_to_one(self):
    assert _dlq_rule_weight({"rule_id": "fk.unmatched"}) == ("fk.unmatched", 1)

  def test_other_rule_weighted_one_per_envelope(self):
    envelope = {"rule_id": "row.duplicate", "raw_request": {"n": 16}}
    assert _dlq_rule_weight(envelope) == ("row.duplicate", 1)

  def test_unknown_rule_id_defaults(self):
    assert _dlq_rule_weight({}) == ("unknown", 1)


def test_build_pipeline_threads_source_values_table_into_context(
    tmp_path, customers_schema, customers_reference):
  """ADR 0023 generate-path wiring: the launcher's reference-table FQN
    must reach GenerationContext so workers can attach the source-value
    store (B.2 builds pools lazily there — no pool branch)."""
  config = _config(
      customers_schema,
      model_client=FakeModelClient(reference_pool=customers_reference),
      source_values_table="p.d.customers",
  )
  options = PipelineOptions(["--runner=DirectRunner"])
  with beam.Pipeline(options=options) as p:
    result = build_pipeline(
        p,
        reference_rows=customers_reference,
        config=config,
        landing_sink=WriteToJsonLines(str(tmp_path / "landing")),
        dlq_sink=WriteToJsonLines(str(tmp_path / "dlq")),
    )
  assert result["generation_context"].source_values_table == "p.d.customers"


def test_build_pipeline_threads_prompt_debug_into_context(
    tmp_path, customers_schema, customers_reference):
  """ADR 0024 §3c wiring: the CLI's --prompt_debug must survive
    PipelineConfig → GenerationContext, or engines silently fall back to
    'off' and no freetext_pool_prompt milestone is ever logged. Constructing
    the config with prompt_debug= is itself part of the regression: the CLI
    passes exactly this keyword (run_pipeline.py), so an unknown-field
    TypeError here means every --prompt_debug invocation crashes."""
  config = _config(
      customers_schema,
      model_client=FakeModelClient(reference_pool=customers_reference),
      prompt_debug="redacted",
  )
  options = PipelineOptions(["--runner=DirectRunner"])
  with beam.Pipeline(options=options) as p:
    result = build_pipeline(
        p,
        reference_rows=customers_reference,
        config=config,
        landing_sink=WriteToJsonLines(str(tmp_path / "landing")),
        dlq_sink=WriteToJsonLines(str(tmp_path / "dlq")),
    )
  assert result["generation_context"].prompt_debug == "redacted"
