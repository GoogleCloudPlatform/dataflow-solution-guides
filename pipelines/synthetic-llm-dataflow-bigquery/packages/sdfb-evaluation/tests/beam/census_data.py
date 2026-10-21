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
"""Test tables for the keyed census: an invented thelook-shaped `users`
table with every census kind, planned by the real planner
(`dense_data.planned_table`), plus the pure helpers the census tests share.

Nothing here is real data: invented names, `example.com` e-mails.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np

from sdfb_evaluation.beam.census import CensusSpec, SideTotals
from sdfb_evaluation.beam.dense import DenseProfile, DenseSpec
from sdfb_evaluation.beam.encode import BatchEncoder, EncodedBatch
from sdfb_evaluation.context.budget import Budget
from sdfb_evaluation.context.offline import planning_stats
from sdfb_evaluation.context.plan import (
    TablePlan,
    apply_planning,
    kinds_from_schema,
)

from .dense_data import planned_table
from .tables import make_panel, table_plan

SALT = "c3n5" * 8
LABEL_KEY = b"census-test-label-key-0123456789"

USERS_FIELDS: tuple[dict[str, str], ...] = (
    {
        "name": "user_id",
        "type": "INT64",
        "mode": "REQUIRED"
    },
    {
        "name": "status",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "city",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "full_name",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "bio",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "sku",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "is_member",
        "type": "BOOL",
        "mode": "NULLABLE"
    },
    {
        "name": "age",
        "type": "INT64",
        "mode": "NULLABLE"
    },
    {
        "name": "signup_date",
        "type": "DATE",
        "mode": "NULLABLE"
    },
    {
        "name": "last_login",
        "type": "TIMESTAMP",
        "mode": "NULLABLE"
    },
)
STATUSES = ("active", "dormant", "banned", "pending", "closed")
_FIRST = ("Ada", "Bo", "Cy", "Di", "Ed", "Flo", "Gus", "Hal", "Ivy", "Jo",
          "Kit", "Lu", "Max", "Ned", "Oz", "Pia", "Quin", "Rex", "Sol", "Tam")
_WORDS = ("likes", "hiking", "and", "long", "walks", "near", "the", "river",
          "bakes", "bread", "on", "sundays", "reads", "novels")


def full_name(i: int) -> str:
  """A unique invented name for index i (rare by construction)."""
  return f"{_FIRST[i % len(_FIRST)]} Zq{i:05d}"


def _user(rng: np.random.Generator, i: int, *, name_base: int,
          city_pool: int) -> dict[str, Any]:
  start = datetime(2023, 1, 1, tzinfo=UTC)
  words = rng.choice(len(_WORDS), size=int(rng.integers(5, 9)))
  sku_kind = rng.random()
  if sku_kind < 0.6:
    sku = f"SKU-{int(rng.integers(0, 999_999)):06d}"
  elif sku_kind < 0.9:
    sku = f"sk{int(rng.integers(0, 9999)):04d}"
  else:
    sku = f"X{int(rng.integers(0, 9))}-{int(rng.integers(0, 99)):02d}-Y"
  return {
      "user_id":
          name_base + i + 1,
      "status":
          None if rng.random() < 0.05 else str(
              rng.choice(STATUSES, p=[0.5, 0.2, 0.15, 0.1, 0.05])),
      "city":
          None if rng.random() < 0.1 else
          f"Town{int(rng.zipf(1.6)) % city_pool:04d}",
      "full_name":
          None if rng.random() < 0.02 else full_name(name_base + i),
      "bio":
          " ".join(_WORDS[w] for w in words) + f" #{i}",
      "sku":
          None if rng.random() < 0.05 else ("" if rng.random() < 0.05 else sku),
      "is_member":
          None if rng.random() < 0.03 else bool(rng.random() < 0.4),
      "age":
          None if rng.random() < 0.05 else int(rng.integers(18, 90)),
      "signup_date":
          None if rng.random() < 0.05 else
          (start + timedelta(days=int(rng.integers(0, 700)))).date(),
      "last_login":
          start + timedelta(seconds=int(rng.integers(0, 600 * 86_400))),
  }


def users_rows(n: int,
               seed: int,
               *,
               name_base: int = 0,
               city_pool: int = 400) -> list[dict[str, Any]]:
  rng = np.random.default_rng(seed)
  return [
      _user(rng, i, name_base=name_base, city_pool=city_pool) for i in range(n)
  ]


def users_table(n_source: int = 4000,
                n_synthetic: int = 3000,
                n_reference: int = 600,
                *,
                copies_from_reference: int = 0,
                copies_from_holdout: int = 0,
                verified: bool = True) -> tuple[TablePlan, dict[str, list]]:
  """The users plan and its rows by side. R and H are the first 2 x
  `n_reference` source rows (the panel); `copies_from_reference`
  synthetic rows reuse the full_name (and the rare numeric age) of an R
  row — planted copies of rare reference-only values; the next
  `copies_from_holdout` synthetic rows copy an H row's full_name alike
  (the null of the value lift when both are set equal)."""
  source = users_rows(n_source, seed=11)
  synthetic = users_rows(n_synthetic, seed=12, name_base=50_000)
  for k in range(copies_from_reference):
    donor = source[k % n_reference]
    synthetic[k] = {**synthetic[k], "full_name": donor["full_name"]}
  for k in range(copies_from_holdout):
    donor = source[n_reference + k % n_reference]
    at = copies_from_reference + k
    synthetic[at] = {**synthetic[at], "full_name": donor["full_name"]}
  reference = source[:n_reference]
  holdout = source[n_reference:2 * n_reference]
  panel = make_panel(reference, holdout)
  if not verified:
    panel = dataclasses.replace(
        panel, verified=False, reason="reference digest mismatch (test)")
  table = planned_table(
      "users",
      USERS_FIELDS,
      source,
      synthetic,
      pk=("user_id",),
      pair_max_columns=0,
      panel=panel)
  return table, {"source": source, "synthetic": synthetic}


def encode(table: TablePlan,
           side: str,
           rows: Sequence[Mapping[str, Any]],
           *,
           chunk: int = 997,
           subsample_rate: float | None = None) -> list[EncodedBatch]:
  """`rows` of one side as encoded batches (the plan's matched-n rate
  unless `subsample_rate` is given)."""
  encoder = BatchEncoder.from_table(
      table, side, salt=SALT, subsample_rate=subsample_rate)
  return [
      encoder.encode(rows[start:start + chunk])
      for start in range(0, len(rows), chunk)
  ]


def totals_for(table: TablePlan,
               batches: Mapping[str, Sequence[EncodedBatch]]) -> dict:
  """The dense pass's per-side totals, as the census side input."""
  dense_spec = DenseSpec.from_table(table)
  spec = CensusSpec.from_table(table)
  out = {}
  for side, side_batches in batches.items():
    profile: DenseProfile | None = None
    for batch in side_batches:
      part = DenseProfile.from_batch(dense_spec, batch)
      profile = part if profile is None else profile.merge(part)
    if profile is not None:
      out[side] = SideTotals.from_profile(spec, profile)
  return out


