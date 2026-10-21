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
"""Tests for the shared enums and the `MetricValue` record.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses

import pytest

from sdfb_evaluation import schemas
from sdfb_evaluation.types import (
    PROFILE_KINDS,
    PROFILE_SIDES,
    ColumnKind,
    Method,
    MetricValue,
    ProfileValue,
    Side,
    Status,
)


def test_column_kind_values():
  assert {member.value for member in ColumnKind} == {
      "numeric", "temporal", "categorical", "boolean", "text", "identifier",
      "nested"
  }


def test_side_values():
  assert {member.value for member in Side
         } == {"source", "synthetic", "reference", "holdout"}


def test_status_values():
  assert {member.value for member in Status
         } == {"pass", "warn", "fail", "info", "not_evaluated"}


def test_method_values():
  assert {member.value for member in Method
         } == {"exact", "binned", "sketch", "sample", "value_sampled"}


def test_enums_are_str_subclasses_for_json_and_bq_writers():
  assert ColumnKind.NUMERIC == "numeric"
  assert isinstance(Status.PASS, str)


def test_metric_value_required_fields_and_defaults():
  metric = MetricValue(metric_id="field.null_rate", table="orders", value=0.1)
  assert metric.metric_id == "field.null_rate"
  assert metric.table == "orders"
  assert metric.value == 0.1
  assert metric.column is None
  assert metric.column_2 is None
  assert metric.edge is None
  assert metric.method is Method.EXACT
  assert metric.column_kind is None
  assert metric.detail == {}


def test_metric_value_accepts_none_value():
  metric = MetricValue(metric_id="field.null_rate", table="orders", value=None)
  assert metric.value is None


def test_metric_value_is_frozen():
  metric = MetricValue(metric_id="field.null_rate", table="orders", value=0.1)
  with pytest.raises(dataclasses.FrozenInstanceError):
    metric.value = 0.2  # type: ignore[misc]


def test_metric_value_detail_default_is_independent_per_instance():
  first = MetricValue(metric_id="x", table="t", value=1.0)
  second = MetricValue(metric_id="x", table="t", value=1.0)
  assert first.detail is not second.detail


def test_metric_value_not_evaluated_carries_reason_in_detail():
  metric = MetricValue.not_evaluated("field.null_rate", "orders",
                                     "source column absent")
  assert metric.metric_id == "field.null_rate"
  assert metric.table == "orders"
  assert metric.value is None
  assert metric.detail == {"reason": "source column absent"}


def test_metric_value_not_evaluated_forwards_scope_kwargs():
  metric = MetricValue.not_evaluated(
      "column.ks", "orders", "sample too small", column="amount", n_source=3)
  assert metric.column == "amount"
  assert metric.n_source == 3
  assert metric.value is None


def _described(field: str) -> set[str]:
  """The ` | `-separated vocabulary in a profiles schema description."""
  spec = next(
      f for f in schemas.load_schema("evaluation_profiles")
      if f["name"] == field)
  return {word.strip() for word in spec["description"].split("|")}


def test_profile_vocabularies_match_the_profiles_schema():
  assert set(PROFILE_KINDS) == _described("profile_kind")
  assert set(PROFILE_SIDES) == _described("side")


def test_profile_value_validates_its_kind_and_side():
  profile = ProfileValue(
      table="orders",
      profile_kind="histogram",
      side="source",
      payload={"edges": []},
      column="amount")
  assert (profile.edge, profile.n, profile.truncated) == (None, None, None)
  with pytest.raises(ValueError, match="profile_kind"):
    ProfileValue(table="t", profile_kind="nope", side="source", payload={})
  with pytest.raises(ValueError, match="side"):
    ProfileValue(table="t", profile_kind="moments", side="nowhere", payload={})
