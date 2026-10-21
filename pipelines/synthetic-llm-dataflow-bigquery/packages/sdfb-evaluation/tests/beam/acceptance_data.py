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
"""The planted-defect acceptance fixture: a seeded generator of invented
thelook-shaped `users` → `orders` → `order_items` tables, a faithful
("good") synthetic twin drawn from the same generator, a "bad" twin with
seven planted defects, and the `EvaluationPlan` of each, planned by the
real planner functions from the rows (`context.offline.table_plan`).

    users (root)       id PK, email identity, first/last name, age,
                       gender, country, city (3 % NULL), traffic_source,
                       created_at, street_address (free text)
    orders (driven)    order_id PK, user_id → users.id, status,
                       num_of_item, amount (∝ num_of_item), created_at,
                       delivery_note (free text, 10 % NULL)
    order_items        id PK, order_id → orders.order_id, sale_price,
      (driven)         cost (≈ half the sale price), status (2 % NULL),
                       created_at

The seven defects of the bad twin (`DEFECTS` names the metrics each must
FAIL):

    1  users        1 % of rows carry the non-key content of distinct R
                    records outside E (ranks 1025..2000), under their
                    own keys
    2  users        0.5 % carry distinct E records (ranks 1..1024)
    3  orders       amount + 500
    4  order_items  cost permuted across rows (the sale-price
                    correlation destroyed, the marginal kept)
    5  order_items  2 % extra rows (fresh content) whose order_id no
                    order holds
    6  orders       status collapsed to one value
    7  orders       2 % of the delivery notes copied from distinct R
                    notes (each unique in the source: rare)

`heavy_copies` is the heavy copier (30 % of the users rows carry R
records) that proves the DCR holdout share FAILs at its design effect
size (defect 1's 1 % moves it by at most half a percent).

Every text value embeds a random code, so a faithful draw reproduces no
source text by chance; every text column's shapes come from a small set
of templates, so its masks are shared by both sides. Nothing here is real
data: invented names and towns, ids in invented ranges, e-mails at
`example.com`, `demo-project`.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from sdfb_evaluation.catalogue import load_catalogue
from sdfb_evaluation.context import offline
from sdfb_evaluation.context.budget import Budget, predict_shuffle_gb
from sdfb_evaluation.context.launch import LaunchContext
from sdfb_evaluation.context.plan import (
    EvaluationPlan,
    Knobs,
    TablePlan,
)
from sdfb_evaluation.context.reference import Panel
from sdfb_evaluation.context.relationships import Edge
from sdfb_evaluation.context.scope import ScopePlan
from sdfb_evaluation.scoring import Thresholds, to_metric_row
from sdfb_evaluation.types import MetricValue

from .tables import DATASET, PROJECT, SOURCE_DATASET

SALT = "acce" * 8
EVALUATED_AT = "2026-09-30T00:00:00.000000Z"
STATS_TABLE = f"{PROJECT}.synthetic_rag.source_table_stats"
N_USERS = 5000  # both sides
USERS_PANEL = 2000  # R and H: the privacy pass's minimum
CHILD_PANEL = 1000  # orders / order_items: lifts only (privacy NE, < 2000)
EXPOSED = 1024  # E = the first 1024 ranks of R (D3)
_BUDGET = Budget(max_shuffle_gb=500.0, max_bytes_billed=1 << 40)

USER_EDGE = Edge(cols=("user_id",), ref="users", ref_cols=("id",))
ORDER_EDGE = Edge(cols=("order_id",), ref="orders", ref_cols=("order_id",))

USERS_FIELDS: tuple[dict[str, str], ...] = (
    {
        "name": "id",
        "type": "INT64",
        "mode": "REQUIRED"
    },
    {
        "name": "email",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "first_name",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "last_name",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "age",
        "type": "INT64",
        "mode": "NULLABLE"
    },
    {
        "name": "gender",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "country",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "city",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "traffic_source",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "created_at",
        "type": "TIMESTAMP",
        "mode": "NULLABLE"
    },
    {
        "name": "street_address",
        "type": "STRING",
        "mode": "NULLABLE"
    },
)
ORDERS_FIELDS: tuple[dict[str, str], ...] = (
    {
        "name": "order_id",
        "type": "INT64",
        "mode": "REQUIRED"
    },
    {
        "name": "user_id",
        "type": "INT64",
        "mode": "NULLABLE"
    },
    {
        "name": "status",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "num_of_item",
        "type": "INT64",
        "mode": "NULLABLE"
    },
    {
        "name": "amount",
        "type": "FLOAT64",
        "mode": "NULLABLE"
    },
    {
        "name": "created_at",
        "type": "TIMESTAMP",
        "mode": "NULLABLE"
    },
    {
        "name": "delivery_note",
        "type": "STRING",
        "mode": "NULLABLE"
    },
)
ITEMS_FIELDS: tuple[dict[str, str], ...] = (
    {
        "name": "id",
        "type": "INT64",
        "mode": "REQUIRED"
    },
    {
        "name": "order_id",
        "type": "INT64",
        "mode": "NULLABLE"
    },
    {
        "name": "sale_price",
        "type": "FLOAT64",
        "mode": "NULLABLE"
    },
    {
        "name": "cost",
        "type": "FLOAT64",
        "mode": "NULLABLE"
    },
    {
        "name": "status",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "created_at",
        "type": "TIMESTAMP",
        "mode": "NULLABLE"
    },
)

# (defect, table, column (a pair: both columns) or None, the metric ids
# that must FAIL). The
# brief also names row.dcr_train_holdout_share for defect 1, but 1 % of
# copies can move the share by at most half of 1 % (0.5 + c / 2), while
# its gate reads ci_low >= 0.60: the acceptance pins its direction at 1 %
# and its FAIL on a heavy copier (`heavy_copies`, 30 %).
Scope = str | tuple[str, str] | None
DEFECTS: tuple[tuple[str, str, Scope, tuple[str, ...]], ...] = (
    ("1 R copies (1 %)", "users", None, ("row.memorization_lift",
                                         "row.exact_match_rate_nonkey")),
    ("2 E copies (0.5 %)", "users", None, ("row.exposure_lift",)),
    ("3 amount + 500", "orders", "amount", ("column.ks", "column.pit_w1",
                                            "column.jsd")),
    ("4 correlation destroyed", "order_items", ("sale_price", "cost"),
     ("pair.pearson_delta",)),
    ("5 2 % orphan items", "order_items", None, ("relationship.orphan_rate",)),
    ("6 status collapsed", "orders", "status", ("column.entropy_ratio",
                                                "column.top1_share_delta")),
    ("7 notes copied from rare R values", "orders", "delivery_note",
     ("field.substantive_copy_rate", "field.value_memorization_lift")),
)

_FIRST = ("Ada", "Bo", "Cyra", "Dax", "Eli", "Fenna", "Gus", "Hale", "Ines",
          "Jory", "Kit", "Lark", "Milo", "Nell", "Otis", "Pia", "Quin", "Rhea",
          "Sven", "Tova", "Ugo", "Vera", "Wren", "Xavi", "Yara", "Zeno", "Arlo",
          "Bea", "Cato", "Dora", "Emrys", "Fay", "Gil", "Hana", "Ivo", "Juno",
          "Kai", "Lia", "Mads", "Noor")
_SYLLABLES = ("ash", "bel", "cor", "dun", "fen", "gar", "hol", "ker", "lom",
              "mar")
_LAST = tuple(f"{a.title()}{b}{c}" for a in _SYLLABLES[:8]
              for b in ("by", "ton")
              for c in ("", "e", "s", "ley", "man")[:5])[:80]
_COUNTRIES = ("Arvenia", "Belloria", "Corvania", "Dalmora", "Estavia",
              "Fennmark", "Galdor", "Hesperin", "Iskaria", "Jorvik")
_COUNTRY_P = (0.3, 0.18, 0.12, 0.1, 0.08, 0.07, 0.05, 0.04, 0.03, 0.03)
_CITIES = tuple(f"Town{k:02d}" for k in range(60))
_TRAFFIC = ("Search", "Organic", "Facebook", "Email", "Display")
_TRAFFIC_P = (0.45, 0.2, 0.15, 0.12, 0.08)
_STREETS = ("Maple", "Cedar", "Birch", "Willow", "Aspen", "Laurel", "Juniper",
            "Hazel", "Rowan", "Alder", "Linden", "Sorrel", "Heather", "Clover",
            "Bramble", "Thistle", "Meadow", "Harbor", "Summit", "Orchard")
_SUFFIXES = ("Street", "Avenue", "Lane", "Road")
_STATUSES = ("Complete", "Shipped", "Processing", "Cancelled", "Returned")
_STATUS_P = (0.45, 0.2, 0.15, 0.12, 0.08)
_ITEMS_P = (0.3, 0.45, 0.2, 0.05)  # orders a user holds: 0..3
_LINES_P = (0.6, 0.3, 0.1)  # items an order holds: 1..3
_GREETINGS = ("Please", "Kindly", "If possible")
_ACTIONS = ("leave the parcel", "ring twice and wait", "hand it over",
            "knock at the side door")
_PLACES = ("green gate", "garden shed", "front porch", "red mailbox",
           "back stairs")
_START = datetime(2024, 1, 1, tzinfo=UTC)
_TWO_YEARS = 2 * 365 * 86_400


def _zipf(k: int, s: float) -> np.ndarray:
  weights = 1.0 / np.arange(1, k + 1)**s
  normalized: np.ndarray = weights / weights.sum()
  return normalized


_CITY_P = _zipf(len(_CITIES), 0.8)
_LAST_P = _zipf(len(_LAST), 0.6)


def _moment(rng: np.random.Generator, base: datetime, span: int) -> datetime:
  return base + timedelta(
      seconds=int(rng.integers(0, span)),
      microseconds=int(rng.integers(0, 1_000_000)))


def _unique(rng: np.random.Generator, seen: set[str], make: Any) -> str:
  """`make(rng)` until it gives a value not in `seen` (then recorded)."""
  while True:
    value = make(rng)
    if value not in seen:
      seen.add(value)
      return str(value)


def _email(first: str, last: str) -> Any:

  def make(rng: np.random.Generator) -> str:
    return f"{first}.{last}{int(rng.integers(100_000, 1_000_000))}@example.com"

  return make


def _address(rng: np.random.Generator) -> str:
  return (f"{int(rng.integers(10_000, 100_000))} "
          f"{_STREETS[int(rng.integers(len(_STREETS)))]} "
          f"{_SUFFIXES[int(rng.integers(len(_SUFFIXES)))]} Unit "
          f"{int(rng.integers(100, 1000))}")


def _note(rng: np.random.Generator) -> str:
  return (f"{_GREETINGS[int(rng.integers(len(_GREETINGS)))]} "
          f"{_ACTIONS[int(rng.integers(len(_ACTIONS)))]} by the "
          f"{_PLACES[int(rng.integers(len(_PLACES)))]}, code "
          f"{int(rng.integers(10_000_000, 100_000_000))}")


def _choice(rng: np.random.Generator, values: Sequence[str],
            p: Sequence[float] | np.ndarray) -> str:
  return values[int(rng.choice(len(values), p=p))]


def users_rows(n: int, seed: int, id_base: int) -> list[dict[str, Any]]:
  """`n` invented users (client Python types), deterministic in `seed`."""
  rng = np.random.default_rng(seed)
  emails: set[str] = set()
  addresses: set[str] = set()
  rows = []
  for i in range(n):
    first = _FIRST[int(rng.integers(len(_FIRST)))]
    last = _choice(rng, _LAST, _LAST_P)
    rows.append({
        "id": id_base + i,
        "email": _unique(rng, emails, _email(first.lower(), last.lower())),
        "first_name": first,
        "last_name": last,
        "age": int(rng.integers(18, 80)),
        "gender": "F" if rng.random() < 0.5 else "M",
        "country": _choice(rng, _COUNTRIES, _COUNTRY_P),
        "city": None if rng.random() < 0.03 else _choice(rng, _CITIES, _CITY_P),
        "traffic_source": _choice(rng, _TRAFFIC, _TRAFFIC_P),
        "created_at": _moment(rng, _START, _TWO_YEARS),
        "street_address": _unique(rng, addresses, _address),
    })
  return rows


def orders_rows(users: Sequence[Mapping[str, Any]], seed: int,
                id_base: int) -> list[dict[str, Any]]:
  """0..3 orders per user, each carrying its user's id."""
  rng = np.random.default_rng(seed)
  notes: set[str] = set()
  rows = []
  for user in users:
    for _ in range(int(rng.choice(len(_ITEMS_P), p=_ITEMS_P))):
      items = int(rng.integers(1, 5))
      rows.append({
          "order_id":
              id_base + len(rows),
          "user_id":
              user["id"],
          "status":
              _choice(rng, _STATUSES, _STATUS_P),
          "num_of_item":
              items,
          "amount":
              round(items * float(rng.lognormal(3.2, 0.5)) + 5.0, 2),
          "created_at":
              _moment(rng, user["created_at"], 90 * 86_400),
          "delivery_note":
              None if rng.random() < 0.1 else _unique(rng, notes, _note),
      })
  return rows


