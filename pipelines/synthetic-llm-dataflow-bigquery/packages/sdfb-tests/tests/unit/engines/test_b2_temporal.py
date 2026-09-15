"""WS1: temporal conversion + novel-range sampling for b2_library.

The 2026-07-20 E2E run landed 8 high-cardinality temporal columns at
copy_ratio=1.0 because b2 resampled the observed value table verbatim.
These tests pin the replacement: epoch round-trips per value type and a
seeded uniform sampler that stays inside the observed [min, max].
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=protected-access

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import pandas as pd
from sdfb_beam.handlers.fake_client import FakeModelClient
from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationConfig, GenerationContext
from sdfb_core.engines.b2_library import B2LibraryEngine
from sdfb_core.engines.b2_library.backends import (
    EmpiricalBackend,
    SdgxBackend,
    _samplable_profiles,
)
from sdfb_core.engines.b2_library.fidelity import (
    ColumnKind,
    ColumnProfile,
    _representative,
    enforce_value,
    profile_table,
)
from sdfb_core.engines.b2_library.temporal import (
    VT_DATE,
    VT_DATETIME,
    VT_DATETIME_UTC,
    VT_STR,
    VT_TIME,
    classify_temporal_values,
    from_epoch,
    sample_temporal,
    to_epoch,
)


def test_classify_date_strings():
  values = [f"2024-03-{d:02d}" for d in range(1, 25)]
  assert classify_temporal_values(values) == (VT_STR, "%Y-%m-%d")


def test_classify_native_types():
  aware = [
      datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
      datetime(2024, 6, 1, tzinfo=UTC)
  ]
  naive = [datetime(2024, 1, 1, 12, 0), datetime(2024, 6, 1)]
  dates = [date(2024, 1, 1), date(2024, 6, 1)]
  times = [time(9, 30), time(17, 45)]
  assert classify_temporal_values(aware) == (VT_DATETIME_UTC, None)
  assert classify_temporal_values(naive) == (VT_DATETIME, None)
  assert classify_temporal_values(dates) == (VT_DATE, None)
  assert classify_temporal_values(times) == (VT_TIME, None)


def test_classify_rejects_non_temporal_and_mixed():
  assert classify_temporal_values(["hello", "world"]) is None
  assert classify_temporal_values(["2024-01-01", "not a date"]) is None
  assert classify_temporal_values([datetime(2024, 1, 1),
                                   date(2024, 1, 2)]) is None
  assert classify_temporal_values([]) is None


def test_epoch_round_trip_every_value_type():
  cases = [
      (datetime(2024, 3, 5, 6, 7, 8, tzinfo=UTC), VT_DATETIME_UTC, None),
      (datetime(2024, 3, 5, 6, 7, 8), VT_DATETIME, None),
      (date(2024, 3, 5), VT_DATE, None),
      (time(6, 7, 8), VT_TIME, None),
      ("2024-03-05", VT_STR, "%Y-%m-%d"),
      ("2024-03-05 06:07:08", VT_STR, "%Y-%m-%d %H:%M:%S"),
  ]
  for value, vt, fmt in cases:
    assert from_epoch(to_epoch(value, vt, fmt), vt, fmt) == value


def test_sample_temporal_in_range_novel_and_seeded():
  lo = to_epoch("2024-01-01", VT_STR, "%Y-%m-%d")
  hi = to_epoch("2024-12-01", VT_STR, "%Y-%m-%d")
  a = sample_temporal(lo, hi, VT_STR, "%Y-%m-%d", 200, np.random.default_rng(7))
  b = sample_temporal(lo, hi, VT_STR, "%Y-%m-%d", 200, np.random.default_rng(7))
  assert a == b  # seeded determinism
  parsed = [datetime.strptime(v, "%Y-%m-%d") for v in a]
  assert all(datetime(2024, 1, 1) <= p <= datetime(2024, 12, 1) for p in parsed)
  assert len(set(a)) > 20  # novel spread, not a handful of repeats


def test_sample_temporal_degenerate_bounds():
  assert sample_temporal(None, None, VT_STR, "%Y-%m-%d", 3,
                         np.random.default_rng(0)) == [None] * 3
  lo = to_epoch("2024-05-05", VT_STR, "%Y-%m-%d")
  assert sample_temporal(lo, lo, VT_STR, "%Y-%m-%d", 2,
                         np.random.default_rng(0)) == ["2024-05-05"] * 2


_SCHEMA = {
    "table_info": {
        "table_id": "demo.events"
    },
    "schema": [
        {
            "name": "event_id",
            "type": "INT64",
            "mode": "REQUIRED"
        },
        {
            "name": "ts",
            "type": "TIMESTAMP",
            "mode": "REQUIRED"
        },
        {
            "name": "ts_low",
            "type": "TIMESTAMP",
            "mode": "REQUIRED"
        },
        {
            "name": "d_str",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "code",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "code_hc",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "enum_col",
            "type": "STRING",
            "mode": "REQUIRED",
            "max_length": 8
        },
    ],
    "primary_keys": ["event_id"],
}


def _reference_rows(n: int = 100) -> list[dict]:
  base = datetime(2024, 1, 1, tzinfo=UTC)
  return [
      {
          "event_id": i,
          # 60 distinct tz-aware timestamps (> cap 20) → TEMPORAL
          "ts": base + timedelta(hours=i % 60),
          # 10 distinct timestamps (≤ cap) → CATEGORICAL (enum-in-disguise)
          "ts_low": base + timedelta(days=i % 10),
          # 30 distinct date strings, ratio 0.3 (< 0.9) → TEMPORAL via shape
          "d_str": f"2024-06-{(i % 30) + 1:02d}",
          # 30 distinct short non-date strings (21..50 band) → CATEGORICAL:
          # a mid-cardinality enum (the COL_901 shape, 2026-07-22
          # b2 E2E). Routing these to the LLM asks it to invent codes for a
          # saturated domain — empty novel yield → strict batch death.
          "code": f"br {i % 30:03d} x",
          # 60 distinct short non-date strings (> 50, b1's string cap) →
          # FREE_TEXT (the genuine high-cardinality LLM route).
          "code_hc": f"hc {i % 60:03d} y",
          # 5 distinct → CATEGORICAL as before
          "enum_col": ["A", "B", "C", "D", "E"][i % 5],
      } for i in range(n)
  ]


def test_classifier_caps_route_families_correctly():
  schema = TableSchema.model_validate(_SCHEMA)
  profiles = profile_table(schema, _reference_rows())
  assert profiles["ts"].kind is ColumnKind.TEMPORAL
  assert profiles["ts"].temporal_value_type == VT_DATETIME_UTC
  assert profiles["ts"].minimum is not None
  assert profiles["ts"].maximum > profiles["ts"].minimum
  assert profiles["ts_low"].kind is ColumnKind.CATEGORICAL
  assert profiles["d_str"].kind is ColumnKind.TEMPORAL
  assert profiles["d_str"].temporal_value_type == VT_STR
  assert profiles["d_str"].temporal_format == "%Y-%m-%d"
  assert profiles["code"].kind is ColumnKind.CATEGORICAL
  assert profiles["code_hc"].kind is ColumnKind.FREE_TEXT
  assert profiles["enum_col"].kind is ColumnKind.CATEGORICAL


def test_unparseable_high_card_temporal_demotes_to_categorical():
  # TIMESTAMP-typed column whose values are mixed strings the detector
  # rejects → stays CATEGORICAL (in-support) instead of crashing.
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.weird"
      },
      "schema": [{
          "name": "w",
          "type": "TIMESTAMP",
          "mode": "REQUIRED"
      }],
      "primary_keys": None,
  })
  rows = [{
      "w": f"junk-{i}" if i % 2 else f"2024-01-{(i % 28) + 1:02d}"
  } for i in range(50)]
  profiles = profile_table(schema, rows)
  assert profiles["w"].kind is ColumnKind.CATEGORICAL


def test_empirical_backend_samples_novel_in_range_temporal():
  schema = TableSchema.model_validate(_SCHEMA)
  rows = _reference_rows()
  profiles = profile_table(schema, rows)
  backend = EmpiricalBackend()
  backend.fit(rows, profiles)
  out = backend.sample_columns(500, np.random.default_rng(11))

  observed_ts = {r["ts"] for r in rows}
  sampled_ts = [v for v in out["ts"] if v is not None]
  assert all(
      isinstance(v, datetime) and v.tzinfo is not None for v in sampled_ts)
  lo, hi = min(observed_ts), max(observed_ts)
  assert all(lo <= v <= hi for v in sampled_ts)
  # Novel-range jitter, not the verbatim observed table (60 distinct
  # observed instants in a 59-hour continuous range → collisions ≈ 0).
  copy_ratio = sum(v in observed_ts for v in sampled_ts) / len(sampled_ts)
  assert copy_ratio < 0.3

  sampled_d = [v for v in out["d_str"] if v is not None]
  assert all(datetime.strptime(v, "%Y-%m-%d") for v in sampled_d)
  # 30 observed days inside a 29-day span is a saturated keyspace, so
  # membership is unavoidable — the defect was FREQUENCY copying; assert
  # the draw is range-uniform, not the empirical table (distinct spread).
  assert len(set(sampled_d)) >= 20

  # Below-cap timestamp column stays empirical (in observed support).
  observed_low = {r["ts_low"] for r in rows}
  assert set(v for v in out["ts_low"] if v is not None) <= observed_low


def test_sdgx_reference_frame_excludes_temporal_columns():
  schema = TableSchema.model_validate(_SCHEMA)
  rows = _reference_rows()
  profiles = profile_table(schema, rows)
  backend = SdgxBackend()
  backend._profiles = _samplable_profiles(profiles)
  frame = backend._reference_frame(rows, pd)
  assert "ts" not in frame.columns and "d_str" not in frame.columns
  assert "enum_col" in frame.columns and "event_id" in frame.columns
  assert "code" in frame.columns  # mid-band enum: backend-sampled
  assert "code_hc" not in frame.columns  # free-text: LLM hook owns it


def test_sdgx_backend_temporal_branch_injects_nulls():
  schema = TableSchema.model_validate(_SCHEMA)
  rows = _reference_rows()
  profiles = profile_table(schema, rows)
  # Nullable temporal with a 30% observed null rate, hand-tuned.
  profiles = dict(profiles)
  p = profiles["ts"]
  profiles["ts"] = ColumnProfile(
      name=p.name,
      bq_type=p.bq_type,
      kind=p.kind,
      nullable=True,
      null_fraction=0.3,
      minimum=p.minimum,
      maximum=p.maximum,
      temporal_value_type=p.temporal_value_type,
      temporal_format=p.temporal_format,
  )
  backend = SdgxBackend()
  backend._profiles = _samplable_profiles(profiles)

  class _StubSynth:

    def sample(self, n):
      return pd.DataFrame({"event_id": [1] * n})

  backend._synthesizer = _StubSynth()
  out = backend.sample_columns(400, np.random.default_rng(5))
  null_rate = sum(v is None for v in out["ts"]) / 400
  assert 0.2 < null_rate < 0.4  # nulls reinjected, not dropped
  assert any(v is not None for v in out["ts"])  # and real values sampled


def test_sdgx_fit_failure_falls_back_with_milestone(caplog, monkeypatch):
  # The 2026-07-22 b2 E2E run left no trace of WHICH backend actually ran
  # (setup finished in 3 s — no CTGAN fit is that fast). The fallback must
  # be loud, like every other fallback in the engine.
  schema = TableSchema.model_validate(_SCHEMA)
  rows = _reference_rows()
  profiles = profile_table(schema, rows)

  def _boom(self, reference_rows):
    raise ModuleNotFoundError("No module named 'dask'")

  monkeypatch.setattr(SdgxBackend, "_fit_sdgx", _boom)
  backend = SdgxBackend()
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    backend.fit(rows, profiles)
  assert backend.used_fallback
  text = "\n".join(r.getMessage() for r in caplog.records)
  assert "SDFB_MILESTONE name=b2_backend_fallback" in text
  assert "error=ModuleNotFoundError" in text
  # The 2026-07-22 re-run logged only the exception TYPE — which module
  # was missing stayed unknowable from worker logs. The message must ride
  # along.
  assert "No module named" in text and "dask" in text


def test_sdgx_fit_success_emits_backend_milestone(caplog, monkeypatch):
  schema = TableSchema.model_validate(_SCHEMA)
  rows = _reference_rows()
  profiles = profile_table(schema, rows)
  monkeypatch.setattr(SdgxBackend, "_fit_sdgx",
                      lambda self, reference_rows: None)
  backend = SdgxBackend()
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    backend.fit(rows, profiles)
  assert not backend.used_fallback
  text = "\n".join(r.getMessage() for r in caplog.records)
  assert "SDFB_MILESTONE name=b2_backend_fitted" in text
  assert "backend=sdgx" in text


# ---------------------------------------------------------------------------
# Fidelity enforcement + end-to-end integration tests
# ---------------------------------------------------------------------------


def test_enforce_value_passes_temporal_through_and_representative_renders():
  schema = TableSchema.model_validate(_SCHEMA)
  profiles = profile_table(schema, _reference_rows())
  p = profiles["d_str"]
  assert enforce_value(p, "2024-06-15") == "2024-06-15"
  rep = _representative(p)
  assert datetime.strptime(rep, "%Y-%m-%d")  # renders minimum, parseable


def test_engine_end_to_end_yields_novel_temporal_rows():
  rows = _reference_rows()
  ctx = GenerationContext(
      table_schema=TableSchema.model_validate(_SCHEMA),
      reference_rows=rows,
      reference_digest="temporal-digest",
      pipeline_run_id="ws1-temporal-run",
  )
  engine = B2LibraryEngine(use_sdgx=False)
  engine.setup(FakeModelClient(responses=rows), ctx)
  records = list(
      engine.generate_batch(50, GenerationConfig(seed=3, batch_size=50)))
  assert len(records) == 50  # no rows dropped by record-model validation

  dumped = [r.model_dump(mode="python") for r in records]
  observed_ts = {r["ts"] for r in rows}
  sampled_ts = [d["ts"] for d in dumped]
  copy_ratio = sum(v in observed_ts for v in sampled_ts) / len(sampled_ts)
  assert copy_ratio < 0.3  # the 2026-07-20 defect was 1.0
  assert all(datetime.strptime(d["d_str"], "%Y-%m-%d") for d in dumped)
  observed_codes = {r["code"] for r in rows}
  assert all(d["code"] in observed_codes for d in dumped)  # enum: in-support
  assert all(isinstance(d["code_hc"], str) and d["code_hc"]
             for d in dumped)  # hook ran


def _one_string_col_schema(name: str = "blob") -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.blobs"
      },
      "schema": [{
          "name": name,
          "type": "STRING",
          "mode": "REQUIRED"
      }],
      "primary_keys": None,
  })


def test_nonprintable_string_column_emits_milestone(caplog):
  # The COL_048 signature: C0/C1 control bytes round-tripped as STRING.
  rows = [{"blob": f"S1\x8e\x9d\x07x{i}"} for i in range(10)]
  with caplog.at_level(logging.WARNING):
    profile_table(_one_string_col_schema(), rows)
  hits = [r for r in caplog.records if "column_nonprintable" in r.getMessage()]
  assert hits and "column=blob" in hits[0].getMessage()


def test_accented_text_does_not_trigger_nonprintable(caplog):
  rows = [{"blob": f"Städte-Übersicht émission {i}"} for i in range(10)]
  with caplog.at_level(logging.WARNING):
    profile_table(_one_string_col_schema(), rows)
  assert not [
      r for r in caplog.records if "column_nonprintable" in r.getMessage()
  ]


# ---------------------------------------------------------------------------
# Sentinel-aware temporal ranges — the 2026-07-23 b2 E2E landed 5 date-STRING
# columns with uniform dates across ~2000 years ("50-08-23", "955-10-29"):
# the source's 0001-01-01 sentinel inflated the jitter [min, max]. Sentinel
# values (year 1 / year 9999) must be excluded from the range and re-injected
# at their observed frequency instead.
# ---------------------------------------------------------------------------


def _sentinel_date_schema() -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.sentinels"
      },
      "schema": [{
          "name": "d",
          "type": "STRING",
          "mode": "REQUIRED"
      }],
      "primary_keys": None,
  })


def _sentinel_date_rows() -> list[dict]:
  real = [{
      "d": f"2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}"
  } for i in range(300)]
  return real + [{"d": "0001-01-01"}] * 200 + [{"d": "9999-12-31"}] * 50


def test_temporal_profile_trims_sentinel_years_from_range():
  profiles = profile_table(_sentinel_date_schema(), _sentinel_date_rows())
  p = profiles["d"]
  assert p.kind is ColumnKind.TEMPORAL
  lo = from_epoch(p.minimum, p.temporal_value_type, p.temporal_format)
  hi = from_epoch(p.maximum, p.temporal_value_type, p.temporal_format)
  assert str(lo).startswith("2024") and str(hi).startswith("2024")
  sentinels = dict(p.temporal_sentinels)
  assert abs(sentinels["0001-01-01"] - 200 / 550) < 0.01
  assert abs(sentinels["9999-12-31"] - 50 / 550) < 0.01


def test_empirical_backend_reinjects_sentinels_and_stays_plausible():
  schema = _sentinel_date_schema()
  rows = _sentinel_date_rows()
  profiles = profile_table(schema, rows)
  backend = EmpiricalBackend()
  backend.fit(rows, profiles)
  out = backend.sample_columns(2000, np.random.default_rng(13))["d"]
  n = len(out)
  lo_frac = sum(v == "0001-01-01" for v in out) / n
  hi_frac = sum(v == "9999-12-31" for v in out) / n
  assert abs(lo_frac - 200 / 550) < 0.05  # sentinel mass preserved
  assert abs(hi_frac - 50 / 550) < 0.05
  regular = [v for v in out if v not in ("0001-01-01", "9999-12-31")]
  assert regular
  # Every non-sentinel value is a plausible in-range date — no year-50s.
  assert all(v.startswith("2024") for v in regular)


def test_all_sentinel_year_temporal_column_keeps_full_range():
  # Degenerate: every observed value is in a sentinel year → nothing to
  # trim toward; the range stays as observed and no injection happens.
  schema = _sentinel_date_schema()
  rows = [{"d": f"9999-01-{(i % 25) + 1:02d}"} for i in range(100)]
  profiles = profile_table(schema, rows)
  p = profiles["d"]
  assert p.kind is ColumnKind.TEMPORAL
  assert p.temporal_sentinels == ()
  assert str(from_epoch(p.minimum, p.temporal_value_type,
                        p.temporal_format)).startswith("9999")


# ---------------------------------------------------------------------------
# Interim temporal-age policy (2026-07-23): generated dates must not be older
# than _MAX_TEMPORAL_AGE_YEARS (10). The jitter floor is clamped to
# max(observed_min, now - 10y); fully-historical columns keep their observed
# range untouched (a fabricated recent window would be worse than old truth —
# per-column DDL-JSON functional descriptions will govern those later).
# ---------------------------------------------------------------------------


def _year_of(profile, epoch):
  rendered = from_epoch(epoch, profile.temporal_value_type,
                        profile.temporal_format)
  return datetime.strptime(str(rendered), "%Y-%m-%d").year


def test_temporal_profile_clamps_lower_bound_to_max_age(caplog):
  schema = _sentinel_date_schema()
  rows = [{
      "d": f"2005-03-{(i % 28) + 1:02d}"
  } for i in range(40)] + [{
      "d": f"2025-04-{(i % 28) + 1:02d}"
  } for i in range(40)]
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    profiles = profile_table(schema, rows)
  p = profiles["d"]
  assert p.kind is ColumnKind.TEMPORAL
  current_year = datetime.now(UTC).year
  assert _year_of(p, p.minimum) >= current_year - 10  # floor engaged
  assert _year_of(p, p.maximum) == 2025  # ceiling untouched
  text = "\n".join(r.getMessage() for r in caplog.records)
  assert "SDFB_MILESTONE name=temporal_range_clamped" in text
  assert "column=d" in text


def test_temporal_profile_keeps_fully_historical_range(caplog):
  schema = _sentinel_date_schema()
  # 84 distinct over 200 rows keeps unique-ratio < 0.9 (the FREE_TEXT
  # route) so the column classifies TEMPORAL via shape.
  rows = [{"d": f"200{5 + (i % 3)}-06-{(i % 28) + 1:02d}"} for i in range(200)]
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    profiles = profile_table(schema, rows)
  p = profiles["d"]
  assert p.kind is ColumnKind.TEMPORAL
  assert _year_of(p, p.minimum) == 2005  # whole column is historical: keep
  assert "temporal_range_clamped" not in "\n".join(
      r.getMessage() for r in caplog.records)
