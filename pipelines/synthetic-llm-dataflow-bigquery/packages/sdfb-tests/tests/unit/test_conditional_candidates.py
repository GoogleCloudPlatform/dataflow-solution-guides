"""Conditional FK edges — the co-partitioned candidate path (ADR 0037,
design 2026-09-11 §4).

A conditional edge shares at least one child column with the driving
edge, so its remaining columns cannot be drawn from a pool: they must
come from a parent row that actually holds the shared value. The
composer projects the co-parent to ``(join_key, rest_value)``, caps the
candidates per shared value with a seeded Top-M, and CoGroupByKeys them
onto the driving keys before batching.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import ast

import apache_beam as beam
from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.util import assert_that, equal_to
from sdfb_beam.pipeline import (
    FkEdgeSpec,
    _attach_matches,
    _conditional_candidates,
)

# `(T, R) -> right`: T is shared with the driving edge `(T, L) -> left`,
# so `rest` is R alone and the edge id is the child-column list.
_EDGE = FkEdgeSpec(
    child_cols=("T", "R"),
    ref_cols=("T", "R"),
    parent_landing="p.land.right",
    parent_table="right",
    parent_pk=(),
    mode="conditional",
    overlap=("T",),
    candidate_cap=3,
)

_PARENT_ROWS = [{
    "T": "t1",
    "R": f"r{i}"
} for i in range(10)] + [
    # Not referenceable: SQL equality never matches a NULL shared value,
    # so this row must never become a candidate.
    {
        "T": None,
        "R": "x"
    }
]


def _one_capped_non_null_key(elements):
  """The in-pipeline claim: one shared value, capped, distinct, and
    never the NULL-keyed row's."""
  assert len(elements) == 1, elements
  ((join_key, candidates),) = elements
  assert join_key == ("t1",)
  # `candidate_cap` is a hard bound per shared value: a hot key never
  # carries an unbounded candidate list into the request payload.
  assert len(candidates) == 3, candidates
  assert len(set(candidates)) == 3, candidates
  assert set(candidates) <= {(f"r{i}",) for i in range(10)}, candidates
  # The NULL-keyed parent row is dropped, so its value can never ride.
  assert ("x",) not in candidates, candidates


def _run_candidates(run_id: str, out_dir, tag: str) -> list:
  """Run the transform and read its elements back THROUGH A SINK — the
    DirectRunner pickles the `assert_that` matcher, so a list captured in
    a closure is a copy the test never sees. `repr`/`literal_eval` keeps
    tuples tuples, which is exactly what the determinism claim is about.
    """
  out = out_dir / f"cand-{tag}"
  with TestPipeline() as p:
    parent = p | beam.Create(_PARENT_ROWS)
    candidates = _conditional_candidates(parent, _EDGE, "cond/", run_id)
    assert_that(candidates, _one_capped_non_null_key)
    _ = (
        candidates
        | "Repr" >> beam.Map(repr)
        | "Write" >> beam.io.WriteToText(str(out)))
  return [
      ast.literal_eval(line)
      for f in sorted(out.parent.glob(out.name + "*"))
      for line in f.read_text().splitlines()
      if line
  ]


def test_candidates_are_capped_deterministic_and_never_null(tmp_path):
  first = _run_candidates("run-a", tmp_path, "first")
  assert first, "the transform produced no candidates at all"
  # Same run_id ⇒ same draw, in the same order: a retried bundle (or a
  # re-run of the job) reproduces the child rows exactly.
  assert _run_candidates("run-a", tmp_path, "again") == first
  # …and the run id REACHES the rank: a different run draws a
  # different sample (this is not luck — blake2b is seeded by it).
  assert _run_candidates("run-b", tmp_path, "other") != first


def test_attach_matches_gives_an_unmatched_driving_key_an_empty_list():
  """A driving key with no candidate must still reach the engine — with
    an EMPTY list, so the NULL policy (§4 ruling B) decides its fate
    there, rather than the key vanishing from the request stream."""
  keyed = [
      (("t1",), (("t1", "l1"), {})),
      (("t2",), (("t2", "l2"), {})),
  ]
  candidates = [(("t1",), [("r1",), ("r2",)])]
  with TestPipeline() as p:
    keys = p | "Keys" >> beam.Create(keyed)
    cands = p | "Cands" >> beam.Create(candidates)
    assert_that(
        _attach_matches(keys, cands, _EDGE, "attach/"),
        equal_to([
            (("t1", "l1"), {
                "(T,R)->right": [("r1",), ("r2",)]
            }),
            (("t2", "l2"), {
                "(T,R)->right": []
            }),
        ]),
    )


