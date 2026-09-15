"""Parent-driven fan-out math (design 2026-09-10, ADR 0036).

A child's rows are its parent's keys x the SOURCE fan-out histogram; the
PK-completing cells of one key are drawn WITHOUT replacement, so a PK
that contains an FK is unique by construction (the 2026-09-09 runs lost
87.9% then 56.5% of C_TABLE to random draws of exactly that PK).
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,invalid-name,missing-class-docstring,protected-access,unused-variable,use-implicit-booleaness-not-comparison

from __future__ import annotations

import random
import time
from collections import Counter
from typing import ClassVar

import pytest
from sdfb_core.engines.base import apply_conditional_overrides, conditional_draws
from sdfb_core.engines.fanout import (
    CellTable,
    ConditionalEdge,
    FanoutHistogram,
    FanoutPlan,
    expand_keys,
    joint_key_draw,
)
from sdfb_core.seeding import derive_key_seed


class TestFanoutHistogram:

  def test_mean_and_zero_share_include_the_zero_bucket(self):
    h = FanoutHistogram({0: 50, 1: 30, 2: 20})
    assert h.mean == pytest.approx(0.7)
    assert h.zero_share == pytest.approx(0.5)
    assert h.max_k == 2

  def test_sampling_follows_the_histogram(self):
    h = FanoutHistogram({0: 50, 1: 30, 2: 20})
    rng = random.Random(1)
    draws = Counter(h.sample(rng) for _ in range(20_000))
    assert draws[0] / 20_000 == pytest.approx(0.5, abs=0.02)
    assert draws[2] / 20_000 == pytest.approx(0.2, abs=0.02)

  def test_payload_round_trip_uses_string_keys(self):
    h = FanoutHistogram({0: 5, 3: 1})
    assert h.to_payload() == {"0": 5, "3": 1}
    assert FanoutHistogram.from_payload({
        "0": 5,
        "3": 1
    }).parents_by_k == {
        0: 5,
        3: 1
    }

  def test_empty_histogram_is_refused(self):
    with pytest.raises(ValueError, match="empty"):
      FanoutHistogram({})


class TestCellTable:
  _cells = CellTable(
      cols=("C2", "D18"),
      rows=[("C0", "K0"), ("C1", "K1"), ("C2", "K2"), ("C3", "K3")],
      counts=[40, 30, 20, 10],
  )

  def test_exact_draw_never_repeats_a_cell(self):
    rng = random.Random(3)
    for _ in range(200):
      drawn = self._cells.draw(4, rng, exact=True)
      assert len(set(drawn)) == 4

  def test_exact_draw_respects_weights_in_aggregate(self):
    rng = random.Random(4)
    first = Counter(
        self._cells.draw(1, rng, exact=True)[0] for _ in range(20_000))
    assert first[("C0", "K0")] / 20_000 == pytest.approx(0.4, abs=0.02)

  def test_exact_draw_beyond_the_table_raises(self):
    with pytest.raises(ValueError, match="4 cells"):
      self._cells.draw(5, random.Random(0), exact=True)

  def test_inexact_draw_may_repeat_and_never_raises(self):
    drawn = self._cells.draw(50, random.Random(0), exact=False)
    assert len(drawn) == 50

  def test_large_table_rejection_path_is_exact_too(self):
    rows = [(i,) for i in range(5_000)]
    table = CellTable(cols=("X",), rows=rows, counts=[1.0] * 5_000)
    drawn = table.draw(
        20, random.Random(9), exact=True)  # k << C: rejection path
    assert len(set(drawn)) == 20
    drawn = table.draw(
        4_000, random.Random(9), exact=True)  # k ~ C: permutation path
    assert len(set(drawn)) == 4_000

  def test_zero_weight_row_is_rejected(self):
    with pytest.raises(ValueError, match="positive count"):
      CellTable(cols=("X",), rows=[(1,), (2,)], counts=[1, 0])

  def test_small_draw_on_a_big_table_stays_cheap(self):
    """ADR 0036 D3 claims O(k), not O(k*C). `random.choices` rebuilds
        the cumulative weights on EVERY call, so the rejection loop was
        O(k*C) — 746 us per key at C=10,000, i.e. ~20 minutes of pure draw
        for a 100M-key parent. The cumulative table is built once in
        `__post_init__` instead and each draw is a bisect."""
    table = CellTable(
        cols=("X",),
        rows=[(i,) for i in range(10_000)],
        counts=[1.0 + (i % 7) for i in range(10_000)],
    )
    rng = random.Random(0)
    started = time.perf_counter()
    for _ in range(1_000):
      assert len(table.draw(3, rng, exact=True)) == 3
    assert time.perf_counter() - started < 2.0


class TestExpandKeys:
  _plan = FanoutPlan(
      driving_cols=("PID",),
      histogram=FanoutHistogram({
          0: 1,
          2: 1,
          3: 1
      }),
      cells=CellTable(
          cols=("CAT",), rows=[("a",), ("b",), ("c",)], counts=[1, 1, 1]),
      exact_cells=True,
  )

  def test_same_key_same_children_under_the_same_run(self):
    keys = [(f"P{i}",) for i in range(50)]
    a = [
        c for chunk in expand_keys(self._plan, keys, "run-1", 1_000)
        for c in chunk
    ]
    b = [
        c for chunk in expand_keys(self._plan, keys, "run-1", 1_000)
        for c in chunk
    ]
    assert a == b
    c = [
        c for chunk in expand_keys(self._plan, keys, "run-2", 1_000)
        for c in chunk
    ]
    assert a != c

  def test_cells_are_unique_within_a_key(self):
    keys = [(f"P{i}",) for i in range(200)]
    by_key: dict[tuple, list[tuple]] = {}
    for chunk in expand_keys(self._plan, keys, "run-1", 1_000):
      for key, cell in chunk:
        by_key.setdefault(key, []).append(cell)
    assert by_key  # some keys had children
    for cells in by_key.values():
      assert len(cells) == len(set(cells))
      assert len(cells) <= 3

  def test_chunks_never_exceed_chunk_rows_and_split_keys(self):
    plan = FanoutPlan(
        driving_cols=("PID",),
        histogram=FanoutHistogram({7: 1}),  # every key has 7 children
        cells=CellTable(
            cols=("CAT",), rows=[(i,) for i in range(10)], counts=[1] * 10),
        exact_cells=True,
    )
    chunks = list(expand_keys(plan, [("P1",), ("P2",)], "r", chunk_rows=5))
    assert [len(c) for c in chunks] == [5, 5, 4]
    cells_p1 = [
        cell for chunk in chunks for key, cell in chunk if key == ("P1",)
    ]
    assert len(cells_p1) == 7 and len(
        set(cells_p1)) == 7  # unique across the split

  def test_no_cells_means_key_only_rows(self):
    plan = FanoutPlan(
        driving_cols=("PID",),
        histogram=FanoutHistogram({2: 1}),
        cells=None,
        exact_cells=False,
    )
    (chunk,) = list(expand_keys(plan, [("P1",)], "r", 100))
    assert chunk == [(("P1",), ()), (("P1",), ())]

  def test_payload_round_trip(self):
    payload = self._plan.to_payload()
    again = FanoutPlan.from_payload(payload)
    assert again.driving_cols == ("PID",)
    assert again.histogram.parents_by_k == {0: 1, 2: 1, 3: 1}
    assert again.cells is not None and again.cells.rows == [("a",), ("b",),
                                                            ("c",)]
    assert again.columns == frozenset({"PID", "CAT"})


def test_key_seed_is_stable_and_key_sensitive():
  assert derive_key_seed("r", ("a", 1)) == derive_key_seed("r", ("a", 1))
  assert derive_key_seed("r", ("a", 1)) != derive_key_seed("r", ("a", 2))
  assert derive_key_seed("r", ("a", 1)) != derive_key_seed("s", ("a", 1))


class TestJointKeyDraw:
  """ADR 0037 fix wave A1: with conditional edges the per-key child
    sequence is ONE walk over the CROSS PRODUCT of (cells x candidates),
    not one independent cyclic walk per factor.

    Before the fix each factor advanced as ``i % len`` on its own, so the
    realised number of distinct combinations per key was
    ``lcm(n_cells, c_1, ..., c_m)`` — e.g. 2 cells x 2 candidates gave
    lcm(2,2)=2 combinations for 4 children, two of them PK-identical —
    and `CellTable.draw(k, exact=True)` RAISED as soon as the fan-out
    exceeded the cell table, killing a whole key batch for a shape
    preflight now accepts.
    """

  _CELLS = CellTable(cols=("S",), rows=[("s1",), ("s2",)], counts=[1, 1])
  # `pk_member=True`: the diamond's `R` is the child PK's last member —
  # the shape ADR 0037 is about, and the only kind of edge that may
  # multiply a key's capacity (G1).
  _EDGE = ConditionalEdge(
      id="(T,R)->right", cols=("R",), nullable=False, pk_member=True)

  def _plan(self, k: int, *, cells=..., exact: bool = True, edges=None):
    return FanoutPlan(
        driving_cols=("T",),
        histogram=FanoutHistogram({k: 1}),
        cells=self._CELLS if cells is ... else cells,
        exact_cells=exact,
        conditional=(self._EDGE,) if edges is None else edges,
    )

  @staticmethod
  def _combos(draw, plan) -> list[tuple]:
    """Every child's FULL combination — the cell tuple plus one entry
        per conditional edge. Asserting per column would pass on a walk
        that repeats whole combinations while each column looks varied."""
    return [(draw.cells[i], *(draw.values[e.id][i]
                              for e in plan.conditional))
            for i in range(draw.n_children)]

  def test_combinations_are_pairwise_distinct_over_the_cross_product(self):
    """2 cells x 3 candidates = 6 combinations, and 6 children get
        SIX different ones. The old lcm(2,3)=6 walk happened to work
        here; the next test is the one it could not do."""
    plan = self._plan(6)
    draw = joint_key_draw(plan, ("t1",), "run-1", [[("r1",), ("r2",), ("r3",)]])
    combos = self._combos(draw, plan)
    assert len(combos) == 6
    assert len(set(combos)) == 6

  def test_shared_factors_no_longer_collapse_to_the_lcm(self):
    """2 cells x 2 candidates: lcm(2,2) = 2, product = 4. Four
        children must land four DISTINCT combinations."""
    plan = self._plan(4)
    draw = joint_key_draw(plan, ("t1",), "run-1", [[("r1",), ("r2",)]])
    combos = self._combos(draw, plan)
    assert len(combos) == 4
    assert len(set(combos)) == 4

  def test_a_fanout_beyond_the_cells_no_longer_raises(self):
    """The blocker: 3 children from a 2-row cell table used to raise
        out of `CellTable.draw(exact=True)` and fail the whole batch. The
        conditional edge's candidates make the third child representable."""
    plan = self._plan(3)
    draw = joint_key_draw(plan, ("t1",), "run-1", [[("r1",), ("r2",)]])
    assert draw.n_children == 3
    assert len(set(self._combos(draw, plan))) == 3
    assert draw.shortfall == 0

  def test_a_fanout_beyond_the_joint_capacity_caps_and_reports_it(self):
    """k > C emits exactly C children and reports the shortfall — it
        never raises and never repeats a combination."""
    plan = self._plan(7)
    draw = joint_key_draw(plan, ("t1",), "run-1", [[("r1",), ("r2",)]])
    assert draw.capacity == 4
    assert draw.n_children == 4
    assert draw.shortfall == 3
    assert len(set(self._combos(draw, plan))) == 4

  def test_a_plan_without_cells_is_bounded_by_the_candidates_alone(self):
    plan = self._plan(5, cells=None)
    draw = joint_key_draw(plan, ("t1",), "run-1", [[("r1",), ("r2",), ("r3",)]])
    assert draw.capacity == 3 and draw.n_children == 3 and draw.shortfall == 2

  def test_an_inexact_pk_is_never_capped(self):
    """`exact_cells=False` means an UNBOUNDED member (pattern,
        numeric, temporal) completes the PK, so the combination need not
        key the child: capping there would silently drop rows the PK can
        represent. The walk wraps instead."""
    plan = self._plan(5, exact=False)
    draw = joint_key_draw(plan, ("t1",), "run-1", [[("r1",), ("r2",)]])
    assert draw.n_children == 5 and draw.shortfall == 0

  def test_an_inexact_cell_draw_follows_the_weights_not_a_permutation(self):
    """E1 (blocker): with `exact_cells=False` the cells are the
        measured MARGINAL, drawn WITH replacement — ADR 0036 draws them
        without replacement only when they must KEY the child.

        The joint walk indexed a permutation PREFIX unconditionally, so
        a fan-out of 2 over a 99:1 cell table handed every key one of
        each and landed 50:50 — silently inverting that column's
        distribution for the whole table."""
    cells = CellTable(cols=("S",), rows=[("heavy",), ("rare",)], counts=[99, 1])
    plan = self._plan(2, cells=cells, exact=False)
    seen: Counter = Counter()
    for i in range(400):
      draw = joint_key_draw(plan, (f"t{i}",), "run-1", [[("r1",), ("r2",)]])
      assert draw.n_children == 2  # inexact never caps
      seen.update(draw.cells)
    total = sum(seen.values())
    assert total == 800
    # True share is 0.99; a permutation prefix gives exactly 0.50.
    assert seen[("heavy",)] / total > 0.9

  def test_a_nullable_edge_without_candidates_null_fills_every_cell(self):
    """A NULL rest is not a key member (ADR 0031), so an edge that
        NULL-fills contributes exactly ONE combination instead of
        dropping the key: the cells still bound the fan-out, and every
        child that the cells can represent lands with a NULL rest."""
    plan = self._plan(
        2,
        edges=(ConditionalEdge(
            id="(T,R)->right", cols=("R",), nullable=True, pk_member=True),),
    )
    draw = joint_key_draw(plan, ("t1",), "run-1", [[]])
    assert draw.capacity == 2  # 2 cells x 1 NULL fill
    assert draw.n_children == 2 and draw.shortfall == 0
    assert draw.values["(T,R)->right"] == [(), ()]
    assert len(set(draw.cells)) == 2

  def test_a_candidateless_nullable_edge_does_not_disable_capping(self):
    """E2: the guard was `exact_cells and all(orders)`, so ONE
        candidate-less nullable edge switched capping off for the whole
        combination. A fan-out of 5 over a 2-row cell table then emitted
        5 children holding 2 distinct (cell, rest) tuples — 3 PK-identical
        rows, with `shortfall == 0` so no `fanout_rows_capped` fired."""
    plan = self._plan(
        5,
        edges=(ConditionalEdge(
            id="(T,R)->right", cols=("R",), nullable=True, pk_member=True),),
    )
    draw = joint_key_draw(plan, ("t1",), "run-1", [[]])
    assert draw.capacity == 2
    assert draw.n_children == 2
    assert draw.shortfall == 3
    assert len(set(self._combos(draw, plan))) == 2
    assert draw.values["(T,R)->right"] == [(), ()]  # the NULL fill still works

  def test_a_null_fill_with_no_cells_can_represent_one_child_only(self):
    """The degenerate end of the same rule: no cells and a NULL rest
        means the driving key IS the whole PK, so exactly one child is
        representable — the rest would be PK duplicates."""
    plan = self._plan(
        2,
        cells=None,
        edges=(ConditionalEdge(
            id="(T,R)->right", cols=("R",), nullable=True, pk_member=True),),
    )
    draw = joint_key_draw(plan, ("t1",), "run-1", [[]])
    assert draw.capacity == 1 and draw.n_children == 1 and draw.shortfall == 1
    assert draw.values["(T,R)->right"] == [()]

  def test_only_pk_supplying_edges_bound_the_capacity(self):
    """G1 (blocker): a conditional edge whose `rest` supplies NO PK
        member used to multiply the capacity all the same.

        Child PK `(T, GRADE, R)`; the cells cover `GRADE` (2 rows); edge
        A `(T,R)->right` supplies `R`, a PK member (3 candidates for this
        key); edge B `(T,DESC)->lookup` supplies `DESC`, which is NOT in
        the PK (20 candidates). The source fans out to 100.

        Preflight passes — it counts only the PK-touching edges (2 x 64
        >= 100) — and the engine then computed 2 x 3 x 20 = 120 >= 100,
        emitted all 100 children and reported `shortfall == 0`. Only
        2 x 3 = 6 distinct `(GRADE, R)` combinations exist, so 94 of
        those rows were PK duplicates that land (or divert as
        `pk.duplicate`) with no `fanout_rows_capped` warning. Drop edge B
        and the same key correctly capped at 6.
        """
    plan = FanoutPlan(
        driving_cols=("T",),
        histogram=FanoutHistogram({100: 1}),
        cells=CellTable(
            cols=("GRADE",), rows=[("g1",), ("g2",)], counts=[1, 1]),
        exact_cells=True,
        conditional=(
            ConditionalEdge(
                id="(T,R)->right", cols=("R",), nullable=False, pk_member=True),
            ConditionalEdge(
                id="(T,DESC)->lookup",
                cols=("DESC",),
                nullable=False,
                pk_member=False,
            ),
        ),
    )
    draw = joint_key_draw(
        plan,
        ("t1",),
        "run-1",
        [
            [(f"r{i}",) for i in range(3)],
            [(f"d{i}",) for i in range(20)],
        ],
    )
    assert draw.capacity == 6  # 2 cells x 3 PK-supplying candidates
    assert draw.n_children == 6
    assert draw.shortfall == 94
    pk_combos = [(draw.cells[i], draw.values["(T,R)->right"][i])
                 for i in range(draw.n_children)]
    assert len(set(pk_combos)) == 6

  def test_a_non_pk_edge_still_gets_a_candidate_per_child(self):
    """G1's other half: the lookup edge stops inflating the cap, but
        each child still draws its own candidate from the shuffled list —
        riding the slow end of one joint walk would have frozen every
        child of a capped key on the same lookup row."""
    plan = FanoutPlan(
        driving_cols=("T",),
        histogram=FanoutHistogram({100: 1}),
        cells=CellTable(
            cols=("GRADE",), rows=[("g1",), ("g2",)], counts=[1, 1]),
        exact_cells=True,
        conditional=(
            ConditionalEdge(
                id="(T,R)->right", cols=("R",), nullable=False, pk_member=True),
            ConditionalEdge(
                id="(T,DESC)->lookup",
                cols=("DESC",),
                nullable=False,
                pk_member=False,
            ),
        ),
    )
    offered = [(f"d{i}",) for i in range(20)]
    draw = joint_key_draw(
        plan,
        ("t1",),
        "run-1",
        [[(f"r{i}",) for i in range(3)], offered],
    )
    lookup = draw.values["(T,DESC)->lookup"]
    assert len(lookup) == draw.n_children == 6
    assert set(lookup) <= set(offered)
    assert len(set(lookup)) == 6  # one per child, not one per key

  def test_the_draw_is_deterministic_per_run_and_key(self):
    """Same run + same key ⇒ the same children (a retried bundle must
        not re-roll them); a different run ⇒ a different ORDER over the
        same combinations. The cell table and the candidate list are both
        widened here on purpose: with 2x2 there are only four
        permutations, so two run ids coincide by chance often enough to
        make the claim flaky rather than false."""
    cells = CellTable(
        cols=("S",), rows=[(f"s{i}",) for i in range(5)], counts=[1] * 5)
    plan = self._plan(5, cells=cells)
    cands = [[("r1",), ("r2",), ("r3",), ("r4",)]]
    again = self._combos(joint_key_draw(plan, ("t1",), "run-1", cands), plan)
    assert self._combos(joint_key_draw(plan, ("t1",), "run-1", cands),
                        plan) == again
    other = self._combos(joint_key_draw(plan, ("t1",), "run-2", cands), plan)
    assert other != again
    assert len(set(other)) == len(other)

  def test_zero_fanout_draws_nothing(self):
    plan = self._plan(0)
    draw = joint_key_draw(plan, ("t1",), "run-1", [[("r1",)]])
    assert draw.n_children == 0 and draw.shortfall == 0


