"""Single-job relational pipeline (ADR 0030): parent keys flow to the
child as an in-DAG side input — one Dataflow job, one vLLM ignition,
referential integrity by construction (no BQ round-trip between
tables).
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=arguments-renamed,import-outside-toplevel,unused-argument

from __future__ import annotations

import json
from pathlib import Path

import apache_beam as beam
import pytest
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.pipeline import PipelineVisitor
from sdfb_beam.io.local_sinks import WriteToJsonLines
from sdfb_beam.pipeline import (
    FkEdgeSpec,
    PipelineConfig,
    TableSpec,
    build_relational_pipeline,
)
from sdfb_core.contracts import TableSchema
from sdfb_tests.fakes import FakeModelClient

_CHILD_SCHEMA = TableSchema.model_validate({
    "table_info": {
        "table_id": "p.src.orders_flat"
    },
    "schema": [
        {
            "name": "ORDER_ID",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "CUST_ID",
            "type": "INT64",
            "mode": "REQUIRED"
        },
        {
            "name": "AMOUNT",
            "type": "INT64",
            "mode": "REQUIRED"
        },
    ],
})


def _child_reference() -> list[dict]:
  # CUST_ID values deliberately DISJOINT from anything the parent can
  # land, so containment proves side-input propagation, not chance.
  return [{
      "ORDER_ID": f"ORD{i:05d}",
      "CUST_ID": 900000 + i,
      "AMOUNT": i * 3
  } for i in range(40)]


class _LabelCollector(PipelineVisitor):
  """Every transform label in a built graph, for DAG-shape assertions."""

  def __init__(self):
    self.labels: list[str] = []

  def visit_transform(self, node):
    self.labels.append(node.full_label)


def _labels_of(pipeline) -> list[str]:
  visitor = _LabelCollector()
  pipeline.visit(visitor)
  return visitor.labels


def _read_jsonl(prefix: Path) -> list[dict]:
  rows: list[dict] = []
  for f in sorted(prefix.parent.glob(prefix.name + "*")):
    rows.extend(json.loads(line) for line in f.read_text().splitlines() if line)
  return rows


def test_child_fk_values_come_from_parent_landed_keys(tmp_path,
                                                      customers_schema,
                                                      customers_reference,
                                                      caplog):
  parent_cfg = PipelineConfig(
      table_schema=customers_schema,
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=customers_reference),
      num_rows=50,
      batch_size=25,
      run_id="rel-parent",
      landing_table="p.land.customers",
      log_table_prefix="customers",
      # PK-unique Pandera check on the fixture: identity synthesis keeps
      # every generated customer_id unique so all 50 land.
      identity_columns=("customer_id",),
  )
  child_cfg = PipelineConfig(
      table_schema=_CHILD_SCHEMA,
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=_child_reference()),
      num_rows=80,
      batch_size=40,
      run_id="rel-child",
      landing_table="p.land.orders_flat",
      log_table_prefix="orders_flat",
  )
  specs = [
      TableSpec(
          config=parent_cfg,
          reference_rows=customers_reference,
          landing_sink=WriteToJsonLines(str(tmp_path / "parent")),
          dlq_sink=WriteToJsonLines(str(tmp_path / "dlq_parent")),
      ),
      TableSpec(
          config=child_cfg,
          reference_rows=_child_reference(),
          landing_sink=WriteToJsonLines(str(tmp_path / "child")),
          dlq_sink=WriteToJsonLines(str(tmp_path / "dlq_child")),
          parent_edges=(FkEdgeSpec(
              child_cols=("CUST_ID",),
              ref_cols=("customer_id",),
              parent_landing="p.land.customers",
              parent_pk=("customer_id",),
          ),),
      ),
  ]
  import logging as _logging

  options = PipelineOptions(["--runner=DirectRunner"])
  with (
      caplog.at_level(_logging.INFO, logger="sdfb.milestone"),
      beam.Pipeline(options=options) as p,
  ):
    results = build_relational_pipeline(p, specs)
  assert set(results) == {"p.land.customers", "p.land.orders_flat"}
  # Every engine milestone carries its table (ADR 0030): two tables
  # interleave in ONE worker log and stay attributable.
  assert "table=customers" in caplog.text
  assert "table=orders_flat" in caplog.text

  parent_rows = _read_jsonl(tmp_path / "parent")
  child_rows = _read_jsonl(tmp_path / "child")
  assert parent_rows and child_rows
  parent_keys = {r["customer_id"] for r in parent_rows}
  child_fks = {r["CUST_ID"] for r in child_rows}
  # Integrity by construction: every child FK value is a landed parent
  # key — and none of the child's own (disjoint) reference values leak.
  assert child_fks <= parent_keys
  assert not any(900000 <= v < 900100 for v in child_fks)


_COMPOSITE_CHILD_SCHEMA = TableSchema.model_validate({
    "table_info": {
        "table_id": "p.src.orders_composite"
    },
    "schema": [
        {
            "name": "ORDER_ID",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "CUST_ID",
            "type": "INT64",
            "mode": "REQUIRED"
        },
        {
            "name": "CC",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "AMOUNT",
            "type": "INT64",
            "mode": "REQUIRED"
        },
    ],
})


def test_composite_fk_lands_only_key_tuples_the_parent_holds(
    tmp_path, customers_schema, customers_reference):
  """The 2026-08-23 defect, in one DirectRunner run.

    `(customer_id, country)` is sparse: each landed customer_id pairs
    with exactly ONE country, so a child drawing the two columns from
    independent pools lands a combination the parent never held roughly
    (1 - 1/|countries|) of the time — the shape that measured 81.8%
    orphans in production. Joint tuple draws make it structurally
    impossible.
    """
  parent_cfg = PipelineConfig(
      table_schema=customers_schema,
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=customers_reference),
      num_rows=60,
      batch_size=30,
      run_id="rel-parent-composite",
      landing_table="p.land.customers",
      log_table_prefix="customers",
      identity_columns=("customer_id",),
  )
  child_reference = [
      # Same country VALUES the parent knows, deliberately paired with
      # customer ids it will never land: only the joint draw can fix
      # both columns at once.
      {
          "ORDER_ID": f"ORD{i:05d}",
          "CUST_ID": 900000 + i,
          "CC": ("DE", "FR", "ES")[i % 3],
          "AMOUNT": i * 7,
      } for i in range(40)
  ]
  child_cfg = PipelineConfig(
      table_schema=_COMPOSITE_CHILD_SCHEMA,
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=child_reference),
      num_rows=120,
      batch_size=60,
      run_id="rel-child-composite",
      landing_table="p.land.orders_composite",
      log_table_prefix="orders_composite",
  )
  specs = [
      TableSpec(
          config=parent_cfg,
          reference_rows=customers_reference,
          landing_sink=WriteToJsonLines(str(tmp_path / "cparent")),
          dlq_sink=WriteToJsonLines(str(tmp_path / "cdlq_parent")),
      ),
      TableSpec(
          config=child_cfg,
          reference_rows=child_reference,
          landing_sink=WriteToJsonLines(str(tmp_path / "cchild")),
          dlq_sink=WriteToJsonLines(str(tmp_path / "cdlq_child")),
          parent_edges=(FkEdgeSpec(
              child_cols=("CUST_ID", "CC"),
              ref_cols=("customer_id", "country"),
              parent_landing="p.land.customers",
              parent_pk=("customer_id",),
          ),),
      ),
  ]
  options = PipelineOptions(["--runner=DirectRunner"])
  with beam.Pipeline(options=options) as p:
    build_relational_pipeline(p, specs)

  parent_rows = _read_jsonl(tmp_path / "cparent")
  child_rows = _read_jsonl(tmp_path / "cchild")
  assert parent_rows and child_rows
  parent_keys = {(r["customer_id"], r["country"])
                 for r in parent_rows
                 if r["country"] is not None}
  child_keys = {(r["CUST_ID"], r["CC"]) for r in child_rows}
  orphans = child_keys - parent_keys
  assert not orphans, f"{len(orphans)} orphan key tuples generated"
  # …and the independent gate ran: no row was diverted, because none
  # could be. A clean DLQ here is a MEASURED 0 orphans, not a claim.
  assert not [
      r for r in _read_jsonl(tmp_path / "cdlq_child")
      if r.get("rule_id") == "fk.orphan"
  ]
  # A NULL parent key is not referenceable (SQL never matches it), so
  # it must never reach the child as a drawable key.
  assert None not in {r["CC"] for r in child_rows}


def test_fk_integrity_gate_is_wired_only_for_tables_with_edges(
    tmp_path, customers_schema, customers_reference):
  """A table with no FK edges keeps its DAG shape unchanged; a child
    with edges gets the `fk.orphan` check between Generate and
    ValidateRecord."""
  from sdfb_beam.pipeline import build_pipeline

  cfg = PipelineConfig(
      table_schema=customers_schema,
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=customers_reference),
      num_rows=10,
      batch_size=10,
      run_id="gate-shape",
      landing_table="p.land.customers",
      identity_columns=("customer_id",),
  )
  options = PipelineOptions(["--runner=DirectRunner"])
  p = beam.Pipeline(options=options)
  build_pipeline(
      p,
      reference_rows=customers_reference,
      config=cfg,
      landing_sink=WriteToJsonLines(str(tmp_path / "plain")),
      dlq_sink=WriteToJsonLines(str(tmp_path / "plain_dlq")),
      label_prefix="plain/",
  )
  assert not any("EnforceFkIntegrity" in lbl for lbl in p.applied_labels)

  cfg.fk_key_pools = [{"cols": ["country"], "keys": [("DE",), ("FR",)]}]
  build_pipeline(
      p,
      reference_rows=customers_reference,
      config=cfg,
      landing_sink=WriteToJsonLines(str(tmp_path / "fk")),
      dlq_sink=WriteToJsonLines(str(tmp_path / "fk_dlq")),
      label_prefix="fk/",
  )
  assert any("EnforceFkIntegrity" in lbl for lbl in p.applied_labels)


def test_independent_tables_share_one_pipeline(tmp_path, customers_schema,
                                               customers_reference):

  def _cfg(run_id: str, landing: str) -> PipelineConfig:
    return PipelineConfig(
        table_schema=customers_schema,
        engine_name="b1_rag",
        model_client=FakeModelClient(reference_pool=customers_reference),
        num_rows=20,
        batch_size=10,
        run_id=run_id,
        landing_table=landing,
        identity_columns=("customer_id",),
    )

  specs = [
      TableSpec(
          config=_cfg("multi-a", "p.land.t_a"),
          reference_rows=customers_reference,
          landing_sink=WriteToJsonLines(str(tmp_path / "a")),
          dlq_sink=WriteToJsonLines(str(tmp_path / "dlq_a")),
          # 2026-08-22 second launch: corp always sets
          # validation_runs_table — the §12 gate subgraph must be
          # label-namespaced too (CountValid collided).
          validation_runs_sink=WriteToJsonLines(str(tmp_path / "vr_a")),
      ),
      TableSpec(
          config=_cfg("multi-b", "p.land.t_b"),
          reference_rows=customers_reference,
          landing_sink=WriteToJsonLines(str(tmp_path / "b")),
          dlq_sink=WriteToJsonLines(str(tmp_path / "dlq_b")),
          validation_runs_sink=WriteToJsonLines(str(tmp_path / "vr_b")),
      ),
  ]
  options = PipelineOptions(["--runner=DirectRunner"])
  with beam.Pipeline(options=options) as p:
    build_relational_pipeline(p, specs)  # unique labels: must not raise
  assert len(_read_jsonl(tmp_path / "a")) == 20
  assert len(_read_jsonl(tmp_path / "b")) == 20


def test_edge_key_sample_cap_bounds_the_parent_keys_the_child_sees(tmp_path):
  """ADR 0035: the side-input sample size is per edge, set by preflight
    from the child's PK — a flat 100k starved C_TABLE's PK on 2026-09-09."""
  from apache_beam.testing.util import assert_that
  from sdfb_beam.pipeline import _edge_key_pools

  parent_rows = [{"customer_id": i} for i in range(300)]
  capped = FkEdgeSpec(
      child_cols=("CUST_ID",),
      ref_cols=("customer_id",),
      parent_landing="p.land.customers",
      parent_pk=("customer_id",),
      key_sample_cap=150,
  )
  default = FkEdgeSpec(
      child_cols=("CUST_ID",),
      ref_cols=("customer_id",),
      parent_landing="p.land.customers",
      parent_pk=("customer_id",),
  )

  def _has_keys(n: int):

    def _check(payloads):
      (payload,) = payloads
      (edge,) = payload
      assert len(edge["keys"]) == n, len(edge["keys"])

    return _check

  with beam.Pipeline(options=PipelineOptions(["--runner=DirectRunner"])) as p:
    parent = p | beam.Create(parent_rows)
    assert_that(
        _edge_key_pools(parent, capped, "capped/"),
        _has_keys(150),
        label="capped",
    )
    assert_that(
        _edge_key_pools(parent, default, "default/"),
        _has_keys(300),
        label="default",
    )


def test_driven_child_is_generated_from_parent_keys_without_a_side_input(
    tmp_path, customers_schema, customers_reference):
  """ADR 0036: every child FK value is a landed parent key, the PK is
    unique by construction, and no fk side input exists in the graph."""
  parent_cfg = PipelineConfig(
      table_schema=customers_schema,
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=customers_reference),
      num_rows=50,
      batch_size=25,
      run_id="fan-parent",
      landing_table="p.land.customers",
      log_table_prefix="customers",
      identity_columns=("customer_id",),
  )
  child_schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.src.orders_fan"
      },
      "schema": [
          {
              "name": "CUST_ID",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "LINE",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "AMOUNT",
              "type": "INT64",
              "mode": "REQUIRED"
          },
      ]
  })
  child_ref = [{
      "CUST_ID": 900000 + i,
      "LINE": "xyz"[i % 3],
      "AMOUNT": i * 3
  } for i in range(40)]
  child_cfg = PipelineConfig(
      table_schema=child_schema,
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=child_ref),
      num_rows=100,  # derived expectation; not a request count
      batch_size=20,
      run_id="fan-child",
      landing_table="p.land.orders_fan",
      log_table_prefix="orders_fan",
      pk_columns=("CUST_ID", "LINE"),
      uniqueness_mode="streaming",
      fanout={
          "driving_cols": ["CUST_ID"],
          "histogram": {
              "0": 1,
              "1": 2,
              "3": 1
          },
          "cells": {
              "cols": ["LINE"],
              "rows": [["x"], ["y"], ["z"]],
              "counts": [1, 1, 1]
          },
          "exact_cells": True
      },
  )
  specs = [
      TableSpec(
          config=parent_cfg,
          reference_rows=customers_reference,
          landing_sink=WriteToJsonLines(str(tmp_path / "parent")),
          dlq_sink=WriteToJsonLines(str(tmp_path / "dlq_parent"))),
      TableSpec(
          config=child_cfg,
          reference_rows=child_ref,
          landing_sink=WriteToJsonLines(str(tmp_path / "child")),
          dlq_sink=WriteToJsonLines(str(tmp_path / "dlq_child")),
          parent_edges=(FkEdgeSpec(
              child_cols=("CUST_ID",),
              ref_cols=("customer_id",),
              parent_landing="p.land.customers",
              parent_pk=("customer_id",),
              mode="fanout",
              keys_per_batch=10),)),
  ]
  with beam.Pipeline(options=PipelineOptions(["--runner=DirectRunner"])) as p:
    build_relational_pipeline(p, specs)

  labels = _labels_of(p)
  parent_rows = _read_jsonl(tmp_path / "parent")
  child_rows = _read_jsonl(tmp_path / "child")
  parent_keys = {r["customer_id"] for r in parent_rows}
  assert child_rows and {r["CUST_ID"] for r in child_rows} <= parent_keys
  assert len({(r["CUST_ID"], r["LINE"]) for r in child_rows
             }) == len(child_rows)  # PK unique
  per_key = {}
  for r in child_rows:
    per_key.setdefault(r["CUST_ID"], 0)
    per_key[r["CUST_ID"]] += 1
  assert set(per_key.values()) <= {1, 3}
  joined = "".join(labels)
  assert any(
      "orders_fan/FanoutKeys" in lbl for lbl in labels)  # no side-input sample
  assert "orders_fan/edge0/FkSample" not in joined
  assert "orders_fan/edge0/FkPools" not in joined


def test_fanout_without_a_declared_parent_pk_deduplicates_keys(
    tmp_path, customers_schema, customers_reference):
  """A duplicated key in the fan-out request stream re-seeds the SAME
    per-key draw (`derive_key_seed`), so PK-identical children collide on
    the child's own PK (the key and cells repeat exactly; the free
    columns differ, being sampled per chunk position). `FkEdgeSpec.parent_pk=()` (undeclared) proves
    nothing about uniqueness, so `_fanout_requests` must Distinct the
    projection instead of trusting an absent PK."""
  parent_cfg = PipelineConfig(
      table_schema=customers_schema,
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=customers_reference),
      num_rows=50,
      batch_size=25,
      run_id="fan-parent-nopk",
      landing_table="p.land.customers",
      log_table_prefix="customers",
      # No identity_columns / pk_columns declared: the customers fixture
      # has only 10 distinct customer_id values, so 50 generated rows
      # land duplicate ids — that is the point.
  )
  child_schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.src.orders_fan"
      },
      "schema": [
          {
              "name": "CUST_ID",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "LINE",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "AMOUNT",
              "type": "INT64",
              "mode": "REQUIRED"
          },
      ]
  })
  child_ref = [{
      "CUST_ID": 900000 + i,
      "LINE": "xyz"[i % 3],
      "AMOUNT": i * 3
  } for i in range(40)]
  child_cfg = PipelineConfig(
      table_schema=child_schema,
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=child_ref),
      num_rows=100,
      batch_size=20,
      run_id="fan-child-nopk",
      landing_table="p.land.orders_fan",
      log_table_prefix="orders_fan",
      pk_columns=("CUST_ID", "LINE"),
      uniqueness_mode="streaming",
      fanout={
          "driving_cols": ["CUST_ID"],
          "histogram": {
              "0": 1,
              "1": 2,
              "3": 1
          },
          "cells": {
              "cols": ["LINE"],
              "rows": [["x"], ["y"], ["z"]],
              "counts": [1, 1, 1]
          },
          "exact_cells": True
      },
  )
  specs = [
      TableSpec(
          config=parent_cfg,
          reference_rows=customers_reference,
          landing_sink=WriteToJsonLines(str(tmp_path / "parent_nopk")),
          dlq_sink=WriteToJsonLines(str(tmp_path / "dlq_parent_nopk"))),
      TableSpec(
          config=child_cfg,
          reference_rows=child_ref,
          landing_sink=WriteToJsonLines(str(tmp_path / "child_nopk")),
          dlq_sink=WriteToJsonLines(str(tmp_path / "dlq_child_nopk")),
          parent_edges=(FkEdgeSpec(
              child_cols=("CUST_ID",),
              ref_cols=("customer_id",),
              parent_landing="p.land.customers",
              parent_pk=(),
              mode="fanout",
              keys_per_batch=10),)),
  ]
  with beam.Pipeline(options=PipelineOptions(["--runner=DirectRunner"])) as p:
    build_relational_pipeline(p, specs)

  labels = _labels_of(p)
  child_rows = _read_jsonl(tmp_path / "child_nopk")
  assert any("orders_fan/FanoutDistinct" in lbl for lbl in labels)
  pairs = [(r["CUST_ID"], r["LINE"]) for r in child_rows]
  assert len(set(pairs)) == len(child_rows)


def test_a_side_input_edge_next_to_the_driving_edge_is_a_star(
    tmp_path, customers_schema, customers_reference):
  """ADR 0037 §5: a driven child MAY carry an independent (side-input)
    edge next to its driving edge — the star-schema fact.

    ADR 0036 D1 stopped the build here because a side-input edge sharing
    columns with the driving edge would have been overwritten; that case
    is `conditional` now, so what remains on the side-input path is
    disjoint by construction. The edge keeps the whole ADR 0030/0031
    path: a sampled parent-key pool on the Generate ParDo AND the
    `fk.orphan` gate that measures it."""
  parent_cfg = PipelineConfig(
      table_schema=customers_schema,
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=customers_reference),
      num_rows=20,
      batch_size=20,
      run_id="fan-parent-star",
      landing_table="p.land.customers",
      log_table_prefix="customers",
      identity_columns=("customer_id",),
  )
  child_schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.src.orders_fan"
      },
      "schema": [
          {
              "name": "CUST_ID",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "LINE",
              "type": "STRING",
              "mode": "REQUIRED"
          },
          {
              "name": "REGION",
              "type": "STRING",
              "mode": "REQUIRED"
          },
      ]
  })
  # REGION values deliberately DISJOINT from anything the parent can
  # land, so containment proves the side input reached the engine.
  child_ref = [{
      "CUST_ID": 900000 + i,
      "LINE": "xyz"[i % 3],
      "REGION": f"ZZ{i}"
  } for i in range(40)]
  child_cfg = PipelineConfig(
      table_schema=child_schema,
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=child_ref),
      num_rows=20,
      batch_size=20,
      run_id="fan-child-star",
      landing_table="p.land.orders_fan",
      log_table_prefix="orders_fan",
      pk_columns=("CUST_ID", "LINE"),
      uniqueness_mode="streaming",
      fanout={
          "driving_cols": ["CUST_ID"],
          "histogram": {
              "1": 1
          },
          "cells": {
              "cols": ["LINE"],
              "rows": [["x"], ["y"], ["z"]],
              "counts": [1, 1, 1]
          },
          "exact_cells": True
      },
  )
  specs = [
      TableSpec(
          config=parent_cfg,
          reference_rows=customers_reference,
          landing_sink=WriteToJsonLines(str(tmp_path / "parent_star")),
          dlq_sink=WriteToJsonLines(str(tmp_path / "dlq_parent_star"))),
      TableSpec(
          config=child_cfg,
          reference_rows=child_ref,
          landing_sink=WriteToJsonLines(str(tmp_path / "child_star")),
          dlq_sink=WriteToJsonLines(str(tmp_path / "dlq_child_star")),
          parent_edges=(
              FkEdgeSpec(
                  child_cols=("CUST_ID",),
                  ref_cols=("customer_id",),
                  parent_landing="p.land.customers",
                  parent_pk=("customer_id",),
                  mode="fanout",
                  keys_per_batch=10),
              FkEdgeSpec(
                  child_cols=("REGION",),
                  ref_cols=("country",),
                  parent_landing="p.land.customers",
                  parent_pk=("customer_id",),
                  mode="side_input"),
          )),
  ]
  with beam.Pipeline(options=PipelineOptions(["--runner=DirectRunner"])) as p:
    build_relational_pipeline(p, specs)

  labels = _labels_of(p)
  # BOTH paths are wired for the same table: the driving request stream
  # and the independent edge's sampled pool + its gate.
  assert any("orders_fan/FanoutKeys" in lbl for lbl in labels)
  assert any("orders_fan/edge1/FkPools" in lbl for lbl in labels)
  assert any(lbl.startswith("orders_fan/Generate") for lbl in labels)
  assert any("orders_fan/EnforceFkIntegrity" in lbl for lbl in labels)

  parent_rows = _read_jsonl(tmp_path / "parent_star")
  child_rows = _read_jsonl(tmp_path / "child_star")
  assert parent_rows and child_rows
  parent_keys = {r["customer_id"] for r in parent_rows}
  parent_countries = {
      r["country"] for r in parent_rows if r["country"] is not None
  }
  assert {r["CUST_ID"] for r in child_rows} <= parent_keys  # driving
  assert {r["REGION"] for r in child_rows} <= parent_countries  # independent
  # …and the gate MEASURED it: no row was diverted as an orphan.
  assert not [
      r for r in _read_jsonl(tmp_path / "dlq_child_star")
      if r.get("rule_id") == "fk.orphan"
  ]


def test_a_null_inherited_column_rides_through_the_fanout_projection(
    tmp_path, customers_schema, customers_reference):
  """ADR 0036 review I5: a widened driving edge carries INHERITED columns
    next to the join key. Requiring every position to be non-NULL silently
    dropped the whole parent — and every child it should have produced —
    because one inherited column was NULL. Only the join-key positions (the
    parent PK inside the ref tuple) may gate; inherited NULLs are copied
    verbatim."""
  parent_schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.src.customers_nullable"
      },
      "schema": [
          {
              "name": "customer_id",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "segment",
              "type": "STRING",
              "mode": "NULLABLE"
          },
      ]
  })
  # Every parent row's inherited `segment` is NULL: pre-fix the projection
  # dropped all of them and the child landed nothing.
  parent_ref = [{"customer_id": 1000 + i, "segment": None} for i in range(20)]
  parent_cfg = PipelineConfig(
      table_schema=parent_schema,
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=parent_ref),
      num_rows=20,
      batch_size=20,
      run_id="fan-parent-null",
      landing_table="p.land.customers_nullable",
      log_table_prefix="customers_nullable",
      identity_columns=("customer_id",),
  )
  child_schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.src.orders_null"
      },
      "schema": [
          {
              "name": "CUST_ID",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "SEGMENT",
              "type": "STRING",
              "mode": "NULLABLE"
          },
          {
              "name": "LINE",
              "type": "STRING",
              "mode": "REQUIRED"
          },
      ]
  })
  child_ref = [{
      "CUST_ID": 900000 + i,
      "SEGMENT": "s",
      "LINE": "xyz"[i % 3]
  } for i in range(40)]
  child_cfg = PipelineConfig(
      table_schema=child_schema,
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=child_ref),
      num_rows=20,
      batch_size=20,
      run_id="fan-child-null",
      landing_table="p.land.orders_null",
      log_table_prefix="orders_null",
      pk_columns=("CUST_ID", "LINE"),
      uniqueness_mode="streaming",
      fanout={
          "driving_cols": ["CUST_ID", "SEGMENT"],
          "histogram": {
              "1": 1
          },
          "cells": {
              "cols": ["LINE"],
              "rows": [["x"], ["y"], ["z"]],
              "counts": [1, 1, 1]
          },
          "exact_cells": True
      },
  )
  specs = [
      TableSpec(
          config=parent_cfg,
          reference_rows=parent_ref,
          landing_sink=WriteToJsonLines(str(tmp_path / "parent_null")),
          dlq_sink=WriteToJsonLines(str(tmp_path / "dlq_parent_null"))),
      TableSpec(
          config=child_cfg,
          reference_rows=child_ref,
          landing_sink=WriteToJsonLines(str(tmp_path / "child_null")),
          dlq_sink=WriteToJsonLines(str(tmp_path / "dlq_child_null")),
          parent_edges=(FkEdgeSpec(
              child_cols=("CUST_ID", "SEGMENT"),
              ref_cols=("customer_id", "segment"),
              parent_landing="p.land.customers_nullable",
              parent_pk=("customer_id",),
              mode="fanout",
              keys_per_batch=10),)),
  ]
  with beam.Pipeline(options=PipelineOptions(["--runner=DirectRunner"])) as p:
    build_relational_pipeline(p, specs)

  parent_rows = _read_jsonl(tmp_path / "parent_null")
  child_rows = _read_jsonl(tmp_path / "child_null")
  assert parent_rows, "the parent must land rows for the test to mean anything"
  assert child_rows, "every parent key was dropped for a NULL INHERITED column"
  assert {r["CUST_ID"] for r in child_rows
         } <= {r["customer_id"] for r in parent_rows}
  assert all(r["SEGMENT"] is None for r in child_rows)  # copied verbatim


_STAR_CHILD_SCHEMA = TableSchema.model_validate({
    "table_info": {
        "table_id": "p.src.orders_star"
    },
    "schema": [
        {
            "name": "CUST_ID",
            "type": "INT64",
            "mode": "REQUIRED"
        },
        {
            "name": "LINE",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "REGION",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "R",
            "type": "STRING",
            "mode": "REQUIRED"
        },
    ],
})

_DRIVING_EDGE = FkEdgeSpec(
    child_cols=("CUST_ID",),
    ref_cols=("customer_id",),
    parent_landing="p.land.customers",
    parent_pk=("customer_id",),
    mode="fanout",
    keys_per_batch=10,
)
_IMPLIED_EDGE = FkEdgeSpec(
    child_cols=("CUST_ID",),
    ref_cols=("customer_id",),
    parent_landing="p.land.hub",
    mode="implied",
)
_INDEPENDENT_EDGE = FkEdgeSpec(
    child_cols=("REGION",),
    ref_cols=("country",),
    parent_landing="p.land.dim",
    mode="side_input",
)
_CONDITIONAL_EDGE = FkEdgeSpec(
    child_cols=("CUST_ID", "R"),
    ref_cols=("customer_id", "r"),
    parent_landing="p.land.right",
    parent_table="right",
    mode="conditional",
    overlap=("CUST_ID",),
    candidate_cap=8,
)


def _star_table_spec(tmp_path, edges: tuple) -> TableSpec:
  """A child TableSpec carrying `edges` — enough for the pure routing
    functions, which never touch the sinks or the reference rows."""
  cfg = PipelineConfig(
      table_schema=_STAR_CHILD_SCHEMA,
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=[]),
      num_rows=10,
      batch_size=10,
      run_id="partition",
      landing_table="p.land.orders_star",
      log_table_prefix="orders_star",
  )
  return TableSpec(
      config=cfg,
      reference_rows=[],
      landing_sink=WriteToJsonLines(str(tmp_path / "star")),
      dlq_sink=WriteToJsonLines(str(tmp_path / "dlq_star")),
      parent_edges=edges,
  )


def test_partition_parent_edges_splits_driving_independent_and_conditional(
    tmp_path,):
  """ADR 0037 §2: every enforced in-set edge gets exactly one of four
    roles, and the partition is what routes it to its DAG path."""
  from sdfb_beam.pipeline import _partition_parent_edges

  spec = _star_table_spec(
      tmp_path,
      (_DRIVING_EDGE, _IMPLIED_EDGE, _INDEPENDENT_EDGE, _CONDITIONAL_EDGE),
  )
  # `p.land.hub` is deliberately ABSENT: an implied edge is satisfied
  # through the driving parent and must never look its own parent up.
  valid = {
      "p.land.customers": "PARENT_DRIVING",
      "p.land.dim": "PARENT_INDEPENDENT",
      "p.land.right": "PARENT_CONDITIONAL",
  }
  fanout, side_inputs, conditional = _partition_parent_edges(spec, valid)
  assert fanout == (_DRIVING_EDGE, "PARENT_DRIVING")
  assert side_inputs == [(2, _INDEPENDENT_EDGE, "PARENT_INDEPENDENT")]
  assert conditional == [(3, _CONDITIONAL_EDGE, "PARENT_CONDITIONAL")]
  # The edge id names the child columns AND the parent — the key the
  # request payload, the plan and the engine all agree on (ruling 14).
  assert _CONDITIONAL_EDGE.edge_id == "(CUST_ID,R)->right"


def test_a_conditional_edge_without_a_driving_edge_stops_the_build(tmp_path):
  """A conditional edge draws its candidates by joining the DRIVING
    key's shared columns; with no driving edge there is nothing to join
    against, so the edge would silently lose referential integrity."""
  from sdfb_beam.pipeline import _partition_parent_edges

  spec = _star_table_spec(tmp_path, (_INDEPENDENT_EDGE, _CONDITIONAL_EDGE))
  with pytest.raises(ValueError) as err:
    _partition_parent_edges(
        spec,
        {
            "p.land.dim": "PARENT_INDEPENDENT",
            "p.land.right": "PARENT_C"
        },
    )
  assert "p.land.orders_star" in str(err.value)
  assert "CUST_ID" in str(err.value) and "R" in str(err.value)


def test_fanout_request_payload_carries_matches_aligned_with_the_keys():
  """The conditional candidates ride WITH the keys, positionally
    aligned, so the engine can hand key i its own candidate list — and a
    plan with no conditional edge keeps today's payload byte for byte."""
  from sdfb_beam.pipeline import _fanout_request_payload

  plain = _fanout_request_payload([("t1", "l1"), ("t2", "l2")],
                                  2.0,
                                  paired=False)
  assert "matches" not in plain
  assert plain["keys"] == [("t1", "l1"), ("t2", "l2")]

  paired = _fanout_request_payload(
      [(("t1", "l1"), {
          "(T,R)->right": [("r1",)]
      }), (("t2", "l2"), {
          "(T,R)->right": []
      })],
      2.0,
      paired=True,
  )
  assert paired["matches"] == {"(T,R)->right": [[("r1",)], []]}
  assert paired["keys"] == [("t1", "l1"), ("t2", "l2")]
  assert paired["n"] == plain["n"] == 4
  # Same first key ⇒ same batch seed: carrying candidates must not
  # re-seed a retried bundle.
  assert paired["batch_id"] == plain["batch_id"]


