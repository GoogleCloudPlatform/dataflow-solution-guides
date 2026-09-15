"""Ruling 10 (ADR 0037 review): the LAUNCHER's own chain, on the laptop.

`test_fanout_shapes.py` hand-builds its `FkEdgeSpec`s, so it proves the
DAG and says nothing about the step BEFORE it — the one that turns a
committed model file into those specs. This test starts from
`config/relationships/example_star_diamond.yaml`, the file the ADR points
operators at, and walks exactly the launcher's path:

    load_relationship_registry(uri)
      -> registry.generation_waves(...)        parents-first run order
      -> registry.edge_roles(table)            driving / independent /
                                               implied / conditional
      -> in_set_parent_edges(...)              the composer's specs
      -> conditional_plan_entries(...)         the plan's `conditional`
      -> build_relational_pipeline             DirectRunner, FakeModelClient

Nothing about the edges is written by hand: the modes, the overlap, the
NULL policy and the `edge_id`s all come out of the model. What the run
then proves is that those derived specs LAND a referentially intact model
— whole-tuple containment on every enforced edge, a unique PK on every
table — and that the plan's `conditional[*].id` is the very string the
composer stamps on the request payload's `matches` (the DoFn looks the
candidates up by that key, so a mismatch is silent data loss).

The measured facts a real launch reads from BigQuery — the fan-out
histogram and the PK cell table — are the only hand-written part; there
is no BigQuery here.
"""

from __future__ import annotations

import json
from pathlib import Path

import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions
from sdfb_beam.cli.run_pipeline import (
    conditional_plan_entries,
    in_set_parent_edges,
)
from sdfb_beam.io.local_sinks import WriteToJsonLines
from sdfb_beam.io.relationships import load_relationship_registry
from sdfb_beam.pipeline import (
    PipelineConfig,
    TableSpec,
    build_relational_pipeline,
)
from sdfb_core.contracts import TableSchema
from sdfb_tests.fakes import FakeModelClient

_MODEL_URI = str(
    Path(__file__).resolve().parents[4] / "config" / "relationships" /
    "example_star_diamond.yaml")

# Small on purpose: the point is the chain, not throughput.
_CANDIDATE_CAP = 8
_KEYS_PER_BATCH = 10

# --- helpers (shape as in test_fanout_shapes.py) ---------------------------


def _schema(table: str, cols: list[tuple[str, ...]]) -> TableSchema:
  """``(name, type)`` is REQUIRED; ``(name, type, mode)`` pins the mode —
    `in_set_parent_edges` reads the LANDING modes to decide a conditional
    edge's NULL policy."""
  return TableSchema.model_validate({
      "table_info": {
          "table_id": f"p.src.{table}"
      },
      "schema": [{
          "name": c[0],
          "type": c[1],
          "mode": c[2] if len(c) > 2 else "REQUIRED"
      } for c in cols]
  })


def _read(prefix: Path) -> list[dict]:
  rows = []
  for f in sorted(prefix.parent.glob(prefix.name + "*")):
    rows.extend(json.loads(line) for line in f.read_text().splitlines() if line)
  return rows


def _sinks(tmp_path: Path, name: str) -> dict:
  return {
      "landing_sink": WriteToJsonLines(str(tmp_path / name)),
      "dlq_sink": WriteToJsonLines(str(tmp_path / f"dlq_{name}"))
  }


def _run(specs: list[TableSpec]) -> None:
  with beam.Pipeline(options=PipelineOptions(["--runner=DirectRunner"])) as p:
    build_relational_pipeline(p, specs)


def _tuples(rows: list[dict], *cols: str) -> set[tuple]:
  return {tuple(r[c] for c in cols) for r in rows}


# --- the model's seven tables ---------------------------------------------

_COLUMNS: dict[str, list[tuple[str, ...]]] = {
    "dim_a": [("A_KEY", "STRING"), ("A_VAL", "INT64")],
    "dim_b": [("B_KEY", "STRING"), ("B_VAL", "INT64")],
    "fact": [("A_KEY", "STRING"), ("B_KEY", "STRING"), ("LINE_NO", "STRING"),
             ("AMOUNT", "INT64")],
    "top": [("T", "STRING"), ("T_VAL", "INT64")],
    "left": [("T", "STRING"), ("L", "STRING"), ("L_VAL", "INT64")],
    "right": [("T", "STRING"), ("R", "STRING"), ("R_VAL", "INT64")],
    # `R` is REQUIRED, so the conditional edge's NULL policy is "drop the
    # key" — and `right` covers every `T`, so nothing is ever dropped.
    "bottom": [("T", "STRING"), ("L", "STRING"), ("R", "STRING"),
               ("B_VAL", "INT64")],
}

