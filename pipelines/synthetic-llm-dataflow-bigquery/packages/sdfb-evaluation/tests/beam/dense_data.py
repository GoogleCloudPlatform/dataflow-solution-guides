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
"""Test tables for the dense accumulators: invented thelook-shaped rows,
planned the way the planning scan would plan them.

`planned_table` takes the statistics the planning SELECT returns
(`COUNTIF(NULL)`, `APPROX_QUANTILES(v, 1000)`, `APPROX_TOP_COUNT`, …)
from `context.offline.planning_stats` — exact, from the rows in memory —
and hands them to the real `apply_planning` and `select_pairs`, so
kinds, grids, atoms, dictionaries and pairs are the planner's own.
Nothing here is real data.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import numpy as np

from sdfb_evaluation.context.budget import Budget
from sdfb_evaluation.context.offline import planning_stats
from sdfb_evaluation.context.plan import (
    TablePlan,
    apply_planning,
    kinds_from_schema,
    select_pairs,
)
from sdfb_evaluation.context.reference import Panel

from .tables import make_panel, table_plan

_BUDGET = Budget(max_shuffle_gb=500.0, max_bytes_billed=1 << 40)


def planned_table(name: str,
                  fields: Sequence[Mapping[str, Any]],
                  source_rows: Sequence[Mapping[str, Any]],
                  synthetic_rows: Sequence[Mapping[str, Any]],
                  *,
                  pk: Sequence[str] = (),
                  pair_max_columns: int = 20,
                  panel: Panel | None = None) -> TablePlan:
  """A `TablePlan` for `name`, planned from both sides' rows."""
  keys = frozenset(pk)
  columns = kinds_from_schema(fields, keys=pk)
  planned = apply_planning(
      columns,
      planning_stats(fields, source_rows, keys),
      planning_stats(fields, synthetic_rows, keys),
      budget=_BUDGET)
  table = table_plan(
      name,
      planned,
      pk=pk,
      panel=panel,
      rows_source=len(source_rows),
      rows_synthetic=len(synthetic_rows))
  return dataclasses.replace(
      table, pairs=select_pairs(planned, pair_max_columns))


# --------------------------------------------------------------------------
# an invented orders-like table with every kind the dense pass handles
# --------------------------------------------------------------------------
ORDERS_FIELDS: tuple[dict[str, str], ...] = (
    {
        "name": "order_id",
        "type": "INT64",
        "mode": "REQUIRED"
    },
    {
        "name": "amount",
        "type": "FLOAT64",
        "mode": "NULLABLE"
    },
    {
        "name": "discount",
        "type": "NUMERIC",
        "mode": "NULLABLE"
    },
    {
        "name": "quantity",
        "type": "INT64",
        "mode": "NULLABLE"
    },
    {
        "name": "created_at",
        "type": "TIMESTAMP",
        "mode": "NULLABLE"
    },
    {
        "name": "ship_date",
        "type": "DATE",
        "mode": "NULLABLE"
    },
    {
        "name": "pickup_time",
        "type": "TIME",
        "mode": "NULLABLE"
    },
    {
        "name": "status",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "is_gift",
        "type": "BOOL",
        "mode": "NULLABLE"
    },
    {
        "name": "note",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "coupon",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "comment",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "sku",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "tags",
        "type": "STRING",
        "mode": "REPEATED"
    },
)
_STATUSES = ("Complete", "Shipped", "Processing", "Cancelled", "Returned")
_WORDS = ("gift", "wrap", "fast", "Ünïcode", "東京", "ǅemo", "a-b", "x_y", "½")


def _order(rng: np.random.Generator, number: int, shift: float,
           start: datetime) -> dict[str, Any]:
  quantity = int(rng.integers(1, 6))
  amount = float(np.round(rng.lognormal(3.5, 0.6) + 10.0 * quantity, 2))
  created = start + timedelta(
      seconds=int(rng.integers(0, 365 * 86_400)),
      microseconds=int(rng.integers(0, 1_000_000)))
  words = rng.choice(len(_WORDS), size=int(rng.integers(1, 4)))
  note = " ".join(_WORDS[w] for w in words)
  return {
      "order_id":
          number,
      "amount":
          None if rng.random() < 0.05 else amount + shift,
      "discount":
          None if rng.random() < 0.3 else Decimal(
              int(rng.choice([0, 0, 5, 10, 15]))),
      "quantity":
          quantity,
      "created_at":
          created,
      "ship_date":
          None if rng.random() < 0.1 else
          (created + timedelta(days=int(rng.integers(0, 5)))).date(),
      "pickup_time":
          time(int(rng.integers(8, 20)), int(rng.integers(0, 60))),
      "status":
          str(rng.choice(_STATUSES, p=[0.5, 0.2, 0.15, 0.1, 0.05])),
      "is_gift":
          None if rng.random() < 0.02 else bool(rng.random() < 0.3),
      "note":
          None if rng.random() < 0.2 else
          (note if rng.random() < 0.9 else "   "),
      "coupon":
          "" if rng.random() < 0.5 else f"CP-{int(rng.integers(0, 999)):03d}",
      "comment":
          f"order {number} was {note} and arrived in good shape",
      "sku":
          f"SKU{number:07d}",
      "tags": []
              if rng.random() < 0.5 else ["a", "b"][:int(rng.integers(1, 3))],
  }


def orders_rows(n: int,
                seed: int,
                *,
                shift: float = 0.0,
                id_base: int = 0) -> list[dict[str, Any]]:
  """`n` invented orders rows (client Python types), deterministic in
  `seed`; `shift` moves `amount`."""
  rng = np.random.default_rng(seed)
  start = datetime(2024, 1, 1, tzinfo=UTC)
  return [_order(rng, id_base + i + 1, shift, start) for i in range(n)]


def orders_table(
    n_source: int = 3000,
    n_synthetic: int = 2500,
    n_reference: int = 600,
    *,
    shift: float = 0.0,
    pair_max_columns: int = 20) -> tuple[TablePlan, dict[str, list]]:
  """The orders plan and its rows by side (reference and holdout drawn from
  the source rows, as the R/H panel is)."""
  source = orders_rows(n_source, seed=1)
  synthetic = orders_rows(n_synthetic, seed=2, shift=shift, id_base=10**6)
  reference = source[:n_reference]
  holdout = source[n_reference:2 * n_reference]
  table = planned_table(
      "orders",
      ORDERS_FIELDS,
      source,
      synthetic,
      pk=("order_id",),
      pair_max_columns=pair_max_columns,
      panel=make_panel(reference, holdout))
  return table, {
      "source": source,
      "synthetic": synthetic,
      "reference": reference,
      "holdout": holdout,
  }