def items_rows(orders: Sequence[Mapping[str, Any]], seed: int,
               id_base: int) -> list[dict[str, Any]]:
  """1..3 items per order; cost is 45-55 % of the sale price."""
  rng = np.random.default_rng(seed)
  rows = []
  for order in orders:
    for _ in range(1 + int(rng.choice(len(_LINES_P), p=_LINES_P))):
      price = round(float(rng.lognormal(3.0, 0.7)), 2)
      rows.append({
          "id":
              id_base + len(rows),
          "order_id":
              order["order_id"],
          "sale_price":
              price,
          "cost":
              round(price * float(rng.uniform(0.45, 0.55)), 2),
          "status":
              None
              if rng.random() < 0.02 else _choice(rng, _STATUSES, _STATUS_P),
          "created_at":
              _moment(rng, order["created_at"], 3 * 86_400),
      })
  return rows


def launch_rows(seed: int, id_base: int) -> dict[str, list[dict[str, Any]]]:
  """One launch's three tables (users, orders, order_items)."""
  users = users_rows(N_USERS, seed, id_base)
  orders = orders_rows(users, seed + 1, 10 * id_base)
  return {
      "users": users,
      "orders": orders,
      "order_items": items_rows(orders, seed + 2, 100 * id_base),
  }


# --------------------------------------------------------------------------
# the defects
# --------------------------------------------------------------------------
def _copy_content(donor: Mapping[str, Any],
                  row: Mapping[str, Any]) -> dict[str, Any]:
  """`donor`'s every column under `row`'s primary key."""
  return {**donor, "id": row["id"]}


