"""Unit tests for `sdfb_beam.io.bq_sources.load_reference_rows`."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from sdfb_beam.io import load_reference_rows


def _row(d: dict) -> MagicMock:
  """A MagicMock that quacks like `google.cloud.bigquery.Row`."""
  r = MagicMock()
  r.items.return_value = list(d.items())
  return r


def test_returns_plain_dicts():
  client = MagicMock()
  client.query.return_value.result.return_value = iter(
      [_row({
          "customer_id": 1,
          "email": "a@example.com"
      })])
  out = load_reference_rows(table="p.d.t", limit=5, client=client)
  assert out == [{"customer_id": 1, "email": "a@example.com"}]


def test_preserves_native_python_types():
  """Datetime, etc. round-trip without stringification."""
  client = MagicMock()
  ts = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
  client.query.return_value.result.return_value = iter(
      [_row({
          "id": 1,
          "signup_at": ts
      })])
  out = load_reference_rows(table="p.d.t", limit=1, client=client)
  assert out[0]["signup_at"] is ts


def test_query_uses_limit_and_table():
  client = MagicMock()
  client.query.return_value.result.return_value = iter([])
  load_reference_rows(table="proj.ds.tbl", limit=42, client=client)
  sent_query = client.query.call_args[0][0]
  assert "`proj.ds.tbl`" in sent_query
  assert "LIMIT 42" in sent_query
  assert "WHERE" not in sent_query


def test_query_spreads_sample_deterministically():
  # A bare `LIMIT n` returns a storage-contiguous slice (the 2026-07-15
  # run sampled essentially one load batch: 51 % of rows shared a single
  # load date). The sample must be spread across the whole table, and
  # deterministically so — same table contents ⇒ same sample ⇒ stable
  # reference_digest.
  client = MagicMock()
  client.query.return_value.result.return_value = iter([])
  load_reference_rows(table="proj.ds.tbl", limit=42, client=client)
  sent_query = client.query.call_args[0][0]
  assert "ORDER BY FARM_FINGERPRINT(TO_JSON_STRING(ref))" in sent_query
  assert sent_query.index("ORDER BY") < sent_query.index("LIMIT")


def test_extra_filters_become_where_clause():
  client = MagicMock()
  client.query.return_value.result.return_value = iter([])
  load_reference_rows(
      table="proj.ds.tbl",
      limit=5,
      extra_filters="tier = 'ENTERPRISE'",
      client=client,
  )
  sent_query = client.query.call_args[0][0]
  assert "WHERE tier = 'ENTERPRISE'" in sent_query
  # WHERE filters first, then the deterministic spread, then the cap.
  assert sent_query.index("WHERE") < sent_query.index("ORDER BY")


def test_returns_empty_list_when_no_rows():
  client = MagicMock()
  client.query.return_value.result.return_value = iter([])
  out = load_reference_rows(table="p.d.t", limit=10, client=client)
  assert out == []
