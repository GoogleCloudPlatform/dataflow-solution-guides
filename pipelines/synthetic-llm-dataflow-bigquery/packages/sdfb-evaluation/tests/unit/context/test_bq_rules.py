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
"""`bq_rules.points_in_time`/`check_one_point_in_time_per_table` (R58):
nine probe forms of `FOR SYSTEM_TIME AS OF` BigQuery actually accepts, and
the fail-closed guard that raises rather than silently reads an
unparseable one as "current"."""

from __future__ import annotations

import pytest

from sdfb_evaluation.context.bq import BqApiError

from .bq_rules import check_one_point_in_time_per_table, points_in_time

TABLE = "demo-project.thelook_synthetic.orders"
TABLE_COLON = "demo-project:thelook_synthetic.orders"
T1 = "2026-09-13T13:49:20.000000Z"
T2 = "2026-09-13T13:49:41.250000Z"


# ---------------------------------------------------------------------------
# parsed correctly
# ---------------------------------------------------------------------------
def test_probe_1_an_alias_before_for_system_time_is_parsed():
  sql = (f"SELECT * FROM `{TABLE}` AS x FOR SYSTEM_TIME AS OF TIMESTAMP "
         f"'{T1}'")
  assert points_in_time(sql) == {TABLE: {T1}}
  # A bare alias (no AS) parses the same way.
  bare = f"SELECT * FROM `{TABLE}` x FOR SYSTEM_TIME AS OF TIMESTAMP '{T1}'"
  assert points_in_time(bare) == {TABLE: {T1}}


def test_probe_2_lowercase_is_parsed():
  sql = f"select * from `{TABLE}` for system_time as of timestamp '{T1}'"
  assert points_in_time(sql) == {TABLE: {T1}}


def test_probe_3_a_newline_or_multiple_spaces_is_parsed():
  sql = f"SELECT * FROM `{TABLE}`\n  FOR   SYSTEM_TIME  AS OF TIMESTAMP '{T1}'"
  assert points_in_time(sql) == {TABLE: {T1}}


def test_probe_6_a_quoted_string_without_timestamp_is_parsed():
  sql = f"SELECT * FROM `{TABLE}` FOR SYSTEM_TIME AS OF '{T1}'"
  assert points_in_time(sql) == {TABLE: {T1}}


def test_probe_7_partial_backticks_are_parsed():
  # `` `project.dataset`.table `` — common when a project id holds
  # hyphens but the rest of the reference does not need quoting.
  sql = (f"SELECT * FROM `demo-project.thelook_synthetic`.orders FOR "
         f"SYSTEM_TIME AS OF TIMESTAMP '{T1}'")
  assert points_in_time(sql) == {TABLE: {T1}}


def test_probe_8_no_backticks_at_all_is_parsed():
  sql = f"SELECT * FROM {TABLE} FOR SYSTEM_TIME AS OF TIMESTAMP '{T1}'"
  assert points_in_time(sql) == {TABLE: {T1}}


def test_probe_9_project_colon_dataset_normalises_and_detects_a_clash():
  # project:dataset and project.dataset spell the same table; reading it
  # at two different points is still one table, two points.
  sql = (f"SELECT * FROM `{TABLE_COLON}` FOR SYSTEM_TIME AS OF TIMESTAMP "
         f"'{T1}' JOIN `{TABLE}` FOR SYSTEM_TIME AS OF TIMESTAMP '{T2}' "
         "USING (order_id)")
  assert points_in_time(sql) == {TABLE: {T1, T2}}
  with pytest.raises(BqApiError, match="more than one point in time"):
    check_one_point_in_time_per_table(sql)
  # The same point under either spelling is not a clash.
  same = (f"SELECT * FROM `{TABLE_COLON}` FOR SYSTEM_TIME AS OF TIMESTAMP "
          f"'{T1}' JOIN `{TABLE}` FOR SYSTEM_TIME AS OF TIMESTAMP '{T1}' "
          "USING (order_id)")
  assert points_in_time(same) == {TABLE: {T1}}
  check_one_point_in_time_per_table(same)


# ---------------------------------------------------------------------------
# rejected loudly — never read as "current"
# ---------------------------------------------------------------------------
def test_probe_4_an_at_param_point_is_rejected():
  sql = f"SELECT * FROM `{TABLE}` FOR SYSTEM_TIME AS OF @ws"
  with pytest.raises(AssertionError):
    points_in_time(sql)


def test_probe_5_a_timestamp_sub_expression_is_rejected():
  sql = (f"SELECT * FROM `{TABLE}` FOR SYSTEM_TIME AS OF "
         "TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)")
  with pytest.raises(AssertionError):
    points_in_time(sql)


# ---------------------------------------------------------------------------
# unaffected by the fail-closed guard
# ---------------------------------------------------------------------------
def test_no_as_of_clause_at_all_reads_at_the_current_time():
  sql = f"SELECT * FROM `{TABLE}` AS t"
  assert points_in_time(sql) == {TABLE: {"current"}}


def test_the_same_table_current_and_a_specific_point_is_a_clash():
  sql = (f"SELECT * FROM `{TABLE}` JOIN (SELECT * FROM `{TABLE}` FOR "
         f"SYSTEM_TIME AS OF TIMESTAMP '{T2}') USING (order_id)")
  with pytest.raises(BqApiError, match="more than one point in time"):
    check_one_point_in_time_per_table(sql)
