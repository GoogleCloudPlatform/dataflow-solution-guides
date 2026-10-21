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
"""Test tables for the relational pass: invented thelook-shaped `users`,
`orders` and `order_items` launch tables and a read-only `products`
parent, as hand-built `TablePlan`s shaped like the planner's (launch
tables carry their edges; a read-only parent has role `external`, only
its key columns, and is read from the landing project's `synthetic_data`
dataset and, on the source side, from its source-dataset twin).

Rows are built from a fan-out list — how many children each parent has —
so a test knows every children-per-parent count exactly.

Nothing here is real data: invented ids (source parents from 10_000,
synthetic from 20_000, children from 100_000), `example.com` e-mails.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any

from sdfb_evaluation.context.plan import TablePlan, kinds_from_schema
from sdfb_evaluation.context.relationships import Edge

from .tables import PROJECT, SOURCE_DATASET, scope, table_plan

SALT = "r3l4" * 8
SOURCE_BASE = 10_000
SYNTHETIC_BASE = 20_000
CHILD_BASE = 100_000
EXTERNAL_DATASET = "synthetic_data"
SIDES = ("source", "synthetic")
_COUNTRIES = ("Spain", "France", "Brazil", "Japan", "Kenya")
_STATUSES = ("Complete", "Shipped", "Processing", "Returned")

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
        "name": "country",
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
        "name": "user_email",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "status",
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
        "name": "product_id",
        "type": "INT64",
        "mode": "NULLABLE"
    },
    {
        "name": "bundle_product_id",
        "type": "INT64",
        "mode": "NULLABLE"
    },
    {
        "name": "sale_price",
        "type": "FLOAT64",
        "mode": "NULLABLE"
    },
)
PRODUCTS_FIELDS: tuple[dict[str, str], ...] = ({
    "name": "id",
    "type": "INT64",
    "mode": "REQUIRED"
},)

USER_EDGE = Edge(cols=("user_id",), ref="users", ref_cols=("id",))
ORDER_EDGE = Edge(cols=("order_id",), ref="orders", ref_cols=("order_id",))
PRODUCT_EDGE = Edge(
    cols=("product_id",), ref=f"{EXTERNAL_DATASET}.products", ref_cols=("id",))
BUNDLE_EDGE = Edge(
    cols=("bundle_product_id",),
    ref=f"{EXTERNAL_DATASET}.products",
    ref_cols=("id",))


def launch_table(name: str,
                 fields: Sequence[Mapping[str, Any]],
                 *,
                 pk: Sequence[str] = (),
                 identity: Sequence[str] = (),
                 edges: Sequence[Edge] = (),
                 role: str = "isolated",
                 rows_source: int | None = None,
                 rows_synthetic: int | None = None) -> TablePlan:
  """A launch table on the invented thelook datasets (keys, identity and
  FK columns planned as identifiers, as the planner does)."""
  keys = set(pk) | {c for e in edges for c in e.cols}
  columns = kinds_from_schema(fields, keys=keys, identity=identity)
  plan = table_plan(
      name,
      columns,
      pk=pk,
      identity=identity,
      edges=edges,
      rows_source=rows_source,
      rows_synthetic=rows_synthetic)
  return dataclasses.replace(plan, role=role)


def external_table(name: str,
                   fields: Sequence[Mapping[str, Any]],
                   *,
                   source_twin: bool = True) -> TablePlan:
  """A read-only parent as `_Planner.locate_parent` plans it: role
  `external`, its key columns only, read from `synthetic_data.<name>` in
  the landing project and, when it exists, `<source dataset>.<name>`."""
  landing = f"{PROJECT}.{EXTERNAL_DATASET}.{name}"
  source = f"{PROJECT}.{SOURCE_DATASET}.{name}" if source_twin else None
  columns = kinds_from_schema(fields, keys=[str(f["name"]) for f in fields])
  return TablePlan(
      name=name,
      landing_table=landing,
      source_table=source,
      run_id=None,
      role="external",
      pk=(),
      identity=(),
      edges=(),
      columns=tuple(columns),
      scope=scope(landing, landing),
      source_read_table=source or "",
      source_pinned=False,
      panel=None,
      pairs=(),
      encoding_plan_digest="e" * 32,
      synthetic_read_table=landing)


def users(ids: Sequence[int]) -> list[dict[str, Any]]:
  return [{
      "id": i,
      "email": f"user{i}@example.com",
      "country": _COUNTRIES[i % len(_COUNTRIES)],
  } for i in ids]


def orders_for(parents: Sequence[Mapping[str, Any]],
               fanouts: Sequence[int],
               *,
               start: int = CHILD_BASE) -> list[dict[str, Any]]:
  """`fanouts[i]` orders for `parents[i]`, each carrying its user's id and
  e-mail (the two FK columns the tests reference)."""
  rows = []
  next_id = start
  for parent, fanout in zip(parents, fanouts, strict=True):
    for _ in range(fanout):
      rows.append({
          "order_id": next_id,
          "user_id": parent["id"],
          "user_email": parent["email"],
          "status": _STATUSES[next_id % len(_STATUSES)],
      })
      next_id += 1
  return rows


def items_for(order_ids: Sequence[int],
              product_ids: Sequence[int],
              *,
              start: int = CHILD_BASE) -> list[dict[str, Any]]:
  """One item per order id, cycling through `product_ids` (its bundled
  product seven places further on)."""
  return [{
      "id": start + k,
      "order_id": order_id,
      "product_id": product_ids[k % len(product_ids)],
      "bundle_product_id": product_ids[(k + 7) % len(product_ids)],
      "sale_price": float(5 + k % 40),
  } for k, order_id in enumerate(order_ids)]


def counted(plan: TablePlan, rows_by: Mapping[tuple[str, str],
                                              Sequence[Any]]) -> TablePlan:
  """`plan` with its planning-scan row counts (both sides read in full)."""
  return dataclasses.replace(
      plan,
      rows_source=len(rows_by[(plan.name, "source")]),
      rows_synthetic=len(rows_by[(plan.name, "synthetic")]),
      sample_rate_source=1.0,
      sample_rate_synthetic=1.0)


def users_orders(
    src_fanouts: Sequence[int],
    syn_fanouts: Sequence[int],
    *,
    edges: Sequence[Edge] = (USER_EDGE,),
    parent_pk: Sequence[str] = ("id",)
) -> tuple[list[TablePlan], dict[tuple[str, str], list[dict[str, Any]]]]:
  """`users` (root, keyed by `parent_pk`) and `orders` (driven through
  `edges`): one user per fan-out entry and `fanouts[i]` orders for user i,
  on each side. The row lists are the caller's to edit; `counted` again
  after changing their lengths."""
  src_users = users(range(SOURCE_BASE, SOURCE_BASE + len(src_fanouts)))
  syn_users = users(range(SYNTHETIC_BASE, SYNTHETIC_BASE + len(syn_fanouts)))
  rows_by: dict[tuple[str, str], list[dict[str, Any]]] = {
      ("users", "source"): src_users,
      ("users", "synthetic"): syn_users,
      ("orders", "source"): orders_for(src_users, src_fanouts),
      ("orders", "synthetic"): orders_for(syn_users, syn_fanouts),
  }
  identity = () if "email" in parent_pk else ("email",)
  parent = launch_table(
      "users", USERS_FIELDS, pk=parent_pk, identity=identity, role="root")
  child = launch_table(
      "orders", ORDERS_FIELDS, pk=("order_id",), edges=edges, role="driven")
  return [counted(parent, rows_by), counted(child, rows_by)], rows_by
