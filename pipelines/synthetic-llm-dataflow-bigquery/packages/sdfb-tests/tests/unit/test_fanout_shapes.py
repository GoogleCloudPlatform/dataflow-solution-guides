"""ADR 0037 acceptance on the laptop: one DirectRunner run per multi-parent
shape (design 2026-09-11 §10).

Before ADR 0037 an edge that was neither driving nor implied was a
`RelationshipError` — the star-schema fact and the true diamond STOPPED the
launch. Each test here builds one of those shapes out of `TableSpec`s, runs
the whole relational DAG on the DirectRunner with `FakeModelClient`, and
reads the landed JSONL back to check WHOLE TUPLES (never per-column
membership, which is the 2026-08-23 orphan defect) against the parent that
actually landed them:

- star          — a fact under two dimensions: one `fanout` edge, one
                  `side_input` edge, zero `fk.orphan`.
- diamond       — two branches under one root, rejoined: the conditional
                  edge's `rest` comes from a co-parent row that holds the
                  SHARED value, never from the child's own marginals.
- existence     — a conditional edge with an EMPTY rest is a pure existence
                  filter: a driving key absent from the co-parent is dropped
                  before generation and reaches the DLQ as `fk.unmatched`,
                  weighted by its expected rows (ruling B, §4).
- nullable      — the same shape with a NULLABLE rest: the unmatched keys
                  land with NULL instead of being dropped, and nothing
                  reaches the DLQ.
- skew          — an INEXACT PK (a synthesized member completes it) under a
                  conditional edge: the PK-completing cells are then not a
                  key, so they keep their 9:1 SOURCE marginal instead of
                  being drawn without replacement (final review, E1).
- graph         — six tables mixing a tree, a star fact and a 1:1 chain in
                  ONE job: every enforced edge holds, every PK is unique.

Every table's key values are controlled by construction: identity columns
are synthesized per row (`apply_identity_columns`), so a root's landed keys
are unique; a low-cardinality STRING column profiles as CATEGORICAL, so a
root's landed values come from ITS OWN reference domain — which is how the
existence filter's half-overlap is built.
"""

from __future__ import annotations

import json
from pathlib import Path

import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions
from sdfb_beam.io.local_sinks import WriteToJsonLines
from sdfb_beam.pipeline import FkEdgeSpec, PipelineConfig, TableSpec, build_relational_pipeline
from sdfb_core.contracts import TableSchema
from sdfb_tests.fakes import FakeModelClient


def _schema(table: str, cols: list[tuple[str, ...]]) -> TableSchema:
  """``(name, type)`` is REQUIRED; ``(name, type, mode)`` pins the mode —
    the NULL policy (§4 ruling B) is read off the LANDING schema, so the
    nullable shape must be able to declare a NULLABLE rest column."""
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


def _cfg(schema: TableSchema, ref: list[dict], name: str,
         **kw) -> PipelineConfig:
  return PipelineConfig(
      table_schema=schema,
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=ref),
      run_id=f"shape-{name}",
      landing_table=f"p.land.{name}",
      log_table_prefix=name,
      batch_size=20,
      **kw)


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


def _dlq(tmp_path: Path, name: str, rule_id: str) -> list[dict]:
  return [
      r for r in _read(tmp_path / f"dlq_{name}") if r.get("rule_id") == rule_id
  ]


def _assert_no_unexpected_dlq(tmp_path: Path, name: str, *allowed: str) -> None:
  """No envelope this shape did not ASK for (ADR 0037 final review).

    Each shape asserted only on `fk.orphan` / `fk.unmatched`, so a run
    whose key batches died as `engine_failure` — every child of every key
    lost, the whole point of the shape unproven — still passed. Anything
    outside `allowed` fails here, with the offending envelopes named."""
  unexpected = [
      r for r in _read(tmp_path / f"dlq_{name}")
      if r.get("rule_id") not in allowed
  ]
  assert not unexpected, [
      (r.get("rule_id"), r.get("error_detail")) for r in unexpected[:3]
  ]


def _root(tmp_path: Path, name: str, key: str, rows: int) -> TableSpec:
  """A root table whose `key` is a synthesized identity column (unique by
    construction) and whose payload column is plain numeric — no free-text
    pool, so the shape tests stay seconds, not minutes."""
  schema = _schema(name, [(key, "STRING"), (f"{key}_VAL", "INT64")])
  # 8 repeated key values keep the column CATEGORICAL (cheap profile);
  # `apply_identity_columns` overwrites every landed value with a UUID.
  # The payload column spans a WIDE range on purpose: `EnforceUniqueness`
  # digests the row with the identity columns REMOVED (they are unique by
  # construction, so hashing them would make the rule idle), and a narrow
  # payload would collapse most of the root as `row.duplicate`.
  ref = [{key: f"seed{i % 8}", f"{key}_VAL": i * 997} for i in range(40)]
  cfg = _cfg(
      schema,
      ref,
      name,
      num_rows=rows,
      pk_columns=(key,),
      identity_columns=(key,))
  return TableSpec(config=cfg, reference_rows=ref, **_sinks(tmp_path, name))


# ---------------------------------------------------------------------------
# star: fact -> dim_a (driving) + fact -> dim_b (independent)
# ---------------------------------------------------------------------------