def test_requests_carry_each_key_its_own_candidates(tmp_path):
  """The whole conditional path in one graph: the co-parent's
    candidates are joined on the shared column BEFORE batching, so a
    request hands key i exactly the candidates its own shared value has —
    and a key the co-parent never matched rides on with an empty list
    instead of vanishing from the stream."""
  from sdfb_beam.pipeline import _fanout_requests

  driving = FkEdgeSpec(
      child_cols=("T", "L"),
      ref_cols=("T", "L"),
      parent_landing="p.land.left",
      parent_pk=("T", "L"),
      mode="fanout",
      keys_per_batch=10,
  )
  # `t2` exists in the driving parent but NOT in the co-parent.
  driving_rows = [{"T": "t1", "L": "l1"}, {"T": "t2", "L": "l2"}]
  out = tmp_path / "requests"
  with TestPipeline() as p:
    left = p | "Left" >> beam.Create(driving_rows)
    right = p | "Right" >> beam.Create(_PARENT_ROWS)
    requests = _fanout_requests(
        left,
        driving,
        "child/",
        2.0,
        conditional=((1, _EDGE, right),),
        run_id="run-a",
    )
    _ = (
        requests
        | "Repr" >> beam.Map(repr)
        | "Write" >> beam.io.WriteToText(str(out)))
  payloads = [
      ast.literal_eval(line)
      for f in sorted(out.parent.glob(out.name + "*"))
      for line in f.read_text().splitlines()
      if line
  ]
  assert payloads
  seen = {}
  for payload in payloads:
    matches = payload["matches"][_EDGE.edge_id]
    assert len(matches) == len(payload["keys"])
    for key, candidates in zip(payload["keys"], matches, strict=True):
      seen[key] = candidates
  assert set(seen) == {("t1", "l1"), ("t2", "l2")}
  assert len(seen[("t1", "l1")]) == _EDGE.candidate_cap
  assert seen[("t2", "l2")] == []


def test_the_candidate_rank_is_an_int_seeded_by_the_run_id():
  """The Top-M ordering must be a stable INT derived from the run id —
    never Python's process-salted `hash()`, which would reorder the
    candidates on every worker."""
  from sdfb_beam.pipeline import _candidate_rank

  join_key, (rank, value) = _candidate_rank((("t1",), ("r1",)), run_id="run-a")
  assert join_key == ("t1",)
  assert value == ("r1",)
  assert isinstance(rank, int)
  assert rank != _candidate_rank((("t1",), ("r1",)), run_id="run-b")[1][0]


def _three_candidates_including_null(elements):
  assert len(elements) == 1, elements
  ((join_key, candidates),) = elements
  assert join_key == ("t1",)
  assert set(candidates) == {(None,), ("a",), ("b",)}, candidates


def test_a_rank_tie_never_falls_through_to_the_candidate_values(monkeypatch):
  """Only the JOIN KEY is NULL-checked, so a `rest_value` legitimately
    holds None. Ordering on the whole `(rank, rest_value)` tuple means a
    rank collision advances the comparison to the values and raises
    `'<' not supported between 'NoneType' and 'str'` — and it does so
    DETERMINISTICALLY, so the retried bundle dies too. The combine must
    order on the rank alone."""
  from sdfb_beam import pipeline as pipeline_mod

  # Every candidate ranks the same: the tie is the point.
  monkeypatch.setattr(
      pipeline_mod,
      "_candidate_rank",
      lambda element, run_id: (element[0], (7, element[1])),
  )
  rows = [
      {
          "T": "t1",
          "R": None
      },
      {
          "T": "t1",
          "R": "a"
      },
      {
          "T": "t1",
          "R": "b"
      },
  ]
  with TestPipeline() as p:
    parent = p | beam.Create(rows)
    assert_that(
        _conditional_candidates(parent, _EDGE, "tie/", "run-a"),
        _three_candidates_including_null,
    )