# A short, low-cardinality STRING profiles as CATEGORICAL, so a root lands
# values from ITS OWN reference domain. `fact`'s own `B_KEY` domain is
# DISJOINT from `dim_b`'s: containment then proves the side input reached
# the engine rather than the reference pool having agreed by accident.
_REFERENCE: dict[str, list[dict]] = {
    "dim_a": [{
        "A_KEY": f"a{i % 8}",
        "A_VAL": i * 997
    } for i in range(40)],
    "dim_b": [{
        "B_KEY": f"b{i % 8}",
        "B_VAL": i * 991
    } for i in range(40)],
    "fact": [{
        "A_KEY": f"a{i % 8}",
        "B_KEY": f"zz{i % 8}",
        "LINE_NO": "123"[i % 3],
        "AMOUNT": i * 7
    } for i in range(40)],
    "top": [{
        "T": f"t{i % 8}",
        "T_VAL": i * 983
    } for i in range(40)],
    "left": [{
        "T": f"t{i % 8}",
        "L": "lmn"[i % 3],
        "L_VAL": i
    } for i in range(40)],
    "right": [{
        "T": f"t{i % 8}",
        "R": "rst"[i % 3],
        "R_VAL": i * 2
    } for i in range(40)],
    "bottom": [{
        "T": f"t{i % 8}",
        "L": "lmn"[i % 3],
        "R": "rst"[i % 3],
        "B_VAL": i * 3
    } for i in range(40)],
}

_ROWS = {
    "dim_a": 40,
    "dim_b": 40,
    "fact": 48,
    "top": 12,
    "left": 24,
    "right": 24,
    "bottom": 48
}

# What a real launch MEASURES in BigQuery (`measure_fanout` /
# `pk_cell_columns`), keyed by the driven table. Everything else about
# these tables is derived from the model file.
_MEASURED: dict[str, dict] = {
    "fact": {
        "driving_cols": ["A_KEY"],
        "histogram": {
            "1": 5,
            "3": 5
        },
        "cells": {
            "cols": ["LINE_NO"],
            "rows": [["1"], ["2"], ["3"]],
            "counts": [1, 1, 1]
        },
        "exact_cells": True
    },
    "left": {
        "driving_cols": ["T"],
        "histogram": {
            "1": 1,
            "2": 1
        },
        "cells": {
            "cols": ["L"],
            "rows": [["l1"], ["l2"]],
            "counts": [1, 1]
        },
        "exact_cells": True
    },
    # Exactly two children per `top` key, so every `T` reaches `bottom`
    # with two distinct `R` candidates — as many as its own fan-out.
    "right": {
        "driving_cols": ["T"],
        "histogram": {
            "2": 1
        },
        "cells": {
            "cols": ["R"],
            "rows": [["r1"], ["r2"]],
            "counts": [1, 1]
        },
        "exact_cells": True
    },
    # `R` is not a cell: the conditional edge supplies it, so the PK's
    # third member comes from the co-parent's candidate draw.
    "bottom": {
        "driving_cols": ["T", "L"],
        "histogram": {
            "1": 1,
            "2": 1
        },
        "cells": None,
        "exact_cells": False
    },
}


def _build_specs(tmp_path: Path, registry, order: list[str]):
  """One `TableSpec` per table, built the way `_prepare_table_spec` does.

    Returns ``(specs, edges_by_table, fanout_by_table)`` so the assertions
    can read the derived specs and plan entries, not just the landed rows.
    """
  in_set = set(_COLUMNS)
  specs: list[TableSpec] = []
  edges_by_table: dict[str, tuple] = {}
  fanout_by_table: dict[str, dict | None] = {}
  for name in order:
    landing_table = f"p.land.{name}"
    landing_schema = _schema(name, _COLUMNS[name])
    roles = registry.edge_roles(landing_table)
    edges = in_set_parent_edges(
        registry,
        landing_table,
        in_set_names=in_set,
        key_sample_caps={},
        edge_roles=roles,
        keys_per_batch=_KEYS_PER_BATCH,
        table_schema=landing_schema,
        candidate_cap=_CANDIDATE_CAP,
    )
    fanout = None
    if name in _MEASURED:
      fanout = {
          **_MEASURED[name],
          "conditional":
              conditional_plan_entries(
                  registry,
                  landing_table,
                  roles,
                  landing_schema,
                  # The model's own `pk:` — the PK the run enforces.
                  effective_pk=tuple(registry.relations(landing_table).pk),
              ),
          "candidate_cap":
              _CANDIDATE_CAP,
      }
    reference = _REFERENCE[name]
    pk = tuple(registry.relations(landing_table).pk)
    # The model declares no `identity:`, and a root's key would then be
    # a repeated CATEGORICAL value — `uniqueness_mode="streaming"`
    # MEASURES duplicates rather than diverting them, so the roots
    # would land a non-unique PK for reasons that have nothing to do
    # with the edges under test. Synthesizing the key per row (what
    # `_root` does in test_fanout_shapes.py) makes every parent key
    # unique by construction, which is the precondition the whole
    # fan-out path assumes.
    identity = pk if not edges else ()
    config = PipelineConfig(
        table_schema=landing_schema,
        engine_name="b1_rag",
        model_client=FakeModelClient(reference_pool=reference),
        run_id=f"chain-{name}",
        landing_table=landing_table,
        log_table_prefix=name,
        batch_size=20,
        num_rows=_ROWS[name],
        pk_columns=pk,
        identity_columns=identity,
        uniqueness_mode="streaming",
        **({
            "fanout": fanout
        } if fanout else {}),
    )
    specs.append(
        TableSpec(
            config=config,
            reference_rows=reference,
            **_sinks(tmp_path, name),
            parent_edges=edges))
    edges_by_table[name] = edges
    fanout_by_table[name] = fanout
  return specs, edges_by_table, fanout_by_table