def test_star_fact_two_dimensions(tmp_path):
  """The shape the registry STOPPED on (design §1): a fact under two
    dimensions with no ancestry between them. The first edge drives — its
    parent's landed keys are the request stream — and the second keeps the
    ADR 0030/0031 sampled-key-pool side input, so both FK columns are a
    parent's landed value and the `fk.orphan` gate measures the second."""
  fact_schema = _schema("star_fact", [("A_ID", "STRING"), ("B_ID", "STRING"),
                                      ("SEQ", "STRING"), ("AMOUNT", "INT64")])
  # B_ID values in the reference are DISJOINT from anything dim_b can land
  # (which is a UUID), so containment proves the side input reached the engine.
  fact_ref = [{
      "A_ID": f"a{i % 8}",
      "B_ID": f"zz{i % 8}",
      "SEQ": "s123"[i % 3],
      "AMOUNT": i * 7
  } for i in range(40)]
  fact_cfg = _cfg(
      fact_schema,
      fact_ref,
      "star_fact",
      num_rows=80,
      pk_columns=("A_ID", "B_ID", "SEQ"),
      uniqueness_mode="streaming",
      fanout={
          "driving_cols": ["A_ID"],
          "histogram": {
              "1": 5,
              "3": 5
          },
          "cells": {
              "cols": ["SEQ"],
              "rows": [["s1"], ["s2"], ["s3"]],
              "counts": [1, 1, 1]
          },
          "exact_cells": True,
          "conditional": [],
          "candidate_cap": 64
      })
  specs = [
      _root(tmp_path, "star_dim_a", "A_ID", 40),
      _root(tmp_path, "star_dim_b", "B_ID", 30),
      TableSpec(
          config=fact_cfg,
          reference_rows=fact_ref,
          **_sinks(tmp_path, "star_fact"),
          parent_edges=(
              FkEdgeSpec(
                  child_cols=("A_ID",),
                  ref_cols=("A_ID",),
                  parent_landing="p.land.star_dim_a",
                  parent_pk=("A_ID",),
                  mode="fanout",
                  keys_per_batch=10),
              FkEdgeSpec(
                  child_cols=("B_ID",),
                  ref_cols=("B_ID",),
                  parent_landing="p.land.star_dim_b",
                  parent_pk=("B_ID",),
                  mode="side_input"),
          )),
  ]
  _run(specs)

  dim_a = _read(tmp_path / "star_dim_a")
  dim_b = _read(tmp_path / "star_dim_b")
  fact = _read(tmp_path / "star_fact")
  assert dim_a and dim_b and fact
  assert _tuples(fact, "A_ID") <= _tuples(dim_a, "A_ID")  # driving edge
  assert _tuples(fact, "B_ID") <= _tuples(dim_b, "B_ID")  # independent edge
  assert len(_tuples(fact, "A_ID", "B_ID", "SEQ")) == len(fact)  # PK unique
  # The histogram bounds the fact: every dimension-a key yields 1 or 3 rows.
  assert len(dim_a) <= len(fact) <= len(dim_a) * 3
  # …and the gate MEASURED the independent edge rather than assuming it.
  assert not _dlq(tmp_path, "star_fact", "fk.orphan")
  _assert_no_unexpected_dlq(tmp_path, "star_fact")


# ---------------------------------------------------------------------------
# diamond: bottom -> left (driving) + bottom -> right (conditional on T)
# ---------------------------------------------------------------------------


