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
"""The R/E/H panel (`panel_sql`, `fetch_panel`): R must be EXACTLY the
generator's reference sample, H the next n rows of the same order.

The proof runs both queries — the generator's (`reference_sql`, pinned
byte-identical by the parity goldens) and the panel's — through one fake
BigQuery that understands exactly those two query shapes over an
invented table whose `FARM_FINGERPRINT(TO_JSON_STRING(ref))` values are
given explicitly (signed, as BigQuery's are).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from sdfb_evaluation.context.reference import (
    EXPOSURE_ROWS,
    REFERENCE_ORDER_BY,
    Panel,
    fetch_panel,
    panel_sql,
    reference_digest,
    reference_sql,
)
from sdfb_evaluation.context.scope import pin_source

TABLE = "demo-project.thelook_ecommerce.users"


def _user(uid: int, first: str, country: str) -> dict[str, Any]:
  return {
      "id": uid,
      "first_name": first,
      "email": f"{first.lower()}.{uid}@example.com",
      "country": country,
  }


# Storage order, then each row's (invented) fingerprint.
USERS = [
    _user(11, "Ava", "Brasil"),
    _user(12, "Wei", "China"),
    _user(13, "Noor", "Germany"),
    _user(14, "Liam", "Spain"),
    _user(15, "Mia", "Japan"),
    _user(16, "Omar", "France"),
    _user(17, "Sofia", "Brasil"),
    _user(18, "Yuki", "Japan"),
]
FINGERPRINTS = [912, -(2**63), 17, -5, 4_000_000_000, -77_777, 3, 250]
# Ascending signed order of the fingerprints above: 12, 16, 14, 17, 13, 18,
# 11, 15.
FP_ORDER = [12, 16, 14, 17, 13, 18, 11, 15]


def _to_json_string(row: Mapping[str, Any]) -> str:
  """BigQuery's `TO_JSON_STRING(ref)`: compact, in column order."""
  return json.dumps(dict(row), separators=(",", ":"))


_REFERENCE_RE = re.compile(r"SELECT \* FROM `(?P<table>[^`]+)` AS ref  "
                           r"ORDER BY (?P<order>.+) LIMIT (?P<limit>\d+)")
_PANEL_RE = re.compile(
    r"SELECT ref\.\*, ROW_NUMBER\(\) OVER \(ORDER BY (?P<rank>.+?)\) "
    r"AS __sdfb_rk FROM \(SELECT \* FROM (?P<source>.+) AS ref "
    r"ORDER BY (?P<order>.+) LIMIT (?P<limit>\d+)\) AS ref ORDER BY __sdfb_rk")