def test_an_overlap_column_the_driving_edge_lacks_stops_the_build(tmp_path):
  """The join key is read off the driving key tuple BY POSITION, so an
    overlap column the driving edge does not carry has no position at
    all — it must fail by name, not as `x not in tuple` deep in the
    graph build."""
  from sdfb_beam.pipeline import _partition_parent_edges

  stray = FkEdgeSpec(
      child_cols=("REGION", "R"),
      ref_cols=("country", "r"),
      parent_landing="p.land.right",
      parent_table="right",
      mode="conditional",
      overlap=("REGION",),
  )
  spec = _star_table_spec(tmp_path, (_DRIVING_EDGE, stray))
  with pytest.raises(ValueError, match="REGION"):
    _partition_parent_edges(
        spec,
        {
            "p.land.customers": "PARENT_DRIVING",
            "p.land.right": "PARENT_C"
        },
    )


def test_an_overlap_column_the_conditional_edge_lacks_stops_the_build(tmp_path):
  """`_conditional_candidates` indexes the CONDITIONAL edge's own
    `child_cols` too, so an overlap column the driving edge carries but
    this edge does not dies as `tuple.index(x)` deep in the build —
    exactly the message the guard exists to replace."""
  from sdfb_beam.pipeline import _partition_parent_edges

  driving = FkEdgeSpec(
      child_cols=("CUST_ID", "LINE"),
      ref_cols=("customer_id", "line"),
      parent_landing="p.land.customers",
      parent_pk=("customer_id",),
      mode="fanout",
  )
  stray = FkEdgeSpec(
      child_cols=("CUST_ID", "R"),
      ref_cols=("customer_id", "r"),
      parent_landing="p.land.right",
      parent_table="right",
      mode="conditional",
      overlap=("LINE",),
  )
  spec = _star_table_spec(tmp_path, (driving, stray))
  with pytest.raises(ValueError, match="LINE"):
    _partition_parent_edges(
        spec,
        {
            "p.land.customers": "PARENT_DRIVING",
            "p.land.right": "PARENT_C"
        },
    )