def _diamond_specs(tmp_path: Path,
                   tag: str,
                   right_histogram: dict,
                   rest_mode: str,
                   nullable: bool,
                   bottom_histogram: dict | None = None) -> list[TableSpec]:
  """`top` -> {`left`, `right`} -> `bottom`, the true diamond of design §1.

    `bottom` is driven by `left` (the whole `(T, L)` tuple) and carries a
    CONDITIONAL edge to `right` sharing `T`: `R` must come from a `right`
    row that holds the same `T`. `right_histogram` decides how much of
    `top` the co-parent covers, `rest_mode`/`nullable` which NULL policy
    branch the unmatched keys take."""
  left_schema = _schema(f"{tag}_left", [("T", "STRING"), ("L", "STRING"),
                                        ("L_VAL", "INT64")])
  left_ref = [{
      "T": f"t{i % 8}",
      "L": "lmn"[i % 3],
      "L_VAL": i
  } for i in range(40)]
  right_schema = _schema(f"{tag}_right", [("T", "STRING"), ("R", "STRING"),
                                          ("R_VAL", "INT64")])
  right_ref = [{
      "T": f"t{i % 8}",
      "R": "rst"[i % 3],
      "R_VAL": i * 2
  } for i in range(40)]
  bottom_schema = _schema(f"{tag}_bottom", [("T", "STRING"), ("L", "STRING"),
                                            ("R", "STRING", rest_mode),
                                            ("S", "STRING"),
                                            ("B_VAL", "INT64")])
  bottom_ref = [{
      "T": f"t{i % 8}",
      "L": "lmn"[i % 3],
      "R": "rst"[i % 3],
      "S": "uv"[i % 2],
      "B_VAL": i * 3
  } for i in range(40)]
  branch = {
      "histogram": {
          "1": 1,
          "2": 1
      },
      "cells": {
          "cols": ["L"],
          "rows": [["l1"], ["l2"]],
          "counts": [1, 1]
      },
      "exact_cells": True,
      "conditional": [],
      "candidate_cap": 64
  }
  left_cfg = _cfg(
      left_schema,
      left_ref,
      f"{tag}_left",
      num_rows=24,
      pk_columns=("T", "L"),
      uniqueness_mode="streaming",
      fanout={
          "driving_cols": ["T"],
          **branch
      })
  right_cfg = _cfg(
      right_schema,
      right_ref,
      f"{tag}_right",
      num_rows=24,
      pk_columns=("T", "R"),
      uniqueness_mode="streaming",
      fanout={
          "driving_cols": ["T"],
          **branch, "histogram": right_histogram,
          "cells": {
              "cols": ["R"],
              "rows": [["r1"], ["r2"]],
              "counts": [1, 1]
          }
      })
  # PK drops R on the nullable shape: a NULL member is not a key (ADR 0031).
  bottom_pk = ("T", "L", "R", "S") if rest_mode == "REQUIRED" else ("T", "L",
                                                                    "S")
  bottom_cfg = _cfg(
      bottom_schema,
      bottom_ref,
      f"{tag}_bottom",
      num_rows=48,
      pk_columns=bottom_pk,
      uniqueness_mode="streaming",
      fanout={
          "driving_cols": ["T", "L"],
          "histogram":
              bottom_histogram or {
                  "1": 1,
                  "2": 1
              },
          "cells": {
              "cols": ["S"],
              "rows": [["s1"], ["s2"]],
              "counts": [1, 1]
          },
          "exact_cells":
              True,
          "candidate_cap":
              64,
          # `id` IS `FkEdgeSpec.edge_id` — the DoFn and the
          # engine look the candidates up by that string.
          # `R` bounds a key only when the PK HOLDS it:
          # the nullable shape drops it from the PK (a
          # NULL member is not a key, ADR 0031), so it
          # must not multiply the capacity there (G1).
          "conditional": [{
              "id": f"(T,R)->{tag}_right",
              "cols": ["R"],
              "nullable": nullable,
              "pk_member": "R" in bottom_pk
          }]
      })
  edge_top = FkEdgeSpec(
      child_cols=("T",),
      ref_cols=("T",),
      parent_landing=f"p.land.{tag}_top",
      parent_table=f"{tag}_top",
      parent_pk=("T",),
      mode="fanout",
      keys_per_batch=10)
  return [
      _root(tmp_path, f"{tag}_top", "T", 12),
      TableSpec(
          config=left_cfg,
          reference_rows=left_ref,
          **_sinks(tmp_path, f"{tag}_left"),
          parent_edges=(edge_top,)),
      TableSpec(
          config=right_cfg,
          reference_rows=right_ref,
          **_sinks(tmp_path, f"{tag}_right"),
          parent_edges=(edge_top,)),
      TableSpec(
          config=bottom_cfg,
          reference_rows=bottom_ref,
          **_sinks(tmp_path, f"{tag}_bottom"),
          parent_edges=(
              FkEdgeSpec(
                  child_cols=("T", "L"),
                  ref_cols=("T", "L"),
                  parent_landing=f"p.land.{tag}_left",
                  parent_table=f"{tag}_left",
                  parent_pk=("T", "L"),
                  mode="fanout",
                  keys_per_batch=10),
              FkEdgeSpec(
                  child_cols=("T", "R"),
                  ref_cols=("T", "R"),
                  parent_landing=f"p.land.{tag}_right",
                  parent_table=f"{tag}_right",
                  parent_pk=("T", "R"),
                  mode="conditional",
                  overlap=("T",),
                  candidate_cap=64,
                  nullable=nullable),
          )),
  ]


def test_diamond_two_branches_rejoin(tmp_path):
  """Both branches hold at once and the two `T`s are ONE value: `(T, L)`
    exists in `left` because it IS a left key, and `(T, R)` exists in
    `right` because the candidate was joined on that same `T`. `right`
    covers every `T` (`histogram {"2": 1}`), so no key is unmatched."""
  _run(_diamond_specs(tmp_path, "dia", {"2": 1}, "REQUIRED", False))

  left = _read(tmp_path / "dia_left")
  right = _read(tmp_path / "dia_right")
  bottom = _read(tmp_path / "dia_bottom")
  assert left and right and bottom
  assert _tuples(bottom, "T", "L") <= _tuples(left, "T", "L")  # driving
  assert _tuples(bottom, "T", "R") <= _tuples(right, "T", "R")  # conditional
  assert len(_tuples(bottom, "T", "L", "R", "S")) == len(bottom)  # PK unique
  assert not _dlq(tmp_path, "dia_bottom", "fk.unmatched")
  _assert_no_unexpected_dlq(tmp_path, "dia_bottom")


