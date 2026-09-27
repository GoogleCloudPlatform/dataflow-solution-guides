#  Copyright 2026 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
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
