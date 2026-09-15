"""Identity-column synthesis contract tests.

The 2026-07 E2E report found identity/PK columns copied verbatim from the
source table (copy_ratio=1.0 — a re-identification leak). These tests pin
`synthesize_identity_value` / `apply_identity_columns` so identity columns
are always per-row-unique and never sampled from reference data.
"""

from __future__ import annotations

import uuid

from sdfb_core.engines.identity import (
    apply_identity_columns,
    synthesize_identity_value,
)


def test_string_identity_is_valid_uuid_and_deterministic():
  v1 = synthesize_identity_value("STRING", "run-a", 0, 0, "customer_id")
  v2 = synthesize_identity_value("STRING", "run-a", 0, 0, "customer_id")
  assert v1 == v2
  uuid.UUID(v1)  # raises if not UUID-shaped


def test_rows_and_batches_and_columns_are_unique():
  vals = {
      synthesize_identity_value("STRING", "run-a", b, r, c)
      for b in range(3)
      for r in range(50)
      for c in ("id_a", "id_b")
  }
  assert len(vals) == 3 * 50 * 2


def test_integer_identity_is_int():
  v = synthesize_identity_value("INTEGER", "run-a", 1, 2, "seq_id")
  assert isinstance(v, int) and v >= 0


def test_string_identity_respects_max_length_below_uuid_width():
  """A STRING identity column with `max_length < 36` (the UUIDv4 string
    width) must not overflow the column — return a `max_length`-char
    deterministic hex slice instead of the full UUID."""
  v = synthesize_identity_value(
      "STRING", "run-a", 0, 0, "short_id", max_length=10)
  assert len(v) == 10
  v2 = synthesize_identity_value(
      "STRING", "run-a", 0, 0, "short_id", max_length=10)
  assert v == v2  # deterministic


def test_string_identity_max_length_stays_unique_across_rows():
  vals = {
      synthesize_identity_value(
          "STRING", "run-a", 0, r, "short_id", max_length=10) for r in range(50)
  }
  assert len(vals) == 50


def test_string_identity_no_max_length_is_unchanged_uuid():
  v = synthesize_identity_value("STRING", "run-a", 0, 0, "customer_id")
  uuid.UUID(v)
  assert len(v) == 36


def test_string_identity_max_length_none_explicit_is_unchanged_uuid():
  v = synthesize_identity_value(
      "STRING", "run-a", 0, 0, "customer_id", max_length=None)
  uuid.UUID(v)


def test_string_identity_max_length_at_or_above_uuid_width_is_unchanged():
  """max_length >= 36 has no reason to truncate — the UUID already fits."""
  v = synthesize_identity_value(
      "STRING", "run-a", 0, 0, "customer_id", max_length=36)
  uuid.UUID(v)


def test_integer_identity_unaffected_by_max_length():
  v = synthesize_identity_value(
      "INTEGER", "run-a", 1, 2, "seq_id", max_length=5)
  assert isinstance(v, int) and v >= 0


def test_apply_identity_columns_forwards_column_max_lengths():
  record = {"short_id": "LEAKED", "amount": 42}
  out = apply_identity_columns(
      record,
      identity_columns=["short_id"],
      column_types={"short_id": "STRING"},
      column_max_lengths={"short_id": 10},
      run_id="run-a",
      batch_id=0,
      row_index=0,
  )
  assert len(out["short_id"]) == 10
  assert out["short_id"] != "LEAKED"


def test_apply_identity_columns_default_max_lengths_is_empty():
  """`column_max_lengths` defaults to `{}` — callers that don't pass it
    keep today's full-UUID behavior."""
  record = {"customer_id": "LEAKED"}
  out = apply_identity_columns(
      record,
      identity_columns=["customer_id"],
      column_types={"customer_id": "STRING"},
      run_id="run-a",
      batch_id=0,
      row_index=0,
  )
  uuid.UUID(out["customer_id"])


def test_apply_overwrites_only_identity_columns():
  record = {"customer_id": "LEAKED-REAL-VALUE", "amount": 42}
  out = apply_identity_columns(
      record,
      identity_columns=["customer_id"],
      column_types={
          "customer_id": "STRING",
          "amount": "INTEGER"
      },
      run_id="run-a",
      batch_id=0,
      row_index=0,
  )
  assert out["amount"] == 42
  assert out["customer_id"] != "LEAKED-REAL-VALUE"
  uuid.UUID(out["customer_id"])