class _FingerprintBq:
  """BigQuery reduced to the two reference-sample query shapes.

  `ORDER BY FARM_FINGERPRINT(TO_JSON_STRING(ref))` sorts by the invented
  fingerprint of each row's JSON text; equal fingerprints (duplicate
  rows) keep storage order, or the reverse with `reverse_ties` — BigQuery
  promises neither.
  """

  def __init__(self,
               rows: Sequence[dict[str, Any]],
               fingerprints: Sequence[int],
               *,
               reverse_ties: bool = False):
    self.rows = [dict(r) for r in rows]
    self.fingerprint = {
        _to_json_string(r): fp
        for r, fp in zip(rows, fingerprints, strict=True)
    }
    self.reverse_ties = reverse_ties
    self.queries: list[str] = []

  def _ordered(
      self,
      order: str,
      rows: Sequence[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    assert order == REFERENCE_ORDER_BY, order
    if rows is None:
      rows = self.rows[::-1] if self.reverse_ties else self.rows
    return sorted(rows, key=lambda r: self.fingerprint[_to_json_string(r)])

  def query(self,
            sql: str,
            params: Mapping[str, Any] | None = None,
            *,
            max_bytes: int | None = None) -> list[dict]:
    del max_bytes
    assert not params, "the reference-sample queries bind nothing"
    self.queries.append(sql)
    ref = _REFERENCE_RE.fullmatch(sql)
    if ref:
      assert ref["table"] == TABLE
      return [dict(r) for r in self._ordered(ref["order"])[:int(ref["limit"])]]
    panel = _PANEL_RE.fullmatch(sql)
    assert panel, f"unexpected SQL {sql!r}"
    assert panel["source"] in (f"`{TABLE}`",) or panel["source"].startswith(
        f"(SELECT * FROM `{TABLE}` FOR SYSTEM_TIME AS OF TIMESTAMP ")
    top = self._ordered(panel["order"])[:int(panel["limit"])]
    # The outer ROW_NUMBER ranks the top rows by the SAME expression.
    ranked = self._ordered(panel["rank"], top)
    return [{**r, "__sdfb_rk": i} for i, r in enumerate(ranked, start=1)]


def test_order_by_expression_is_shared(load_fixture):
  assert REFERENCE_ORDER_BY == "FARM_FINGERPRINT(TO_JSON_STRING(ref))"
  golden = load_fixture("parity/goldens.json")["reference_sql"]["sql"]
  assert f"ORDER BY {REFERENCE_ORDER_BY} LIMIT" in golden
  assert f"ORDER BY {REFERENCE_ORDER_BY} LIMIT 5" in reference_sql(TABLE, 5)
  sql = panel_sql(TABLE, 5)
  assert f"OVER (ORDER BY {REFERENCE_ORDER_BY})" in sql
  assert f"ORDER BY {REFERENCE_ORDER_BY} LIMIT 10)" in sql


def test_panel_sql_orders_like_generator():
  bq = _FingerprintBq(USERS, FINGERPRINTS)
  assert [r["id"] for r in bq.query(reference_sql(TABLE, 8))] == FP_ORDER
  for n in (1, 2, 3, 4):
    sql = panel_sql(TABLE, n)
    assert REFERENCE_ORDER_BY in sql
    rows = bq.query(sql)
    ranks = [r["__sdfb_rk"] for r in rows]
    assert ranks == list(range(1, min(2 * n, len(USERS)) + 1))
    assert max(ranks) <= 2 * n
    stripped = [{k: v for k, v in r.items() if k != "__sdfb_rk"} for r in rows]
    # R = ranks 1..n is exactly the generator's LIMIT n sample ...
    assert stripped[:n] == bq.query(reference_sql(TABLE, n))
    # ... and H = ranks n+1..2n the next n rows of the same order.
    assert stripped[n:] == bq.query(reference_sql(TABLE, 2 * n))[n:]


def test_fetch_panel_splits_r_and_h_and_verifies_the_digest():
  bq = _FingerprintBq(USERS, FINGERPRINTS)
  generator_rows = bq.query(reference_sql(TABLE, 3))
  panel = fetch_panel(
      bq,
      source_read_table=TABLE,
      n=3,
      expected_digest=reference_digest(generator_rows))
  assert isinstance(panel, Panel)
  assert [r["id"] for r in panel.r_rows] == FP_ORDER[:3]
  assert [r["id"] for r in panel.h_rows] == FP_ORDER[3:6]
  assert all("__sdfb_rk" not in r for r in panel.r_rows + panel.h_rows)
  assert panel.r_rows == generator_rows
  assert panel.verified is True
  assert panel.digest == panel.expected_digest == reference_digest(
      generator_rows)
  assert panel.reason is None
  assert (panel.e_n, panel.he_n) == (3, 3)


def test_reference_digest_mismatch_unverified():
  bq = _FingerprintBq(USERS, FINGERPRINTS)
  # The generator saw a different source state (one row changed since).
  seen = bq.query(reference_sql(TABLE, 3))
  seen[0] = {**seen[0], "country": "Portugal"}
  recorded = reference_digest(seen)
  panel = fetch_panel(
      bq, source_read_table=TABLE, n=3, expected_digest=recorded)
  assert panel.verified is False
  assert panel.expected_digest == recorded
  assert panel.digest == reference_digest(bq.query(reference_sql(TABLE, 3)))
  assert panel.digest != recorded
  assert "mismatch" in panel.reason
  # Nothing recorded: not verified either, and it says why.
  unknown = fetch_panel(bq, source_read_table=TABLE, n=3, expected_digest=None)
  assert unknown.verified is False and unknown.expected_digest is None
  assert "no reference digest" in unknown.reason


def test_duplicate_rows_at_the_r_h_boundary_leave_the_digest_unchanged():
  # Rows 3 and 4 are identical, so they tie on the fingerprint and
  # straddle rank n = 3: either copy may land in R. They are
  # interchangeable, so R's digest cannot tell which one did.
  dup = _user(13, "Noor", "Germany")
  rows = [_user(11, "Ava", "Brasil"), _user(12, "Wei", "China"), dup, dup]
  fps = [1, 2, 5, 5]
  first = fetch_panel(
      _FingerprintBq(rows, fps),
      source_read_table=TABLE,
      n=3,
      expected_digest=None)
  second = fetch_panel(
      _FingerprintBq(rows, fps, reverse_ties=True),
      source_read_table=TABLE,
      n=3,
      expected_digest=None)
  assert first.digest == second.digest
  assert len(first.h_rows) == 1


def test_panel_exposure_counts_cap_at_the_row_doc_limit():
  n = EXPOSURE_ROWS + 6
  rows = [{"id": i, "__sdfb_rk": i} for i in range(1, n + 12)]

  class _Bq:

    def query(self, sql, params=None, *, max_bytes=None):
      del sql, params, max_bytes
      return [dict(r) for r in rows]

  panel = fetch_panel(_Bq(), source_read_table=TABLE, n=n, expected_digest=None)
  assert len(panel.r_rows) == n and len(panel.h_rows) == 11
  assert (panel.e_n, panel.he_n) == (EXPOSURE_ROWS, 11)
  assert EXPOSURE_ROWS == 1024


def test_fetch_panel_passes_the_byte_cap():
  seen = []

  class _Bq:

    def query(self, sql, params=None, *, max_bytes=None):
      seen.append((sql, params, max_bytes))
      return [{"id": 1, "__sdfb_rk": 1}]

  fetch_panel(
      _Bq(),
      source_read_table=TABLE,
      n=1,
      expected_digest=None,
      max_bytes=5_000_000_000)
  assert seen == [(panel_sql(TABLE, 1), {}, 5_000_000_000)]


def _pin(**overrides):
  kwargs = {
      "source_table": TABLE,
      "job_create_time": "2026-09-13T13:10:16.512345Z",
      "now": "2026-09-14T12:00:00Z",
      "time_travel_hours": 168,
      "temp_dataset": "demo-project.sdfb_eval_tmp",
      "evaluation_id": "ev_20260914_0001",
  }
  kwargs.update(overrides)
  return pin_source(**kwargs)


def test_panel_reads_the_pinned_source():
  pin = _pin()
  sql = panel_sql(pin, 2)
  assert f"FROM {pin.read_expr} AS ref ORDER BY" in sql
  bq = _FingerprintBq(USERS, FINGERPRINTS)
  panel = fetch_panel(bq, source_read_table=pin, n=2, expected_digest=None)
  assert [r["id"] for r in panel.r_rows] == FP_ORDER[:2]
  # The materialized snapshot (after prepare_sql ran) reads the same way.
  assert f"FROM `{pin.read_table}` AS ref" in panel_sql(pin.read_table, 2)


def test_panel_reads_an_unpinned_source_through_its_read_expr():
  pin = _pin(job_create_time=None)
  assert not pin.pinned and pin.read_expr == f"`{TABLE}`"
  assert panel_sql(pin, 2) == panel_sql(TABLE, 2)
  bq = _FingerprintBq(USERS, FINGERPRINTS)
  panel = fetch_panel(bq, source_read_table=pin, n=2, expected_digest=None)
  assert [r["id"] for r in panel.r_rows] == FP_ORDER[:2]


@pytest.mark.parametrize("source", [
    "(SELECT 1)",
    "(SELECT * FROM `demo-project.x.users` FOR SYSTEM_TIME AS OF "
    "TIMESTAMP '2026-09-13T13:10:16Z'; DROP TABLE x --')",
    "users",
])
def test_panel_rejects_text_that_is_not_a_table_name(source):
  with pytest.raises(ValueError):
    panel_sql(source, 2)


@pytest.mark.parametrize("n", [0, -3, True, 2.0])
def test_panel_rejects_a_bad_n(n):
  with pytest.raises(ValueError):
    panel_sql(TABLE, n)


def test_fetch_panel_rejects_malformed_ranks():

  class _Bq:

    def __init__(self, rows):
      self.rows = rows

    def query(self, sql, params=None, *, max_bytes=None):
      del sql, params, max_bytes
      return [dict(r) for r in self.rows]

  duplicated = [{"id": 1, "__sdfb_rk": 1}, {"id": 2, "__sdfb_rk": 1}]
  gap = [{"id": 1, "__sdfb_rk": 1}, {"id": 2, "__sdfb_rk": 3}]
  missing = [{"id": 1}]
  beyond = [{"id": i, "__sdfb_rk": i} for i in range(1, 6)]
  for rows in (duplicated, gap, missing, beyond):
    with pytest.raises(ValueError):
      fetch_panel(_Bq(rows), source_read_table=TABLE, n=2, expected_digest=None)