def test_fanout_beyond_the_cells_rides_the_conditional_candidates(tmp_path):
  """Fix wave A1 end to end (the shape the review found missing): the
    driven child asks for THREE children per driving key from a TWO-row
    cell table, with a conditional edge covering the rest.

    Before the fix `CellTable.draw(3, rng, exact=True)` RAISED inside the
    engine and the whole key batch landed as `engine_failure` — a
    configuration preflight accepts, because the cells and the edge's
    candidates jointly offer 2 x 2 = 4 combinations. Three children per
    key are representable, and the three PKs must differ."""
  _run(
      _diamond_specs(
          tmp_path,
          "cap", {"2": 1},
          "REQUIRED",
          False,
          bottom_histogram={"3": 1}))

  right = _read(tmp_path / "cap_right")
  bottom = _read(tmp_path / "cap_bottom")
  assert right and bottom
  assert _tuples(bottom, "T", "R") <= _tuples(right, "T", "R")  # conditional
  assert len(_tuples(bottom, "T", "L", "R", "S")) == len(bottom)  # PK unique
  per_key: dict[tuple, list[tuple]] = {}
  for row in bottom:
    per_key.setdefault((row["T"], row["L"]), []).append((row["S"], row["R"]))
  # The fan-out really did exceed the cell table…
  assert any(len(v) == 3 for v in per_key.values()), per_key
  # …and every child of a key still holds a DIFFERENT (cell, candidate)
  # combination — the joint walk, not two independent cyclic ones.
  for combos in per_key.values():
    assert len(set(combos)) == len(combos)
  _assert_no_unexpected_dlq(tmp_path, "cap_bottom")


def test_nullable_branch_lands_null(tmp_path):
  """NULL policy, nullable half (ruling B): `right` covers only half the
    `T`s (`histogram {"0": 1, "2": 1}`) and `R` is NULLABLE, so a key with
    no candidate is NOT dropped — its rows land with `R = NULL` (a
    legitimately parentless tuple, ADR 0031) and nothing reaches the DLQ."""
  _run(_diamond_specs(tmp_path, "nul", {"0": 1, "2": 1}, "NULLABLE", True))

  right = _read(tmp_path / "nul_right")
  bottom = _read(tmp_path / "nul_bottom")
  assert right and bottom
  covered = _tuples(right, "T")
  matched = [r for r in bottom if (r["T"],) in covered]
  unmatched = [r for r in bottom if (r["T"],) not in covered]
  # Both branches are exercised — otherwise the claim is vacuous.
  assert matched and unmatched
  assert all(r["R"] is None for r in unmatched)
  assert _tuples(matched, "T", "R") <= _tuples(right, "T", "R")
  # PK unique on the NARROWED key — `R` is excluded because a NULL member
  # is not a key (ADR 0031), which is exactly what this shape lands.
  assert len(_tuples(bottom, "T", "L", "S")) == len(bottom)
  assert not _read(tmp_path / "dlq_nul_bottom")


# ---------------------------------------------------------------------------
# skew: an INEXACT PK under a conditional edge — the cells are a MARGINAL
# ---------------------------------------------------------------------------