def test_a_conditional_edge_with_no_shared_column_stops_the_build(tmp_path):
  """`overlap=()` keys BOTH sides of the join on `()`: the Top-M
    combine and the CoGroupByKey collapse onto a single key — no
    parallelism at all for the whole driving stream — and the edge is
    semantically independent anyway (`relationships.edge_roles` calls it
    that). Invisible until the job stalls, so it must stop at build."""
  from sdfb_beam.pipeline import _partition_parent_edges

  disjoint = FkEdgeSpec(
      child_cols=("R",),
      ref_cols=("r",),
      parent_landing="p.land.right",
      mode="conditional",
      overlap=(),
  )
  spec = _star_table_spec(tmp_path, (_DRIVING_EDGE, disjoint))
  with pytest.raises(ValueError, match="side_input"):
    _partition_parent_edges(
        spec,
        {
            "p.land.customers": "PARENT_DRIVING",
            "p.land.right": "PARENT_C"
        },
    )


def test_a_side_input_edge_sharing_the_driving_columns_stops_the_build(
    tmp_path):
  """The star is only sound because the independent path is DISJOINT
    from the driving key (design §5). An overlapping side-input edge has
    its shared column overwritten by the driving key after the pool draw,
    so it lands a tuple its parent never held — ADR 0036 D1's corruption,
    which the gate can only MEASURE. The composer names it instead."""
  from sdfb_beam.pipeline import _partition_parent_edges

  overlapping = FkEdgeSpec(
      child_cols=("CUST_ID", "REGION"),
      ref_cols=("customer_id", "country"),
      parent_landing="p.land.dim",
      mode="side_input",
  )
  spec = _star_table_spec(tmp_path, (_DRIVING_EDGE, overlapping))
  with pytest.raises(ValueError, match="must be conditional"):
    _partition_parent_edges(
        spec,
        {
            "p.land.customers": "PARENT_DRIVING",
            "p.land.dim": "PARENT_I"
        },
    )


