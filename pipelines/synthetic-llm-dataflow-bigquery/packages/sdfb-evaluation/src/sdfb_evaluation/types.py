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
"""Shared enums and the `MetricValue` / `ProfileValue` records the metric
and profile producers return.

`MetricValue` is the in-memory shape a metric function produces; Task 14's
writer maps its fields onto `evaluation_metrics` rows (see
`src/sdfb_evaluation/schemas/evaluation_metrics.schema.json`). It carries the
raw measurement only — `status` and `score` are derived later from the
catalogue (thresholds, direction) against `value` (or `ci_low` for
`uses_ci_bound` metrics), not stored here. `ProfileValue` is its
`evaluation_profiles` counterpart (`scoring.to_profile_row` writes it).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ColumnKind(StrEnum):
  """The evaluator's kind for a column, independent of its BigQuery type."""
  NUMERIC = "numeric"
  TEMPORAL = "temporal"
  CATEGORICAL = "categorical"
  BOOLEAN = "boolean"
  TEXT = "text"
  IDENTIFIER = "identifier"
  NESTED = "nested"


class Side(StrEnum):
  """Which population a sampled row or rank comes from (see ADR's R/E/H/H_E)."""
  SOURCE = "source"
  SYNTHETIC = "synthetic"
  REFERENCE = "reference"
  HOLDOUT = "holdout"


class Status(StrEnum):
  """A metric's gate outcome, threshold-and-noise-floor derived (D5)."""
  PASS = "pass"
  WARN = "warn"
  FAIL = "fail"
  INFO = "info"
  NOT_EVALUATED = "not_evaluated"


class Method(StrEnum):
  """How a metric's underlying statistic was estimated."""
  EXACT = "exact"
  BINNED = "binned"
  SKETCH = "sketch"
  SAMPLE = "sample"
  VALUE_SAMPLED = "value_sampled"


@dataclass(frozen=True)
class MetricValue:
  """One measured metric, at whatever scope it applies to.

  `column`/`column_2`/`edge` scope a field/column/pair/relationship-level
  metric; all `None` for a table-level metric. `detail` carries
  metric-specific structured data (e.g. a `not_evaluated` reason, or
  Chao-Shen coverage detail) and is written to the `detail` JSON column.
  """
  metric_id: str
  table: str
  value: float | None
  column: str | None = None
  column_2: str | None = None
  edge: str | None = None
  source_value: float | None = None
  synthetic_value: float | None = None
  baseline_value: float | None = None
  noise_floor: float | None = None
  ci_low: float | None = None
  ci_high: float | None = None
  n_source: int | None = None
  n_synthetic: int | None = None
  method: Method = Method.EXACT
  sample_rate: float | None = None
  column_kind: str | None = None
  encoding_plan_digest: str | None = None
  feature_set_digest: str | None = None
  detail: Mapping[str, Any] = field(default_factory=dict)

  @classmethod
  def not_evaluated(cls, metric_id: str, table: str, reason: str,
                    **scope: Any) -> MetricValue:
    """A `value=None` row for a metric that could not be computed.

    `reason` is recorded in `detail`; every other optional field (`column`,
    `n_source`, ...) is forwarded from `scope` unchanged, so a caller can
    still scope a skipped field/column/pair metric precisely.
    """
    return cls(
        metric_id=metric_id,
        table=table,
        value=None,
        detail={"reason": reason},
        **scope)


# `evaluation_profiles.profile_kind` / `.side` (the schema's vocabularies).
PROFILE_KINDS: tuple[str, ...] = (
    "histogram",
    "quantiles",
    "topk",
    "length_hist",
    "shape_mix",
    "char_classes",
    "temporal_mix",
    "null_patterns",
    "corr_matrix",
    "contingency",
    "dcr_hist",
    "nndr_hist",
    "roc_curve",
    "moments",
)
PROFILE_SIDES: tuple[str, ...] = (*(side.value for side in Side), "both")


@dataclass(frozen=True)
class ProfileValue:
  """One distribution payload for `evaluation_profiles`, before the run's
  ids are attached (`scoring.to_profile_row` writes the row).

  `payload` is the kind's JSON shape (the GUI's contract, Ruling R27); a
  producer keeps it bounded and applies the D6 literal policy. `column`/
  `edge` scope it like a `MetricValue`; `n` counts the values it was
  computed over; `edges_digest` names its bin edges, so two payloads that
  share it are directly comparable.
  """
  table: str
  profile_kind: str
  side: str
  payload: Mapping[str, Any]
  column: str | None = None
  edge: str | None = None
  n: int | None = None
  truncated: bool | None = None
  edges_digest: str | None = None

  def __post_init__(self) -> None:
    if self.profile_kind not in PROFILE_KINDS:
      raise ValueError(f"profile_kind {self.profile_kind!r}: expected one of "
                       f"{list(PROFILE_KINDS)}")
    if self.side not in PROFILE_SIDES:
      raise ValueError(f"side {self.side!r}: expected one of "
                       f"{list(PROFILE_SIDES)}")