class TestJointDrawThroughExpandKeys:
  """The engines' seam: `conditional_draws` decides each key's children
    ONCE, `expand_keys` emits their cells and `apply_conditional_overrides`
    writes the matching candidate — including across a chunk split."""

  # `pk_member=True`: the diamond's `R` is the child PK's last member —
  # the shape ADR 0037 is about, and the only kind of edge that may
  # multiply a key's capacity (G1).
  _EDGE = ConditionalEdge(
      id="(T,R)->right", cols=("R",), nullable=False, pk_member=True)
  _PLAN = FanoutPlan(
      driving_cols=("T",),
      histogram=FanoutHistogram({4: 1}),
      cells=CellTable(cols=("S",), rows=[("s1",), ("s2",)], counts=[1, 1]),
      exact_cells=True,
      conditional=(_EDGE,),
  )
  _MATCHES: ClassVar[dict] = {"(T,R)->right": [[("r1",), ("r2",)]]}

  def _walk(self, chunk_rows: int) -> list[tuple]:
    keys = [("t1",)]
    draws = conditional_draws(self._PLAN, keys, "run-1", self._MATCHES)
    child_index: dict[tuple, int] = {}
    combos: list[tuple] = []
    for chunk in expand_keys(
        self._PLAN, keys, "run-1", chunk_rows, draws=draws):
      columns, skip = apply_conditional_overrides(self._PLAN, chunk, draws,
                                                  child_index)
      for i, (_key, cell) in enumerate(chunk):
        assert not skip[i]
        combos.append((cell, columns["R"][i]))
    return combos

  def test_the_child_index_keeps_counting_across_chunk_splits(self):
    """`expand_keys` can split one key's children across chunks — the
        candidate a child gets must follow the child, not the position in
        its chunk."""
    whole = self._walk(1_000)
    assert len(whole) == 4 and len(set(whole)) == 4
    assert self._walk(1) == whole
    assert self._walk(3) == whole

  def test_a_dropped_key_emits_no_rows(self):
    """A non-nullable conditional edge with no candidates: the DoFn
        drops the key first, and the engine seam emits nothing for it."""
    keys = [("t1",), ("t2",)]
    matches = {"(T,R)->right": [[("r1",), ("r2",)], []]}
    draws = conditional_draws(self._PLAN, keys, "run-1", matches)
    assert draws[("t2",)] is None
    emitted = [
        key
        for chunk in expand_keys(self._PLAN, keys, "run-1", 100, draws=draws)
        for key, _cell in chunk
    ]
    assert set(emitted) == {("t1",)}

  # Both keys carry candidates, so BOTH reach the draw and BOTH cap —
  # the old fixture listed one per-key entry, so key two was dropped
  # before it drew and the milestone guard could not fail (E3).
  _BOTH: ClassVar[dict] = {
      "(T,R)->right": [[("r1",), ("r2",)], [("r1",), ("r2",)]]
  }
  _CAPPED_PLAN = FanoutPlan(
      driving_cols=("T",),
      histogram=FanoutHistogram({9: 1}),  # 9 > 2 cells x 2 candidates
      cells=CellTable(cols=("S",), rows=[("s1",), ("s2",)], counts=[1, 1]),
      exact_cells=True,
      conditional=(_EDGE,),
  )

  @staticmethod
  def _capped_lines(caplog) -> list[str]:
    return [
        ln for ln in caplog.text.splitlines() if "name=fanout_rows_capped" in ln
    ]

  def test_capping_logs_one_milestone_for_many_keys_of_one_table(self, caplog):
    """Two keys of ONE table both cap ⇒ exactly one WARNING. The old
        test could not fail: its second key was dropped before it drew,
        so deleting the guard kept the suite green."""
    import logging as _logging

    from sdfb_core.engines import base as base_mod

    base_mod._reset_rows_capped_log()
    with caplog.at_level(_logging.WARNING, logger="sdfb.milestone"):
      draws = conditional_draws(
          self._CAPPED_PLAN,
          [("t1",), ("t2",)],
          "run-1",
          self._BOTH,
          table="p.land.child_a",
      )
    assert [d.shortfall for d in draws.values()] == [5, 5]  # both really capped
    lines = self._capped_lines(caplog)
    assert len(lines) == 1
    assert "capacity=4" in lines[0]

  def test_every_driven_table_reports_its_own_capping(self, caplog):
    """E3: the guard is scoped per LANDING TABLE, not per process. In
        a single-job relational run (ADR 0030) several driven tables share
        one worker, and a process-global guard reported only the first."""
    import logging as _logging

    from sdfb_core.engines import base as base_mod

    base_mod._reset_rows_capped_log()
    with caplog.at_level(_logging.WARNING, logger="sdfb.milestone"):
      for table in ("p.land.child_a", "p.land.child_b"):
        conditional_draws(
            self._CAPPED_PLAN,
            [("t1",), ("t2",)],
            "run-1",
            self._BOTH,
            table=table,
        )
    assert len(self._capped_lines(caplog)) == 2


