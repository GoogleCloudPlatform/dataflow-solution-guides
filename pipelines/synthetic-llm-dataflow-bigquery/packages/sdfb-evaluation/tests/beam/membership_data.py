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
"""Test tables for the row-membership pass: an invented high-entropy
`people` table (a unique surname per row, so chance matches are nil) and
an invented low-entropy `catalog` table whose rows are drawn from a small
space of (category, brand, size) patterns, so R and H both collide with
the synthetic side by chance. Both are planned by the real planner.

Nothing here is real data: invented names, `example.com` e-mails, ids in
invented ranges (source 7_000_000+, synthetic 5_000_000+).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

import numpy as np

from sdfb_evaluation.beam.encode import BatchEncoder, EncodedBatch
from sdfb_evaluation.context.budget import Budget
from sdfb_evaluation.context.offline import planning_stats
from sdfb_evaluation.context.plan import (
    TablePlan,
    apply_planning,
    kinds_from_schema,
)
from sdfb_evaluation.context.reference import Panel

from .tables import make_panel, table_plan

SALT = "m3mb" * 8
LABEL_KEY = b"membership-test-label-key-012345"
SOURCE_ID_BASE = 7_000_000
SYNTHETIC_ID_BASE = 5_000_000
_BUDGET = Budget(max_shuffle_gb=500.0, max_bytes_billed=1 << 40)

PEOPLE_FIELDS: tuple[dict[str, str], ...] = (
    {
        "name": "person_id",
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
        "name": "surname",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "city",
        "type": "STRING",
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
        "name": "balance",
        "type": "FLOAT64",
        "mode": "NULLABLE"
    },
)
# `balance` (a cent amount up to 10k) keeps chance near matches negligible:
# two rows agreeing on every non-key column but one are copies, not luck
PEOPLE_NONKEY = ("first_name", "surname", "city", "age", "signup_date",
                 "balance")
PEOPLE_IDENTITY = ("email",)  # unique but not a key: outside the content
_FIRST = ("Ada", "Bo", "Cy", "Di", "Ed", "Flo", "Gus", "Hal", "Ivy", "Jo")
_CITIES = tuple(f"Town{k:03d}" for k in range(60))

CATALOG_FIELDS: tuple[dict[str, str], ...] = (
    {
        "name": "item_id",
        "type": "INT64",
        "mode": "REQUIRED"
    },
    {
        "name": "category",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "brand",
        "type": "STRING",
        "mode": "NULLABLE"
    },
    {
        "name": "size",
        "type": "STRING",
        "mode": "NULLABLE"
    },
)
CATALOG_NONKEY = ("category", "brand", "size")
CATALOG_PATTERNS = 10 * 100 * 10  # category x brand x size


def planned(name: str,
            fields: Sequence[Mapping[str, Any]],
            source: Sequence[Mapping[str, Any]],
            synthetic: Sequence[Mapping[str, Any]],
            *,
            pk: Sequence[str] = (),
            identity: Sequence[str] = (),
            panel: Panel | None = None) -> TablePlan:
  """A `TablePlan` planned from both sides' rows (keys as identifiers)."""
  keys = frozenset(pk)
  columns = kinds_from_schema(fields, keys=pk, identity=identity)
  plans = apply_planning(
      columns,
      planning_stats(fields, source, keys),
      planning_stats(fields, synthetic, keys),
      budget=_BUDGET)
  return table_plan(
      name,
      plans,
      pk=pk,
      identity=identity,
      panel=panel,
      rows_source=len(source),
      rows_synthetic=len(synthetic))


def panel_of(source: Sequence[dict],
             n_reference: int,
             *,
             e_n: int | None = None,
             verified: bool = True) -> Panel:
  """R = the first `n_reference` source rows, H the next as many; E and
  H_E their first `e_n` rows (default: all of R, up to 1024)."""
  panel = make_panel(source[:n_reference], source[n_reference:2 * n_reference])
  if e_n is not None:
    panel = dataclasses.replace(
        panel,
        e_n=min(e_n, len(panel.r_rows)),
        he_n=min(e_n, len(panel.h_rows)))
  if not verified:
    panel = dataclasses.replace(
        panel, verified=False, reason="reference digest mismatch (test)")
  return panel