def test_inexact_cells_keep_their_skewed_marginal(tmp_path):
  """ADR 0037 final review E1, end to end — the combination every other
    shape here was missing: `exact_cells=False` AND a conditional edge.

    `GRADE` is NOT a PK member (a synthesized `SEQ_ID` completes the key),
    so ADR 0036 does not need it to key anything: its cells carry only the
    source MARGINAL — 9:1 — and are drawn WITH replacement. The joint walk
    indexed a weighted PERMUTATION prefix regardless of exactness, which
    hands every key one of each cell at a fan-out of 2 and lands an exact
    50:50 — a whole column's distribution inverted, with not one
    end-to-end shape noticing.
    """
  right_schema = _schema("sk_right", [("T", "STRING"), ("R", "STRING"),
                                      ("R_VAL", "INT64")])
  right_ref = [{
      "T": f"t{i % 8}",
      "R": "rst"[i % 3],
      "R_VAL": i * 2
  } for i in range(40)]
  right_cfg = _cfg(
      right_schema,
      right_ref,
      "sk_right",
      num_rows=40,
      pk_columns=("T", "R"),
      uniqueness_mode="streaming",
      fanout={
          "driving_cols": ["T"],
          "histogram": {
              "2": 1
          },
          "cells": {
              "cols": ["R"],
              "rows": [["r1"], ["r2"]],
              "counts": [1, 1]
          },
          "exact_cells": True,
          "conditional": [],
          "candidate_cap": 64
      })
  child_schema = _schema("sk_child", [("T", "STRING"), ("R", "STRING"),
                                      ("GRADE", "STRING"), ("SEQ_ID", "STRING"),
                                      ("C_VAL", "INT64")])
  child_ref = [{
      "T": f"t{i % 8}",
      "R": "rst"[i % 3],
      "GRADE": "gh"[i % 2],
      "SEQ_ID": f"seq{i % 8}",
      "C_VAL": i * 13
  } for i in range(40)]
  right_edge = FkEdgeSpec(
      child_cols=("T", "R"),
      ref_cols=("T", "R"),
      parent_landing="p.land.sk_right",
      parent_table="sk_right",
      parent_pk=("T", "R"),
      mode="conditional",
      overlap=("T",),
      candidate_cap=64)
  top_edge = FkEdgeSpec(
      child_cols=("T",),
      ref_cols=("T",),
      parent_landing="p.land.sk_top",
      parent_table="sk_top",
      parent_pk=("T",),
      mode="fanout",
      keys_per_batch=10)
  # The PK is (T, SEQ_ID): `SEQ_ID` is synthesized per row, which is the
  # UNBOUNDED member that makes the cells inexact in the first place.
  child_cfg = _cfg(
      child_schema,
      child_ref,
      "sk_child",
      num_rows=80,
      pk_columns=("T", "SEQ_ID"),
      identity_columns=("SEQ_ID",),
      uniqueness_mode="streaming",
      fanout={
          "driving_cols": ["T"],
          "histogram": {
              "2": 1
          },
          "cells": {
              "cols": ["GRADE"],
              "rows": [["g1"], ["g2"]],
              "counts": [9, 1]
          },
          "exact_cells":
              False,
          "candidate_cap":
              64,
          "conditional": [{
              "id": right_edge.edge_id,
              "cols": ["R"],
              "nullable": False
          }]
      })
  _run([
      _root(tmp_path, "sk_top", "T", 40),
      TableSpec(
          config=right_cfg,
          reference_rows=right_ref,
          **_sinks(tmp_path, "sk_right"),
          parent_edges=(top_edge,)),
      TableSpec(
          config=child_cfg,
          reference_rows=child_ref,
          **_sinks(tmp_path, "sk_child"),
          parent_edges=(top_edge, right_edge)),
  ])

  right = _read(tmp_path / "sk_right")
  child = _read(tmp_path / "sk_child")
  assert right and child
  assert _tuples(child, "T", "R") <= _tuples(right, "T",
                                             "R")  # conditional, whole tuple
  assert len(_tuples(child, "T", "SEQ_ID")) == len(child)  # PK unique
  assert len(child) >= 60, len(child)  # 40 driving keys x 2: a stable sample
  heavy = sum(1 for row in child if row["GRADE"] == "g1") / len(child)
  # Source weight 0.9. A permutation prefix lands EXACTLY 0.5 here; the
  # upper bound catches the opposite collapse (the rare cell never drawn).
  assert 0.75 < heavy < 1.0, heavy
  # The direct signature of drawing WITH replacement: some key holds the
  # same cell twice, which a permutation prefix can never do.
  by_key: dict[str, list[str]] = {}
  for row in child:
    by_key.setdefault(row["T"], []).append(row["GRADE"])
  assert any(len(set(g)) == 1 for g in by_key.values() if len(g) > 1), by_key
  _assert_no_unexpected_dlq(tmp_path, "sk_child")


# ---------------------------------------------------------------------------
# existence filter: (K)->P driving, (K)->Q conditional with an EMPTY rest
# ---------------------------------------------------------------------------


def test_existence_filter_drops_unmatched_keys(tmp_path):
  """A conditional edge whose `rest` is empty adds no column — it is a
    pure existence filter: the child's `K` must exist in `Q` as well as in
    `P`. `Q`'s reference domain is half of `P`'s, and both are CATEGORICAL
    (a short, low-cardinality STRING), so each root lands exactly its own
    domain. Every `P` key outside `Q` is removed BEFORE generation and
    reaches the DLQ as `fk.unmatched`, weighted by its expected rows."""
  p_schema = _schema("ex_p", [("K", "STRING"), ("P_VAL", "INT64")])
  p_ref = [{"K": f"K{i % 8}", "P_VAL": i} for i in range(48)]
  q_schema = _schema("ex_q", [("K", "STRING"), ("Q_VAL", "INT64")])
  q_ref = [{"K": f"K{i % 4}", "Q_VAL": i} for i in range(48)]
  child_schema = _schema("ex_child", [("K", "STRING"), ("C_SEQ", "STRING"),
                                      ("C_VAL", "INT64")])
  child_ref = [{
      "K": f"K{i % 8}",
      "C_SEQ": "cd"[i % 2],
      "C_VAL": i
  } for i in range(40)]
  child_cfg = _cfg(
      child_schema,
      child_ref,
      "ex_child",
      num_rows=32,
      pk_columns=("K", "C_SEQ"),
      uniqueness_mode="streaming",
      fanout={
          "driving_cols": ["K"],
          "histogram": {
              "2": 1
          },
          "cells": {
              "cols": ["C_SEQ"],
              "rows": [["c1"], ["c2"]],
              "counts": [1, 1]
          },
          "exact_cells": True,
          "candidate_cap": 64,
          "conditional": [{
              "id": "(K)->ex_q",
              "cols": [],
              "nullable": False
          }]
      })
  specs = [
      TableSpec(
          config=_cfg(
              p_schema, p_ref, "ex_p", num_rows=60, seed=11, pk_columns=("K",)),
          reference_rows=p_ref,
          **_sinks(tmp_path, "ex_p")),
      TableSpec(
          config=_cfg(
              q_schema, q_ref, "ex_q", num_rows=60, seed=13, pk_columns=("K",)),
          reference_rows=q_ref,
          **_sinks(tmp_path, "ex_q")),
      TableSpec(
          config=child_cfg,
          reference_rows=child_ref,
          **_sinks(tmp_path, "ex_child"),
          parent_edges=(
              FkEdgeSpec(
                  child_cols=("K",),
                  ref_cols=("K",),
                  parent_landing="p.land.ex_p",
                  parent_table="ex_p",
                  parent_pk=("K",),
                  mode="fanout",
                  keys_per_batch=10),
              FkEdgeSpec(
                  child_cols=("K",),
                  ref_cols=("K",),
                  parent_landing="p.land.ex_q",
                  parent_table="ex_q",
                  parent_pk=("K",),
                  mode="conditional",
                  overlap=("K",),
                  candidate_cap=64),
          )),
  ]
  _run(specs)

  p_keys = _tuples(_read(tmp_path / "ex_p"), "K")
  q_keys = _tuples(_read(tmp_path / "ex_q"), "K")
  child = _read(tmp_path / "ex_child")
  # The shape is only meaningful if BOTH sides of the filter are populated.
  assert 0 < len(p_keys & q_keys) < len(p_keys)
  assert child and _tuples(child, "K") <= q_keys
  assert len(_tuples(child, "K", "C_SEQ")) == len(child)  # PK unique
  envelopes = _dlq(tmp_path, "ex_child", "fk.unmatched")
  assert len(envelopes) == len(p_keys - q_keys)
  _assert_no_unexpected_dlq(tmp_path, "ex_child", "fk.unmatched")
  for envelope in envelopes:
    # `raw_record` is the normalized envelope's JSON-encoded raw_request.
    raw = json.loads(envelope["raw_record"])
    assert len(raw["keys"]) == 1
    assert (raw["keys"][0][0],) in p_keys - q_keys
    # Weighted by the key's EXPECTED rows, so the BLOCKER gate sees the
    # rows the drop cost, not one row per key.
    assert raw["n"] >= 1


