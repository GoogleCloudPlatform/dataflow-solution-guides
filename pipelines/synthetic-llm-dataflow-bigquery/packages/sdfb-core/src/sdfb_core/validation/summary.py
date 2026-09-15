"""Run-level summary + BLOCKER gate — one row of ``validation_runs``.

The Beam pipeline counts valid rows and DLQ entries (grouped by
``rule_id``), then builds a :class:`RunSummary`. If the fraction of
BLOCKER-severity failures exceeds ``blocker_failure_ratio`` the gate
raises :class:`BlockerThresholdExceeded`, which fails the Dataflow job
(skill: validation-mode-a §"Failing the job").

REF: .claude/skills/validation-mode-a.md
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from sdfb_core.contracts.model_adjustment import (
    landing_repeat_share,
    repeat_share_verdict,
)
from sdfb_core.validation.thresholds import Thresholds

# rule_ids whose failures count toward the BLOCKER gate. Mirrors the
# BLOCKER-severity rows in config/thresholds.yml.
BLOCKER_RULE_IDS = frozenset({
    "schema.types",
    "null.required",
    "pk.duplicate",
    "row.duplicate",
    "identity.unique",
    # An engine crash (incl. strict_freetext re-raise reaching the DoFn)
    # loses the whole batch — that must count toward the gate, not PASS
    # with fewer rows. The pipeline layer (`sdfb_beam.pipeline._dlq_rule_weight`)
    # weights each `engine_failure` DLQ envelope by its lost batch size
    # (not 1-per-envelope like every other rule) before it ever reaches
    # `dlq_by_rule` here, so "loses the whole batch" is arithmetically
    # true by the time this function sees the counts.
    "engine_failure",
})

STATUS_PASSED = "PASSED"
STATUS_FAILED_BLOCKER = "FAILED_BLOCKER"


class BlockerThresholdExceeded(  # noqa: N818 — name fixed by validation-mode-a skill
    RuntimeError):
  """Raised to FAIL the Dataflow job when the BLOCKER gate trips."""


class RunSummary(BaseModel):
  """One row of ``synthetic_data_quality.validation_runs``."""

  run_id: str
  reference_digest: str
  reference_table: str = ""
  landing_table: str = ""
  engine: str = ""
  model_uri: str = ""
  env: str = "dev"
  num_rows_requested: int = 0
  valid_count: int = 0
  dlq_count: int = 0
  blocker_count: int = 0
  dlq_by_rule: dict[str, int] = Field(default_factory=dict)
  blocker_failure_ratio: float = 0.0
  observed_blocker_ratio: float = 0.0
  status: str = STATUS_PASSED
  created_at: str = ""
  # ADR 0038 — rule_ids left OUT of `blocker_count` for this table, as
  # a comma-separated list. `pk.duplicate` lands here when the launch
  # ADJUSTED this table's model (the duplicates are the point). Named
  # in the row rather than hidden, so a PASSED summary always says what
  # it did not weigh.
  excluded_blocker_rules: str = ""
  # ADR 0038 — the proof the adjusted copy is faithful: the SOURCE
  # key-repeat share (measured at launch off the fan-out histogram)
  # beside the one the landing table actually reached, and the verdict.
  source_repeat_share: float | None = None
  landing_repeat_share: float | None = None
  repeat_share_delta: float | None = None
  repeat_share_within_tolerance: bool | None = None

  def to_bq_row(self) -> dict:
    """JSON-load-shaped row. ``dlq_by_rule`` is a JSON string so the
        column can be a plain STRING (robust under FILE_LOADS)."""
    row = self.model_dump()
    row["dlq_by_rule"] = json.dumps(
        row["dlq_by_rule"], sort_keys=True, default=str)
    return row


def gate_total(
    valid_count: int,
    dlq_by_rule: Mapping[str, int],
    excluded: Collection[str] = (),
) -> int:
  """The BLOCKER gate's DENOMINATOR — the rows this run actually
    generated, which is ``valid_count`` plus every DLQ row the gate
    still weighs.

    An EXCLUDED rule (ADR 0038) leaves BOTH sides of the ratio. Leaving
    it in the denominator alone divides every OTHER blocker rule on that
    table by the expected duplicates too, so the gate stops firing at the
    configured threshold: on the 2026-09-12 E_TABLE shape a genuine
    1,400-row `engine_failure` over 104,209 generated rows read 0.00886
    against a 0.01 gate and PASSED, where the honest 0.01343 fails.
    `pipeline._gate_inputs` already argues this arithmetic for
    `valid_count` in streaming mode ("a silently weaker gate"); the same
    reasoning ends at the excluded rules.

    ``dlq_count`` and ``dlq_by_rule`` are deliberately NOT touched: the
    summary row still REPORTS every diverted record. Only the gate's
    arithmetic narrows.
    """
  excluded_ids = frozenset(excluded)
  return int(valid_count) + sum(
      c for rid, c in dlq_by_rule.items() if rid not in excluded_ids)


def build_run_summary(
    *,
    run_id: str,
    reference_digest: str,
    valid_count: int,
    dlq_by_rule: dict[str, int],
    thresholds: Thresholds,
    num_rows_requested: int = 0,
    reference_table: str = "",
    landing_table: str = "",
    engine: str = "",
    model_uri: str = "",
    created_at: str | None = None,
    excluded_blocker_rules: Sequence[str] = (),
    source_repeat_share: float | None = None,
) -> RunSummary:
  """Fold counts + thresholds into a :class:`RunSummary` with a status.

    ``excluded_blocker_rules`` (ADR 0038) drops rule_ids from the gate's
    NUMERATOR only — they stay in ``dlq_by_rule`` and ``dlq_count``, so
    the run still reports them. Today one caller passes one thing:
    ``pk.duplicate`` on a table whose declared `pk:` the SOURCE disproved
    and the launch therefore dropped. Those duplicates are the faithful
    copy, not a defect; weighing them would fail every correct run of
    such a table. Every other table's `pk.duplicate` is untouched.

    ``source_repeat_share`` arms the comparison that proves the copy:
    the landed share is derived from the same counts (`pk.duplicate`
    over the rows generated) and the delta is checked against
    ``REPEAT_SHARE_TOLERANCE``. Both describe the DECLARED PK (fix J
    measures the source over exactly the columns `pk.duplicate` counts),
    so the verdict is like-for-like wherever a share arrives at all —
    fix H4's "not comparable" case cannot occur any more, and a launch
    that measured no share writes none.
    """
  excluded = frozenset(excluded_blocker_rules)
  dlq_count = sum(dlq_by_rule.values())
  blocker_count = sum(c for rid, c in dlq_by_rule.items()
                      if rid in BLOCKER_RULE_IDS and rid not in excluded)
  total = gate_total(valid_count, dlq_by_rule, excluded)
  landed = landing_repeat_share(
      valid_count=valid_count, dlq_by_rule=dlq_by_rule)
  delta, within = repeat_share_verdict(source_repeat_share, landed)
  observed = (blocker_count / total) if total else 0.0
  status = (
      STATUS_FAILED_BLOCKER
      if observed > thresholds.blocker_failure_ratio else STATUS_PASSED)
  return RunSummary(
      run_id=run_id,
      reference_digest=reference_digest,
      reference_table=reference_table,
      landing_table=landing_table,
      engine=engine,
      model_uri=model_uri,
      env=thresholds.env,
      num_rows_requested=num_rows_requested,
      valid_count=valid_count,
      dlq_count=dlq_count,
      blocker_count=blocker_count,
      dlq_by_rule=dict(dlq_by_rule),
      blocker_failure_ratio=thresholds.blocker_failure_ratio,
      observed_blocker_ratio=observed,
      status=status,
      created_at=created_at or datetime.now(tz=UTC).isoformat(),
      excluded_blocker_rules=",".join(sorted(excluded)),
      source_repeat_share=source_repeat_share,
      landing_repeat_share=(landed
                            if source_repeat_share is not None else None),
      repeat_share_delta=delta,
      repeat_share_within_tolerance=within,
  )


def evaluate_blocker_gate(summary: RunSummary) -> None:
  """Raise :class:`BlockerThresholdExceeded` if the summary failed the gate."""
  if summary.status == STATUS_FAILED_BLOCKER:
    # The same denominator `observed_blocker_ratio` was computed over
    # — an excluded rule is out of both (`gate_total`), so the
    # message never prints a ratio the operator cannot reproduce.
    total = gate_total(
        summary.valid_count,
        summary.dlq_by_rule,
        [r for r in summary.excluded_blocker_rules.split(",") if r],
    )
    raise BlockerThresholdExceeded(
        f"BLOCKER failures {summary.blocker_count}/{total} = "
        f"{summary.observed_blocker_ratio:.4f} exceeds gate "
        f"{summary.blocker_failure_ratio:.4f} (env={summary.env}, "
        f"run_id={summary.run_id})")