# --- the two join translations -------------------------------------------
#
# A conditional edge is read on BOTH sides by index, never by name:
#
#   parent side   `edge.ref_cols[edge.child_cols.index(o)]` — the PARENT's
#                 name for a shared child column
#   driving side  `driving.child_cols.index(o)` — that column's POSITION
#                 inside the driving key tuple
#
# Both collapse to the identity when the child and parent spell a column
# the same way AND both edges list the shared columns in the same order,
# which every other test in the suite does. These two pin them.

_RENAMED = FkEdgeSpec(
    child_cols=("T", "R"),
    # The co-parent calls them PT / PR — child names are NOT parent names.
    ref_cols=("PT", "PR"),
    parent_landing="p.land.right",
    parent_table="right",
    mode="conditional",
    overlap=("T",),
    candidate_cap=8,
)

_RENAMED_PARENT_ROWS = [{"PT": "t1", "PR": f"r{i}"} for i in range(4)]


def _one_key_with_the_parents_own_columns(elements):
  assert len(elements) == 1, elements
  ((join_key, candidates),) = elements
  assert join_key == ("t1",)
  assert set(candidates) == {(f"r{i}",) for i in range(4)}, candidates


def test_the_parent_side_translates_child_names_to_parent_names():
  """`join_cols = edge.overlap` (child names) would look up `T` on a
    co-parent row that only has `PT` — a KeyError per element, and only
    for models whose parent spells the column differently."""
  with TestPipeline() as p:
    parent = p | beam.Create(_RENAMED_PARENT_ROWS)
    assert_that(
        _conditional_candidates(parent, _RENAMED, "renamed/", "run-a"),
        _one_key_with_the_parents_own_columns,
    )


def test_a_multi_column_overlap_joins_by_position_not_by_declaration_order(
    tmp_path,):
  """The driving edge lists the shared columns as (A, B); the
    conditional edge lists them as (B, A) — a legal declaration, since
    `overlap` is in the CONDITIONAL edge's `cols` order. The parent-side
    projection and the driving-side positions must therefore agree on
    (B, A), not each on its own order: getting it wrong produces a join
    key that matches NOTHING, so every key rides on with an empty
    candidate list and the whole conditional edge silently evaporates.
    """
  from sdfb_beam.pipeline import _fanout_requests

  driving = FkEdgeSpec(
      child_cols=("A", "B"),
      ref_cols=("pa", "pb"),
      parent_landing="p.land.left",
      parent_pk=("pa", "pb"),
      mode="fanout",
      keys_per_batch=10,
  )
  conditional = FkEdgeSpec(
      # (B, A) — the reverse of the driving edge's order — plus R.
      child_cols=("B", "A", "R"),
      ref_cols=("qb", "qa", "qr"),
      parent_landing="p.land.right",
      parent_table="right",
      mode="conditional",
      overlap=("B", "A"),
      candidate_cap=8,
  )
  # Values are never interchangeable: a swapped join key matches no
  # co-parent row at all.
  driving_rows = [{"pa": "a1", "pb": "b1"}]
  co_parent_rows = [{"qb": "b1", "qa": "a1", "qr": f"r{i}"} for i in range(3)]
  out = tmp_path / "ordered"
  with TestPipeline() as p:
    left = p | "Left" >> beam.Create(driving_rows)
    right = p | "Right" >> beam.Create(co_parent_rows)
    requests = _fanout_requests(
        left,
        driving,
        "ordered/",
        2.0,
        conditional=((1, conditional, right),),
        run_id="run-a",
    )
    _ = (
        requests
        | "Repr" >> beam.Map(repr)
        | "Write" >> beam.io.WriteToText(str(out)))
  payloads = [
      ast.literal_eval(line)
      for f in sorted(out.parent.glob(out.name + "*"))
      for line in f.read_text().splitlines()
      if line
  ]
  assert payloads, "the conditional join produced no request at all"
  (payload,) = payloads
  assert payload["keys"] == [("a1", "b1")]
  (candidates,) = payload["matches"][conditional.edge_id]
  assert {c for c in candidates} == {("r0",), ("r1",), ("r2",)}, candidates
