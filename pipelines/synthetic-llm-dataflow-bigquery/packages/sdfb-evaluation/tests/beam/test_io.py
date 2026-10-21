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
"""Tests for `sdfb_evaluation.beam.io` (Task 20): where rows come from
(BigQuery DIRECT_READ, in-memory, the plan's panel) and where output rows
go (BigQuery FILE_LOADS, local JSON, client load jobs). No network: the
BigQuery transforms are inspected, never run.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import json
import math
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import apache_beam as beam
import numpy as np
import pytest
from apache_beam.io.gcp.bigquery import (
    BigQueryDisposition,
    ReadFromBigQuery,
    WriteToBigQuery,
)
from apache_beam.testing.test_pipeline import TestPipeline as BeamTestPipeline
from apache_beam.testing.util import assert_that, equal_to

from sdfb_evaluation.beam.encode import BatchEncoder
from sdfb_evaluation.beam.io import (
    LOAD_ORDER,
    BigQuerySinks,
    BigQuerySources,
    ClientLoadSinks,
    InMemorySources,
    LocalJsonSinks,
    json_line,
    normalize_direct_read,
)
from sdfb_evaluation.context.plan import ColumnPlan
from sdfb_evaluation.schemas import TABLES, load_schema
from sdfb_evaluation.types import ColumnKind, Side

from .tables import (
    PROJECT,
    all_types,
    all_types_plan,
    arrow_rows,
    client_rows,
    make_panel,
)

SALT = "5a17" * 8
QDS = "synthetic_data_quality"


def _comparable(row: dict[str, Any]) -> tuple:
  """A row as a hashable, NaN-safe value for `equal_to` (NaN != NaN)."""
  return tuple(
      sorted((k, "NaN" if isinstance(v, float) and math.isnan(v) else repr(v))
             for k, v in row.items()))


def _nan_safe(row: dict[str, Any]) -> dict[str, Any]:
  """`row` with NaN replaced by a marker, so `==` compares it."""
  return {
      k: "NaN" if isinstance(v, float) and math.isnan(v) else v
      for k, v in row.items()
  }


def _metric_row(i: int, value: float) -> dict[str, Any]:
  return {
      "evaluation_id": "ev_20260914T080000_ab12cd34",
      "evaluated_at": "2026-09-14T08:00:00.000000+00:00",
      "table_name": "orders",
      "metric_id": f"column.ks#{i}",
      "value": value,
      "detail": {
          "reason": None,
          "bins": [1, 2, 3]
      },
  }


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------


def test_bigquery_sources_read_each_side_by_direct_read():
  table = all_types_plan()
  sources = BigQuerySources()
  names = [c.name for c in table.columns]
  for side, read_table in ((Side.SOURCE, table.source_read_table),
                           (Side.SYNTHETIC, table.synthetic_read_table)):
    read = sources.transform(table, side)
    assert isinstance(read, ReadFromBigQuery)
    assert read.method == ReadFromBigQuery.Method.DIRECT_READ
    # Arrow: DATETIME arrives as a native datetime, as the client reads it
    assert read.use_native_datetime is True
    assert read._kwargs["table"] == read_table  # pylint: disable=protected-access  # the transform keeps its read arguments only there
    assert read._kwargs["selected_fields"] == names  # pylint: disable=protected-access  # as above
    assert "query" not in read._kwargs  # pylint: disable=protected-access  # as above


def test_bigquery_sources_compose_every_side_in_one_pipeline():
  # Construction only (never run): labels are unique per table and side,
  # and the JSON/DATETIME type fix follows each BigQuery read.
  rows = client_rows()
  table = all_types_plan(panel=make_panel(rows[:2], rows[2:]))
  p = beam.Pipeline()
  sources = BigQuerySources()
  outs = [sources.read(p, table, side) for side in Side]
  assert all(isinstance(out, beam.PCollection) for out in outs)
  labels = set(p.applied_labels)
  for side in ("source", "synthetic"):
    assert f"Read[{table.landing_table}/{side}]/Types" in labels


def test_panel_rows_are_projected_to_the_plan_columns():
  # The panel is `SELECT ref.*` of the source: columns the landing table
  # lacks are dropped before beam.Create, so the job graph carries only
  # what the encoder reads.
  rows = [dict(r, source_only="x" * 64) for r in client_rows()]
  table = all_types_plan(panel=make_panel(rows[:2], rows[2:]))
  names = {c.name for c in table.columns}
  with BeamTestPipeline() as p:
    keys = (InMemorySources({}).read(p, table, Side.HOLDOUT) | beam.Map(sorted))
    assert_that(keys, equal_to([sorted(names)] * 2))
  assert "source_only" in rows[0]  # the plan's own rows are not mutated


def test_bigquery_sources_refuse_a_side_with_nothing_to_read():
  table = all_types_plan()
  empty = dataclasses.replace(table, source_read_table="")
  with pytest.raises(ValueError, match="nothing to read"):
    BigQuerySources().transform(empty, Side.SOURCE)


def test_panel_sides_come_from_the_plan_panel_for_every_sources():
  rows = client_rows()
  table = all_types_plan(panel=make_panel(rows[:2], rows[2:]))
  for sources in (BigQuerySources(),
                  InMemorySources({("order_items", Side.SOURCE): rows})):
    with BeamTestPipeline() as p:
      r = sources.read(p, table, Side.REFERENCE)
      h = sources.read(p, table, "holdout")
      assert_that(
          r | "CR" >> beam.Map(_comparable),
          equal_to([_comparable(row) for row in rows[:2]]),
          label="R")
      assert_that(
          h | "CH" >> beam.Map(_comparable),
          equal_to([_comparable(row) for row in rows[2:]]),
          label="H")


def test_a_panel_side_without_a_panel_raises():
  with pytest.raises(ValueError, match="panel"):
    InMemorySources({}).read(beam.Pipeline(), all_types_plan(), Side.HOLDOUT)


def test_in_memory_sources_read_rows_by_table_and_side():
  rows = client_rows()
  table = all_types_plan()
  sources = InMemorySources({
      ("order_items", "source"): rows[:3],
      ("order_items", Side.SYNTHETIC): rows[3:],
  })
  with BeamTestPipeline() as p:
    assert_that(
        sources.read(p, table, Side.SOURCE) | "CS" >> beam.Map(_comparable),
        equal_to([_comparable(row) for row in rows[:3]]),
        label="S")
    assert_that(
        sources.read(p, table, Side.SYNTHETIC) | "CY" >> beam.Map(_comparable),
        equal_to([_comparable(row) for row in rows[3:]]),
        label="Y")
  with pytest.raises(ValueError, match="order_items"):
    InMemorySources({}).read(beam.Pipeline(), table, Side.SOURCE)
  with pytest.raises(ValueError, match="panel"):
    InMemorySources({("order_items", Side.REFERENCE): rows})


# ---------------------------------------------------------------------------
# DIRECT_READ types -> the BigQuery client's types
# ---------------------------------------------------------------------------


def test_direct_read_rows_normalize_to_the_client_types():
  fields = all_types()["schema"]
  client = client_rows()
  direct = arrow_rows(fields, client)
  # Arrow already matches the client on every type but JSON (text)
  assert isinstance(direct[0]["attributes"], str)
  assert isinstance(direct[0]["sale_price"], type(client[0]["sale_price"]))
  columns = all_types_plan().columns
  normalized = [normalize_direct_read(row, columns) for row in direct]
  # equal values (Decimal scale and tz object aside) and equal types
  assert [_nan_safe(row) for row in normalized
         ] == [_nan_safe(row) for row in client]
  assert [{
      k: type(v) for k, v in row.items()
  } for row in normalized] == [{
      k: type(v) for k, v in row.items()
  } for row in client]
  assert normalized[3]["attributes"] == "plain"  # a JSON string scalar
  assert normalized[2]["attributes"] == [1, 2.5, "x"]
  # a RECORD's DATETIME/NUMERIC/TIMESTAMP/DATE sub-fields and a BIGNUMERIC
  # arrive typed as the client types them, at every depth (Arrow)
  for got, want in zip(normalized, client, strict=True):
    assert type(got["weight_kg"]) is type(want["weight_kg"])
    if want["fulfilment"] is not None:
      assert {
          k: type(v) for k, v in got["fulfilment"].items()
      } == {
          k: type(v) for k, v in want["fulfilment"].items()
      }
  sub = normalized[0]["fulfilment"]
  assert isinstance(sub["packed_at"],
                    datetime) and sub["packed_at"].tzinfo is None
  assert sub["scanned_at"].utcoffset() == timedelta(0)
  assert isinstance(sub["cost"], Decimal) and isinstance(sub["due"], date)
  assert normalized[0]["weight_kg"] == Decimal("0.4500000000000000000000000001")


def test_direct_read_rows_encode_exactly_like_client_rows():
  columns = all_types_plan().columns
  client = client_rows()
  direct = [
      normalize_direct_read(row, columns)
      for row in arrow_rows(all_types()["schema"], client)
  ]
  encoder = BatchEncoder.from_table(all_types_plan(), Side.SOURCE, salt=SALT)
  a, b = encoder.encode(client), encoder.encode(direct)
  for name in ("row_hash", "nonkey_hash", "h_nonkey", "cat", "pk_hash",
               "identity_hash", "fk_hash", "null_bits", "subsample_m"):
    np.testing.assert_array_equal(getattr(a, name), getattr(b, name), name)
  np.testing.assert_array_equal(a.num, b.num)
  assert a.text == b.text


def test_avro_style_datetime_text_and_repeated_json_are_normalized_too():
  columns = all_types_plan().columns
  row = dict(client_rows()[0])
  row["shipped_at"] = "2026-01-03T10:00:00"  # Avro DIRECT_READ: text
  normalized = normalize_direct_read(row, columns)
  assert normalized["shipped_at"] == datetime(2026, 1, 3, 10)
  assert normalized["delivery_date"] == date(2026, 1, 5)
  repeated_json = ColumnPlan(
      name="attributes",
      bq_type="JSON",
      mode="REPEATED",
      kind=ColumnKind.NESTED,
      is_key=False,
      day_granularity=False)
  out = normalize_direct_read({"attributes": ['{"a":1}', "2"]}, [repeated_json])
  assert out == {"attributes": [{"a": 1}, 2]}


def test_malformed_json_text_raises_naming_the_column():
  columns = all_types_plan().columns
  row = dict(client_rows()[0], attributes="{not json")
  with pytest.raises(ValueError, match="attributes"):
    normalize_direct_read(row, columns)


# ---------------------------------------------------------------------------
# sinks
# ---------------------------------------------------------------------------


def test_bigquery_sinks_use_file_loads_append_and_the_package_schemas():
  sinks = BigQuerySinks(PROJECT, QDS)
  for table in TABLES:
    write = sinks.transform(table)
    assert isinstance(write, WriteToBigQuery)
    assert write.method == WriteToBigQuery.Method.FILE_LOADS
    assert write.method != WriteToBigQuery.Method.STREAMING_INSERTS
    assert write.write_disposition == BigQueryDisposition.WRITE_APPEND
    assert write.create_disposition == BigQueryDisposition.CREATE_NEVER
    assert write.schema == {"fields": load_schema(table)}
    ref = write.table_reference
    assert (ref.projectId, ref.datasetId, ref.tableId) == (PROJECT, QDS, table)
  with pytest.raises(ValueError, match="evaluation_nope"):
    sinks.transform("evaluation_nope")


def test_bigquery_sinks_write_returns_the_load_and_copy_job_signals():
  # Construction only: the pipeline is never run, so nothing is loaded.
  p = beam.Pipeline()
  rows = p | beam.Create([_metric_row(0, 0.5)])
  signals = BigQuerySinks(
      PROJECT, QDS,
      gcs_temp_location="gs://demo-bucket/tmp").write(rows,
                                                      "evaluation_metrics")
  assert len(signals) == 2
  assert all(isinstance(s, beam.PCollection) for s in signals)


def test_json_line_is_canonical_and_json_safe():
  line = json_line(_metric_row(1, math.nan), "evaluation_metrics")
  assert json.loads(line)["value"] is None
  assert line == json.dumps(
      json.loads(line), sort_keys=True, separators=(",", ":"))
  with pytest.raises(TypeError, match="evaluation_metrics"):
    json_line({"when": datetime(2026, 9, 14)}, "evaluation_metrics")


def test_local_json_sinks_round_trip(tmp_path: Path):
  sinks = LocalJsonSinks(str(tmp_path))
  rows = [_metric_row(i, i / 10) for i in range(25)]
  rows.append(_metric_row(99, math.inf))
  with BeamTestPipeline() as p:
    signals = sinks.write(p | beam.Create(rows), "evaluation_metrics")
    assert len(signals) == 1
  back = sinks.read_rows("evaluation_metrics")
  expected = [json.loads(json_line(r, "evaluation_metrics")) for r in rows]
  by_id = {row["metric_id"]: row for row in back}
  assert len(back) == len(rows) == len(by_id)
  assert by_id == {row["metric_id"]: row for row in expected}
  assert by_id["column.ks#99"]["value"] is None  # +inf: a load job rejects it
  assert sinks.read_rows("evaluation_profiles") == []
  with pytest.raises(ValueError, match="evaluation_nope"):
    sinks.read_rows("evaluation_nope")


class _FakeBq:
  """Records `load_json` calls; loads nothing."""

  def __init__(self) -> None:
    self.loads: list[tuple[str, list[dict], list[dict]]] = []

  def load_json(self, fqn: str, rows: list[dict], schema: list[dict]) -> str:
    self.loads.append((fqn, list(rows), schema))
    return f"job_{len(self.loads)}"


def test_client_load_sinks_load_after_the_pipeline_registry_last(
    tmp_path: Path):
  sinks = ClientLoadSinks(str(tmp_path), project=PROJECT, dataset=QDS)
  final = {"evaluation_id": "ev_1", "event": "FINAL", "status": "SUCCEEDED"}
  profile = {"evaluation_id": "ev_1", "profile_kind": "histogram"}
  metrics = [_metric_row(i, float(i)) for i in range(3)]
  with BeamTestPipeline() as p:
    sinks.write(p | "H" >> beam.Create([final]), "evaluation_data_history")
    sinks.write(p | "M" >> beam.Create(metrics), "evaluation_metrics")
    sinks.write(p | "P" >> beam.Create([profile]), "evaluation_profiles")
  bq = _FakeBq()
  jobs = sinks.load(bq)
  loaded = [fqn.rsplit(".", 1)[1] for fqn, _, _ in bq.loads]
  # no row flags were written: nothing to load for them
  assert loaded == [
      "evaluation_metrics", "evaluation_profiles", "evaluation_data_history"
  ]
  assert LOAD_ORDER[-1] == "evaluation_data_history"
  assert set(LOAD_ORDER) == set(TABLES)
  for fqn, rows, schema in bq.loads:
    table = fqn.rsplit(".", 1)[1]
    assert fqn == f"{PROJECT}.{QDS}.{table}"
    assert schema == load_schema(table)
    assert sorted(map(json_line, rows, [table] * len(rows),
                      strict=True)) == sorted(
                          json_line(r, table) for r in sinks.read_rows(table))
  assert jobs == {
      "evaluation_metrics": "job_1",
      "evaluation_profiles": "job_2",
      "evaluation_data_history": "job_3",
  }


def test_two_writes_to_one_table_keep_every_row(tmp_path: Path):
  # Task 26 may write one table from two branches: each write gets its own
  # shard prefix, so neither finalises over the other.
  sinks = ClientLoadSinks(str(tmp_path), project=PROJECT, dataset=QDS)
  first = [_metric_row(i, 1.0) for i in range(3)]
  second = [_metric_row(i, 2.0) for i in range(100, 105)]
  with BeamTestPipeline() as p:
    sinks.write(p | "A" >> beam.Create(first), "evaluation_metrics")
    sinks.write(
        p | "B" >> beam.Create(second),
        "evaluation_metrics",
        label="Write[evaluation_metrics]")  # the same label twice
  assert len(sinks.read_rows("evaluation_metrics")) == 8
  bq = _FakeBq()
  sinks.load(bq)
  assert len(bq.loads) == 1
  loaded = bq.loads[0][1]
  assert len(loaded) == 8
  assert sorted(r["metric_id"] for r in loaded) == sorted(
      r["metric_id"] for r in first + second)


def _stale_shard(tmp_path: Path, table: str = "evaluation_metrics") -> Path:
  """A shard an earlier run left behind in the same output directory."""
  stale = tmp_path / table / "part-00000-of-00001.jsonl"
  stale.parent.mkdir(parents=True)
  stale.write_text(json_line(_metric_row(7, 0.7), table) + "\n")
  return stale


def test_a_table_dir_holding_another_runs_files_is_refused(tmp_path: Path):
  _stale_shard(tmp_path)
  sinks = LocalJsonSinks(str(tmp_path))
  with pytest.raises(ValueError, match="another run") as info:
    sinks.write(beam.Pipeline() | beam.Create([]), "evaluation_metrics")
  assert "evaluation_metrics" in str(info.value)
  # a table this run has not touched is still writable
  sinks.write(beam.Pipeline() | beam.Create([]), "evaluation_profiles")


def test_an_out_dir_beam_would_read_as_a_glob_is_refused(tmp_path: Path):
  # Beam's file sink matches its shards by glob: under "ev_[1]*" it cannot
  # find them and fails at finalize, so the sink refuses the path upfront.
  with pytest.raises(ValueError, match="glob"):
    LocalJsonSinks(str(tmp_path / "ev_[1]*"))


def test_read_rows_never_reloads_stale_shards(tmp_path: Path):
  _stale_shard(tmp_path)
  sinks = ClientLoadSinks(str(tmp_path), project=PROJECT, dataset=QDS)
  assert not sinks.read_rows("evaluation_metrics")  # not written by this run
  profile = {"evaluation_id": "ev_1", "profile_kind": "histogram"}
  with BeamTestPipeline() as p:
    sinks.write(p | beam.Create([profile]), "evaluation_profiles")
  bq = _FakeBq()
  assert sinks.load(bq) == {"evaluation_profiles": "job_1"}
  assert [fqn.rsplit(".", 1)[1] for fqn, _, _ in bq.loads
         ] == ["evaluation_profiles"]