# ---------------------------------------------------------------------------
# graph: a tree, a star fact and a 1:1 chain in ONE job
# ---------------------------------------------------------------------------


def _graph_specs(tmp_path: Path) -> list[TableSpec]:
  """root -> mid -> leaf (tree, `mid` implied on `leaf`), `dim` (root),
    `fact` (driven by `mid`, `dim` independent), `twin` (1:1 of `leaf`:
    its PK IS the driving edge, so the histogram is all-ones and there are
    no PK-completing cells)."""
  mid_schema = _schema("g_mid", [("RT", "STRING"), ("MD", "STRING"),
                                 ("M_VAL", "INT64")])
  mid_ref = [{
      "RT": f"r{i % 8}",
      "MD": "mno"[i % 3],
      "M_VAL": i
  } for i in range(40)]
  leaf_schema = _schema("g_leaf", [("RT", "STRING"), ("MD", "STRING"),
                                   ("LF", "STRING"), ("L_VAL", "INT64")])
  leaf_ref = [{
      "RT": f"r{i % 8}",
      "MD": "mno"[i % 3],
      "LF": "fg"[i % 2],
      "L_VAL": i
  } for i in range(40)]
  fact_schema = _schema("g_fact", [("RT", "STRING"), ("MD", "STRING"),
                                   ("DM", "STRING"), ("FSEQ", "STRING"),
                                   ("F_VAL", "INT64")])
  fact_ref = [{
      "RT": f"r{i % 8}",
      "MD": "mno"[i % 3],
      "DM": f"zz{i % 8}",
      "FSEQ": "qxy"[i % 3],
      "F_VAL": i
  } for i in range(40)]
  twin_schema = _schema("g_twin", [("RT", "STRING"), ("MD", "STRING"),
                                   ("LF", "STRING"), ("TW_VAL", "INT64")])
  twin_ref = [{
      "RT": f"r{i % 8}",
      "MD": "mno"[i % 3],
      "LF": "fg"[i % 2],
      "TW_VAL": i * 5
  } for i in range(40)]
  mid_cfg = _cfg(
      mid_schema,
      mid_ref,
      "g_mid",
      num_rows=16,
      pk_columns=("RT", "MD"),
      uniqueness_mode="streaming",
      fanout={
          "driving_cols": ["RT"],
          "histogram": {
              "2": 1
          },
          "cells": {
              "cols": ["MD"],
              "rows": [["m1"], ["m2"]],
              "counts": [1, 1]
          },
          "exact_cells": True,
          "conditional": [],
          "candidate_cap": 64
      })
  leaf_cfg = _cfg(
      leaf_schema,
      leaf_ref,
      "g_leaf",
      num_rows=24,
      pk_columns=("RT", "MD", "LF"),
      uniqueness_mode="streaming",
      fanout={
          "driving_cols": ["RT", "MD"],
          "histogram": {
              "1": 1,
              "2": 1
          },
          "cells": {
              "cols": ["LF"],
              "rows": [["f1"], ["f2"]],
              "counts": [1, 1]
          },
          "exact_cells": True,
          "conditional": [],
          "candidate_cap": 64
      })
  fact_cfg = _cfg(
      fact_schema,
      fact_ref,
      "g_fact",
      num_rows=32,
      pk_columns=("RT", "MD", "FSEQ"),
      uniqueness_mode="streaming",
      fanout={
          "driving_cols": ["RT", "MD"],
          "histogram": {
              "1": 1,
              "3": 1
          },
          "cells": {
              "cols": ["FSEQ"],
              "rows": [["q1"], ["q2"], ["q3"]],
              "counts": [1, 1, 1]
          },
          "exact_cells": True,
          "conditional": [],
          "candidate_cap": 64
      })
  twin_cfg = _cfg(
      twin_schema,
      twin_ref,
      "g_twin",
      num_rows=24,
      pk_columns=("RT", "MD", "LF"),
      uniqueness_mode="streaming",
      fanout={
          "driving_cols": ["RT", "MD", "LF"],
          "histogram": {
              "1": 1
          },
          "cells": None,
          "exact_cells": False,
          "conditional": [],
          "candidate_cap": 64
      })
  return [
      _root(tmp_path, "g_root", "RT", 8),
      TableSpec(
          config=mid_cfg,
          reference_rows=mid_ref,
          **_sinks(tmp_path, "g_mid"),
          parent_edges=(FkEdgeSpec(
              child_cols=("RT",),
              ref_cols=("RT",),
              parent_landing="p.land.g_root",
              parent_pk=("RT",),
              mode="fanout",
              keys_per_batch=10),)),
      TableSpec(
          config=leaf_cfg,
          reference_rows=leaf_ref,
          **_sinks(tmp_path, "g_leaf"),
          parent_edges=(
              FkEdgeSpec(
                  child_cols=("RT", "MD"),
                  ref_cols=("RT", "MD"),
                  parent_landing="p.land.g_mid",
                  parent_pk=("RT", "MD"),
                  mode="fanout",
                  keys_per_batch=10),
              # Carried through the driving edge (ADR 0036 D4):
              # no DAG edge, not even a parent lookup.
              FkEdgeSpec(
                  child_cols=("RT",),
                  ref_cols=("RT",),
                  parent_landing="p.land.g_root",
                  parent_pk=("RT",),
                  mode="implied"))),
      _root(tmp_path, "g_dim", "DM", 10),
      TableSpec(
          config=fact_cfg,
          reference_rows=fact_ref,
          **_sinks(tmp_path, "g_fact"),
          parent_edges=(FkEdgeSpec(
              child_cols=("RT", "MD"),
              ref_cols=("RT", "MD"),
              parent_landing="p.land.g_mid",
              parent_pk=("RT", "MD"),
              mode="fanout",
              keys_per_batch=10),
                        FkEdgeSpec(
                            child_cols=("DM",),
                            ref_cols=("DM",),
                            parent_landing="p.land.g_dim",
                            parent_pk=("DM",),
                            mode="side_input"))),
      TableSpec(
          config=twin_cfg,
          reference_rows=twin_ref,
          **_sinks(tmp_path, "g_twin"),
          parent_edges=(FkEdgeSpec(
              child_cols=("RT", "MD", "LF"),
              ref_cols=("RT", "MD", "LF"),
              parent_landing="p.land.g_leaf",
              parent_pk=("RT", "MD", "LF"),
              mode="fanout",
              keys_per_batch=10),)),
  ]


