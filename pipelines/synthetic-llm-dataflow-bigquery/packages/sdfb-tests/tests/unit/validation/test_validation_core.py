"""Unit tests for the pure-Python Mode-A validation core (M1 §12)."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,missing-class-docstring,redefined-outer-name,reimported

from __future__ import annotations

import json

import pytest
from sdfb_core.validation import (
    BLOCKER_RULE_IDS,
    STATUS_FAILED_BLOCKER,
    STATUS_PASSED,
    BlockerThresholdExceeded,
    Thresholds,
    build_run_summary,
    evaluate_blocker_gate,
    load_thresholds,
    normalize_dlq_record,
)


def _thresholds(ratio: float = 0.05, env: str = "dev") -> Thresholds:
  return Thresholds(env=env, blocker_failure_ratio=ratio)


class TestThresholds:

  def test_from_mapping_resolves_env(self):
    data = {
        "defaults": {
            "blocker_failure_ratio": {
                "dev": 0.2,
                "prd": 0.01
            }
        },
        "rules": {
            "schema.types": {
                "severity": "BLOCKER"
            }
        },
    }
    t = Thresholds.from_mapping(data, "prd")
    assert t.env == "prd"
    assert t.blocker_failure_ratio == 0.01
    assert t.rules == {"schema.types": {"severity": "BLOCKER"}}

  def test_from_mapping_scalar_ratio(self):
    t = Thresholds.from_mapping({"defaults": {
        "blocker_failure_ratio": 0.1
    }}, "dev")
    assert t.blocker_failure_ratio == 0.1

  def test_from_mapping_missing_env_raises(self):
    with pytest.raises(ValueError, match="env="):
      Thresholds.from_mapping(
          {"defaults": {
              "blocker_failure_ratio": {
                  "dev": 0.2
              }
          }}, "prd")

  def test_load_thresholds_from_file(self, tmp_path):
    p = tmp_path / "t.yml"
    p.write_text("defaults:\n  blocker_failure_ratio:\n    dev: 0.2\n")
    assert load_thresholds(p, "dev").blocker_failure_ratio == 0.2


class TestRunSummary:

  def test_critical_failures_do_not_count_as_blocker(self):
    s = build_run_summary(
        run_id="r",
        reference_digest="d",
        valid_count=100,
        dlq_by_rule={"schema.batch": 5},
        thresholds=_thresholds(0.05),
    )
    assert s.blocker_count == 0
    assert s.dlq_count == 5
    assert s.status == STATUS_PASSED

  def test_blocker_ratio_exceeded_fails(self):
    s = build_run_summary(
        run_id="r",
        reference_digest="d",
        valid_count=90,
        dlq_by_rule={"schema.types": 10},
        thresholds=_thresholds(0.05),
    )
    assert s.blocker_count == 10
    assert s.observed_blocker_ratio == pytest.approx(0.10)
    assert s.status == STATUS_FAILED_BLOCKER

  def test_boundary_equal_is_pass(self):
    # exactly at the ratio is NOT strictly greater -> pass
    s = build_run_summary(
        run_id="r",
        reference_digest="d",
        valid_count=95,
        dlq_by_rule={"schema.types": 5},
        thresholds=_thresholds(0.05),
    )
    assert s.observed_blocker_ratio == pytest.approx(0.05)
    assert s.status == STATUS_PASSED

  def test_empty_run_is_pass(self):
    s = build_run_summary(
        run_id="r",
        reference_digest="d",
        valid_count=0,
        dlq_by_rule={},
        thresholds=_thresholds(0.05),
    )
    assert s.observed_blocker_ratio == 0.0
    assert s.status == STATUS_PASSED

  def test_to_bq_row_serializes_dlq_map(self):
    s = build_run_summary(
        run_id="r",
        reference_digest="d",
        valid_count=1,
        dlq_by_rule={"schema.types": 2},
        thresholds=_thresholds(1.0),
    )
    row = s.to_bq_row()
    assert isinstance(row["dlq_by_rule"], str)
    assert json.loads(row["dlq_by_rule"]) == {"schema.types": 2}
    assert row["created_at"]


class TestBlockerGate:

  def test_raises_on_failed_blocker(self):
    s = build_run_summary(
        run_id="r",
        reference_digest="d",
        valid_count=0,
        dlq_by_rule={"schema.types": 1},
        thresholds=_thresholds(0.0),
    )
    assert s.status == STATUS_FAILED_BLOCKER
    with pytest.raises(BlockerThresholdExceeded):
      evaluate_blocker_gate(s)

  def test_passes_silently(self):
    s = build_run_summary(
        run_id="r",
        reference_digest="d",
        valid_count=10,
        dlq_by_rule={},
        thresholds=_thresholds(0.05),
    )
    evaluate_blocker_gate(s)


class TestDlqNormalize:

  def test_pydantic_envelope(self):
    raw = {
        "raw_record": {
            "a": 1
        },
        "error_type": "pydantic",
        "error_detail": [{
            "loc": ["a"]
        }],
        "rule_id": "schema.types",
        "stage": "pre_write",
    }
    out = normalize_dlq_record(raw, run_id="r1")
    assert out["run_id"] == "r1"
    assert json.loads(out["raw_record"]) == {"a": 1}
    assert out["error_type"] == "pydantic"
    assert out["pipeline_step"] == "ValidateRecordDoFn"
    assert out["rule_id"] == "schema.types"
    assert out["stage"] == "pre_write"
    assert out["dlq_inserted_at"]

  def test_engine_envelope_uses_raw_request(self):
    raw = {
        "raw_request": {
            "batch_id": 0,
            "n": 5
        },
        "error_type": "engine",
        "error_detail": "ValueError: boom",
        "rule_id": "engine_failure",
        "stage": "pre_write",
    }
    out = normalize_dlq_record(raw, run_id="r2")
    assert json.loads(out["raw_record"]) == {"batch_id": 0, "n": 5}
    assert out["pipeline_step"] == "GenerateRecordsDoFn"

  def test_uniqueness_envelope_maps_pipeline_step(self):
    raw = {
        "raw_request": {
            "id": 1
        },
        "error_type":
            "uniqueness",
        "error_detail":
            "row.duplicate: duplicate of an earlier record in this run",
        "rule_id":
            "row.duplicate",
        "stage":
            "pre_write",
    }
    out = normalize_dlq_record(raw, run_id="r3")
    assert json.loads(out["raw_record"]) == {"id": 1}
    assert out["error_type"] == "uniqueness"
    assert out["pipeline_step"] == "EnforceUniqueness"
    assert out["rule_id"] == "row.duplicate"

  def test_explicit_inserted_at_and_step(self):
    out = normalize_dlq_record(
        {"error_type": "pandera"},
        run_id="r",
        pipeline_step="X",
        inserted_at="2026-01-01T00:00:00+00:00",
    )
    assert out["dlq_inserted_at"] == "2026-01-01T00:00:00+00:00"
    assert out["pipeline_step"] == "X"

  def test_fk_unmatched_envelope_maps_pipeline_step(self):
    """ADR 0037: `GenerateRecordsDoFn`'s `fk.unmatched` envelope
        shares `error_type="referential_integrity"` with `fk.orphan`
        (`EnforceFkIntegrityDoFn`, see the next test) — the two rules
        must still resolve to their own pipeline step."""
    raw = {
        "raw_request": {
            "batch_id": 3,
            "keys": [["K2"]],
            "n": 5
        },
        "error_type": "referential_integrity",
        "error_detail": "no T,R candidate for key ('K2',)",
        "rule_id": "fk.unmatched",
        "stage": "pre_generate",
    }
    out = normalize_dlq_record(raw, run_id="r4")
    assert json.loads(out["raw_record"]) == {
        "batch_id": 3,
        "keys": [["K2"]],
        "n": 5
    }
    assert out["error_type"] == "referential_integrity"
    assert out["pipeline_step"] == "GenerateRecordsDoFn"
    assert out["rule_id"] == "fk.unmatched"

  def test_fk_orphan_envelope_keeps_its_own_pipeline_step(self):
    """Same `error_type` as `fk.unmatched` above, different `rule_id`
        (ADR 0031) — proves the mapping is keyed by rule, not error_type
        alone, so adding `fk.unmatched` never relabels `fk.orphan` rows."""
    raw = {
        "raw_record": {
            "PID": "P1"
        },
        "error_type": "referential_integrity",
        "error_detail": "COL=('P1',) is not a landed parent key",
        "rule_id": "fk.orphan",
        "stage": "pre_write",
    }
    out = normalize_dlq_record(raw, run_id="r5")
    assert out["error_type"] == "referential_integrity"
    assert out["pipeline_step"] == "EnforceFkIntegrityDoFn"
    assert out["rule_id"] == "fk.orphan"


def test_blocker_rule_ids_constant():
  assert "schema.types" in BLOCKER_RULE_IDS
  assert "schema.batch" not in BLOCKER_RULE_IDS


def test_engine_failure_counts_as_blocker():
  from sdfb_core.validation.summary import BLOCKER_RULE_IDS

  assert "engine_failure" in BLOCKER_RULE_IDS


class TestAdjustedTableGate:
  """ADR 0038 — an ADJUSTED table's `pk.duplicate` is expected, so it
    stops counting toward the BLOCKER gate. Every other table's, and
    every other rule on the adjusted table itself, keeps blocking."""

  def test_pk_duplicate_is_excluded_for_an_adjusted_table(self):
    s = build_run_summary(
        run_id="r",
        reference_digest="d",
        valid_count=50,
        dlq_by_rule={"pk.duplicate": 50},
        thresholds=_thresholds(0.05),
        excluded_blocker_rules=("pk.duplicate",),
    )
    assert s.blocker_count == 0
    assert s.dlq_count == 50  # still counted and reported
    assert s.status == STATUS_PASSED
    assert s.excluded_blocker_rules == "pk.duplicate"

  def test_another_tables_pk_duplicate_still_blocks(self):
    s = build_run_summary(
        run_id="r",
        reference_digest="d",
        valid_count=50,
        dlq_by_rule={"pk.duplicate": 50},
        thresholds=_thresholds(0.05),
    )
    assert s.blocker_count == 50
    assert s.status == STATUS_FAILED_BLOCKER
    assert s.excluded_blocker_rules == ""

  def test_an_adjusted_table_still_blocks_on_every_other_rule(self):
    s = build_run_summary(
        run_id="r",
        reference_digest="d",
        valid_count=50,
        dlq_by_rule={
            "pk.duplicate": 40,
            "null.required": 10
        },
        thresholds=_thresholds(0.05),
        excluded_blocker_rules=("pk.duplicate",),
    )
    assert s.blocker_count == 10
    assert s.status == STATUS_FAILED_BLOCKER

  def test_the_repeat_shares_are_compared_in_the_summary(self):
    s = build_run_summary(
        run_id="r",
        reference_digest="d",
        valid_count=80,
        dlq_by_rule={
            "row.duplicate": 20,
            "pk.duplicate": 51
        },
        thresholds=_thresholds(0.05),
        excluded_blocker_rules=("pk.duplicate",),
        source_repeat_share=0.5025,
    )
    assert s.source_repeat_share == pytest.approx(0.5025)
    assert s.landing_repeat_share == pytest.approx(0.51)
    assert s.repeat_share_delta == pytest.approx(0.51 - 0.5025)
    assert s.repeat_share_within_tolerance is True

  def test_a_landing_share_far_from_the_source_is_flagged(self):
    # A fan-out capped to one child per key lands ~0 repeats.
    s = build_run_summary(
        run_id="r",
        reference_digest="d",
        valid_count=100,
        dlq_by_rule={},
        thresholds=_thresholds(0.05),
        excluded_blocker_rules=("pk.duplicate",),
        source_repeat_share=0.5025,
    )
    assert s.landing_repeat_share == pytest.approx(0.0)
    assert s.repeat_share_within_tolerance is False

  def test_no_source_share_claims_no_verdict(self):
    s = build_run_summary(
        run_id="r",
        reference_digest="d",
        valid_count=100,
        dlq_by_rule={},
        thresholds=_thresholds(0.05),
    )
    assert s.source_repeat_share is None
    assert s.repeat_share_within_tolerance is None
    row = s.to_bq_row()
    assert row["repeat_share_within_tolerance"] is None


class TestExcludedRuleLeavesBothSidesOfTheRatio:
  """H1 (2026-09-13 verification) — an excluded rule left the gate's
    NUMERATOR but stayed in the DENOMINATOR through
    ``total = valid_count + dlq_count``, so every OTHER blocker rule on
    an adjusted table was divided by the expected duplicates too and
    stopped firing at the configured threshold. The gate's total now
    counts only the rows actually generated; ``dlq_count`` and
    ``dlq_by_rule`` stay faithful for reporting.
    """

  # The 2026-09-12 E_TABLE shape: 105,609 derived rows at a 0.5025
  # source repeat share, minus a genuine 1,400-row engine_failure.
  _VALID = 105_609 - 1_400
  _PK_DUPES = round(0.5025 * (105_609 - 1_400))

  def test_a_genuine_engine_failure_fails_the_prd_gate(self):
    s = build_run_summary(
        run_id="r",
        reference_digest="d",
        valid_count=self._VALID,
        dlq_by_rule={
            "pk.duplicate": self._PK_DUPES,
            "engine_failure": 1_400,
        },
        thresholds=_thresholds(0.01),
        excluded_blocker_rules=("pk.duplicate",),
        source_repeat_share=0.5025,
    )
    # The honest ratio is over the rows the run actually generated,
    # NOT over those plus the expected duplicates.
    assert s.observed_blocker_ratio == pytest.approx(1_400 /
                                                     (self._VALID + 1_400))
    assert s.observed_blocker_ratio != pytest.approx(
        1_400 / (self._VALID + self._PK_DUPES + 1_400), abs=1e-6)
    assert s.status == STATUS_FAILED_BLOCKER
    # Reporting is untouched: the duplicates are still counted.
    assert s.dlq_count == self._PK_DUPES + 1_400
    assert s.dlq_by_rule["pk.duplicate"] == self._PK_DUPES

  def test_a_schema_failure_reads_its_true_share(self):
    """26,000 schema.types failures on the same table read 0.1639
        against the dev 0.2 gate while the duplicates padded the
        denominator; the true share is 0.2462."""
    s = build_run_summary(
        run_id="r",
        reference_digest="d",
        valid_count=105_609 - 26_000,
        dlq_by_rule={
            "pk.duplicate": round(0.5025 * 105_609),
            "schema.types": 26_000,
        },
        thresholds=_thresholds(0.2),
        excluded_blocker_rules=("pk.duplicate",),
    )
    assert s.observed_blocker_ratio == pytest.approx(26_000 / 105_609, abs=1e-4)
    assert s.status == STATUS_FAILED_BLOCKER

  def test_a_non_adjusted_tables_ratio_is_unchanged(self):
    s = build_run_summary(
        run_id="r",
        reference_digest="d",
        valid_count=900,
        dlq_by_rule={"pk.duplicate": 100},
        thresholds=_thresholds(0.2),
    )
    assert s.observed_blocker_ratio == pytest.approx(100 / 1_000)
    assert s.status == STATUS_PASSED

  def test_the_gate_message_names_the_honest_denominator(self):
    s = build_run_summary(
        run_id="r",
        reference_digest="d",
        valid_count=self._VALID,
        dlq_by_rule={
            "pk.duplicate": self._PK_DUPES,
            "engine_failure": 1_400,
        },
        thresholds=_thresholds(0.01),
        excluded_blocker_rules=("pk.duplicate",),
    )
    with pytest.raises(BlockerThresholdExceeded) as exc:
      evaluate_blocker_gate(s)
    assert f"1400/{self._VALID + 1_400}" in str(exc.value)
