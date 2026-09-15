"""One-scan exact source stats (--source_stats=exact, ADR 0022)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from sdfb_beam.cli.preflight import PreflightResult
from sdfb_beam.cli.run_pipeline import _emit_source_stats
from sdfb_beam.io.exact_stats import build_exact_stats_sql, compute_exact_stats
from sdfb_core.contracts.schema import TableSchema
from sdfb_core.stats import profile_source_table

_SCHEMA = TableSchema.model_validate({
    "table_info": {
        "table_id": "p.d.src"
    },
    "schema": [
        {
            "name": "STATUS",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "AMOUNT",
            "type": "FLOAT64",
            "mode": "NULLABLE"
        },
        {
            "name": "NOTES",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "GEO",
            "type": "GEOGRAPHY",
            "mode": "NULLABLE"
        },
    ],
})


def _rows() -> list[dict]:
  return [{
      "STATUS": "OPEN" if i % 2 else "CLOSED",
      "AMOUNT": float(i),
      "NOTES": f"UNIQUE LONG CUSTOMER NOTE NUMBER {i:04d} WITH DETAIL",
      "GEO": "POINT(0 0)",
  } for i in range(60)]


def _tier1() -> dict[str, dict]:
  return profile_source_table(_SCHEMA, _rows())


def _exact_row() -> dict:
  # Aliases are positional over castable columns: STATUS=c0, AMOUNT=c1,
  # NOTES=c2; GEOGRAPHY is skipped entirely.
  return {
      "total_rows": 1_000_000,
      "c0_null": 0,
      "c0_empty": 0,
      "c0_distinct": 2,
      "c0_top": '[{"value": "OPEN", "count": 700000}, '
                '{"value": "CLOSED", "count": 300000}]',
      "c1_null": 500_000,
      "c1_empty": 0,
      "c1_distinct": 90_000,
      "c1_q": [0.0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 1000.0],
      "c1_avg": 12.5,
      "c1_sd": 99.0,
      "c2_null": 0,
      "c2_empty": 900_000,
      "c2_distinct": 73_231,
  }


def _client(row: dict) -> MagicMock:
  client = MagicMock()
  client.query.return_value.result.return_value = [row]
  return client


def test_sql_is_one_scan_with_privacy_gate():
  sql = build_exact_stats_sql("p.d.src", _SCHEMA, _tier1())
  assert sql.count("FROM `p.d.src`") == 1
  assert "APPROX_COUNT_DISTINCT" in sql
  assert sql.count("APPROX_QUANTILES") == 1  # AMOUNT only
  # STATUS (2 distinct, enum-routed) gets literals; NOTES (60 distinct,
  # above the 50 gate) must not leak literal values.
  assert sql.count("APPROX_TOP_COUNT") == 1
  assert "c0_top" in sql and "c2_top" not in sql
  assert "GEO" not in sql  # non-castable → stays Tier 1


def test_merge_overrides_with_exact_and_keeps_skipped_sample():
  client = _client(_exact_row())
  merged = compute_exact_stats("p.d.src", _SCHEMA, _tier1(), client=client)
  assert client.query.call_count == 1
  notes = merged["NOTES"]
  assert notes["stats_tier"] == "exact"
  assert notes["distinct"] == 73_231
  assert notes["sample_rows"] == 1_000_000
  assert notes["empty_fraction"] == 0.9
  amount = merged["AMOUNT"]
  assert amount["deciles"][-1] == 1000.0
  assert amount["mean"] == 12.5
  assert amount["null_fraction"] == 0.5
  status = merged["STATUS"]
  assert status["top_values"] == [["OPEN", 0.7], ["CLOSED", 0.3]]
  # Skipped column and the pseudo-column stay honestly sample-tier.
  assert merged["GEO"]["stats_tier"] == "sample"
  assert merged["__table__"]["stats_tier"] == "sample"


def _args(mode: str) -> SimpleNamespace:
  return SimpleNamespace(
      source_stats=mode,
      source_stats_json="",
      source_stats_table="",
      run_id="r1",
      reference_table="p.d.src",
  )


def test_emit_exact_returns_pool_sizing_hints(monkeypatch):
  merged = {
      "NOTES": {
          "distinct": 73_231,
          "stats_tier": "exact",
          "empty_fraction": 0.9,
          "generation_plan": ""
      },
      "__table__": {
          "distinct": 2,
          "stats_tier": "sample",
          "empty_fraction": 0.0,
          "generation_plan": ""
      },
  }
  monkeypatch.setattr(
      "sdfb_beam.io.exact_stats.compute_exact_stats",
      lambda *a, **k: merged,
  )
  out = _emit_source_stats(
      _args("exact"), _SCHEMA, _rows(), PreflightResult((), (), None))
  assert out == {"NOTES": 73_231}  # sample-tier pseudo row excluded


def test_emit_exact_degrades_loudly_not_fatally(monkeypatch):

  def _boom(*a, **k):
    raise RuntimeError("quota")

  monkeypatch.setattr("sdfb_beam.io.exact_stats.compute_exact_stats", _boom)
  out = _emit_source_stats(
      _args("exact"), _SCHEMA, _rows(), PreflightResult((), (), None))
  assert out == {}  # run continues on Tier-1 stats, no pool hints