def test_a_dict_valued_key_column_is_never_mistaken_for_matches():
  """A widened driving edge (D4) carries INHERITED columns, and a
    RECORD one lands as a dict — so `(cid, {...})` is a plain two-column
    key. Sniffing the element shape read it as a `(key, matches)` pair:
    the keys became scalars, the batch_id moved (every downstream seed
    with it) and a spurious `"matches"` tripped the DoFn's gate. The
    payload must decide from the GRAPH, never from the element."""
  from sdfb_beam.pipeline import _fanout_request_payload

  ks = [(1, {"tier": "gold"}), (2, {"tier": "silver"})]
  payload = _fanout_request_payload(ks, 2.0, paired=False)
  assert "matches" not in payload
  assert payload["keys"] == ks
  # The batch_id hashes the WHOLE first key, not its first member.
  scalar = _fanout_request_payload([1, 2], 2.0, paired=False)
  assert payload["batch_id"] != scalar["batch_id"]


def test_a_side_input_edge_clashing_with_a_conditional_rest_stops_the_build(
    tmp_path,):
  """The composer's guard was one-sided: it compared every non-driving
    edge with the DRIVING columns and never with each other. A
    side_input edge disjoint from the driving key but sharing a column
    with a conditional edge's `rest` therefore built without complaint —
    and then the conditional override clobbered the pool-drawn tuple
    (`apply_conditional_overrides` runs after the pool draws), landing a
    combination the side-input parent never held. Defence in depth
    behind the registry's own cross-edge stop, for hand-built specs."""
  from sdfb_beam.pipeline import _partition_parent_edges

  clashing = FkEdgeSpec(
      child_cols=("R",),
      ref_cols=("r",),
      parent_landing="p.land.dim",
      mode="side_input",
  )
  spec = _star_table_spec(tmp_path,
                          (_DRIVING_EDGE, _CONDITIONAL_EDGE, clashing))
  with pytest.raises(ValueError) as err:
    _partition_parent_edges(
        spec,
        {
            "p.land.customers": "PARENT_DRIVING",
            "p.land.right": "PARENT_C",
            "p.land.dim": "PARENT_I",
        },
    )
  message = str(err.value)
  assert "p.land.orders_star" in message
  assert "conditional" in message and "side_input" in message
  assert "'R'" in message


def test_a_conditional_rest_colliding_with_the_driving_key_stops_the_build(
    tmp_path,):
  """`overlap` is DECLARED, not derived, so a conditional edge can
    name a driving column it does NOT list as shared: that column then
    falls into `rest` and the candidate draw overwrites the driving
    key's own value — the ADR 0036 D1 corruption, from the conditional
    path this time."""
  from sdfb_beam.pipeline import _partition_parent_edges

  driving = FkEdgeSpec(
      child_cols=("CUST_ID", "LINE"),
      ref_cols=("customer_id", "line"),
      parent_landing="p.land.customers",
      parent_pk=("customer_id", "line"),
      mode="fanout",
  )
  sloppy = FkEdgeSpec(
      child_cols=("CUST_ID", "LINE", "R"),
      ref_cols=("customer_id", "line", "r"),
      parent_landing="p.land.right",
      parent_table="right",
      mode="conditional",
      overlap=("CUST_ID",),
  )
  spec = _star_table_spec(tmp_path, (driving, sloppy))
  with pytest.raises(ValueError, match="LINE"):
    _partition_parent_edges(
        spec,
        {
            "p.land.customers": "PARENT_DRIVING",
            "p.land.right": "PARENT_C"
        },
    )
