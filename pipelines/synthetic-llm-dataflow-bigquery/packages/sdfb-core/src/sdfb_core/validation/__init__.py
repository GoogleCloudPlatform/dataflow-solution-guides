"""Mode-A run-level validation: thresholds, run summary, BLOCKER gate, DLQ
normalization. Pure-Python — no Beam, no GCP (M1 §12)."""

from sdfb_core.validation.dlq import normalize_dlq_record
from sdfb_core.validation.summary import (
    BLOCKER_RULE_IDS,
    STATUS_FAILED_BLOCKER,
    STATUS_PASSED,
    BlockerThresholdExceeded,
    RunSummary,
    build_run_summary,
    evaluate_blocker_gate,
)
from sdfb_core.validation.thresholds import Thresholds, load_thresholds

__all__ = [
    "BLOCKER_RULE_IDS",
    "STATUS_FAILED_BLOCKER",
    "STATUS_PASSED",
    "BlockerThresholdExceeded",
    "RunSummary",
    "Thresholds",
    "build_run_summary",
    "evaluate_blocker_gate",
    "load_thresholds",
    "normalize_dlq_record",
]
