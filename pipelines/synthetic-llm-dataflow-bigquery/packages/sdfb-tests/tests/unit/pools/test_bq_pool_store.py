"""Row <-> FreeTextPool mapping and the BQ-backed store (WS5 T5).

Mirrors tests/unit/rag/test_bq_chunk_store.py: a fake client stands in for
google.cloud.bigquery so nothing here touches GCP.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,invalid-name,missing-class-docstring,protected-access,unused-variable,use-implicit-booleaness-not-comparison

from __future__ import annotations

from sdfb_beam.pools.store import BigQueryFreeTextPoolStore, pool_to_row, row_to_pool
from sdfb_core.pools import FreeTextPool


class _FakeBqClient:

  def __init__(self, rows: list[dict]) -> None:
    self.rows = rows
    self.queries: list[tuple[str, dict]] = []
    self.loads: list[tuple[list[dict], str, object]] = []
    self.load_results = 0

  def query(self, sql, job_config=None):
    params = {
        p.name: p.value
        for p in (job_config.query_parameters if job_config else [])
    }
    self.queries.append((sql, params))
    return self

  def result(self):
    return list(self.rows)

  def load_table_from_json(self, rows, table, job_config=None):
    self.loads.append((list(rows), table, job_config))
    client = self

    class _Job:

      def result(self):
        client.load_results += 1

    return _Job()


def _pool() -> FreeTextPool:
  return FreeTextPool(
      reference_digest="d1",
      model_uri="gs://b/m",
      column="notes",
      target=64,
      values=("alpha", "beta"),
      stagnated=True,
      attempts=9,
  )


def test_row_round_trip_preserves_every_field():
  assert row_to_pool(pool_to_row(_pool())) == _pool()


def test_values_persist_as_a_repeated_field_not_a_blob():
  """REPEATED STRING keeps the pool queryable in BQ (UNNEST) instead of
    hiding it inside a JSON string."""
  assert pool_to_row(_pool())["values"] == ["alpha", "beta"]


def test_fetch_maps_rows():
  client = _FakeBqClient([pool_to_row(_pool())])
  store = BigQueryFreeTextPoolStore(
      "p.synthetic_rag.freetext_pools", client=client)
  assert store.fetch("d1", "gs://b/m") == [_pool()]


def test_fetch_binds_digest_and_model_as_query_parameters():
  """Never string-interpolated — these come from run configuration."""
  client = _FakeBqClient([])
  store = BigQueryFreeTextPoolStore(
      "p.synthetic_rag.freetext_pools", client=client)
  store.fetch("d1", "gs://b/m")
  _sql, params = client.queries[0]
  assert params == {"reference_digest": "d1", "model_uri": "gs://b/m"}


def test_exists_is_false_on_empty_result():
  client = _FakeBqClient([])
  store = BigQueryFreeTextPoolStore(
      "p.synthetic_rag.freetext_pools", client=client)
  assert store.exists("d1", "gs://b/m") is False


def test_exists_is_true_when_a_row_comes_back():
  client = _FakeBqClient([{"n": 1}])
  store = BigQueryFreeTextPoolStore(
      "p.synthetic_rag.freetext_pools", client=client)
  assert store.exists("d1", "gs://b/m") is True


def test_select_quotes_reserved_identifiers():
  """`values` is a BigQuery RESERVED keyword — unquoted it is a syntax
    error against the real service, which no fake client can surface."""
  client = _FakeBqClient([])
  store = BigQueryFreeTextPoolStore(
      "p.synthetic_rag.freetext_pools", client=client)
  store.fetch("d1", "m")
  sql, _ = client.queries[0]
  assert "`values`" in sql
  assert " values," not in sql and " values " not in sql


def test_row_to_pool_tolerates_a_null_repeated_field():
  """BigQuery returns None, not [], for an empty REPEATED column."""
  row = pool_to_row(_pool())
  row["values"] = None
  assert row_to_pool(row).values == ()


def test_write_rows_uses_a_load_job_never_streaming_inserts():
  """2026-07-29 four-run postmortem: the branch now writes its own rows.
    A LOAD job keeps the rows out of the streaming buffer, so the digest
    DELETE between seeding arms (RUN_PLAYBOOK §6c) works immediately —
    insert_rows_json rows are undeletable for up to ~90 min."""
  client = _FakeBqClient([])
  store = BigQueryFreeTextPoolStore(
      "p.synthetic_rag.freetext_pools", client=client)
  rows = [pool_to_row(_pool())]
  store.write_rows(rows)
  assert client.loads, "write_rows must issue a load job"
  loaded_rows, table, job_config = client.loads[0]
  assert loaded_rows == rows
  assert table == "p.synthetic_rag.freetext_pools"
  assert job_config.write_disposition == "WRITE_APPEND"
  assert client.load_results == 1, "write_rows must block until the load lands"
  assert not getattr(client, "inserts", []), "streaming inserts are forbidden"


def test_in_memory_store_write_rows_round_trips_into_fetch():
  from sdfb_core.pools import InMemoryFreeTextPoolStore

  store = InMemoryFreeTextPoolStore()
  store.write_rows([pool_to_row(_pool())])
  assert store.fetch("d1", "gs://b/m") == [_pool()]


def test_store_pickles_even_after_the_lazy_client_materialized():
  """2026-07-29 R1 launch failure: the driver's exists() digest check
    materialized the real bigquery.Client inside the store, and the
    BuildFreeTextPoolsDoFn carrying that store died at graph-pickling time
    ("Pickling client objects is explicitly not supported"). The lazy
    client is a cache, not state — it must be dropped on pickle and
    rebuilt on demand."""
  import pickle

  class _RefusesPickling:
    """Mimics google.cloud.client.Client.__getstate__."""

    def __getstate__(self):
      raise pickle.PicklingError(
          "Pickling client objects is explicitly not supported.")

  store = BigQueryFreeTextPoolStore(
      "p.synthetic_rag.freetext_pools", client=_RefusesPickling())
  clone = pickle.loads(pickle.dumps(store))
  assert clone.table_fqn == "p.synthetic_rag.freetext_pools"
  assert clone._client is None, "the client cache must not survive pickling"


def test_delete_removes_a_digest_and_binds_parameters():
  """Taint-triggered rebuilds (2026-08-07 10M warm run replayed the
    memorized 2026-08-05 pools) must clear the old rows first — `fetch`
    has no per-column dedup, so appending rebuilt pools would leave the
    stale values racing the clean ones."""
  client = _FakeBqClient([])
  store = BigQueryFreeTextPoolStore(
      "p.synthetic_rag.freetext_pools", client=client)
  store.delete("d1", "gs://b/m")
  sql, params = client.queries[0]
  assert sql.startswith("DELETE FROM `p.synthetic_rag.freetext_pools`")
  assert params == {"reference_digest": "d1", "model_uri": "gs://b/m"}