class TestLegacyPathUnchanged:
  """No conditional edges ⇒ ADR 0036's code path, byte for byte."""

  _PLAN = FanoutPlan(
      driving_cols=("PID",),
      histogram=FanoutHistogram({5: 1}),
      cells=CellTable(cols=("CAT",), rows=[("a",), ("b",)], counts=[1, 1]),
      exact_cells=True,
  )

  def test_an_exact_plan_without_conditional_edges_still_raises(self):
    with pytest.raises(ValueError, match="2 cells"):
      list(expand_keys(self._PLAN, [("P1",)], "r", 100))

  def test_conditional_draws_on_such_a_plan_is_empty(self):
    assert conditional_draws(self._PLAN, [("P1",)], "r", None) == {}


class TestConditionalEdge:

  def test_pk_member_defaults_to_bounding_nothing(self):
    """G1: the flag decides whether this edge MULTIPLIES a key's
        capacity, so an ABSENT value must never inflate it. Under-counting
        caps a key early and says so (`fanout_rows_capped`, a non-zero
        shortfall); over-counting emits rows the PK cannot represent, and
        they land or divert as `pk.duplicate` with nothing in the log. A
        payload written before this field — or by a caller that does not
        know the PK — therefore bounds nothing."""
    edge = ConditionalEdge.from_payload({
        "id": "(T,DESC)->lookup",
        "cols": ["DESC"],
        "nullable": False
    })
    assert edge.pk_member is False
    plan = FanoutPlan(
        driving_cols=("T",),
        histogram=FanoutHistogram({9: 1}),
        cells=CellTable(
            cols=("GRADE",), rows=[("g1",), ("g2",)], counts=[1, 1]),
        exact_cells=True,
        conditional=(edge,),
    )
    draw = joint_key_draw(plan, ("t1",), "run-1",
                          [[(f"d{i}",) for i in range(20)]])
    assert draw.capacity == 2  # the cells alone, never x 20
    assert draw.shortfall == 7

  def test_payload_round_trip(self):
    edge = ConditionalEdge(
        id="(T,R)->right", cols=("R",), nullable=True, pk_member=True)
    assert ConditionalEdge.from_payload(edge.to_payload()) == edge

  def test_to_payload_shape(self):
    edge = ConditionalEdge(id="(T,R)->right", cols=("T", "R"), nullable=False)
    assert edge.to_payload() == {
        "id": "(T,R)->right",
        "cols": ["T", "R"],
        "nullable": False,
        "pk_member": False
    }