def test_the_example_model_launches_as_declared(tmp_path):
  """The committed star+diamond model, from YAML to landed rows."""
  registry = load_relationship_registry(_MODEL_URI)
  tables = tuple(_COLUMNS)
  assert set(registry.model_for("fact").tables) == set(tables)

  # 1. Run order comes from the registry, not from this file.
  waves = registry.generation_waves(tables)
  order = [t for wave in waves for t in wave]
  assert sorted(order) == sorted(tables)
  for name in order:
    for edge in registry.enforced_edges(f"p.land.{name}"):
      assert order.index(edge.ref) < order.index(name)

  specs, edges, fanout = _build_specs(tmp_path, registry, order)

  # 2. The four roles of ADR 0037 came out of the MODEL.
  assert [(e.child_cols, e.mode) for e in edges["fact"]] == [
      (("A_KEY",), "fanout"),  # first declared drives (ruling A)
      (("B_KEY",), "side_input"),  # the star's other dimension
  ]
  assert registry.driving_choice("fact") == "first_declared"
  assert [(e.child_cols, e.mode) for e in edges["bottom"]] == [
      (("T", "L"), "fanout"),  # `drives: true` in the model file
      (("T", "R"), "conditional"),
  ]
  conditional = [e for e in edges["bottom"] if e.mode == "conditional"]
  assert conditional[0].overlap == ("T",)  # the shared ancestor column
  assert conditional[0].parent_landing == "p.land.right"
  assert conditional[0].parent_table == "right"
  assert conditional[0].nullable is False  # `R` is REQUIRED

  # 3. The plan's `conditional` ids ARE the composer's `edge_id`s — the
  #    key the DoFn looks the candidates up by. Asserted through the
  #    specs, because that is the only pairing the DAG ever makes.
  assert [c["id"] for c in fanout["bottom"]["conditional"]
         ] == [e.edge_id for e in conditional]
  assert [c["id"] for c in fanout["bottom"]["conditional"]] == ["(T,R)->right"]
  assert fanout["bottom"]["conditional"][0]["cols"] == ["R"]
  # G1: `R` completes bottom's `pk: [T, L, R]`, so this edge is the one
  # that may multiply a key's capacity — read off the MODEL, never
  # guessed from the role (`conditional` only means "shares a column").
  assert fanout["bottom"]["conditional"][0]["pk_member"] is True
  assert fanout["bottom"]["candidate_cap"] == _CANDIDATE_CAP
  # The star's fact has no conditional edge at all.
  assert fanout["fact"]["conditional"] == []

  _run(specs)

  landed = {name: _read(tmp_path / name) for name in tables}
  assert all(landed[name] for name in tables)

  # 4. Referential integrity on WHOLE TUPLES, every enforced edge.
  for name in tables:
    for edge in registry.enforced_edges(f"p.land.{name}"):
      child = _tuples(landed[name], *edge.cols)
      parent = _tuples(landed[edge.ref], *edge.ref_cols)
      assert child <= parent, f"{name} {edge.cols} -> {edge.ref}"

  # 5. Every PK unique, on the PK the MODEL declares.
  for name in tables:
    pk = registry.relations(f"p.land.{name}").pk
    assert len(_tuples(landed[name], *pk)) == len(landed[name]), name

  # 6. Nothing referential was lost to get there: no orphan diverted by
  #    the FK gate, no key dropped for want of a conditional candidate.
  for name in tables:
    rules = {r.get("rule_id") for r in _read(tmp_path / f"dlq_{name}")}
    assert not rules & {"fk.orphan", "fk.unmatched"}, (name, rules)