# --------------------------------------------------------------------------
# people: high entropy (a unique surname per row)
# --------------------------------------------------------------------------
def person(rng: np.random.Generator, i: int, id_base: int) -> dict[str, Any]:
  pid = id_base + i
  return {
      "person_id":
          pid,
      "email":
          f"p{pid}@example.com",
      "first_name":
          str(rng.choice(_FIRST)),
      "surname":
          f"Zq{pid:08d}",
      "city":
          None if rng.random() < 0.05 else str(rng.choice(_CITIES)),
      "age":
          int(rng.integers(18, 90)),
      "signup_date":
          date(2023, 1, 1) + timedelta(days=int(rng.integers(0, 700))),
      "balance":
          round(float(rng.uniform(0.0, 10_000.0)), 2),
  }


def people_rows(n: int, seed: int, id_base: int) -> list[dict[str, Any]]:
  rng = np.random.default_rng(seed)
  return [person(rng, i, id_base) for i in range(n)]


def copy_content(donor: Mapping[str, Any],
                 target: Mapping[str, Any],
                 columns: Sequence[str] = PEOPLE_NONKEY) -> dict[str, Any]:
  """`target` (its keys kept) carrying `donor`'s content columns: a
  verbatim copy of a record under a fresh key."""
  return {**target, **{c: donor[c] for c in columns}}


def people_table(
    n_source: int = 3000,
    n_synthetic: int = 2000,
    n_reference: int = 600,
    *,
    e_n: int | None = None,
    verified: bool = True,
    identity: Sequence[str] = PEOPLE_IDENTITY) -> tuple[TablePlan, dict]:
  """A clean people table: synthetic rows drawn afresh (no copies)."""
  source = people_rows(n_source, 31, SOURCE_ID_BASE)
  synthetic = people_rows(n_synthetic, 32, SYNTHETIC_ID_BASE)
  panel = panel_of(source, n_reference, e_n=e_n, verified=verified)
  table = planned(
      "people",
      PEOPLE_FIELDS,
      source,
      synthetic,
      pk=("person_id",),
      identity=identity,
      panel=panel)
  return table, {"source": source, "synthetic": synthetic}


# --------------------------------------------------------------------------
# catalog: low entropy (patterns over category x brand x size)
# --------------------------------------------------------------------------
def pattern_row(pattern: int, item_id: int) -> dict[str, Any]:
  return {
      "item_id": item_id,
      "category": f"cat{pattern % 10}",
      "brand": f"brand{(pattern // 10) % 100:03d}",
      "size": f"s{pattern // 1000}",
  }


def zipf_weights(k: int, s: float) -> np.ndarray:
  w = 1.0 / np.arange(1, k + 1)**s
  return np.asarray(w / w.sum())


def catalog_table(patterns_source: Sequence[int],
                  patterns_synthetic: Sequence[int],
                  n_reference: int,
                  *,
                  e_n: int | None = None) -> tuple[TablePlan, dict]:
  """The catalog table from explicit pattern ids (fresh ids per side)."""
  source = [
      pattern_row(int(p), SOURCE_ID_BASE + i)
      for i, p in enumerate(patterns_source)
  ]
  synthetic = [
      pattern_row(int(p), SYNTHETIC_ID_BASE + i)
      for i, p in enumerate(patterns_synthetic)
  ]
  table = planned(
      "catalog",
      CATALOG_FIELDS,
      source,
      synthetic,
      pk=("item_id",),
      panel=panel_of(source, n_reference, e_n=e_n))
  return table, {"source": source, "synthetic": synthetic}


# --------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------
def encode(table: TablePlan,
           side: str,
           rows: Sequence[Mapping[str, Any]],
           *,
           chunk: int = 997) -> list[EncodedBatch]:
  """`rows` of one side as encoded batches, as `EncodeSide` makes them."""
  encoder = BatchEncoder.from_table(table, side, salt=SALT)
  return [
      encoder.encode(rows[start:start + chunk])
      for start in range(0, len(rows), chunk)
  ]


def encode_all(table: TablePlan,
               rows_by: Mapping[str, Sequence[Mapping[str, Any]]],
               *,
               chunk: int = 997) -> list[EncodedBatch]:
  return [
      batch for side, rows in rows_by.items()
      for batch in encode(table, side, rows, chunk=chunk)
  ]
