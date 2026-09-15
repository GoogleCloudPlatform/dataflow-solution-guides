"""BigQuerySourceStatsStore + driver stats emission (Task 15)."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=protected-access

import json
import pickle
from types import SimpleNamespace
from unittest.mock import MagicMock

from sdfb_beam.cli.preflight import PreflightResult
from sdfb_beam.cli.run_pipeline import _emit_source_stats
from sdfb_beam.io.stats_store import BigQuerySourceStatsStore
from sdfb_core.contracts import TableSchema


def test_exists_and_write_rows_use_load_jobs():
  client = MagicMock()
  client.query.return_value.result.return_value = [1]
  store = BigQuerySourceStatsStore(
      "p.synthetic_rag.source_table_stats", client=client)
  assert store.exists("p.d.t", "digest1") is True
  store.write_rows([{"table_fqn": "p.d.t"}])
  client.load_table_from_json.assert_called_once()
  assert client.insert_rows_json.call_count == 0  # never streaming


def test_exists_versioned_skip_key_filters():
  """profiler_version/stats_tier must reach the WHERE clause: the digest
    hashes reference rows only, so without these filters an upgraded
    profiler (or the exact tier) is skipped forever (ADR 0022)."""
  client = MagicMock()
  client.query.return_value.result.return_value = []
  store = BigQuerySourceStatsStore(
      "p.synthetic_rag.source_table_stats", client=client)
  assert store.exists(
      "p.d.t", "digest1", profiler_version="2", stats_tier="exact") is False
  sql = client.query.call_args[0][0]
  assert "`profiler_version` = @profiler_version" in sql
  assert "`stats_tier` = @stats_tier" in sql
  params = {
      p.name: p.value
      for p in client.query.call_args[1]["job_config"].query_parameters
  }
  assert params["profiler_version"] == "2"
  assert params["stats_tier"] == "exact"


def test_exists_without_filters_keeps_legacy_shape():
  client = MagicMock()
  client.query.return_value.result.return_value = [1]
  store = BigQuerySourceStatsStore("p.d.s", client=client)
  assert store.exists("p.d.t", "digest1") is True
  sql = client.query.call_args[0][0]
  assert "profiler_version" not in sql
  assert "stats_tier" not in sql


def test_pickle_drops_the_client():
  store = BigQuerySourceStatsStore("p.d.s", client=MagicMock())
  restored = pickle.loads(pickle.dumps(store))
  assert restored._client is None
  assert restored.table_fqn == "p.d.s"


def _schema() -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t"
      },
      "schema": [{
          "name": "NOTES",
          "type": "STRING",
          "mode": "NULLABLE"
      }],
  })


def test_emit_source_stats_writes_json(tmp_path):
  out = tmp_path / "stats.json"
  args = SimpleNamespace(
      source_stats="sample",
      source_stats_json=str(out),
      source_stats_table="",
      run_id="r1",
  )
  rows = [{
      "NOTES": ""
  }] * 4 + [{
      "NOTES": f"LONG VALUE NUMBER {i} PADDED OUT WELL"
  } for i in range(60)]
  _emit_source_stats(args, _schema(), rows, PreflightResult((), (), None))
  payload = json.loads(out.read_text())
  assert payload["NOTES"]["empty_fraction"] == round(4 / 64, 6)


def test_emit_source_stats_off_is_noop(tmp_path):
  out = tmp_path / "stats.json"
  args = SimpleNamespace(
      source_stats="off",
      source_stats_json=str(out),
      source_stats_table="",
      run_id="r1",
  )
  _emit_source_stats(args, _schema(), [{
      "NOTES": "x"
  }], PreflightResult((), (), None))
  assert not out.exists()