def plant_defects(source: Mapping[str, Sequence[Mapping[str, Any]]],
                  good: Mapping[str, Sequence[Mapping[str, Any]]],
                  seed: int = 99) -> dict[str, list[dict[str, Any]]]:
  """The bad twin: `good` with the seven defects of the module docstring."""
  rng = np.random.default_rng(seed)
  users = [dict(row) for row in good["users"]]
  n = len(users)
  r_rest = source["users"][EXPOSED:USERS_PANEL]  # R outside E
  exposed = source["users"][:EXPOSED]
  for k, donor in enumerate(r_rest[:n // 100]):  # 1: 1 % from R \ E
    at = 100 * k + 3
    users[at] = _copy_content(donor, users[at])
  for k, donor in enumerate(exposed[:n // 200]):  # 2: 0.5 % from E
    at = 200 * k + 51
    users[at] = _copy_content(donor, users[at])
  orders = [dict(row) for row in good["orders"]]
  for row in orders:
    row["amount"] = round(row["amount"] + 500.0, 2)  # 3
    row["status"] = "Complete"  # 6
  notes = [
      row["delivery_note"]
      for row in source["orders"][:CHILD_PANEL]
      if row["delivery_note"] is not None
  ]
  copies = len(orders) // 50  # 7: 2 %, distinct R notes
  for k, note in enumerate(notes[:copies]):
    orders[50 * k + 7]["delivery_note"] = note
  items = [dict(row) for row in good["order_items"]]
  costs = [row["cost"] for row in items]
  for row, cost in zip(
      items, rng.permutation(np.array(costs)).tolist(), strict=True):
    row["cost"] = cost  # 4
  orphans = round(0.02 * len(items) / 0.98)  # 5: 2 % of the final rows
  missing = [{
      "order_id": 990_000_000 + k,
      "created_at": _moment(rng, _START, _TWO_YEARS)
  } for k in range(orphans)]
  top = max(row["id"] for row in items) + 1
  items += items_rows(missing, seed + 1, top)[:orphans]  # fresh content
  return {"users": users, "orders": orders, "order_items": items}


def heavy_copies(source: Sequence[Mapping[str, Any]],
                 good: Sequence[Mapping[str, Any]],
                 share: float = 0.3) -> list[dict[str, Any]]:
  """`good` users with `share` of the rows carrying distinct R records'
  content (a heavy copier: the holdout share's design effect size)."""
  users = [dict(row) for row in good]
  step = round(1 / share)
  for k, donor in enumerate(source[:min(USERS_PANEL, int(len(users) * share))]):
    at = (step * k) % len(users)
    users[at] = _copy_content(donor, users[at])
  return users


# --------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------
def panel(rows: Sequence[Mapping[str, Any]], n: int) -> Panel:
  """R = the first `n` source rows, H the next `n` (the source rows are
  i.i.d., so the prefix is a random sample, as D3's ranking is)."""
  r_rows = [dict(row) for row in rows[:n]]
  h_rows = [dict(row) for row in rows[n:2 * n]]
  return Panel(
      r_rows=r_rows,
      h_rows=h_rows,
      e_n=min(len(r_rows), EXPOSED),
      he_n=min(len(h_rows), EXPOSED),
      digest="d" * 64,
      verified=True,
      expected_digest="d" * 64)


def scope_of(landing: str,
             *,
             expected: int | None,
             status: str = "ok",
             reason: str | None = None) -> ScopePlan:
  return ScopePlan(
      landing_table=landing,
      mode="table",
      status=status,
      reason=reason,
      read_table=landing if status != "empty" else "",
      prepare_sql=(),
      window=(None, None),
      expected_rows=expected,
      read_expr=f"`{landing}`")


def table_plan(name: str,
               fields: Sequence[Mapping[str, Any]],
               source_rows: Sequence[Mapping[str, Any]],
               synthetic_rows: Sequence[Mapping[str, Any]],
               *,
               pk: Sequence[str],
               identity: Sequence[str] = (),
               edges: Sequence[Edge] = (),
               role: str = "isolated",
               panel_rows: int = 0,
               pair_max_columns: int = 20) -> TablePlan:
  """A launch table planned from both sides' rows by the planner's own
  `apply_planning`/`select_pairs` (`context.offline.table_plan`; census
  exact: a 500 GB budget)."""
  return offline.table_plan(
      name,
      fields,
      source_rows,
      synthetic_rows,
      landing_table=f"{PROJECT}.{DATASET}.{name}",
      source_table=f"{PROJECT}.{SOURCE_DATASET}.{name}",
      budget=_BUDGET,
      pk=pk,
      identity=identity,
      edges=edges,
      role=role,
      panel=panel(source_rows, panel_rows) if panel_rows else None,
      pair_max_columns=pair_max_columns,
      run_id=f"run-acc-{name}",
      model="thelook_acceptance",
      reference_digest="d" * 64)


def launch_context(tables: Sequence[TablePlan]) -> LaunchContext:
  """An invented manual launch of `tables` (source stats on the sample
  tier, written to `STATS_TABLE`)."""
  return LaunchContext(
      generation_job_id="2026-09-29_01_00_00-4242",
      job_name="synthetic-acceptance",
      region="europe-west1",
      started_at="2026-09-29T01:00:00Z",
      finished_at="2026-09-29T01:40:00Z",
      base_run_id="run-acc",
      run_ids=tuple(str(t.run_id) for t in tables),
      tables_in_order=tuple(t.landing_table for t in tables),
      reference_table=None,
      write_disposition="WRITE_APPEND",
      relationships_uri="config/relationships/thelook_acceptance.yaml",
      params={
          "source_stats": "sample",
          "source_stats_table": STATS_TABLE,
          "reference_rows_limit": USERS_PANEL,
          "engine": "b1_rag",
      },
      model_sha=None,
      model_name="thelook_acceptance",
      adjusted_model_uri=None,
      params_source="manual",
      writes=())


def evaluation_plan(
    tables: Sequence[TablePlan],
    *,
    evaluation_id: str,
    label_key_uri: str | None,
    knobs: Knobs | None = None,
    warnings: Sequence[str] = ()) -> EvaluationPlan:
  """The `EvaluationPlan` of `tables`, as `build_plan` assembles one."""
  return EvaluationPlan(
      evaluation_id=evaluation_id,
      evaluation_key="a" * 32,
      evaluated_at=EVALUATED_AT,
      salt=SALT,
      mode="exact",
      trigger="cli",
      runner="DirectRunner",
      launch=launch_context(tables),
      models=(),
      model_sha=None,
      tables=tuple(tables),
      knobs=knobs or Knobs(
          privacy_sample_rows=2500,
          detection_sample_rows=1000,
          row_flags_top_k=25),
      prepare_sql=(),
      bq_bytes_estimate=0,
      predicted_shuffle_gb=predict_shuffle_gb(tables),
      warnings=tuple(warnings),
      catalogue_version=load_catalogue().version,
      temp_dataset=f"{PROJECT}.synthetic_data_quality",
      label_key_uri=label_key_uri)


def acceptance_plan(source: Mapping[str, Sequence[Mapping[str, Any]]],
                    synthetic: Mapping[str, Sequence[Mapping[str, Any]]], *,
                    evaluation_id: str,
                    label_key_uri: str | None) -> EvaluationPlan:
  """The three-table plan of one (source, synthetic) pair."""
  users = table_plan(
      "users",
      USERS_FIELDS,
      source["users"],
      synthetic["users"],
      pk=("id",),
      identity=("email",),
      role="root",
      panel_rows=USERS_PANEL)
  orders = table_plan(
      "orders",
      ORDERS_FIELDS,
      source["orders"],
      synthetic["orders"],
      pk=("order_id",),
      edges=(USER_EDGE,),
      role="driven",
      panel_rows=CHILD_PANEL)
  items = table_plan(
      "order_items",
      ITEMS_FIELDS,
      source["order_items"],
      synthetic["order_items"],
      pk=("id",),
      edges=(ORDER_EDGE,),
      role="driven",
      panel_rows=CHILD_PANEL)
  return evaluation_plan([users, orders, items],
                         evaluation_id=evaluation_id,
                         label_key_uri=label_key_uri)


# --------------------------------------------------------------------------
# the generator's source_table_stats rows (an independent re-statement of
# its profiler's definitions, never an import of it)
# --------------------------------------------------------------------------
_NUMERIC_TYPES = frozenset({"INT64", "FLOAT64", "NUMERIC", "BIGNUMERIC"})


def _column_stats(field: Mapping[str, Any],
                  rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
  """What the generator's profiler records for one column of its
  reference sample: NULL fraction, distinct non-empty non-NULL values
  (as text), and for a numeric BigQuery type the 11-point deciles
  `ordered[round(i * (n - 1) / 10)]`."""
  n = len(rows)
  values = [row[field["name"]] for row in rows]
  present = [v for v in values if v is not None]
  substantive = [
      v for v in present if not (isinstance(v, str) and not v.strip())
  ]
  entry: dict[str, Any] = {
      "null_fraction": round((n - len(present)) / n, 6) if n else 0.0,
      "distinct": len(Counter(str(v) for v in substantive)),
      "deciles": [],
      "stats_tier": "sample",
  }
  if field["type"] in _NUMERIC_TYPES and substantive:
    ordered = sorted(float(v) for v in substantive)
    last = len(ordered) - 1
    entry["deciles"] = [
        round(ordered[round(i * last / 10)], 6) for i in range(11)
    ]
  return entry


def stats_rows(table: TablePlan, fields: Sequence[Mapping[str, Any]],
               reference: Sequence[Mapping[str, Any]],
               **overrides: Any) -> list[dict[str, Any]]:
  """`source_table_stats` rows for `table`, computed from its reference
  sample the way the generator's profiler does (tier `sample`);
  `overrides` replace fields of every column's row."""
  rows = []
  for field in fields:
    entry = {**_column_stats(field, reference), **overrides}
    rows.append({
        "table_fqn": table.source_table,
        "reference_digest": table.reference_digest,
        "run_id": table.run_id,
        "column": field["name"],
        "null_fraction": entry["null_fraction"],
        "distinct": entry["distinct"],
        "stats": json.dumps(entry),
        "sample_rows": len(reference),
        "stats_tier": entry["stats_tier"],
        "profiler_version": "2",
        "computed_at": "2026-09-29T01:05:00Z",
    })
  return rows


FIELDS_BY_TABLE = {
    "users": USERS_FIELDS,
    "orders": ORDERS_FIELDS,
    "order_items": ITEMS_FIELDS,
}


class FakeStatsQuery:
  """The driver's `source_table_stats` read (`Bq.query`'s shape), served
  from rows computed on each table's reference sample, answered as
  `assemble.source_stats_sql` asks: the table and reference digest, the
  tier (a row without one counts as `sample`) and the latest row per
  column (`computed_at`). `add` stores more rows (older stats, another
  tier); `calls` records every (sql, params)."""

  def __init__(self, plan: EvaluationPlan, **overrides: Any):
    self.calls: list[tuple[str, dict[str, Any]]] = []
    self._rows: list[dict[str, Any]] = []
    for table in plan.tables:
      if table.panel is None or table.name not in FIELDS_BY_TABLE:
        continue
      self._rows += stats_rows(table, FIELDS_BY_TABLE[table.name],
                               table.panel.r_rows, **overrides)

  def add(self, rows: Sequence[Mapping[str, Any]]) -> None:
    self._rows += [dict(row) for row in rows]

  def __call__(self, sql: str, params: Mapping[str, Any],
               **kwargs: Any) -> list[dict[str, Any]]:
    del kwargs  # max_bytes: the stats table is tiny
    self.calls.append((sql, dict(params)))
    latest: dict[str, dict[str, Any]] = {}
    for row in self._rows:
      if (row["table_fqn"] != params.get("table_fqn") or
          row["reference_digest"] != params.get("reference_digest") or
          (row["stats_tier"] or "sample") != params.get("tier")):
        continue
      held = latest.get(row["column"])
      if held is None or row["computed_at"] > held["computed_at"]:
        latest[row["column"]] = row
    return list(latest.values())


# what a reader of evaluation_metrics has of a row's measurement
_MEASURED = ("value", "source_value", "synthetic_value", "baseline_value",
             "noise_floor", "ci_low", "ci_high")


def rescored(row: Mapping[str, Any],
             *,
             thresholds: Thresholds | None = None) -> dict[str, Any]:
  """A persisted `evaluation_metrics` row scored again by
  `scoring.to_metric_row` from nothing but its own fields, as any reader
  of the table can. JSON holds no infinity: a gated value that was
  infinite is persisted NULL with `detail.nonfinite` naming its sign,
  and is read back from there. `thresholds` are the run's overrides
  (R93-6), for a row stored under them."""
  detail = dict(row["detail"] or {})
  measured = {name: row[name] for name in _MEASURED}
  sign = detail.get("nonfinite")
  if sign is not None:
    metric = load_catalogue().get(row["metric_id"])
    gated = "value"
    if metric.uses_ci_bound:
      gated = "ci_high" if metric.direction == "higher_better" else "ci_low"
    measured[gated] = math.inf if sign == "+inf" else -math.inf
  enforced = detail.get("enforced", True)
  return to_metric_row(
      MetricValue(
          metric_id=row["metric_id"],
          table=row["table_name"],
          column=row["column_name"],
          column_2=row["column_name_2"],
          edge=row["edge"],
          column_kind=row["column_kind"],
          detail=detail,
          **measured),
      evaluation_id=row["evaluation_id"],
      evaluated_at=row["evaluated_at"],
      landing_table=row["landing_table"],
      source_table=row["source_table"],
      enforced=enforced if isinstance(enforced, bool) else True,
      thresholds=thresholds)


def with_scope(table: TablePlan, *, status: str, reason: str) -> TablePlan:
  """`table` with a non-readable scope, skipped as the planner skips it."""
  return dataclasses.replace(
      table,
      scope=scope_of(
          table.landing_table, expected=None, status=status, reason=reason),
      skip_reason=f"scope {status}: {reason}",
      rows_source=None,
      rows_synthetic=None,
      sample_rate_source=None,
      sample_rate_synthetic=None,
      panel=None)
