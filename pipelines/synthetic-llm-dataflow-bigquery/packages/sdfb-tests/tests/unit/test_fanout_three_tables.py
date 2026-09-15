"""ADR 0036 acceptance on the laptop: B -> C -> A with C driven by B and
A driven by C (B implied). Every FK tuple exists in its parent, every PK
is unique, sizes follow the histograms, and A's implied edge holds."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=unnecessary-lambda-assignment

from __future__ import annotations

import json
from pathlib import Path

import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions
from sdfb_beam.io.local_sinks import WriteToJsonLines
from sdfb_beam.pipeline import FkEdgeSpec, PipelineConfig, TableSpec, build_relational_pipeline
from sdfb_core.contracts import TableSchema
from sdfb_tests.fakes import FakeModelClient


def _schema(table: str, cols: list[tuple[str, str]]) -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": f"p.src.{table}"
      },
      "schema": [{
          "name": n,
          "type": t,
          "mode": "REQUIRED"
      } for n, t in cols]
  })


def _read(prefix: Path) -> list[dict]:
  rows = []
  for f in sorted(prefix.parent.glob(prefix.name + "*")):
    rows.extend(json.loads(line) for line in f.read_text().splitlines() if line)
  return rows


def test_three_tables_by_construction(tmp_path):
  b_schema = _schema("B_TABLE", [("D_COL_001", "STRING"),
                                 ("D_COL_024", "STRING"), ("AMT", "INT64")])
  b_ref = [{
      "D_COL_001": f"B{i:05d}",
      "D_COL_024": "AB"[i % 2],
      "AMT": i
  } for i in range(60)]
  c_schema = _schema("C_TABLE",
                     [("D_COL_001", "STRING"), ("D_COL_024", "STRING"),
                      ("C_COL_002", "STRING"), ("V", "INT64")])
  c_ref = [{
      "D_COL_001": f"X{i}",
      "D_COL_024": "A",
      "C_COL_002": "pqr"[i % 3],
      "V": i
  } for i in range(60)]
  a_schema = _schema("A_TABLE", [("D_COL_024", "STRING"),
                                 ("A_COL_005", "STRING"), ("W", "INT64")])
  a_ref = [{
      "D_COL_024": "Z",
      "A_COL_005": f"S{i:06d}",
      "W": i
  } for i in range(60)]

  def cfg(schema, ref, name, **kw):
    return PipelineConfig(
        table_schema=schema,
        engine_name="b1_rag",
        model_client=FakeModelClient(reference_pool=ref),
        run_id=f"fan-{name}",
        landing_table=f"p.land.{name}",
        log_table_prefix=name,
        batch_size=20,
        **kw)

  b_cfg = cfg(
      b_schema,
      b_ref,
      "B_TABLE",
      num_rows=80,
      pk_columns=("D_COL_001",),
      identity_columns=("D_COL_001",))
  c_cfg = cfg(
      c_schema,
      c_ref,
      "C_TABLE",
      num_rows=160,
      pk_columns=("D_COL_001", "C_COL_002"),
      uniqueness_mode="streaming",
      fanout={
          "driving_cols": ["D_COL_001", "D_COL_024"],
          "histogram": {
              "0": 1,
              "2": 2,
              "3": 1
          },
          "cells": {
              "cols": ["C_COL_002"],
              "rows": [["p"], ["q"], ["r"]],
              "counts": [3, 2, 1]
          },
          "exact_cells": True
      })
  # A is keyed by (D_COL_024, A_COL_005); A_COL_005 is unbounded -> inexact cells (none).
  a_cfg = cfg(
      a_schema,
      a_ref,
      "A_TABLE",
      num_rows=100,
      pk_columns=("D_COL_024", "A_COL_005"),
      identity_columns=("A_COL_005",),
      uniqueness_mode="exact",
      fanout={
          "driving_cols": ["D_COL_024"],
          "histogram": {
              "1": 1,
              "2": 1
          },
          "cells": None,
          "exact_cells": False
      })
  sinks = lambda name: dict(  # noqa: E731
      landing_sink=WriteToJsonLines(str(tmp_path / name)),
      dlq_sink=WriteToJsonLines(str(tmp_path / f"dlq_{name}")))
  specs = [
      TableSpec(config=b_cfg, reference_rows=b_ref, **sinks("B_TABLE")),
      TableSpec(
          config=c_cfg,
          reference_rows=c_ref,
          **sinks("C_TABLE"),
          parent_edges=(FkEdgeSpec(
              child_cols=("D_COL_001", "D_COL_024"),
              ref_cols=("D_COL_001", "D_COL_024"),
              parent_landing="p.land.B_TABLE",
              parent_pk=("D_COL_001",),
              mode="fanout",
              keys_per_batch=10),)),
      TableSpec(
          config=a_cfg,
          reference_rows=a_ref,
          **sinks("A_TABLE"),
          parent_edges=(FkEdgeSpec(
              child_cols=("D_COL_024",),
              ref_cols=("D_COL_024",),
              parent_landing="p.land.C_TABLE",
              parent_pk=("D_COL_001", "C_COL_002"),
              mode="fanout",
              keys_per_batch=10),
                        FkEdgeSpec(
                            child_cols=("D_COL_024",),
                            ref_cols=("D_COL_024",),
                            parent_landing="p.land.B_TABLE",
                            parent_pk=("D_COL_001",),
                            mode="implied"))),
  ]
  with beam.Pipeline(options=PipelineOptions(["--runner=DirectRunner"])) as p:
    build_relational_pipeline(p, specs)

  b = _read(tmp_path / "B_TABLE")
  c = _read(tmp_path / "C_TABLE")
  a = _read(tmp_path / "A_TABLE")
  assert b and c and a
  b_keys = {(r["D_COL_001"], r["D_COL_024"]) for r in b}
  assert {(r["D_COL_001"], r["D_COL_024"]) for r in c
         } <= b_keys  # C -> B by construction
  assert len({(r["D_COL_001"], r["C_COL_002"]) for r in c
             }) == len(c)  # C PK unique
  per_b = {}
  for r in c:
    per_b[r["D_COL_001"]] = per_b.get(r["D_COL_001"], 0) + 1
  assert set(per_b.values()) <= {2, 3}  # the histogram
  c_tuples = {r["D_COL_024"] for r in c}
  assert {r["D_COL_024"] for r in a} <= c_tuples  # A -> C (driving)
  assert {r["D_COL_024"] for r in a} <= {r["D_COL_024"] for r in b
                                        }  # A -> B (implied) holds
  assert not _read(tmp_path / "dlq_C_TABLE")  # nothing diverted
