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
"""The offline planner behind `--fixture_dir` (`context.offline`,
`cli.fixture`) against the real one: the plan it makes from rows in
memory is the plan `build_plan` makes from the same rows through
BigQuery (here `PlanBq`, which answers the planning SQL from its text,
an implementation of its own).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from unit.context.plan_fakes import (
    QDS,
    REFERENCE_N,
    RUN_IDS,
    SCHEMAS,
    TABLES,
    PlanBq,
    thelook_launch,
    thelook_models,
    thelook_rows,
)

from sdfb_evaluation.cli.fixture import load_fixture
from sdfb_evaluation.context import offline
from sdfb_evaluation.context.plan import (
    ATOM_TOP_K,
    DICTIONARY_TOP_K,
    Knobs,
    build_plan,
    evaluation_key,
    kinds_from_schema,
    planning_outputs,
    table_roles,
)
from sdfb_evaluation.context.relationships import Edge

from .helpers import NOW, write_fixture

NAMES = ("users", "orders")
SPECS: dict[str, dict[str, Any]] = {
    "users": {
        "name": "users",
        "schema": SCHEMAS["users"],
        "pk": ["id"],
        "identity": ["email"],
    },
    "orders": {
        "name": "orders",
        "schema": SCHEMAS["orders"],
        "pk": ["order_id"],
        "edges": [{
            "cols": ["user_id"],
            "ref": "users",
            "ref_cols": ["id"]
        }],
    },
}
# what the two planners must agree on, column by column. Left out, by
# design: the 1,001-point quantile grids between their ends and the
# atoms — `PlanBq` picks a quantile by nearest rank and breaks top-k
# ties by text, the offline planner by inverted CDF and canonical value
# (BigQuery's own APPROX_ functions promise neither)
_COLUMN_FIELDS = ("name", "bq_type", "mode", "kind", "is_key",
                  "day_granularity", "census", "value_sample_rate",
                  "dictionary", "literal_ok", "source_distinct",
                  "detection_dictionary", "synthetic_distinct", "census_head",
                  "avg_len")


@pytest.fixture(scope="module", name="rows")
def fixture_rows_() -> dict[str, dict[str, list[dict]]]:
  return {"source": thelook_rows(1), "synthetic": thelook_rows(2)}


@pytest.fixture(scope="module", name="directory")
def fixture_directory(tmp_path_factory, rows) -> Path:
  tmp = tmp_path_factory.mktemp("cli_fixture")
  return write_fixture(
      tmp / "fixture",
      rows["source"],
      rows["synthetic"],
      tables=NAMES,
      panel=REFERENCE_N,
      specs=SPECS)


def _real_plan(rows):
  bq = PlanBq(source=rows["source"], landing=rows["synthetic"])
  launch = thelook_launch(bq, tables=TABLES[:2], run_ids=RUN_IDS[:2])
  plan = build_plan(
      launch=launch,
      models=thelook_models(),
      bq=bq,
      knobs=Knobs(temp_dataset=QDS),
      mode="exact",
      trigger="cli",
      runner="DirectRunner",
      now=NOW)
  return plan, launch


def test_fixture_rows_come_back_with_the_clients_types(directory, rows):
  fixture = load_fixture(str(directory))
  users, orders = fixture.tables
  assert users.source_rows == rows["source"]["users"]  # DATE, TIMESTAMP, None
  assert orders.synthetic_rows == rows["synthetic"]["orders"]
  assert users.edges == () and orders.edges == (Edge(
      cols=("user_id",), ref="users", ref_cols=("id",)),)
  assert (users.pk, users.identity, users.panel_rows) == (("id",), ("email",),
                                                          REFERENCE_N)


def test_the_fixture_plan_agrees_with_build_plan(directory, rows):
  """One parity for the whole offline planner: tables, roles, keys,
  edges, row counts, pairs and — per column — everything the two
  planners derive from the same rows."""
  real, launch = _real_plan(rows)
  fixture = load_fixture(str(directory))
  offline_plan = fixture.plan(
      knobs=Knobs(),
      mode="exact",
      trigger="cli",
      runner="DirectRunner",
      now=NOW,
      evaluation_id="eval-20260914T080000Z-0badc0de")
  wanted = {t.name: t for t in real.tables if t.role != "external"}
  assert [t.name for t in offline_plan.tables] == list(wanted) == list(NAMES)
  for planned in offline_plan.tables:
    table = wanted[planned.name]
    assert (planned.role, planned.pk, planned.identity,
            planned.edges) == (table.role, table.pk, table.identity,
                               table.edges), planned.name
    assert (planned.rows_source,
            planned.rows_synthetic) == (table.rows_source, table.rows_synthetic)
    assert planned.scope.mode == table.scope.mode == "table"
    assert planned.scope.ok and planned.scope.expected_rows == (
        table.scope.expected_rows)
    assert planned.evaluated and planned.skip_reason is None
    for mine, theirs in zip(planned.columns, table.columns, strict=True):
      for name in _COLUMN_FIELDS:
        assert getattr(mine,
                       name) == getattr(theirs,
                                        name), (planned.name, mine.name, name)
      for name in ("mean_src", "std_src"):
        assert getattr(mine, name) == pytest.approx(getattr(theirs, name))
      for side in ("quantiles_src", "quantiles_syn"):
        grid, other = getattr(mine, side), getattr(theirs, side)
        assert (grid is None) == (other is None), (mine.name, side)
        if grid is not None:  # the same ends, the same number of points
          assert (len(grid), grid[0], grid[-1]) == (len(other), other[0],
                                                    other[-1])
    assert planned.pairs == table.pairs
    assert len(planned.panel.r_rows) == len(table.panel.r_rows) == REFERENCE_N
    assert planned.panel.verified and planned.panel.e_n == table.panel.e_n
  assert offline_plan.skip_reason is None and not offline_plan.prepare_sql
  assert offline_plan.mode == real.mode
  assert offline_plan.knobs.key_dict() == real.knobs.key_dict()
  # one definition each of the key, the roles and the top-k sizes
  assert evaluation_key(launch, "exact", real.knobs) == real.evaluation_key
  assert evaluation_key(launch, "sampled", real.knobs) != real.evaluation_key
  assert table_roles({t.name: t.edges for t in offline_plan.tables}) == {
      "users": "root",
      "orders": "driven"
  }
  tops = {
      output.sql.rsplit(", ", 1)[1].rstrip(")")
      for table in real.tables
      for output in planning_outputs(table.columns)
      if output.stat == "top"
  }
  assert tops == {str(ATOM_TOP_K), str(DICTIONARY_TOP_K)}


def test_offline_statistics_have_the_planning_selects_shape(rows):
  """`context.offline.planning_stats` returns what `parse_planning`
  returns: the statistics each column's planning SELECT asks for, no
  more (a key column stops after its distinct count)."""
  real, _ = _real_plan(rows)
  orders = next(t for t in real.tables if t.name == "orders")
  keys = frozenset({"order_id", "user_id"})
  stats = offline.planning_stats(SCHEMAS["orders"], rows["source"]["orders"],
                                 keys)
  assert stats["rows"] == len(rows["source"]["orders"])
  asked: dict[str, set[str]] = {}
  for output in planning_outputs(orders.columns):
    asked.setdefault(output.column, set()).add(output.stat)
  for name, wanted in asked.items():
    given = {k for k, v in stats["columns"][name].items() if v is not None}
    assert given <= wanted, name
    assert wanted - given <= {"avg_len"}, name  # None for an all-NULL column
  assert set(stats["columns"]["order_id"]) == {"null", "distinct"}


def test_offline_statistics_take_their_type_names_from_the_planner():
  """One definition of which types are numbers, times, booleans, text
  and not scalars at all: a type the planner counts only NULLs of
  (`GEOGRAPHY`, `INTERVAL`, `RANGE` beside `RECORD` and `JSON`) gets no
  other statistic here, and every alias is planned like its type."""
  fields = [{
      "name": name,
      "type": bq_type,
      "mode": "NULLABLE"
  } for name, bq_type in (("spot", "GEOGRAPHY"), ("gap", "INTERVAL"),
                          ("span", "RANGE"), ("blob", "JSON"), ("n", "INTEGER"),
                          ("x", "FLOAT"), ("d", "DECIMAL"), ("flag", "BOOLEAN"),
                          ("raw", "BYTES"))]
  rows = [{
      "spot": "POINT(1 2)",
      "gap": "0-0 1 0:0:0",
      "span": "[2026-01-01, 2026-02-01)",
      "blob": {
          "a": 1
      },
      "n": 3,
      "x": 0.5,
      "d": 2,
      "flag": True,
      "raw": b"ab",
  },
          dict.fromkeys(f["name"] for f in fields)]
  stats = offline.planning_stats(fields, rows)["columns"]
  asked: dict[str, set[str]] = {}
  for output in planning_outputs(kinds_from_schema(fields, keys=())):
    asked.setdefault(output.column, set()).add(output.stat)
  for name in ("spot", "gap", "span", "blob"):
    assert asked[name] == {"null"} and stats[name] == {"null": 1}, name
  for name in ("n", "x", "d", "flag", "raw"):
    assert set(stats[name]) == asked[name], name


def test_a_malformed_fixture_says_what_is_wrong(directory, tmp_path):
  with pytest.raises(ValueError, match=r"fixture\.json"):
    load_fixture(str(tmp_path))
  broken = tmp_path / "broken"
  broken.mkdir()
  manifest = json.loads((directory / "fixture.json").read_text())
  manifest["tables"][0]["primary_key"] = ["id"]
  (broken / "fixture.json").write_text(json.dumps(manifest))
  with pytest.raises(ValueError, match="primary_key"):
    load_fixture(str(broken))
  del manifest["tables"][0]["primary_key"]
  (broken / "fixture.json").write_text(json.dumps(manifest))
  with pytest.raises(ValueError, match=r"users\.source\.json"):
    load_fixture(str(broken))
  for name in NAMES:
    for side in ("source", "synthetic"):
      path = directory / f"{name}.{side}.json"
      (broken / path.name).write_text(path.read_text())
  load_fixture(str(broken))
  cells = json.loads((broken / "users.source.json").read_text())
  cells[0]["age"] = "about forty"
  (broken / "users.source.json").write_text(json.dumps(cells))
  with pytest.raises(ValueError, match="'age'") as info:
    load_fixture(str(broken))
  assert "about forty" not in str(info.value)  # the column, never the value
