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
"""The evaluation plan (`sdfb_evaluation.context.plan` / `.budget`): column
kinds and routing, the one-scan planning SQL, census budgets, pairs, the
encoding digest, `build_plan` over an invented thelook launch, and the
registry seed checked against the frozen schema file."""

from __future__ import annotations

import dataclasses
import json
import math
import re

import pytest

from sdfb_evaluation import schemas
from sdfb_evaluation.canonical import hash64
from sdfb_evaluation.context.budget import (
    CENSUS_KEY_BYTES,
    MASK_KEY_OVERHEAD,
    MASK_UTF8_FACTOR,
    Budget,
    BudgetExceededError,
    census_bytes,
    mask_bytes,
    mask_key_bytes,
    predict_shuffle_gb,
    value_census_bytes,
    water_fill,
)
from sdfb_evaluation.context.plan import (
    MAX_ROW_BYTES,
    TOP_VALUE_MAX_BYTES,
    ColumnPlan,
    Knobs,
    PlanError,
    apply_planning,
    build_plan,
    encoding_plan_digest,
    kinds_from_schema,
    parse_planning,
    planning_queries,
    planning_sql,
    select_pairs,
)
from sdfb_evaluation.context.bq import BqApiError
from sdfb_evaluation.context.scope import MODES as SCOPE_MODES
from sdfb_evaluation.types import ColumnKind

from .plan_fakes import (
    BASE,
    DS,
    EXTERNAL,
    MODEL_URI,
    NOW,
    QDS,
    REFERENCE_N,
    RUN_IDS,
    SRC,
    TABLES,
    thelook_bq,
    thelook_launch,
    thelook_models,
)

KNOBS = Knobs(temp_dataset=QDS)
BIG = Budget(max_shuffle_gb=500.0, max_bytes_billed=1 << 40)


def _plan(bq=None, launch=None, *, models=None, knobs=KNOBS, mode="exact"):
  bq = bq or thelook_bq()
  return build_plan(
      launch=launch or thelook_launch(bq),
      models=thelook_models() if models is None else models,
      bq=bq,
      knobs=knobs,
      mode=mode,
      trigger="cli",
      runner="DirectRunner",
      now=NOW), bq


def _by_name(columns):
  return {c.name: c for c in columns}


# --------------------------------------------------------------------------
# kinds from the BigQuery schema
# --------------------------------------------------------------------------
_ALL_TYPES = [
    ("i", "INTEGER", "NULLABLE", ColumnKind.NUMERIC),
    ("i64", "INT64", "NULLABLE", ColumnKind.NUMERIC),
    ("f", "FLOAT", "NULLABLE", ColumnKind.NUMERIC),
    ("f64", "FLOAT64", "NULLABLE", ColumnKind.NUMERIC),
    ("num", "NUMERIC", "NULLABLE", ColumnKind.NUMERIC),
    ("bignum", "BIGNUMERIC", "NULLABLE", ColumnKind.NUMERIC),
    ("dec", "DECIMAL", "NULLABLE", ColumnKind.NUMERIC),
    ("bigdec", "BIGDECIMAL", "NULLABLE", ColumnKind.NUMERIC),
    ("b", "BOOLEAN", "NULLABLE", ColumnKind.BOOLEAN),
    ("b2", "BOOL", "REQUIRED", ColumnKind.BOOLEAN),
    ("s", "STRING", "NULLABLE", ColumnKind.CATEGORICAL),
    ("by", "BYTES", "NULLABLE", ColumnKind.CATEGORICAL),
    ("d", "DATE", "NULLABLE", ColumnKind.TEMPORAL),
    ("dt", "DATETIME", "NULLABLE", ColumnKind.TEMPORAL),
    ("t", "TIME", "NULLABLE", ColumnKind.TEMPORAL),
    ("ts", "TIMESTAMP", "NULLABLE", ColumnKind.TEMPORAL),
    ("g", "GEOGRAPHY", "NULLABLE", ColumnKind.NESTED),
    ("j", "JSON", "NULLABLE", ColumnKind.NESTED),
    ("iv", "INTERVAL", "NULLABLE", ColumnKind.NESTED),
    ("r", "RANGE", "NULLABLE", ColumnKind.NESTED),
    ("rec", "RECORD", "NULLABLE", ColumnKind.NESTED),
    ("st", "STRUCT", "NULLABLE", ColumnKind.NESTED),
    ("arr", "INTEGER", "REPEATED", ColumnKind.NESTED),
    ("future", "SOME_FUTURE_TYPE", "NULLABLE", ColumnKind.NESTED),
]


def test_kinds_from_schema_all_types():
  fields = [{"name": n, "type": t, "mode": m} for n, t, m, _ in _ALL_TYPES]
  fields.append({"name": "lower", "type": "string"})  # no mode, lower case
  cols = kinds_from_schema(fields, keys=set())
  by = _by_name(cols)
  for name, bq_type, mode, kind in _ALL_TYPES:
    col = by[name]
    assert col.kind is kind, name
    assert (col.bq_type, col.mode) == (bq_type, mode)
    assert not col.is_key
    assert col.census == "none" and col.dictionary is None
  assert by["lower"].bq_type == "STRING" and by["lower"].mode == "NULLABLE"
  assert by["lower"].kind is ColumnKind.CATEGORICAL
  assert [c.name for c in cols] == [f["name"] for f in fields]
  # Only a DATE is day-granular before the planning scan looks at values.
  assert [c.name for c in cols if c.day_granularity] == ["d"]


def test_keys_excluded_and_identifier():
  fields = [
      {
          "name": "id",
          "type": "INTEGER",
          "mode": "REQUIRED"
      },
      {
          "name": "user_id",
          "type": "STRING"
      },
      {
          "name": "email",
          "type": "STRING"
      },
      {
          "name": "amount",
          "type": "FLOAT"
      },
      {
          "name": "status",
          "type": "STRING"
      },
      {
          "name": "tags",
          "type": "STRING",
          "mode": "REPEATED"
      },
  ]
  cols = kinds_from_schema(
      fields, keys={"id", "user_id", "tags"}, identity={"email"})
  by = _by_name(cols)
  assert by["id"].is_key and by["id"].kind is ColumnKind.IDENTIFIER
  assert by["user_id"].is_key and by["user_id"].kind is ColumnKind.IDENTIFIER
  # identity: unique-but-not-a-key, so it stays in the value census.
  assert not by["email"].is_key and by["email"].kind is ColumnKind.IDENTIFIER
  # A nested column is never a key, whatever the model says.
  assert not by["tags"].is_key and by["tags"].kind is ColumnKind.NESTED

  sql = planning_sql("demo-project.thelook_synthetic.t", cols)
  key_lines = [line for line in sql.splitlines() if "t.`id`" in line]
  assert key_lines and not any(
      "APPROX_QUANTILES" in line or "APPROX_TOP_COUNT" in line
      for line in key_lines)

  n = 1000
  stats = {
      "rows": n,
      "columns": {
          "id": {
              "null": 0,
              "distinct": n
          },
          "user_id": {
              "null": 0,
              "empty": 0,
              "distinct": 300
          },
          "email": {
              "null": 0,
              "empty": 0,
              "distinct": n,
              "avg_len": 18.0,
              "top": []
          },
          "amount": {
              "null": 0,
              "distinct": 700,
              "quantiles": [float(i) for i in range(1001)],
              "mean": 1.0,
              "std": 2.0,
              "top": []
          },
          "status": {
              "null":
                  0,
              "empty":
                  0,
              "distinct":
                  4,
              "avg_len":
                  7.0,
              "top": [{
                  "value": "a",
                  "count": 400
              }, {
                  "value": "b",
                  "count": 300
              }, {
                  "value": "c",
                  "count": 200
              }, {
                  "value": "d",
                  "count": 100
              }]
          },
          "tags": {
              "null": 0
          },
      }
  }
  planned = _by_name(apply_planning(cols, stats, stats, budget=BIG))
  for key in ("id", "user_id"):
    col = planned[key]
    assert col.kind is ColumnKind.IDENTIFIER and col.is_key
    assert col.census == "none"
    assert col.dictionary is None and col.quantiles_src is None
    assert not col.literal_ok
  assert planned["email"].census == "exact"
  assert planned["tags"].census == "none"
  pairs = select_pairs(list(planned.values()), 20)
  paired = {i for pair in pairs for i in pair}
  names = [c.name for c in planned.values()]
  assert {names[i] for i in paired} == {"amount", "status"}