def test_graph_six_tables(tmp_path):
  """Six tables, one pipeline, one worker fleet (ADR 0030): the tree, the
    star fact and the 1:1 chain all hold at once — zero orphans on every
    enforced edge (whole-tuple containment), a unique PK everywhere, and a
    twin for exactly every leaf."""
  _run(_graph_specs(tmp_path))

  root = _read(tmp_path / "g_root")
  mid = _read(tmp_path / "g_mid")
  leaf = _read(tmp_path / "g_leaf")
  dim = _read(tmp_path / "g_dim")
  fact = _read(tmp_path / "g_fact")
  twin = _read(tmp_path / "g_twin")
  assert root and mid and leaf and dim and fact and twin
  assert _tuples(mid, "RT") <= _tuples(root, "RT")  # tree
  assert _tuples(leaf, "RT", "MD") <= _tuples(mid, "RT", "MD")
  assert _tuples(leaf, "RT") <= _tuples(root, "RT")  # implied
  assert _tuples(fact, "RT", "MD") <= _tuples(mid, "RT", "MD")  # star driving
  assert _tuples(fact, "DM") <= _tuples(dim, "DM")  # star independent
  assert _tuples(twin, "RT", "MD", "LF") <= _tuples(leaf, "RT", "MD",
                                                    "LF")  # 1:1
  for rows, pk in ((root, ("RT",)), (mid, ("RT", "MD")),
                   (leaf, ("RT", "MD", "LF")), (dim, ("DM",)),
                   (fact, ("RT", "MD", "FSEQ")), (twin, ("RT", "MD", "LF"))):
    assert len(_tuples(rows, *pk)) == len(rows)
  assert len(twin) == len(leaf)  # the all-ones histogram is a 1:1
  assert not _dlq(tmp_path, "g_fact", "fk.orphan")
  for table in ("g_mid", "g_leaf", "g_fact", "g_twin"):
    _assert_no_unexpected_dlq(tmp_path, table)


# ---------------------------------------------------------------------------
# two co-parents: (K)->P driving, (K)->Q and (K)->R both conditional
# ---------------------------------------------------------------------------


