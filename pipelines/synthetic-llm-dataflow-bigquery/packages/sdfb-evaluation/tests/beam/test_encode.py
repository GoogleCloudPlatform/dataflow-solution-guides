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
"""Tests for `sdfb_evaluation.beam.encode` (Task 20): the numpy batch
encoding the Beam accumulators consume, driven by the plan's columns.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import logging
import math
import random
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

import apache_beam as beam
import numpy as np
import pytest
from apache_beam.testing.test_pipeline import TestPipeline as BeamTestPipeline
from apache_beam.testing.util import assert_that, equal_to

from sdfb_evaluation.beam import encode as encode_module
from sdfb_evaluation.beam.encode import (
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    NULL_BITS_MAX_COLUMNS,
    BatchEncoder,
    BatchLayout,
    EncodeBatchFn,
    EncodedBatch,
    EncodeSide,
    key_hash,
    key_hashes,
    matched_rate,
    subsample_flags,
)
from sdfb_evaluation.beam.io import InMemorySources
from sdfb_evaluation.canonical import (
    NULL_CODE,
    hash64,
    linear_hash,
    multipliers,
)
from sdfb_evaluation.context.plan import ColumnPlan
from sdfb_evaluation.context.relationships import Edge
from sdfb_evaluation.types import ColumnKind, Side

from .tables import all_types_plan, client_rows, table_plan

SALT = "5a17" * 8
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_DAY_US = 86_400_000_000


def _micros(moment: datetime) -> float:
  aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
  return float((aware - _EPOCH) // timedelta(microseconds=1))


def _encode(rows: Sequence[Mapping[str, Any]],
            table: Any = None,
            side: Side = Side.SYNTHETIC,
            rate: float | None = None) -> EncodedBatch:
  table = table if table is not None else all_types_plan()
  encoder = BatchEncoder.from_table(table, side, salt=SALT, subsample_rate=rate)
  return encoder.encode(rows)


def _col(batch: EncodedBatch, group: str, name: str) -> np.ndarray:
  names = getattr(batch.layout, f"{group}_columns")
  return getattr(batch, group)[:, names.index(name)]


def _numeric_plan(name: str, n_cols: int, **kwargs: Any) -> Any:
  columns = [
      ColumnPlan(
          name=f"c{j:02d}",
          bq_type="FLOAT64",
          mode="NULLABLE",
          kind=ColumnKind.NUMERIC,
          is_key=False,
          day_granularity=False) for j in range(n_cols)
  ]
  return table_plan(name, columns, **kwargs)


# ---------------------------------------------------------------------------
# layout: the plan decides every column's place
# ---------------------------------------------------------------------------


def test_layout_follows_the_plan_kinds():
  layout = BatchLayout.from_table(all_types_plan())
  assert layout.table == "order_items"
  assert layout.columns == ("id", "order_id", "user_email", "status",
                            "sale_price", "discount", "quantity", "is_gift",
                            "created_at", "shipped_at", "delivery_date",
                            "pickup_time", "barcode", "review", "tags",
                            "shipping", "attributes", "store_location",
                            "weight_kg", "fulfilment")
  assert layout.num_columns == ("sale_price", "discount", "quantity", "is_gift",
                                "created_at", "shipped_at", "delivery_date",
                                "pickup_time", "weight_kg")
  # booleans also carry their hash64 code, like categoricals (dictionaries)
  assert layout.cat_columns == ("status", "is_gift", "barcode", "tags",
                                "shipping", "attributes", "store_location",
                                "fulfilment")
  assert layout.text_columns == ("id", "order_id", "user_email", "status",
                                 "barcode", "review")
  # PK, identity and FK columns are left out of the non-key hash
  assert layout.nonkey_columns == layout.columns[3:]
  assert layout.pk == ("id",)
  assert layout.identity == ("user_email",)
  assert layout.fk == (("order_id",),)
  assert layout.edge_labels == ("order_items(order_id) -> orders(order_id)",)
  assert layout.null_bits_columns == len(layout.columns)
  assert layout.null_bits_complete


def test_placement_follows_the_plan_never_the_values():
  # `status` holds four short labels, yet a plan that routed it to `text`
  # is obeyed: it is encoded as text, with no categorical code.
  plan = all_types_plan()
  routed = tuple(
      dataclasses.replace(c, kind=ColumnKind.TEXT) if c.name == "status" else c
      for c in plan.columns)
  batch = _encode(
      client_rows(), table=dataclasses.replace(plan, columns=routed))
  assert "status" in batch.layout.text_columns
  assert "status" not in batch.layout.cat_columns
  assert batch.cat.shape == (4, 7)
  # an INT64 key column is an identifier: text, never a number
  assert "id" not in batch.layout.num_columns


# ---------------------------------------------------------------------------
# the all-types fixture, cell by cell
# ---------------------------------------------------------------------------


def test_all_types_numeric_block_is_float64_micros_and_bool01():
  batch = _encode(client_rows())
  assert batch.num.dtype == np.float64
  assert batch.num.shape == (4, 9)
  nan = math.nan
  expected = {
      "sale_price": [19.9, 5.0, nan, 120.5],
      "discount": [0.15, nan, nan, nan],  # NULL, NaN and +inf are all NaN
      "quantity": [2.0, 1.0, nan, 3.0],
      "is_gift": [1.0, 0.0, nan, 1.0],
      "created_at": [
          _micros(datetime(2026, 1, 2, 3, 4, 5, 123_456, tzinfo=UTC)),
          _micros(datetime(2026, 1, 2, 3, 4, 6, tzinfo=UTC)), nan, 0.0
      ],
      "shipped_at": [
          _micros(datetime(2026, 1, 3, 10)), nan,
          _micros(datetime(2025, 12, 31, 23, 59, 59, 999_999)), 0.0
      ],
      "delivery_date": [
          _micros(datetime(2026, 1, 5)), nan, -float(_DAY_US),
          float(_DAY_US)
      ],
      "pickup_time": [(9 * 3600 + 30 * 60) * 1e6 + 250_000, nan, 0.0,
                      _DAY_US - 1.0],
      # BIGNUMERIC: float of the exact Decimal, 30 fractional digits
      "weight_kg": [
          float(Decimal("0.450000000000000000000000000100")), nan, 12.0, -7.25
      ],
  }
  for name, values in expected.items():
    np.testing.assert_array_equal(
        _col(batch, "num", name), np.array(values), err_msg=name)


def test_all_types_cat_block_is_hash64_with_null_code():
  rows = client_rows()
  batch = _encode(rows)
  assert batch.cat.dtype == np.uint64
  assert batch.cat.shape == (4, 8)
  for name in batch.layout.cat_columns:
    expected = []
    for row in rows:
      value = row[name]
      empty = value is None or (name == "tags" and value == [])
      expected.append(NULL_CODE if empty else hash64(name, value))
    np.testing.assert_array_equal(
        _col(batch, "cat", name),
        np.array(expected, dtype=np.uint64),
        err_msg=name)
  # nested values hash their canonical JSON: key order does not matter
  shuffled = [dict(row) for row in rows]
  shuffled[0]["shipping"] = {"zip": "46001", "city": "Valencia"}
  np.testing.assert_array_equal(_encode(shuffled).cat, batch.cat)


def test_all_types_text_block_keeps_strings_column_major():
  batch = _encode(client_rows())
  text = dict(zip(batch.layout.text_columns, batch.text, strict=True))
  assert text["status"] == ["Complete", "Shipped", None, "Returned"]
  assert text["review"][2] == ""
  assert text["review"][1] is None
  # BYTES read as their canonical base64 text; an INT64 id as its digits
  assert text["barcode"] == ["QUJDLTAwMQ==", None, "", "WFlaLTk5OQ=="]
  assert text["id"] == ["1", "2", "3", "4"]
  assert text["user_email"][3] == "li.wei@example.com"


def test_row_and_nonkey_hashes_are_salted_linear_hashes_of_hash64():
  rows = client_rows()
  batch = _encode(rows)
  layout = batch.layout

  def cells(columns: Sequence[str]) -> np.ndarray:
    return np.array([[
        NULL_CODE if row[c] is None or
        (c == "tags" and not row[c]) else hash64(c, row[c]) for c in columns
    ] for row in rows],
                    dtype=np.uint64)

  full = cells(layout.columns)
  np.testing.assert_array_equal(
      batch.row_hash, linear_hash(full, multipliers(layout.columns, SALT)))
  np.testing.assert_array_equal(batch.h_nonkey, cells(layout.nonkey_columns))
  np.testing.assert_array_equal(
      batch.nonkey_hash,
      linear_hash(batch.h_nonkey, multipliers(layout.nonkey_columns, SALT)))
  assert batch.row_hash.dtype == np.uint64
  assert batch.h_nonkey.shape == (4, len(layout.nonkey_columns))


def test_identical_rows_hash_identically_whatever_the_side_or_batch():
  rows = client_rows()
  whole = _encode(rows, side=Side.SYNTHETIC)
  alone = _encode([rows[2]], side=Side.SOURCE)
  assert alone.row_hash[0] == whole.row_hash[2]
  assert alone.nonkey_hash[0] == whole.nonkey_hash[2]
  twin = _encode([rows[0], dict(rows[0])])
  assert twin.row_hash[0] == twin.row_hash[1]
  # equal values from another read path (Decimal scale, tz object)
  other = dict(rows[0])
  other["sale_price"] = Decimal("19.900000000")
  other["created_at"] = rows[0]["created_at"].astimezone(
      timezone(timedelta(hours=5)))
  assert _encode([other]).row_hash[0] == whole.row_hash[0]


def test_nonkey_hash_ignores_pk_identity_and_fk_columns():
  rows = client_rows()
  copy = dict(rows[0], id=99, order_id=555, user_email="new.user@example.com")
  batch = _encode([rows[0], copy])
  assert batch.nonkey_hash[0] == batch.nonkey_hash[1]
  assert batch.row_hash[0] != batch.row_hash[1]
  changed = dict(rows[0], review="A different review.")
  other = _encode([rows[0], changed])
  assert other.nonkey_hash[0] != other.nonkey_hash[1]


def test_pk_identity_and_fk_hashes_are_key_tuple_hashes():
  rows = client_rows()
  batch = _encode(rows)
  assert batch.pk_hash.tolist() == [key_hash([r["id"]]) for r in rows]
  assert batch.identity_hash.tolist() == [
      key_hash([r["user_email"]]) for r in rows
  ]
  assert batch.identity_hash[2] == NULL_CODE  # NULL identity
  assert batch.fk_hash.shape == (4, 1)
  assert batch.fk_hash.dtype == np.uint64
  assert batch.fk_hash[:,
                       0].tolist() == [key_hash([r["order_id"]]) for r in rows]
  assert batch.fk_hash[2, 0] == NULL_CODE  # NULL FK: counted, never joined
  np.testing.assert_array_equal(batch.pk_hash, key_hashes(rows, ("id",)))


def _key_column(name: str, bq_type: str) -> ColumnPlan:
  return ColumnPlan(
      name=name,
      bq_type=bq_type,
      mode="NULLABLE",
      kind=ColumnKind.IDENTIFIER,
      is_key=True,
      day_granularity=False)


def test_fk_hash_equals_the_parent_pk_hash_for_matching_tuples():
  # shipments(order_id, line_no) <- returns(ret_order, ret_line): different
  # names, composite, a NUMERIC part read at two scales.
  parent = table_plan(
      "shipments",
      [_key_column("order_id", "NUMERIC"),
       _key_column("line_no", "STRING")],
      pk=("order_id", "line_no"))
  edge = Edge(
      cols=("ret_order", "ret_line"),
      ref="shipments",
      ref_cols=("order_id", "line_no"))
  child = table_plan(
      "returns",
      [_key_column("ret_order", "NUMERIC"),
       _key_column("ret_line", "STRING")],
      edges=(edge,))
  parents = [{
      "order_id": Decimal("7.50"),
      "line_no": "a"
  }, {
      "order_id": Decimal("8"),
      "line_no": "b"
  }]
  children = [
      {
          "ret_order": Decimal("7.5"),
          "ret_line": "a"
      },  # matches parent 0
      {
          "ret_order": Decimal("8.000"),
          "ret_line": "b"
      },  # matches parent 1
      {
          "ret_order": Decimal("8"),
          "ret_line": "a"
      },  # an orphan tuple
      {
          "ret_order": None,
          "ret_line": "a"
      },  # MATCH SIMPLE: a NULL part
  ]
  pk = _encode(parents, table=parent).pk_hash
  fk = _encode(children, table=child).fk_hash[:, 0]
  assert fk[0] == pk[0]
  assert fk[1] == pk[1]
  assert fk[2] not in set(pk.tolist())
  assert fk[3] == NULL_CODE
  # order matters: (line, order) is not (order, line)
  assert key_hash(["a", Decimal("7.5")]) != pk[0]
  np.testing.assert_array_equal(
      key_hashes(parents, ("order_id", "line_no")), pk)


def test_an_all_key_table_has_no_nonkey_hash():
  # A pure link table: every column is a key, so no content can "match".
  edge = Edge(cols=("user_id",), ref="users", ref_cols=("id",))
  plan = table_plan(
      "user_links",
      [_key_column("link_id", "INT64"),
       _key_column("user_id", "INT64")],
      pk=("link_id",),
      edges=(edge,))
  batch = _encode([{
      "link_id": 1,
      "user_id": 7
  }, {
      "link_id": 2,
      "user_id": 8
  }],
                  table=plan)
  assert batch.layout.nonkey_columns == ()
  assert batch.h_nonkey.shape == (2, 0)
  assert batch.nonkey_hash.tolist() == [NULL_CODE, NULL_CODE]
  assert batch.row_hash[0] != batch.row_hash[1]


def test_a_key_column_missing_from_the_plan_columns_raises():
  plan = table_plan(
      "orders", [_key_column("order_id", "INT64")], pk=("order_id", "line"))
  with pytest.raises(ValueError, match="line"):
    BatchLayout.from_table(plan)


def test_tables_without_keys_fill_null_code_and_no_fk_columns():
  plan = _numeric_plan("plain", 3)
  batch = _encode([{"c00": 1.0, "c01": 2.0, "c02": None}], table=plan)
  assert batch.pk_hash.tolist() == [NULL_CODE]
  assert batch.identity_hash.tolist() == [NULL_CODE]
  assert batch.fk_hash.shape == (1, 0)
  assert key_hash([]) == NULL_CODE
  assert key_hashes([{}, {}], ()).tolist() == [NULL_CODE, NULL_CODE]


def test_null_bits_mark_sql_nulls_and_empty_arrays_in_plan_order():
  batch = _encode(client_rows())
  columns = batch.layout.columns

  def bits(*names: str) -> int:
    return sum(1 << columns.index(name) for name in names)

  assert batch.null_bits.dtype == np.uint64
  assert int(batch.null_bits[0]) == 0
  assert int(batch.null_bits[1]) == bits("discount", "shipped_at",
                                         "delivery_date", "pickup_time",
                                         "barcode", "review", "tags",
                                         "shipping", "attributes",
                                         "store_location", "weight_kg",
                                         "fulfilment")
  # row 3: NaN is a value, not a NULL; "" is empty, not NULL; a RECORD of
  # NULL sub-fields is a value
  assert int(batch.null_bits[2]) == bits("order_id", "user_email", "status",
                                         "sale_price", "quantity", "is_gift",
                                         "created_at")
  assert int(batch.null_bits[3]) == 0


def test_null_bits_cover_the_first_64_columns_then_warn(caplog):
  plan = _numeric_plan("wide", 70)
  layout = BatchLayout.from_table(plan)
  assert layout.null_bits_columns == NULL_BITS_MAX_COLUMNS == 64
  assert not layout.null_bits_complete
  row: dict[str, Any] = {f"c{j:02d}": 1.0 for j in range(70)}
  row["c63"] = None
  row["c64"] = None  # beyond the mask: not representable
  with caplog.at_level(logging.WARNING):
    batch = _encode([row], table=plan)
  assert int(batch.null_bits[0]) == 1 << 63
  assert any("64" in r.getMessage() and "wide" in r.getMessage()
             for r in caplog.records)


# ---------------------------------------------------------------------------
# matched-n subsample
# ---------------------------------------------------------------------------


def _orders(n: int, seed: int = 7) -> list[dict[str, Any]]:
  rng = random.Random(seed)
  return [{
      "order_id":
          i,
      "user_id":
          rng.randint(1, 500),
      "status":
          rng.choice(("Complete", "Shipped", "Returned")),
      "num_of_item":
          rng.randint(1, 4),
      "created_at":
          datetime(2026, 1, 1, tzinfo=UTC) +
          timedelta(minutes=rng.randint(0, 90_000)),
  } for i in range(1, n + 1)]


def _orders_plan(**kwargs: Any) -> Any:
  columns = [
      _key_column("order_id", "INT64"),
      _key_column("user_id", "INT64"),
      ColumnPlan("status", "STRING", "NULLABLE", ColumnKind.CATEGORICAL, False,
                 False),
      ColumnPlan("num_of_item", "INT64", "NULLABLE", ColumnKind.NUMERIC, False,
                 False),
      ColumnPlan("created_at", "TIMESTAMP", "NULLABLE", ColumnKind.TEMPORAL,
                 False, False),
  ]
  edge = Edge(cols=("user_id",), ref="users", ref_cols=("id",))
  return table_plan(
      "orders", columns, pk=("order_id",), edges=(edge,), **kwargs)


def test_subsample_rate_is_m_over_n_and_deterministic():
  rows = _orders(20_000)
  plan = _orders_plan()
  batch = _encode(rows, table=plan, rate=0.25)
  share = float(batch.subsample_m.mean())
  sigma = math.sqrt(0.25 * 0.75 / len(rows))
  assert abs(share - 0.25) < 4 * sigma
  assert batch.subsample_m.dtype == np.bool_
  # the rule itself: hash64(salt, row_hash) < p * 2**64
  for i in range(50):
    expected = hash64(SALT, int(batch.row_hash[i])) < 0.25 * 2**64
    assert bool(batch.subsample_m[i]) == expected
  # the same decision in any batch, on any run
  halves = np.concatenate([
      _encode(rows[:7_000], table=plan, rate=0.25).subsample_m,
      _encode(rows[7_000:], table=plan, rate=0.25).subsample_m
  ])
  np.testing.assert_array_equal(halves, batch.subsample_m)


def test_subsample_extremes_and_validation():
  row_hash = np.array([0, 1, 2**63, 2**64 - 1], dtype=np.uint64)
  assert subsample_flags(row_hash, SALT, 1.0).all()
  assert not subsample_flags(row_hash, SALT, 0.0).any()
  with pytest.raises(ValueError, match="subsample_rate"):
    BatchEncoder.from_table(
        _orders_plan(), Side.SOURCE, salt=SALT, subsample_rate=1.5)


def test_matched_rate_thins_the_larger_side_to_the_smaller():
  plan = _orders_plan(rows_source=1_000, rows_synthetic=4_000)
  assert matched_rate(plan, Side.SOURCE) == 1.0
  assert matched_rate(plan, Side.SYNTHETIC) == 0.25
  assert matched_rate(plan, "reference") == 1.0
  assert matched_rate(plan, Side.HOLDOUT) == 1.0
  unplanned = _orders_plan()  # no row counts: nothing to match
  assert matched_rate(unplanned, Side.SYNTHETIC) == 1.0
  encoder = BatchEncoder.from_table(plan, Side.SYNTHETIC, salt=SALT)
  assert encoder.subsample_rate == 0.25


def test_matched_rate_none_counts_keep_everything_zero_counts_are_empty():
  half = _orders_plan(rows_source=1_000)  # synthetic count unknown (None)
  assert half.rows_synthetic is None
  assert matched_rate(half, Side.SOURCE) == 1.0
  assert matched_rate(half, Side.SYNTHETIC) == 1.0
  empty_syn = _orders_plan(rows_source=1_000, rows_synthetic=0)
  assert matched_rate(empty_syn, Side.SYNTHETIC) == 1.0  # nothing to thin
  assert matched_rate(empty_syn, Side.SOURCE) == 0.0  # no matched n exists
  both_empty = _orders_plan(rows_source=0, rows_synthetic=0)
  assert matched_rate(both_empty, Side.SOURCE) == 1.0


# ---------------------------------------------------------------------------
# no silent drops
# ---------------------------------------------------------------------------


def _one_numeric(bq_type: str) -> Any:
  column = ColumnPlan("x", bq_type, "NULLABLE", ColumnKind.NUMERIC, False,
                      False)
  return table_plan("measures", [column])


def test_float64_and_int64_fast_path_equals_the_per_cell_path():
  # FLOAT64/INT64 columns skip numeric_value (numpy converts directly);
  # a NUMERIC column of the same Python values takes the per-cell path.
  rng = random.Random(11)
  floats: list[Any] = [rng.uniform(-1e6, 1e6) for _ in range(500)]
  floats += [None, math.nan, math.inf, -math.inf, -0.0, 1e308, 5e-324]
  ints: list[Any] = [rng.randint(-2**62, 2**62) for _ in range(500)]
  ints += [None, 0, 2**53 + 1, -2**63, 2**63 - 1]
  for bq_type, values in (("FLOAT64", floats), ("FLOAT", floats),
                          ("INT64", ints), ("INTEGER", ints)):
    rows = [{"x": v} for v in values]
    fast = _encode(rows, table=_one_numeric(bq_type)).num
    slow = _encode(rows, table=_one_numeric("NUMERIC")).num
    np.testing.assert_array_equal(fast, slow, err_msg=bq_type)
  mixed = [{"x": 1}, {"x": 2.5}, {"x": None}]  # int cells in a FLOAT64 column
  np.testing.assert_array_equal(
      _encode(mixed, table=_one_numeric("FLOAT64")).num[:, 0],
      [1.0, 2.5, math.nan])


def test_the_fast_path_never_calls_numeric_value(monkeypatch):

  def per_cell(value: Any) -> float:
    raise AssertionError(f"per-cell path used for {value!r}")

  monkeypatch.setattr(encode_module, "numeric_value", per_cell)
  rows = [{"x": 1.5}, {"x": None}, {"x": 7}]
  for bq_type in ("FLOAT64", "INT64"):
    _encode(rows, table=_one_numeric(bq_type))
  with pytest.raises(AssertionError, match="per-cell"):
    _encode(rows, table=_one_numeric("NUMERIC"))


def test_the_fast_path_still_refuses_non_numbers():
  # numpy would parse "1.5" and read True as 1: those fall back to the
  # per-cell path, which reads a bool as 0/1 and refuses text.
  table = _one_numeric("FLOAT64")
  assert _encode([{
      "x": True
  }, {
      "x": 2.0
  }], table=table).num[:, 0].tolist() == [1.0, 2.0]
  with pytest.raises(ValueError, match="'x'") as info:
    _encode([{"x": 1.0}, {"x": "1.5"}], table=table)
  assert "1.5" not in str(info.value)


def test_a_row_missing_a_plan_column_raises():
  row = client_rows()[0]
  del row["review"]
  with pytest.raises(ValueError, match="review"):
    _encode([row])


def test_a_value_with_no_numeric_reading_raises_naming_column_not_value():
  row = client_rows()[0]
  row["quantity"] = "two"
  with pytest.raises(ValueError, match="quantity") as info:
    _encode([row])
  assert "two" not in str(info.value)
  assert "str" in str(info.value)


def test_empty_input_encodes_nothing():
  fn = EncodeBatchFn(all_types_plan(), Side.SOURCE, salt=SALT)
  fn.setup()
  assert not list(fn.process([]))


# ---------------------------------------------------------------------------
# DirectRunner: BatchElements -> EncodeBatchFn
# ---------------------------------------------------------------------------


def _row_view(batch: EncodedBatch) -> list[tuple]:
  """Per-row facts of a batch, order-free, for `equal_to`."""
  return [(int(batch.row_hash[i]), int(batch.nonkey_hash[i]),
           int(batch.pk_hash[i]), int(batch.fk_hash[i, 0]),
           int(batch.null_bits[i]), bool(batch.subsample_m[i]),
           float(batch.num[i, 1])) for i in range(batch.n)]


def test_encode_side_on_direct_runner_matches_the_pure_encoder():
  rows = _orders(3_000)
  plan = _orders_plan(rows_source=750, rows_synthetic=3_000)
  expected = _row_view(
      BatchEncoder.from_table(plan, Side.SYNTHETIC, salt=SALT).encode(rows))
  sources = InMemorySources({("orders", Side.SYNTHETIC): rows})
  with BeamTestPipeline() as p:
    batches = (
        sources.read(p, plan, Side.SYNTHETIC)
        | EncodeSide(plan, Side.SYNTHETIC, salt=SALT))
    facts = batches | beam.FlatMap(_row_view)
    assert_that(facts, equal_to(expected))
    sizes = batches | "Sizes" >> beam.Map(lambda b: (b.table, b.side, b.n))
    total = (sizes | beam.Map(lambda t: t[2]) | beam.CombineGlobally(sum))
    assert_that(total, equal_to([3_000]), label="AllRows")
    bounded = (
        sizes
        | "Bounds" >> beam.Map(lambda t: t[2] <= MAX_BATCH_SIZE and t[0] ==
                               "orders" and t[1] == "synthetic")
        | "All" >> beam.CombineGlobally(all))
    assert_that(bounded, equal_to([True]), label="Bounded")
  # matched n: 750 of 3,000 synthetic rows expected in the subsample
  kept = sum(1 for fact in expected if fact[5])
  assert abs(kept - 750) < 4 * math.sqrt(3_000 * 0.25 * 0.75)
  assert MIN_BATCH_SIZE == 512 and MAX_BATCH_SIZE == 8192


def test_encoded_batch_shapes_and_dtypes():
  rows = client_rows()
  batch = _encode(rows, side=Side.REFERENCE)
  assert (batch.table, batch.side, batch.n) == ("order_items", Side.REFERENCE,
                                                4)
  for name in ("row_hash", "nonkey_hash", "pk_hash", "identity_hash",
               "null_bits"):
    array = getattr(batch, name)
    assert array.shape == (4,) and array.dtype == np.uint64, name
  assert batch.subsample_m.shape == (4,)
  assert len(batch.text) == len(batch.layout.text_columns)
  assert all(len(column) == 4 for column in batch.text)
  assert date(2026, 1, 5) == rows[0]["delivery_date"]
  assert time(9, 30, 0, 250_000) == rows[0]["pickup_time"]