# --------------------------------------------------------------------------
# the planning SQL
# --------------------------------------------------------------------------
def test_planning_sql_one_scan_per_side():
  plan, bq = _plan()
  planning = bq.planning_queries()
  assert len(planning) == 2 * len(TABLES)  # one per side, none for products
  for landing in TABLES:
    name = landing.rsplit(".", 1)[1]
    source = f"{SRC}.{name}"
    reads = [
        (sql, p) for sql, p in planning if bq.reads(sql) in (landing, source)
    ]
    assert sorted(bq.reads(sql) for sql, _ in reads) == sorted(
        [landing, source])
    for sql, _ in reads:
      assert sql.count("\nFROM ") == 1  # one aggregate SELECT, one FROM item
      assert sql.lstrip().startswith("SELECT") and "GROUP BY" not in sql
      assert "COUNT(*) AS n_rows" in sql
      assert "APPROX_COUNT_DISTINCT(" in sql
      assert "APPROX_QUANTILES(" in sql and ", 1000)" in sql
      assert "UNIX_MICROS(t.`created_at`)" in sql
      assert "COUNTIF(TIME(t.`created_at`) = TIME '00:00:00')" in sql
  source_sql = next(
      sql for sql, _ in planning if bq.reads(sql) == f"{SRC}.users")
  # The source is read as the generation job saw it (pinned, D4).
  assert "FOR SYSTEM_TIME AS OF TIMESTAMP '2026-09-13T13:10:16.000000Z'" in source_sql
  assert "UNIX_MICROS(TIMESTAMP(t.`signup_date`))" in source_sql
  # 255 slots: NULL may take one and still leave the top-254 (R32).
  assert ("APPROX_TOP_COUNT(IF(BYTE_LENGTH(t.`country`) <= 1024, "
          "t.`country`, NULL), 255)") in source_sql
  assert "APPROX_TOP_COUNT(CAST(t.`age` AS FLOAT64), 11)" in source_sql
  # Every planning and panel query was dry-run before any of them ran.
  first_billed = next(
      i for i, (event, sql) in enumerate(bq.events)
      if event == "query" and ("n_rows" in sql or "__sdfb_rk" in sql))
  dry = [i for i, (event, _) in enumerate(bq.events) if event == "dry"]
  assert dry and max(dry) < first_billed
  assert len([s for s, _ in bq.dry_runs if "n_rows" in s]) == len(planning)
  kinds = [("JOBS_BY_PROJECT" in sql, "n_rows" in sql, "__sdfb_rk" in sql)
           for sql, _ in bq.queries]
  assert sum(k[0] for k in kinds) == len(TABLES)  # one contamination check
  assert sum(k[1] or k[2] for k in kinds) == 3 * len(TABLES)
  # Every query — planning, panel AND contamination — is capped.
  assert all(cap == KNOBS.max_bytes_billed for cap in bq.max_bytes)
  assert plan.bq_bytes_estimate == bq.dry_bytes * len(bq.dry_runs)


def test_planning_sql_temporal_epochs_are_unix_micros():
  cols = kinds_from_schema([
      {
          "name": "d",
          "type": "DATE"
      },
      {
          "name": "dt",
          "type": "DATETIME"
      },
      {
          "name": "tm",
          "type": "TIME"
      },
      {
          "name": "ts",
          "type": "TIMESTAMP"
      },
      {
          "name": "x",
          "type": "FLOAT64"
      },
      {
          "name": "n",
          "type": "NUMERIC"
      },
  ],
                           keys=set())
  sql = planning_sql("demo-project.thelook_synthetic.t", cols)
  assert "APPROX_QUANTILES(UNIX_MICROS(TIMESTAMP(t.`d`)), 1000)" in sql
  assert "APPROX_QUANTILES(UNIX_MICROS(TIMESTAMP(t.`dt`)), 1000)" in sql
  assert "APPROX_QUANTILES(TIME_DIFF(t.`tm`, TIME '00:00:00', MICROSECOND), 1000)" in sql
  assert "APPROX_QUANTILES(UNIX_MICROS(t.`ts`), 1000)" in sql
  assert "IF(IS_NAN(t.`x`) OR IS_INF(t.`x`), NULL, t.`x`)" in sql
  assert "CAST(t.`n` AS FLOAT64)" in sql
  assert "COUNTIF(TIME(t.`dt`) = TIME '00:00:00')" in sql
  assert "TIME(t.`d`)" not in sql  # a DATE is day-granular by type


def test_planning_sql_rejects_unsafe_input():
  cols = kinds_from_schema([{"name": "a`b", "type": "STRING"}], keys=set())
  with pytest.raises(ValueError, match="column name"):
    planning_sql("demo-project.thelook_synthetic.t", cols)
  ok = kinds_from_schema([{"name": "a", "type": "STRING"}], keys=set())
  with pytest.raises(ValueError):
    planning_sql("demo-project.x.t; DROP TABLE y", ok)
  with pytest.raises(ValueError):
    planning_sql("(SELECT 1); DROP TABLE y", ok)
  # Text is never parsed into a subquery, however harmless it looks...
  with pytest.raises(ValueError):
    planning_sql("(SELECT * FROM `demo-project.thelook_synthetic.t`)", ok)
  assert "FROM `demo-project.thelook_synthetic.t` AS t" in planning_sql(
      "`demo-project.thelook_synthetic.t`", ok)
  # ...a SourcePin/ScopePlan contributes the read_expr built for it.
  plan, _ = _plan()
  users = plan.tables[1]
  for source in (users.source_pin, users.scope):
    assert f"FROM {source.read_expr} AS t" in planning_sql(source, ok)


def _leaves(sql):
  """Output columns as BigQuery counts them: an APPROX_TOP_COUNT's
  ARRAY<STRUCT<value, count>> is three."""
  outputs = re.findall(r" AS (c\d+_[a-z_]+|n_rows)", sql)
  return len(outputs) + 2 * sql.count("APPROX_TOP_COUNT(")