def test_two_conditional_edges_to_different_parents(tmp_path):
  """Ruling 14, end to end: a child whose SAME column is an existence
    filter against TWO co-parents. Both filters must hold — under the old
    `edge_id = ",".join(cols)` the two edges answered to one id, the second
    `_emit_matches` overwrote the first in `matches`, and the surviving
    edge's candidates answered for both (keys absent from `Q` landed).

    `Q` misses `K6, K7` and `R` misses `K0, K1`, so each edge is the SOLE
    reason some key is dropped — its id has to appear in the DLQ detail."""
  p_schema = _schema("tp_p", [("K", "STRING"), ("P_VAL", "INT64")])
  p_ref = [{"K": f"K{i % 8}", "P_VAL": i} for i in range(48)]
  q_schema = _schema("tp_q", [("K", "STRING"), ("Q_VAL", "INT64")])
  q_ref = [{"K": f"K{i % 6}", "Q_VAL": i} for i in range(48)]
  r_schema = _schema("tp_r", [("K", "STRING"), ("R_VAL", "INT64")])
  r_ref = [{"K": f"K{2 + i % 6}", "R_VAL": i} for i in range(48)]
  child_schema = _schema("tp_child", [("K", "STRING"), ("C_SEQ", "STRING"),
                                      ("C_VAL", "INT64")])
  child_ref = [{
      "K": f"K{i % 8}",
      "C_SEQ": "cd"[i % 2],
      "C_VAL": i
  } for i in range(40)]
  q_edge = FkEdgeSpec(
      child_cols=("K",),
      ref_cols=("K",),
      parent_landing="p.land.tp_q",
      parent_table="tp_q",
      parent_pk=("K",),
      mode="conditional",
      overlap=("K",),
      candidate_cap=64)
  r_edge = FkEdgeSpec(
      child_cols=("K",),
      ref_cols=("K",),
      parent_landing="p.land.tp_r",
      parent_table="tp_r",
      parent_pk=("K",),
      mode="conditional",
      overlap=("K",),
      candidate_cap=64)
  child_cfg = _cfg(
      child_schema,
      child_ref,
      "tp_child",
      num_rows=32,
      pk_columns=("K", "C_SEQ"),
      uniqueness_mode="streaming",
      fanout={
          "driving_cols": ["K"],
          "histogram": {
              "2": 1
          },
          "cells": {
              "cols": ["C_SEQ"],
              "rows": [["c1"], ["c2"]],
              "counts": [1, 1]
          },
          "exact_cells":
              True,
          "candidate_cap":
              64,
          # One entry per EDGE — `id` is the composer's
          # `edge_id`, which now carries the parent.
          "conditional": [
              {
                  "id": q_edge.edge_id,
                  "cols": [],
                  "nullable": False
              },
              {
                  "id": r_edge.edge_id,
                  "cols": [],
                  "nullable": False
              },
          ]
      })
  specs = [
      TableSpec(
          config=_cfg(
              p_schema, p_ref, "tp_p", num_rows=60, seed=11, pk_columns=("K",)),
          reference_rows=p_ref,
          **_sinks(tmp_path, "tp_p")),
      TableSpec(
          config=_cfg(
              q_schema, q_ref, "tp_q", num_rows=60, seed=13, pk_columns=("K",)),
          reference_rows=q_ref,
          **_sinks(tmp_path, "tp_q")),
      TableSpec(
          config=_cfg(
              r_schema, r_ref, "tp_r", num_rows=60, seed=17, pk_columns=("K",)),
          reference_rows=r_ref,
          **_sinks(tmp_path, "tp_r")),
      TableSpec(
          config=child_cfg,
          reference_rows=child_ref,
          **_sinks(tmp_path, "tp_child"),
          parent_edges=(
              FkEdgeSpec(
                  child_cols=("K",),
                  ref_cols=("K",),
                  parent_landing="p.land.tp_p",
                  parent_table="tp_p",
                  parent_pk=("K",),
                  mode="fanout",
                  keys_per_batch=10),
              q_edge,
              r_edge,
          )),
  ]
  _run(specs)

  p_keys = _tuples(_read(tmp_path / "tp_p"), "K")
  q_keys = _tuples(_read(tmp_path / "tp_q"), "K")
  r_keys = _tuples(_read(tmp_path / "tp_r"), "K")
  child = _read(tmp_path / "tp_child")
  # Each co-parent must exclude keys the OTHER one holds, or the claim
  # that both filters ran is vacuous.
  assert q_keys - r_keys and r_keys - q_keys
  assert child
  assert _tuples(child, "K") <= q_keys & r_keys
  assert len(_tuples(child, "K", "C_SEQ")) == len(child)  # PK unique
  dropped = p_keys - (q_keys & r_keys)
  envelopes = _dlq(tmp_path, "tp_child", "fk.unmatched")
  # One envelope per dropped KEY, whichever edge (or both) rejected it.
  assert len(envelopes) == len(dropped)
  _assert_no_unexpected_dlq(tmp_path, "tp_child", "fk.unmatched")
  blamed = set()
  for envelope in envelopes:
    raw = json.loads(envelope["raw_record"])
    assert (raw["keys"][0][0],) in dropped
    assert raw["n"] >= 1
    blamed.add(envelope["error_detail"].split()[1])
  # Both edges are named: the ids are distinct end to end.
  assert blamed == {q_edge.edge_id, r_edge.edge_id}