class TestFanoutPlanConditional:

  def test_plan_payload_round_trips_conditional_edges(self):
    plan = FanoutPlan(
        driving_cols=("T", "L"),
        histogram=FanoutHistogram({1: 1}),
        cells=None,
        exact_cells=True,
        conditional=(ConditionalEdge(
            id="(T,R)->right", cols=("R",), nullable=True, pk_member=True),),
    )
    assert FanoutPlan.from_payload(plan.to_payload()) == plan
    assert "R" in plan.columns

  def test_plan_payload_missing_conditional_key_defaults_to_empty(self):
    plan = FanoutPlan(
        driving_cols=("T", "L"),
        histogram=FanoutHistogram({1: 1}),
        cells=None,
        exact_cells=True,
        conditional=(ConditionalEdge(
            id="(T,R)->right", cols=("R",), nullable=True, pk_member=True),),
    )
    assert FanoutPlan.from_payload({
        **plan.to_payload(), "conditional": None
    }).conditional == ()

  def test_plan_without_conditional_edges_round_trips_to_empty_tuple(self):
    plan = FanoutPlan(
        driving_cols=("PID",),
        histogram=FanoutHistogram({1: 1}),
        cells=None,
        exact_cells=False,
    )
    assert plan.conditional == ()
    assert FanoutPlan.from_payload(plan.to_payload()).conditional == ()
    assert plan.to_payload()["conditional"] == []