def test_wide_string_table_respects_leaf_and_row_size_limits():
  """2,100 STRING columns: 7 leaves each (null, empty, distinct, avg_len,
  a three-leaf top list) and up to 255 values of 1 KiB per top list."""
  fields = [{"name": f"s{i}", "type": "STRING"} for i in range(2100)]
  cols = kinds_from_schema(fields, keys=set())
  queries = planning_queries("demo-project.thelook_synthetic.wide", cols)
  per_top = 255 * (TOP_VALUE_MAX_BYTES + 16)
  seen = []
  for sql in queries:
    assert _leaves(sql) <= 10_000
    tops = sql.count("APPROX_TOP_COUNT(")
    assert tops * per_top <= MAX_ROW_BYTES
    seen += re.findall(r" AS (c\d+)_null", sql)
  assert len(seen) == len(set(seen)) == 2100  # every column exactly once
  assert len(queries) == -(-2100 // (MAX_ROW_BYTES // per_top))


def test_wide_table_chunks_under_the_output_column_limit():
  fields = [{"name": f"x{i}", "type": "FLOAT64"} for i in range(1500)]
  cols = kinds_from_schema(fields, keys=set())
  with pytest.raises(ValueError, match="10000"):
    planning_sql("demo-project.thelook_synthetic.wide", cols)
  queries = planning_queries("demo-project.thelook_synthetic.wide", cols)
  assert len(queries) >= 2
  aliases = set()
  for sql in queries:
    outputs = re.findall(r" AS (c\d+_[a-z_]+|n_rows)", sql)
    assert _leaves(sql) <= 10_000
    assert "n_rows" in outputs
    assert sql.count("FROM `demo-project.thelook_synthetic.wide` AS t") == 1
    aliases.update(o for o in outputs if o != "n_rows")
  assert len(aliases) == sum(
      len(re.findall(r" AS c\d+_", q)) for q in queries)  # no duplicates
  rows = [{
      "n_rows": 7,
      **dict.fromkeys(re.findall(r" AS (c\d+_[a-z_]+)", q), None)
  } for q in queries]
  merged = parse_planning(rows, cols)
  assert merged["rows"] == 7 and len(merged["columns"]) == 1500
  rows[1]["n_rows"] = 8
  with pytest.raises(ValueError, match="row counts"):
    parse_planning(rows, cols)


# --------------------------------------------------------------------------
# routing, literal policy, census budget
# --------------------------------------------------------------------------
def _string_stats(distinct, nonnull, avg_len, n=None, top=None):
  n = nonnull if n is None else n
  return {
      "null": n - nonnull,
      "empty": 0,
      "distinct": distinct,
      "avg_len": avg_len,
      "top": top or []
  }


def test_string_routing():
  names = ("low", "edge", "ident", "text", "text_edge", "mid", "mid_long",
           "all_null", "keyed")
  cols = kinds_from_schema([{
      "name": n,
      "type": "STRING"
  } for n in names],
                           keys={"keyed"})
  n = 10_000
  columns = {
      "low": _string_stats(5, n, 6.0),
      "edge": _string_stats(1000, n, 30.0),  # ≤ 1000 wins over length
      "ident": _string_stats(9_500, n, 12.0),
      "text": _string_stats(9_800, n, 45.0),
      "text_edge": _string_stats(9_000, n, 20.0),  # exactly .9 and 20
      "mid": _string_stats(3_000, n, 8.0),
      "mid_long": _string_stats(3_000, n, 60.0),
      "all_null": _string_stats(0, 0, None, n=n),
      "keyed": _string_stats(5, n, 3.0),
  }
  stats = {"rows": n, "columns": columns}
  kinds = {
      c.name: c.kind for c in apply_planning(cols, stats, stats, budget=BIG)
  }
  assert kinds == {
      "low": ColumnKind.CATEGORICAL,
      "edge": ColumnKind.CATEGORICAL,
      "ident": ColumnKind.IDENTIFIER,
      "text": ColumnKind.TEXT,
      "text_edge": ColumnKind.TEXT,
      "mid": ColumnKind.CATEGORICAL,
      "mid_long": ColumnKind.TEXT,
      "all_null": ColumnKind.CATEGORICAL,
      "keyed": ColumnKind.IDENTIFIER,
  }


def _top(*pairs):
  return [{"value": v, "count": c} for v, c in pairs]


def test_census_head_is_each_sides_top_values_for_string_and_bool():
  """R67: the census's certainty stratum — source top-254 then synthetic
  top-254 codes, each once — for every non-key STRING/BYTES/BOOL column
  whatever its routed kind; none for numeric, temporal or key columns."""
  cols = kinds_from_schema([{
      "name": "note",
      "type": "STRING"
  }, {
      "name": "flag",
      "type": "BOOL"
  }, {
      "name": "qty",
      "type": "INT64"
  }, {
      "name": "id",
      "type": "STRING"
  }],
                           keys={"id"})
  n = 10_000
  src_top = _top(*((f"s{i}", 300 - i) for i in range(300)), (None, 5))
  syn_top = _top(("s0", 90), *((f"y{i}", 80 - i // 10) for i in range(300)))
  src = {
      "rows": n,
      "columns": {
          "note": _string_stats(9_800, n, 45.0, top=src_top),
          "flag": {
              "null": 0,
              "distinct": 2,
              "top": _top((True, 6000), (False, 4000))
          },
          "qty": {
              "null": 0,
              "distinct": 50,
              "top": _top((3.0, 900))
          },
          "id": _string_stats(n, n, 8.0, top=_top(("k1", 1))),
      }
  }
  syn = dict(src)
  syn["columns"] = {
      **src["columns"], "note": _string_stats(9_800, n, 45.0, top=syn_top)
  }
  by = _by_name(apply_planning(cols, src, syn, budget=BIG))
  head = by["note"].census_head
  assert head is not None and by["note"].kind is ColumnKind.TEXT
  source_part = [hash64("note", f"s{i}") for i in range(254)]
  assert list(head[:254]) == source_part  # NULL never takes a slot
  # the synthetic top-254 follows: s0 again (listed once) and 253 new
  assert hash64("note", "s0") not in head[254:]
  assert list(head[254:]) == [hash64("note", f"y{i}") for i in range(253)]
  assert set(by["flag"].census_head or
             ()) == {hash64("flag", True),
                     hash64("flag", False)}
  assert by["qty"].census_head is None and by["id"].census_head is None
  # R70: the planned length sizes the mask pass's keys
  assert by["note"].avg_len == 45.0 and by["qty"].avg_len is None
  # the head moves the encoding digest (it changes the census)
  columns = list(by.values())
  moved = [
      dataclasses.replace(c, census_head=c.census_head[1:])
      if c.name == "note" else c for c in columns
  ]
  assert encoding_plan_digest(columns, (),
                              ()) != encoding_plan_digest(moved, (), ())


def test_census_bytes_count_the_head_unsampled_and_the_mask_pass():
  """M2: a value-sampled column's head enters with certainty, and the
  mask pass of text/identifier columns is never sampled."""
  text = ColumnPlan(
      name="note",
      bq_type="STRING",
      mode="NULLABLE",
      kind=ColumnKind.TEXT,
      is_key=False,
      day_granularity=False,
      census="value_sampled",
      value_sample_rate=0.01,
      source_distinct=100_000,
      synthetic_distinct=100_000,
      census_head=tuple(range(500)),
      avg_len=40.0)
  flag = dataclasses.replace(
      text,
      name="flag",
      kind=ColumnKind.BOOLEAN,
      census="exact",
      value_sample_rate=None,
      source_distinct=2,
      synthetic_distinct=2,
      census_head=(1, 2))
  rows = 100_000.0
  keys = 200_000
  assert value_census_bytes([text], rows, rows) == pytest.approx(
      (500 + (keys - 500) * 0.01) * CENSUS_KEY_BYTES)
  # R70: a mask key carries the mask's text, as long as the value
  assert mask_key_bytes(text) == MASK_KEY_OVERHEAD + MASK_UTF8_FACTOR * 40
  assert mask_bytes([text, flag], rows, rows) == keys * mask_key_bytes(text)
  # R75: values over 256 characters pool into ONE `<long>` key a column
  prose = dataclasses.replace(text, avg_len=900.0)
  assert mask_key_bytes(
      prose) == MASK_KEY_OVERHEAD + MASK_UTF8_FACTOR * len("<long>")
  assert mask_bytes([prose], rows, rows) == 2 * mask_key_bytes(prose)
  edge = dataclasses.replace(text, avg_len=256.0)  # still one key a value
  assert mask_bytes([edge], rows, rows) == keys * (
      MASK_KEY_OVERHEAD + MASK_UTF8_FACTOR * 256)
  assert mask_key_bytes(dataclasses.replace(text, avg_len=None)) > (
      MASK_KEY_OVERHEAD)
  assert census_bytes([text, flag], rows, rows) == pytest.approx(
      value_census_bytes([text, flag], rows, rows) +
      mask_bytes([text, flag], rows, rows))


def test_literal_policy_and_dictionaries():
  names = ("safe", "rare", "wide", "flag")
  cols = kinds_from_schema([{
      "name": n,
      "type": "BOOL" if n == "flag" else "STRING"
  } for n in names],
                           keys=set())
  n = 1000
  safe_top = _top(("b", 500), (None, 40), ("a", 300), ("c", 160))
  columns = {
      "safe":
          _string_stats(3, 960, 1.0, n=n, top=safe_top),
      "rare":
          _string_stats(3, n, 1.0, top=_top(("a", 600), ("b", 395), ("c", 5))),
      "wide":
          _string_stats(
              60,
              n,
              3.0,
              top=_top(*[(f"v{i:02d}", 20 - i % 3) for i in range(60)])),
      "flag": {
          "null": 0,
          "distinct": 2,
          "top": _top((True, 700), (False, 300))
      },
  }
  stats = {"rows": n, "columns": columns}
  by = _by_name(apply_planning(cols, stats, stats, budget=BIG))
  assert by["safe"].literal_ok and by["flag"].literal_ok
  assert not by["rare"].literal_ok  # one value under the k-anon floor of 10
  assert not by["wide"].literal_ok  # more than 50 source-distinct values
  # uint64 hash64 codes, most frequent first, NULL never a dictionary value.
  assert by["safe"].dictionary == tuple(
      hash64("safe", v) for v in ("b", "a", "c"))
  assert by["flag"].dictionary == (hash64("flag", True), hash64("flag", False))
  wide = by["wide"]
  assert len(wide.dictionary) == 9 and len(wide.detection_dictionary) == 60
  assert wide.dictionary == wide.detection_dictionary[:9]
  assert all(0 <= h < 1 << 64 for h in wide.detection_dictionary)
  assert by["safe"].source_distinct == 3


def test_d6_synthetic_only_values_are_never_in_the_dictionaries():
  """A value is literal iff literal_ok AND its hash is in the source-built
  detection_dictionary: a value only the synthetic side holds is hashed."""
  cols = kinds_from_schema([{"name": "status", "type": "STRING"}], keys=set())
  src_top = _top(("Complete", 60), ("Shipped", 40))
  syn_top = _top(("Complete", 50), ("Shipped", 30), ("Teleported", 20))
  src = {
      "rows": 100,
      "columns": {
          "status": _string_stats(2, 100, 8.0, top=src_top)
      }
  }
  syn = {
      "rows": 100,
      "columns": {
          "status": _string_stats(3, 100, 8.0, top=syn_top)
      }
  }
  (status,) = apply_planning(cols, src, syn, budget=BIG)
  assert status.literal_ok
  assert hash64("status", "Complete") in status.detection_dictionary
  assert hash64("status", "Teleported") not in status.detection_dictionary
  assert hash64("status", "Teleported") not in status.dictionary
  assert status.synthetic_distinct == 3


def test_literal_ok_counts_the_exhaustive_top_list_not_the_sketch():
  """HLL can under- or over-count: the gate is the top list itself, which
  covers every non-NULL row when the column is small enough."""
  cols = kinds_from_schema([{"name": "code", "type": "STRING"}], keys=set())

  def planned(values, hll):
    top = _top(*[(f"v{i:02d}", 20) for i in range(values)])
    n = 20 * values
    stats = {
        "rows": n,
        "columns": {
            "code": _string_stats(hll, n, 3.0, top=top)
        }
    }
    return apply_planning(cols, stats, stats, budget=BIG)[0]

  assert not planned(51, hll=50).literal_ok  # the probe: 51 values, HLL 50
  assert planned(50, hll=51).literal_ok
  assert planned(50, hll=50).literal_ok


def test_tiny_census_demands_stay_exact_without_capacity():
  cols = kinds_from_schema([{
      "name": "code",
      "type": "STRING"
  }, {
      "name": "note",
      "type": "STRING"
  }, {
      "name": "flag",
      "type": "BOOL"
  }],
                           keys=set())
  stats = _census_stats(1_000_000)
  none = Budget(max_shuffle_gb=0.0, max_bytes_billed=1 << 40)
  by = _by_name(apply_planning(cols, stats, stats, budget=none))
  assert by["code"].census == "exact" and by["flag"].census == "exact"
  assert by["note"].census == "value_sampled"
  assert by["note"].value_sample_rate == 1 / 10_000


def _census_stats(n):
  return {
      "rows": n,
      "columns": {
          "code": _string_stats(20, n, 3.0),
          "note": _string_stats(990_000, n, 40.0),
          "flag": {
              "null": 0,
              "distinct": 2,
              "top": _top((True, n // 2), (False, n // 2))
          },
      }
  }


def test_census_method_switches_to_value_sampled_over_budget():
  cols = kinds_from_schema([{
      "name": "code",
      "type": "STRING"
  }, {
      "name": "note",
      "type": "STRING"
  }, {
      "name": "flag",
      "type": "BOOL"
  }],
                           keys=set())
  n = 1_000_000
  stats = _census_stats(n)
  roomy = _by_name(apply_planning(cols, stats, stats, budget=BIG))
  assert {c.census for c in roomy.values()} == {"exact"}
  assert all(c.value_sample_rate is None for c in roomy.values())

  tight = Budget(max_shuffle_gb=0.001, max_bytes_billed=1 << 40)  # 1e6 bytes
  by = _by_name(apply_planning(cols, stats, stats, budget=tight))
  assert by["code"].census == "exact" and by["flag"].census == "exact"
  note = by["note"]
  assert note.census == "value_sampled"
  assert 0 < note.value_sample_rate < 1
  capacity_keys = 1e6 / 24
  demand = 2 * 990_000
  assert note.value_sample_rate * demand <= capacity_keys - 2 * (20 + 2)
  # The quantized rate still uses most of what was left for the column.
  assert note.value_sample_rate * demand > 0.9 * (capacity_keys - 44)


def test_water_fill_is_max_min_fair():
  assert water_fill([10, 50, 100], 90) == [10, 40, 40]
  assert water_fill([10, 20], 100) == [10, 20]
  assert water_fill([5, 5], 0) == [0, 0]
  assert water_fill([], 10) == []


# --------------------------------------------------------------------------
# pairs and the encoding digest
# --------------------------------------------------------------------------
def _col(name, kind, distinct, *, is_key=False, **extra):
  return ColumnPlan(
      name=name,
      bq_type="STRING",
      mode="NULLABLE",
      kind=kind,
      is_key=is_key,
      day_granularity=False,
      source_distinct=distinct,
      **extra)


def test_pair_selection_ranks_mid_cardinality_by_distinct():
  cols = [
      _col("id", ColumnKind.IDENTIFIER, 1000, is_key=True),
      _col("flag", ColumnKind.BOOLEAN, 2),
      _col("city", ColumnKind.CATEGORICAL, 900),
      _col("status", ColumnKind.CATEGORICAL, 12),
      _col("amount", ColumnKind.NUMERIC, 5000),
      _col("const", ColumnKind.CATEGORICAL, 1),
      _col("note", ColumnKind.TEXT, 990),
      _col("tier", ColumnKind.CATEGORICAL, 10),
      _col("when", ColumnKind.TEMPORAL, 800),
      _col("blob", ColumnKind.NESTED, None),
  ]
  top3 = select_pairs(cols, 3)
  # amount, tier and when fill the 10-cell pair grid exactly (entropy proxy
  # log2(min(distinct, 10)) = log2 10, and distance to 10 cells = 0).
  assert top3 == ((4, 7), (4, 8), (7, 8))
  top5 = select_pairs(cols, 5)
  chosen = {i for pair in top5 for i in pair}
  # then status (12 values, closer to 10 cells than city's 900)
  assert chosen == {4, 7, 8, 3, 2}
  assert len(top5) == 10 and all(i < j for i, j in top5)
  every = {i for pair in select_pairs(cols, 20) for i in pair}
  assert every == {1, 2, 3, 4, 7, 8}  # never a key, text, nested or constant
  assert select_pairs(cols, 1) == () and select_pairs(cols, 0) == ()


def test_encoding_plan_digest_stable_and_sensitive():
  plan, _ = _plan()
  table = plan.tables[-1]  # order_items
  cols = list(table.columns)
  pairs = table.pairs
  base = encoding_plan_digest(cols, pairs, table.edges)
  assert base == table.encoding_plan_digest
  # Same content in another column order (pairs re-indexed) and edge order.
  order = list(reversed(range(len(cols))))
  position = {old: new for new, old in enumerate(order)}
  shuffled = [cols[i] for i in order]
  remapped = tuple(tuple(sorted((position[i], position[j]))) for i, j in pairs)
  assert encoding_plan_digest(shuffled, remapped,
                              tuple(reversed(table.edges))) == base
  numeric = next(i for i, c in enumerate(cols) if c.quantiles_src)
  grid = list(cols[numeric].quantiles_src)
  grid[500] += 0.5
  moved = list(cols)
  moved[numeric] = dataclasses.replace(cols[numeric], quantiles_src=tuple(grid))
  assert encoding_plan_digest(moved, pairs, table.edges) != base
  categorical = next(i for i, c in enumerate(cols) if c.dictionary)
  recoded = list(cols)
  recoded[categorical] = dataclasses.replace(
      cols[categorical], dictionary=cols[categorical].dictionary[1:])
  assert encoding_plan_digest(recoded, pairs, table.edges) != base
  sampled = list(cols)
  sampled[categorical] = dataclasses.replace(
      cols[categorical], census="value_sampled", value_sample_rate=0.5)
  assert encoding_plan_digest(sampled, pairs, table.edges) != base
  assert encoding_plan_digest(cols, pairs[1:], table.edges) != base
  assert encoding_plan_digest(cols, pairs, table.edges[:1]) != base


# --------------------------------------------------------------------------
# build_plan over the thelook closure
# --------------------------------------------------------------------------
def test_build_plan_thelook_component():
  plan, bq = _plan()
  names = [t.name for t in plan.tables]
  assert names == ["products", "users", "orders", "order_items"]
  roles = {t.name: t.role for t in plan.tables}
  assert roles == {
      "products": "external",
      "users": "root",
      "orders": "driven",
      "order_items": "driven",
  }
  by = {t.name: t for t in plan.tables}
  products = by["products"]
  assert products.landing_table == EXTERNAL
  assert products.source_table == f"{SRC}.products"
  assert [c.name for c in products.columns] == ["id"]
  assert all(c.is_key and c.census == "none" for c in products.columns)
  assert products.panel is None and products.pairs == ()
  assert products.scope.readable and not products.source_pinned
  assert not any(
      bq.reads(s) in (EXTERNAL, f"{SRC}.products")
      for s, _ in bq.planning_queries())

  users = by["users"]
  assert users.landing_table == f"{DS}.users"
  assert users.source_table == f"{SRC}.users" and users.run_id == f"{BASE}-00-users"
  assert users.pk == ("id",) and users.identity == ("email",)
  assert users.scope.ok and users.source_pinned
  assert users.source_read_table.startswith(f"{QDS}.sdfb_eval_")
  assert users.panel.verified and len(users.panel.r_rows) == REFERENCE_N
  assert len(users.panel.h_rows) == REFERENCE_N
  ucols = _by_name(users.columns)
  assert ucols["id"].is_key and ucols["id"].census == "none"
  assert ucols["email"].kind is ColumnKind.IDENTIFIER
  assert ucols["email"].census == "exact"
  assert ucols["country"].kind is ColumnKind.CATEGORICAL
  assert ucols["country"].literal_ok and len(ucols["country"].dictionary) == 5
  assert ucols["signup_date"].day_granularity
  assert not ucols["created_at"].day_granularity
  assert len(ucols["age"].quantiles_src) == 1001
  assert len(ucols["age"].quantiles_syn) == 1001
  assert ucols["age"].mean_src is not None and ucols["age"].std_src > 0
  assert users.pairs and all(
      not users.columns[i].is_key and not users.columns[j].is_key
      for i, j in users.pairs)

  items = by["order_items"]
  assert [e.label("order_items") for e in items.edges] == [
      "order_items(order_id) -> orders(order_id)",
      "order_items(product_id) -> synthetic_data.products(id)",
  ]
  icols = _by_name(items.columns)
  assert icols["order_id"].is_key and icols["product_id"].is_key
  assert by["orders"].edges[0].ref == "users"

  again, _ = _plan()
  assert again.evaluation_key == plan.evaluation_key
  assert again.salt == plan.salt and again.evaluation_id == plan.evaluation_id
  assert [t.encoding_plan_digest for t in again.tables
         ] == [t.encoding_plan_digest for t in plan.tables]
  assert len({t.encoding_plan_digest for t in plan.tables}) == 4
  assert re.fullmatch(r"[A-Za-z0-9_-]{1,128}", plan.evaluation_id)
  assert plan.predicted_shuffle_gb > 0
  assert plan.model_sha is not None and len(plan.model_sha) == 12


def test_evaluation_key_moves_with_mode_and_value_knobs_only():
  plan, _ = _plan()
  sampled, _ = _plan(mode="sampled")
  assert sampled.evaluation_key != plan.evaluation_key
  pairs10, _ = _plan(knobs=dataclasses.replace(KNOBS, pair_max_columns=10))
  assert pairs10.evaluation_key != plan.evaluation_key
  # Budgets that only refuse, and where temp tables go, change no metric.
  roomier, _ = _plan(knobs=dataclasses.replace(KNOBS, max_bytes_billed=1 << 41))
  assert roomier.evaluation_key == plan.evaluation_key
  assert plan.salt != plan.evaluation_key and len(plan.salt) == 32


def test_knobs_accept_a_mapping_and_reject_unknown_keys():
  plan, _ = _plan(knobs={"temp_dataset": QDS, "pair_max_columns": 3})
  assert plan.knobs.pair_max_columns == 3
  assert all(len({i for p in t.pairs for i in p}) <= 3 for t in plan.tables)
  with pytest.raises(ValueError, match="pair_max_colums"):
    Knobs.from_mapping({"pair_max_colums": 3})
  with pytest.raises(ValueError, match="scope"):
    Knobs(scope="sometimes")
  for mode in ("auto", *SCOPE_MODES):  # the knob follows scope.py's modes
    assert Knobs(scope=mode).scope == mode


def test_raw_row_flag_source_keys_are_refused():
  """Row flags carry a source key only as a keyed hash (R64/R68): the
  `raw` value is refused with its reason, never silently ignored."""
  assert Knobs().row_flags_source_keys == "hashed"
  with pytest.raises(ValueError, match="'raw' is not supported"):
    Knobs(row_flags_source_keys="raw")
  with pytest.raises(ValueError, match="expected one of"):
    Knobs(row_flags_source_keys="plain")


def test_plan_records_reject_unknown_vocabulary():
  with pytest.raises(ValueError, match="census"):
    _col("x", ColumnKind.NUMERIC, 3, census="sometimes")
  with pytest.raises(ValueError, match="value_sample_rate"):
    _col("x", ColumnKind.NUMERIC, 3, census="value_sampled")
  plan, _ = _plan()
  with pytest.raises(ValueError, match="role"):
    dataclasses.replace(plan.tables[1], role="parent")
  with pytest.raises(ValueError, match="no planning result row"):
    parse_planning([], [])


def test_bytes_over_budget_refused_before_any_billed_query():
  bq = thelook_bq(dry_bytes=10_000_000)
  knobs = dataclasses.replace(KNOBS, max_bytes_billed=50_000_000)
  with pytest.raises(BudgetExceededError) as info:
    _plan(bq, knobs=knobs)
  message = str(info.value)
  assert "50000000" in message
  total = 10_000_000 * len(bq.dry_runs)
  assert str(total) in message
  assert "planning" in message and "panel" in message
  assert not bq.planning_queries()
  assert not any("__sdfb_rk" in s for s, _ in bq.queries)


def test_temporal_type_families_must_match_to_compare():
  """A TIMESTAMP compared with a DATE (or TIME with anything else) is not
  the same quantity on the micros scale: skipped with a warning."""
  bq = thelook_bq()
  source = bq.tables[f"{SRC}.users"]["schema"]
  for field in source:
    if field["name"] == "created_at":
      field["type"] = "DATE"  # landing: TIMESTAMP
    if field["name"] == "signup_date":
      field["type"] = "DATETIME"  # landing: DATE — same civil family
  plan, _ = _plan(bq)
  users = next(t for t in plan.tables if t.name == "users")
  names = [c.name for c in users.columns]
  assert "created_at" not in names and "signup_date" in names
  assert any("created_at" in w and "DATE" in w and "TIMESTAMP" in w
             for w in plan.warnings)


def test_sampled_mode_refuses_on_the_worst_case_before_billing():
  """Phase A counts every side whose table holds more than sample_rows as
  sampled (a full read each), so the refusal comes before any billed
  query."""
  bq = thelook_bq()
  knobs = dataclasses.replace(
      KNOBS, sample_rows=100, max_bytes_billed=15 * bq.dry_bytes)
  with pytest.raises(BudgetExceededError, match="sample") as info:
    _plan(bq, knobs=knobs, mode="sampled")
  assert str(18 * bq.dry_bytes) in str(info.value)  # 12 + 6 worst-case reads
  assert not bq.planning_queries()
  assert not any("__sdfb_rk" in s for s, _ in bq.queries)
  plan, _ = _plan(
      knobs=dataclasses.replace(knobs, max_bytes_billed=1 << 40),
      mode="sampled")
  sampled = [t for t in plan.tables if t.evaluated]
  assert all(t.sample_rate_source < 1 for t in sampled)


def test_appends_scope_params_are_bound_everywhere():
  """ScopePlan.params are opaque: planning, dry runs and prepare pass the
  scope's own mapping, never parameter names the planner made up."""
  bq = thelook_bq()
  plan, _ = _plan(bq, thelook_launch(bq, write_disposition="append"))
  for table in plan.tables[1:]:
    scope = table.scope
    assert scope.params and scope.read_expr != scope.read_table
    reads = [(s, p) for s, p in bq.planning_queries() if scope.read_expr in s]
    assert len(reads) == 1 and reads[0][1] == scope.params
    dry = [(s, p) for s, p in bq.dry_runs if scope.read_expr in s]
    assert dry and all(p == scope.params for _, p in dry)
    stmts = [s for s in plan.prepare_sql if s.sql in scope.prepare_sql]
    assert stmts and all(s.params == scope.params for s in stmts)
  bq.executed.clear()
  plan.prepare(bq)
  assert [(s, p) for s, p in bq.executed
         ] == [(s.sql, dict(s.params)) for s in plan.prepare_sql]
  # Every prepare DDL is capped at the budget (a CTAS can scan a lot).
  assert bq.execute_caps and all(
      cap == KNOBS.max_bytes_billed for cap in bq.execute_caps)
  pins = [s for s in plan.prepare_sql if "CREATE SNAPSHOT TABLE" in s.sql]
  assert len(pins) == 3 and all(not s.params for s in pins)


def _copy_launch(bq):
  launch = thelook_launch(bq, write_disposition="append")
  copies = tuple(dataclasses.replace(w, job_type="COPY") for w in launch.writes)
  return dataclasses.replace(launch, writes=copies)


def test_copy_job_appends_plan_the_as_of_difference():
  bq = thelook_bq()
  plan, _ = _plan(bq, _copy_launch(bq))
  # The one table planning may create (R57): each as_of_diff scope's
  # zero-byte, expiring start snapshot — in phase A, before any dry run,
  # because the scope's read_expr reads it.
  executed = [sql for sql, _ in bq.executed]
  assert len(executed) == len(TABLES)
  assert all(
      sql.startswith("CREATE SNAPSHOT TABLE") and "_start_" in sql
      for sql in executed)
  first_dry = next(
      i for i, (event, _) in enumerate(bq.events) if event == "dry")
  assert max(i for i, (event, _) in enumerate(bq.events)
             if event == "execute") < first_dry
  assert [s.sql for s in plan.planning_ddl] == executed
  assert all(cap == KNOBS.max_bytes_billed for cap in bq.execute_caps)
  for table in plan.tables[1:]:
    scope = table.scope
    assert scope.mode == "as_of_diff" and scope.ok and scope.params == {}
    assert scope.planning_sql[0] in executed
    assert f"`{scope.start_table}` AS t" in scope.read_expr
    assert "LEFT JOIN" in scope.prepare_sql[0]
    # Created once, by the planner: never again at prepare time.
    assert scope.planning_sql[0] not in {s.sql for s in plan.prepare_sql}
    reads = [s for s, _ in bq.planning_queries() if scope.read_expr in s]
    assert len(reads) == 1
    assert scope.observed_rows == scope.written_rows
    assert any(scope.start_table in w for w in table.warnings)


def test_plan_fake_rejects_one_table_at_two_points_in_time():
  bq = thelook_bq()
  r51 = (f"SELECT * FROM (SELECT * FROM `{DS}.orders` FOR SYSTEM_TIME AS OF "
         "TIMESTAMP '2026-09-13T13:49:41.250000Z') AS e JOIN (SELECT * FROM "
         f"`{DS}.orders` FOR SYSTEM_TIME AS OF TIMESTAMP "
         "'2026-09-13T13:49:20.000000Z') AS s USING (order_id)")
  for call in (bq.query, bq.dry_run_bytes, bq.execute):
    with pytest.raises(BqApiError, match="more than one point in time"):
      call(r51)


def test_a_failing_scope_dry_run_skips_only_that_table():
  bq = thelook_bq()
  bq.dry_failures[f"`{DS}.orders`"] = BqApiError("400 (fake) Syntax error")
  plan, _ = _plan(bq)
  orders = next(t for t in plan.tables if t.name == "orders")
  assert not orders.evaluated
  assert orders.scope.status == "unknown" and not orders.scope.readable
  assert "Syntax error" in orders.scope.reason
  assert "Syntax error" in orders.skip_reason
  assert all(
      t.evaluated for t in plan.tables if t.name in ("users", "order_items"))
  assert not any(f"`{DS}.orders`" in s for s, _ in bq.planning_queries())
  assert plan.skip_reason is None


def test_a_failing_source_dry_run_names_the_source_not_scope_unknown():
  # R58: a source-side dry-run failure reads "source dry run failed: …"
  # and names the source table, not the generic "scope unknown" reason
  # that a synthetic-side (scope) dry-run failure carries.
  bq = thelook_bq()
  bq.dry_failures[f"`{SRC}.orders`"] = BqApiError(
      "400 (fake) Source unreachable")
  plan, _ = _plan(bq)
  orders = next(t for t in plan.tables if t.name == "orders")
  assert not orders.evaluated and orders.scope.status == "unknown"
  assert orders.scope.reason.startswith("source dry run failed:")
  assert f"{SRC}.orders" in orders.scope.reason
  assert "Source unreachable" in orders.scope.reason
  assert "scope unknown" not in orders.scope.reason
  assert orders.skip_reason == orders.scope.reason
  assert all(
      t.evaluated for t in plan.tables if t.name in ("users", "order_items"))


def test_a_dry_prepare_fallback_note_is_dropped_if_the_table_later_fails():
  # The source pin's own snapshot-clone prepare cannot be dry-run: its
  # full read stands in (a fallback note, R58: pending, not yet kept).
  # Then the synthetic side's own planning query fails outright: the
  # whole table is skipped, and that fallback note must not survive as a
  # stale leftover in its warnings.
  bq = thelook_bq()
  bq.dry_failures[f"CLONE `{SRC}.orders`"] = BqApiError(
      "400 (fake) cannot dry-run the clone")
  bq.dry_failures[f"`{DS}.orders`"] = BqApiError("400 (fake) Syntax error")
  plan, _ = _plan(bq)
  orders = next(t for t in plan.tables if t.name == "orders")
  assert not orders.evaluated
  assert not any("could not be dry-run" in w for w in orders.warnings)
  assert not any("full read is counted instead" in w for w in orders.warnings)


def test_a_dry_prepare_fallback_note_is_kept_when_the_table_succeeds():
  # The mirror of the test above: nothing else fails, so the fallback
  # note the source pin's clone dry run left behind is committed.
  bq = thelook_bq()
  bq.dry_failures[f"CLONE `{SRC}.orders`"] = BqApiError(
      "400 (fake) cannot dry-run the clone")
  plan, _ = _plan(bq)
  orders = next(t for t in plan.tables if t.name == "orders")
  assert orders.evaluated
  assert any("could not be dry-run" in w for w in orders.warnings)


def test_a_failing_start_snapshot_skips_only_that_table():
  # e.g. the table did not exist yet at the window start. users is a
  # parent of orders (an enforced edge): the parent is skipped, but the
  # child — its own scope resolved fine — is still evaluated (R58).
  bq = thelook_bq()
  bq.execute_failures[f"CLONE `{DS}.users`"] = BqApiError(
      "400 (fake) Invalid snapshot time")
  plan, _ = _plan(bq, _copy_launch(bq))
  users = next(t for t in plan.tables if t.name == "users")
  orders = next(t for t in plan.tables if t.name == "orders")
  assert orders.edges and orders.edges[0].enforced and orders.edges[0].ref == (
      "users")
  assert not users.evaluated and users.scope.status == "unknown"
  assert "Invalid snapshot time" in users.scope.reason
  assert not any(users.scope.start_table and users.scope.start_table in s.sql
                 for s in plan.planning_ddl)
  assert all(
      t.evaluated for t in plan.tables if t.name in ("orders", "order_items"))
  # R58: a skipped table carries no temp table, snapshot or pin — pinned
  # or not — left over from before the failure.
  assert users.source_read_table == "" and users.synthetic_read_table == ""
  assert users.scope.read_table == "" and users.scope.start_table != ""
  assert not users.source_pinned and users.source_pin is None


def test_a_conflicting_start_snapshot_raises_instead_of_skipping():
  # A 409 Already Exists means the same evaluation_id already created it
  # under a still-live attempt — never silently treated as unreadable.
  bq = thelook_bq()
  bq.execute_failures[f"CLONE `{DS}.users`"] = BqApiError(
      "409 POST https://bigquery.googleapis.com/bigquery/v2/projects/x: "
      "Already Exists: Table demo-project:thelook_synthetic."
      "sdfb_eval_ev1_start_users_a1b2c3d4",
      status=409)
  with pytest.raises(PlanError, match="already exists") as info:
    _plan(bq, _copy_launch(bq))
  assert "fresh evaluation_id" in str(info.value)
  assert info.value.planning_ddl == ()  # users is the first table planned


def test_a_409_without_already_exists_text_is_a_normal_failure():
  # R61: BqApiError.status is checked first, but BigQuery uses 409 for
  # more than "Already Exists" (e.g. a concurrent-job conflict) — the
  # text is still the discriminator, so this is handled like any other
  # execute failure, not raised as a fresh-evaluation_id PlanError.
  bq = thelook_bq()
  bq.execute_failures[f"CLONE `{DS}.users`"] = BqApiError(
      "409 POST https://bigquery.googleapis.com/bigquery/v2/projects/x: "
      "concurrentJobs: another job is already running for this table",
      status=409)
  plan, _ = _plan(bq, _copy_launch(bq))
  users = next(t for t in plan.tables if t.name == "users")
  assert not users.evaluated and users.scope.status == "unknown"
  assert "concurrentJobs" in users.scope.reason
  assert all(
      t.evaluated for t in plan.tables if t.name in ("orders", "order_items"))


def test_a_409_with_the_text_but_no_structured_status_is_a_normal_failure():
  # The mirror case: text alone ("already exists") is no longer enough —
  # status must say 409 too (R61). A BqApiError that never went through
  # context/bq.py's `_translated` (so it carries no status) does not
  # trip the fresh-evaluation_id raise.
  bq = thelook_bq()
  bq.execute_failures[f"CLONE `{DS}.users`"] = BqApiError(
      "409 POST https://bigquery.googleapis.com/bigquery/v2/projects/x: "
      "Already Exists: Table demo-project:thelook_synthetic."
      "sdfb_eval_ev1_start_users_a1b2c3d4")
  plan, _ = _plan(bq, _copy_launch(bq))
  users = next(t for t in plan.tables if t.name == "users")
  assert not users.evaluated and users.scope.status == "unknown"
  assert all(
      t.evaluated for t in plan.tables if t.name in ("orders", "order_items"))


def test_planning_ddl_survives_a_budget_refusal_after_phase_a():
  # R58: phase A (the as_of_diff start snapshots) already ran by the time
  # the dry-run budget check refuses the plan; the CLI needs them back to
  # report or clean up.
  bq = thelook_bq()
  knobs = dataclasses.replace(KNOBS, max_bytes_billed=1)
  with pytest.raises(BudgetExceededError) as info:
    _plan(bq, _copy_launch(bq), knobs=knobs)
  created = [
      sql for sql, _ in bq.executed if sql.startswith("CREATE SNAPSHOT TABLE")
  ]
  assert len(created) == len(TABLES) == 3
  assert [s.sql for s in info.value.planning_ddl] == created


def test_malformed_time_travel_hours_skips_only_that_table():
  # R61: a later table's own metadata being malformed (here `orders`'
  # timeTravelHours, not a BqApiError but a bare str where an int is
  # expected) must not escape build_plan's phase-A loop — `users`,
  # planned first, keeps its already-created start snapshot, and
  # `order_items` is still planned after `orders` is skipped.
  bq = thelook_bq()
  bq.tables[f"{DS}.orders"]["timeTravelHours"] = "not-a-number"
  plan, _ = _plan(bq, _copy_launch(bq))
  users = next(t for t in plan.tables if t.name == "users")
  orders = next(t for t in plan.tables if t.name == "orders")
  items = next(t for t in plan.tables if t.name == "order_items")
  assert users.evaluated and items.evaluated
  assert not orders.evaluated and orders.scope.status == "unknown"
  assert "metadata is malformed" in orders.scope.reason
  assert orders.skip_reason == orders.scope.reason
  assert orders.scope.start_table == "" and orders.scope.read_table == ""
  created = [
      sql for sql, _ in bq.executed if sql.startswith("CREATE SNAPSHOT TABLE")
  ]
  assert len(created) == 2  # users and order_items only — never orders
  assert not any("orders_" in sql for sql in created)
  assert [s.sql for s in plan.planning_ddl] == created


def test_the_source_table_missing_clears_the_scope_temp_names():
  # R61: generalises R58's _unreadable_scope principle to
  # _pin_and_columns's own early bail — a skipped table carries no
  # temp-table names, whichever phase-A step skipped it. An "append"
  # scope has real temp names (a clone in prepare_sql, read_table !=
  # read_expr) to prove they were actually cleared, not just absent.
  bq = thelook_bq()
  del bq.tables[f"{SRC}.orders"]
  launch = thelook_launch(bq, write_disposition="append")
  plan, _ = _plan(bq, launch)
  users = next(t for t in plan.tables if t.name == "users")
  orders = next(t for t in plan.tables if t.name == "orders")
  items = next(t for t in plan.tables if t.name == "order_items")
  assert users.evaluated and items.evaluated
  assert not orders.evaluated and orders.scope.status == "unknown"
  assert f"the source table {SRC}.orders could not be read" in (
      orders.scope.reason)
  assert orders.skip_reason == orders.scope.reason
  assert orders.source_read_table == "" and orders.synthetic_read_table == ""
  assert orders.scope.read_table == "" and orders.scope.read_expr == ""
  assert orders.scope.prepare_sql == () and orders.scope.params == {}
  assert not orders.source_pinned and orders.source_pin is None


def test_an_unpinned_source_is_read_through_its_own_read_expr():
  # The job's create time is past the source's time travel: the source is
  # read as it is now — planning, dry runs and the panel all use the
  # pin's read_expr (the table itself), not a special case.
  bq = thelook_bq()
  launch = thelook_launch(bq, started_at="2026-09-01T00:00:00Z")
  plan, _ = _plan(bq, launch)
  users = next(t for t in plan.tables if t.name == "users")
  assert not users.source_pinned
  assert users.source_pin.read_expr == f"`{SRC}.users`"
  panels = [s for s, _ in bq.queries if "__sdfb_rk" in s]
  assert any(f"FROM `{SRC}.users` AS ref" in s for s in panels)
  assert not any("FOR SYSTEM_TIME" in s for s in panels)
  assert users.panel is not None and users.panel.verified
  assert not any("CREATE SNAPSHOT" in s.sql for s in plan.prepare_sql)


def test_sampled_mode_samples_large_sides():
  knobs = dataclasses.replace(KNOBS, sample_rows=200)
  plan, bq = _plan(knobs=knobs, mode="sampled")
  users = next(t for t in plan.tables if t.name == "users")
  items = next(t for t in plan.tables if t.name == "order_items")
  # 120 users fit in 200 rows: read whole; 480 items do not.
  assert users.sample_rate_source == users.sample_rate_synthetic == 1.0
  assert users.synthetic_read_table == users.scope.read_table
  assert items.sample_rate_source == items.sample_rate_synthetic
  assert math.isclose(items.sample_rate_source, 200 / 480, rel_tol=1e-3)
  assert items.synthetic_read_table != items.scope.read_table
  assert items.source_read_table != items.source_pin.read_table
  # A sampled side's census demand is sized on the rows actually read.
  assert plan.predicted_shuffle_gb < _plan()[0].predicted_shuffle_gb
  samples = [
      s for s in plan.prepare_sql if "FARM_FINGERPRINT(CONCAT(@salt" in s.sql
  ]
  assert samples and all(s.params == {"salt": plan.salt} for s in samples)
  # A sample CTAS reads what an earlier prepare statement materialized.
  first_sample = plan.prepare_sql.index(samples[0])
  assert all(
      "CREATE SNAPSHOT" in s.sql for s in plan.prepare_sql[:first_sample])
  seed = plan.registry_seed()
  entry = next(t for t in seed["tables"] if t["name"] == "order_items")
  assert entry["sampled"] is True
  assert entry["sample_rate_synthetic"] == items.sample_rate_synthetic
  assert any("FARM_FINGERPRINT" not in s and "SELECT * FROM" in s
             for s, _ in bq.dry_runs)  # each sample's read was dry-run


# --------------------------------------------------------------------------
# R50 — no silent skip of a table the model does not know
# --------------------------------------------------------------------------
def test_table_without_a_model_entry_raises_naming_table_and_uri():
  bq = thelook_bq()
  tables = (*TABLES, f"{DS}.inventory_items")
  launch = thelook_launch(bq, tables=tables)
  with pytest.raises(PlanError) as info:
    _plan(bq, launch)
  assert f"{DS}.inventory_items" in str(info.value)
  assert MODEL_URI in str(info.value)
  assert not bq.queries and not bq.dry_runs


def test_models_missing_for_a_relational_launch_raise():
  bq = thelook_bq()
  with pytest.raises(PlanError, match="users"):
    _plan(bq, models=())


def test_single_undeclared_table_with_a_loaded_model_is_standalone():
  """R55: the generator always loads its models and generates a table they
  do not declare in isolation, so the evaluator accepts it too."""
  bq = thelook_bq()
  launch = thelook_launch(
      bq, tables=(EXTERNAL,), run_ids=(BASE,), params={"pk_cols": "id"})
  plan, _ = _plan(bq, launch)
  (products,) = plan.tables
  assert products.role == "standalone" and products.edges == ()
  assert products.pk == ("id",) and products.evaluated
  assert any(EXTERNAL in w and "not declared" in w and "thelook" in w
             for w in plan.warnings)
  assert plan.registry_seed()["status"] == "RUNNING"


def test_two_tables_one_undeclared_with_a_loaded_model_raise():
  bq = thelook_bq()
  launch = thelook_launch(bq, tables=(f"{DS}.users", EXTERNAL))
  with pytest.raises(PlanError) as info:
    _plan(bq, launch)
  assert EXTERNAL in str(info.value) and MODEL_URI in str(info.value)
  assert f"{DS}.users," not in str(info.value)  # only the undeclared one
  assert not bq.queries and not bq.dry_runs


def test_single_table_without_a_model_is_standalone():
  bq = thelook_bq()
  launch = thelook_launch(
      bq,
      tables=(f"{DS}.orders",),
      run_ids=(BASE,),
      relationships_uri=None,
      params={
          "pk_cols": "order_id",
          "identity_cols": ""
      })
  plan, _ = _plan(bq, launch, models=())
  (orders,) = plan.tables
  assert orders.role == "standalone" and orders.edges == ()
  assert orders.pk == ("order_id",)
  assert _by_name(orders.columns)["order_id"].is_key
  assert not _by_name(orders.columns)["user_id"].is_key
  seed = plan.registry_seed()
  assert seed["status"] == "RUNNING"
  assert seed["tables"][0]["role"] == "standalone"


def test_partial_launch_reads_unwritten_parents_and_warns_on_children():
  """A launch that wrote orders only: users (its FK parent) is read-only,
  orders drew keys from an already-landed parent (side_input), and the
  connected child order_items, never written, is named in a warning."""
  bq = thelook_bq()
  launch = thelook_launch(bq, tables=(f"{DS}.orders",), run_ids=(BASE,))
  plan, _ = _plan(bq, launch)
  roles = {t.name: t.role for t in plan.tables}
  assert roles == {"users": "external", "orders": "side_input"}
  users = plan.tables[0]
  assert users.landing_table == f"{DS}.users"
  assert [c.name for c in users.columns] == ["id"]
  assert users.source_table == f"{SRC}.users" and users.panel is None
  assert any("order_items" in w and "not generated" in w for w in plan.warnings)


def test_relationships_off_launch_evaluates_each_table_standalone():
  bq = thelook_bq()
  launch = thelook_launch(
      bq, relationships_uri=None, params={"generate_fk_relationships": "false"})
  plan, _ = _plan(bq, launch, models=())
  assert [t.role for t in plan.tables] == ["standalone"] * 3
  assert all(t.edges == () for t in plan.tables)
  assert any("relationships are off" in w for w in plan.warnings)


# --------------------------------------------------------------------------
# the registry seed
# --------------------------------------------------------------------------
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)")


def _check_value(field, value, where):
  name = where + field["name"]
  if field.get("mode") == "REPEATED":
    assert isinstance(value, list), name
    for item in value:
      _check_scalar(field, item, name)
    return
  if value is None:
    assert field.get("mode") != "REQUIRED", f"{name} is REQUIRED"
    return
  _check_scalar(field, value, name)


def _check_scalar(field, value, name):
  kind = field["type"]
  if kind == "RECORD":
    assert isinstance(value, dict), name
    assert list(value) == [f["name"] for f in field["fields"]], name
    for sub in field["fields"]:
      _check_value(sub, value[sub["name"]], f"{name}.")
  elif kind == "STRING":
    assert isinstance(value, str), name
  elif kind == "INT64":
    assert isinstance(value, int) and not isinstance(value, bool), name
  elif kind == "FLOAT64":
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    assert math.isfinite(value), name
  elif kind == "BOOL":
    assert isinstance(value, bool), name
  elif kind == "TIMESTAMP":
    assert isinstance(value, str) and _TIMESTAMP_RE.fullmatch(value), name
  elif kind == "JSON":
    json.dumps(value)
  else:
    raise AssertionError(f"{name}: unchecked type {kind}")


def _check_row(row):
  fields = schemas.load_schema("evaluation_data_history")
  assert list(row) == [f["name"] for f in fields]
  for field in fields:
    _check_value(field, row[field["name"]], "")


def test_registry_seed_complete():
  plan, _ = _plan()
  seed = plan.registry_seed()
  _check_row(seed)
  assert seed["status"] == "RUNNING" and seed["event"] == "RUNNING"
  assert seed["finished_at"] is None and seed["status_reason"] is None
  assert seed["evaluation_id"] == plan.evaluation_id
  assert seed["evaluation_key"] == plan.evaluation_key
  assert seed["evaluated_at"] == seed["recorded_at"] == plan.evaluated_at
  assert (seed["trigger"], seed["runner"],
          seed["mode"]) == ("cli", "DirectRunner", "exact")
  assert seed["catalogue_version"] == plan.catalogue_version
  assert seed["evaluator_version"] == plan.evaluator_version
  assert seed["generation_job_id"] == plan.launch.generation_job_id
  assert seed["run_ids"] == list(plan.launch.run_ids)
  assert seed["base_run_id"] == BASE
  assert seed["params_source"] == "jobs_labels+logs"
  assert seed["relationship_model"] == "thelook"
  assert seed["relationship_model_sha"] == plan.model_sha
  assert seed["relationship_model_uri"] == MODEL_URI
  assert seed["model_adjusted"] is False
  for key, value in plan.launch.typed_filters().items():
    assert seed[key] == value
  assert seed["generation_params"]["run_id"] == BASE
  assert seed["evaluation_params"]["pair_max_columns"] == 20
  assert seed["bq_bytes_processed"] == plan.bq_bytes_estimate
  assert seed["predicted_shuffle_gb"] == plan.predicted_shuffle_gb
  assert seed["metrics_total"] is None and seed["overall_score"] is None
  entries = {t["name"]: t for t in seed["tables"]}
  assert list(entries) == ["products", "users", "orders", "order_items"]
  users = entries["users"]
  assert users["role"] == "root" and users["scope_ok"] is True
  assert users["scope_mode"] == "table" and users["scope_status"] == "ok"
  assert users["reference_verified"] is True
  assert users["reference_n"] == REFERENCE_N and users[
      "holdout_n"] == REFERENCE_N
  assert users["exposure_n"] == REFERENCE_N
  assert users["rows_source"] == 120 and users["rows_synthetic"] == 120
  assert users["rows_expected"] == 120
  assert users["source_snapshot_ts"].startswith("2026-09-13T13:10:16")
  assert users["source_drifted"] is False
  assert users["sampled"] is False and users["sample_rate_source"] == 1.0
  assert users["encoding_plan_digest"] == plan.tables[1].encoding_plan_digest
  assert users["table_score"] is None
  assert entries["products"]["role"] == "external"
  assert entries["products"]["reference_verified"] is None


def test_all_scopes_unreadable_seed_is_skipped():
  bq = thelook_bq()
  plan, _ = _plan(bq, thelook_launch(bq, write_disposition=None))
  assert not bq.planning_queries()
  assert not any("__sdfb_rk" in s for s, _ in bq.queries)
  seed = plan.registry_seed()
  _check_row(seed)
  assert seed["status"] == "SKIPPED" and seed["event"] == "FINAL"
  assert seed["finished_at"] == plan.evaluated_at
  for name in ("users", "orders", "order_items"):
    assert name in seed["status_reason"]
  assert seed["warnings"]
  assert seed["metrics_total"] == 0 and seed["metrics_fail"] == 0
  launch_entries = [t for t in seed["tables"] if t["role"] != "external"]
  assert all(t["scope_ok"] is False for t in launch_entries)
  assert all(t["scope_status"] == "unknown" for t in launch_entries)
  assert plan.prepare_sql == ()


def test_predict_shuffle_counts_census_relational_and_null_patterns():
  plan, _ = _plan()
  total = predict_shuffle_gb(plan.tables)
  assert total == plan.predicted_shuffle_gb
  items = next(t for t in plan.tables if t.name == "order_items")
  alone = predict_shuffle_gb([items])
  no_edges = predict_shuffle_gb([dataclasses.replace(items, edges=())])
  # Each edge shuffles the child rows of both sides at 16 B a key.
  assert math.isclose(
      alone - no_edges, 2 * (480 + 480) * 16 / 1e9, rel_tol=1e-9)
  unsampled = [dataclasses.replace(c, census="none") for c in items.columns]
  bare = predict_shuffle_gb(
      [dataclasses.replace(items, columns=tuple(unsampled))])
  assert bare < alone


# --------------------------------------------------------------------------
# planning says where its time goes (R138)
# --------------------------------------------------------------------------
_PLAN_LINE = re.compile(r"planning (?P<id>\S+) (?P<table>[^:]+): "
                        r"(?P<step>[a-z ]+) (?P<seconds>\d+\.\d+)s")
_SUMMARY = re.compile(
    r"planning (?P<id>\S+): done in (?P<seconds>\d+\.\d+)s: "
    r"(?P<tables>\d+) tables, (?P<total>\d+) BigQuery statements "
    r"\((?P<meta>\d+) metadata reads, (?P<queries>\d+) queries, "
    r"(?P<dry>\d+) dry runs, (?P<ddl>\d+) DDL\)")


def test_planning_logs_every_step_of_every_table_and_a_summary(caplog):
  bq = thelook_bq()
  launch = thelook_launch(bq, tables=TABLES[:2], run_ids=RUN_IDS[:2])
  with caplog.at_level("INFO", logger="sdfb_evaluation.context.plan"):
    plan, _ = _plan(bq, launch)
  lines = [r.getMessage() for r in caplog.records]
  steps = [m for m in map(_PLAN_LINE.fullmatch, lines) if m]
  found = [(m["table"], m["step"]) for m in steps]
  scan = ["planning statistics", "scope verification", "reference panel"]
  assert found[:7] == [
      ("users", "locate"),
      ("users", "source pin"),
      ("users", "planning tables"),
      ("orders", "locate"),
      ("orders", "source pin"),
      ("orders", "planning tables"),
      ("(all tables)", "dry runs"),
  ]
  # The tables are scanned at once (R153): their lines interleave, each
  # table's own stay in order, and one line times the whole phase.
  scanned = found[7:7 + 2 * len(scan)]
  for name in ("users", "orders"):
    assert [step for table, step in scanned if table == name] == scan
  assert found[7 + 2 * len(scan):] == [
      ("(all tables)", "scans"),
      ("users", "sampling"),
      ("orders", "sampling"),
      ("(all tables)", "census"),
  ]
  assert all(m["id"] == plan.evaluation_id for m in steps)
  assert all(float(m["seconds"]) >= 0 for m in steps)
  (summary,) = [m for m in map(_SUMMARY.fullmatch, lines) if m]
  assert summary["id"] == plan.evaluation_id and summary["tables"] == "2"
  # the counts agree with what the fake saw
  assert int(summary["queries"]) == len(bq.queries)
  assert int(summary["dry"]) == len(bq.dry_runs)
  assert int(summary["ddl"]) == len(bq.executed)
  assert int(summary["meta"]) > 0
  assert int(summary["total"]) == sum(
      int(summary[k]) for k in ("meta", "queries", "dry", "ddl"))
  # the summary is the last line; no SQL text and no row value is logged
  assert _SUMMARY.fullmatch(lines[-1])
  assert not [m for m in lines if "SELECT" in m or "example.com" in m]