def with_census(table: TablePlan,
                **by_column: tuple[str, float | None]) -> TablePlan:
  """`table` with the named columns' census method forced (a planner
  budget in a test's hands)."""
  columns = []
  for column in table.columns:
    if column.name in by_column:
      method, rate = by_column[column.name]
      columns.append(
          dataclasses.replace(column, census=method, value_sample_rate=rate))
    else:
      columns.append(column)
  return dataclasses.replace(table, columns=tuple(columns))


def identity_table(n_source: int = 1500,
                   n_synthetic: int = 1200,
                   n_reference: int = 300,
                   *,
                   copies: int = 0) -> tuple[TablePlan, dict[str, list]]:
  """An invented `members` table whose identity (unique-but-not-a-key)
  columns — an e-mail and an INT64 member number — are censused by their
  text (they sit outside the encoder's non-key block). `copies`
  synthetic rows reuse an R row's e-mail and member number."""
  fields = (
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
          "name": "member_no",
          "type": "INT64",
          "mode": "NULLABLE"
      },
      {
          "name": "tier",
          "type": "STRING",
          "mode": "NULLABLE"
      },
  )
  rng = np.random.default_rng(21)

  def row(i: int, base: int) -> dict[str, Any]:
    return {
        "id": base + i,
        "email": f"member{base + i:06d}@example.com",
        "member_no": 700_000 + base + i,
        "tier": str(rng.choice(("gold", "silver", "bronze"))),
    }

  source = [row(i, 0) for i in range(n_source)]
  synthetic = [row(i, 100_000) for i in range(n_synthetic)]
  for k in range(copies):
    donor = source[k]
    synthetic[k] = {
        **synthetic[k], "email": donor["email"],
        "member_no": donor["member_no"]
    }
  keys = frozenset({"id"})
  identity = ("email", "member_no")
  columns = kinds_from_schema(fields, keys=("id",), identity=identity)
  planned = apply_planning(
      columns,
      planning_stats(fields, source, keys),
      planning_stats(fields, synthetic, keys),
      budget=Budget(max_shuffle_gb=500.0, max_bytes_billed=1 << 40))
  table = table_plan(
      "members",
      planned,
      pk=("id",),
      identity=identity,
      panel=make_panel(source[:n_reference],
                       source[n_reference:2 * n_reference]),
      rows_source=n_source,
      rows_synthetic=n_synthetic)
  return table, {"source": source, "synthetic": synthetic}


def day(d: int) -> date:
  return date(2024, 1, 1) + timedelta(days=d)
