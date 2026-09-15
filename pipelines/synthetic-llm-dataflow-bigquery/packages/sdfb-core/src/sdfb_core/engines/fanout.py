"""Parent-driven fan-out (design 2026-09-10, ADR 0036).

A child table generated from its parent's landed keys: per key, how many
children (the SOURCE fan-out histogram, zero bucket included), which
PK-completing cells (drawn WITHOUT replacement from the source's joint
cell distribution when they must KEY the child, by that distribution's
weights otherwise), and which columns are inherited (copied from the
key tuple). This is the part both engines share; everything that is not
a key, a cell or an inherited column is the engine's own sampling.

Why without replacement: a PK that contains an FK is a per-parent key.
Drawing its remaining members at random inside a parent collides as
balls into bins (`1 - C/k (1 - e^(-k/C))`, the 2026-09-09 runs); the
source PK guarantees k <= C, so drawing k distinct cells reproduces it.

Pure Python; no Beam, no GCP.
"""

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

import random
from bisect import bisect_right
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import accumulate
from math import prod

from sdfb_core.seeding import derive_key_seed

__all__ = [
    "CellTable",
    "ConditionalEdge",
    "FanoutHistogram",
    "FanoutPlan",
    "KeyDraw",
    "conditional_edge_id",
    "expand_keys",
    "joint_key_draw",
]

# Above this fraction of the table, sampling without replacement switches
# from rejection (expected O(k) draws) to a weighted permutation
# (Efraimidis & Spirakis 2006, O(C log k)).
_REJECTION_MAX_FILL = 0.5


@dataclass(frozen=True)
class FanoutHistogram:
  """``k -> number of parent tuples with k children`` in the source."""

  parents_by_k: dict[int, int]
  _ks: tuple[int, ...] = field(init=False, repr=False)
  _cum: tuple[float, ...] = field(init=False, repr=False)

  def __post_init__(self) -> None:
    if not self.parents_by_k or sum(self.parents_by_k.values()) <= 0:
      raise ValueError("fan-out histogram is empty")
    ks = tuple(sorted(int(k) for k in self.parents_by_k))
    total = float(sum(self.parents_by_k.values()))
    cum = tuple(accumulate(self.parents_by_k[k] / total for k in ks))
    object.__setattr__(self, "_ks", ks)
    object.__setattr__(self, "_cum", cum)

  @property
  def mean(self) -> float:
    total = sum(self.parents_by_k.values())
    return sum(k * n for k, n in self.parents_by_k.items()) / total

  @property
  def max_k(self) -> int:
    return max(self.parents_by_k)

  @property
  def zero_share(self) -> float:
    return self.parents_by_k.get(0, 0) / sum(self.parents_by_k.values())

  def sample(self, rng: random.Random) -> int:
    i = min(bisect_right(self._cum, rng.random()), len(self._ks) - 1)
    return self._ks[i]

  def to_payload(self) -> dict[str, int]:
    return {str(k): int(n) for k, n in sorted(self.parents_by_k.items())}

  @classmethod
  def from_payload(cls, payload: dict) -> FanoutHistogram:
    return cls({int(k): int(n) for k, n in payload.items()})


@dataclass(frozen=True)
class CellTable:
  """The joint values of the PK-completing categorical members, with
    their source row counts as weights."""

  cols: tuple[str, ...]
  rows: list[tuple]
  counts: list[float]
  _cum: tuple[float, ...] = field(init=False, repr=False)
  _total: float = field(init=False, repr=False)

  def __post_init__(self) -> None:
    if len(self.rows) != len(self.counts) or not self.rows:
      raise ValueError("cell table needs one positive count per row")
    if not all(c > 0 for c in self.counts):
      raise ValueError("cell table needs one positive count per row")
    # Cumulative counts ONCE, exactly as `FanoutHistogram` does.
    # `random.choices(..., weights=...)` rebuilds them on every call, so
    # a k-draw cost O(k*C) and not the O(k) ADR 0036 D3 claims — 746 us
    # per key at C = 10,000, ~20 minutes of pure draw on a 100M-key
    # parent.
    cum = tuple(accumulate(float(c) for c in self.counts))
    object.__setattr__(self, "_cum", cum)
    object.__setattr__(self, "_total", cum[-1])

  @property
  def size(self) -> int:
    return len(self.rows)

  def _weighted_index(self, rng: random.Random) -> int:
    """One weighted index in O(log C) — a bisect over `_cum`."""
    return min(
        bisect_right(self._cum,
                     rng.random() * self._total), self.size - 1)

  def draw(self, k: int, rng: random.Random, *, exact: bool) -> list[tuple]:
    """``k`` cells. ``exact`` = without replacement (raises when
        ``k`` exceeds the table); otherwise weighted with replacement."""
    if k <= 0:
      return []
    if not exact:
      return [self.rows[self._weighted_index(rng)] for _ in range(k)]
    if k > self.size:
      raise ValueError(f"{k} children requested from {self.size} cells over "
                       f"{list(self.cols)} — the declared PK is not a key")
    if k <= self.size * _REJECTION_MAX_FILL:
      chosen: dict[int, None] = {}
      while len(chosen) < k:
        chosen.setdefault(self._weighted_index(rng), None)
      return [self.rows[i] for i in chosen]
    return [self.rows[i] for i in self.permutation(rng)[:k]]

  def permutation(self, rng: random.Random) -> list[int]:
    """Every row index once, in weighted random order (Efraimidis &
        Spirakis 2006: sort on ``u^(1/w)``).

        A prefix of it IS a draw without replacement, which is why the
        exact draw's high-fill branch takes the first ``k``. The joint
        per-key walk (`joint_key_draw`) indexes into the WHOLE thing:
        a child's cell is then a pure function of its combination index,
        so two children of one key can never collide on it, and the
        weights still decide which cells come first.

        ONLY for an ``exact_cells`` plan. When the cells do not key the
        child they carry a marginal, and a permutation prefix flattens
        it — a 99:1 cell table handed out 50:50 at a fan-out of 2 (ADR
        0037 final review, E1). Those plans call `draw(exact=False)`.
        """
    return sorted(
        range(self.size),
        key=lambda i: -(rng.random()**(1.0 / self.counts[i])),
    )

  def to_payload(self) -> dict:
    return {
        "cols": list(self.cols),
        "rows": [list(r) for r in self.rows],
        "counts": [float(c) for c in self.counts],
    }

  @classmethod
  def from_payload(cls, payload: dict) -> CellTable:
    return cls(
        cols=tuple(payload["cols"]),
        rows=[tuple(r) for r in payload["rows"]],
        counts=[float(c) for c in payload["counts"]],
    )


def conditional_edge_id(cols: Sequence[str], ref: str) -> str:
  """The ONE name a conditional edge answers to — ``(cols)->ref``, the
    label the launcher's milestones already print.

    The composer (`FkEdgeSpec.edge_id`), the plan (`ConditionalEdge.id`),
    the request payload's ``matches`` and preflight's ``conditional_rest``
    must all spell it the same way or an edge's candidates are looked up
    under a key nothing wrote. It carries the PARENT because the child
    columns alone do not identify an edge: two conditional edges from the
    same columns to DIFFERENT parents are declarable (the parse-time
    duplicate check only rejects an identical ``(cols, ref, ref_cols)``),
    and under a columns-only id they collided — the second join
    overwrote the first in ``matches``, so one edge's candidates
    answered for both (ADR 0037 review, ruling 14)."""
  return f"({','.join(cols)})->{ref}"


@dataclass(frozen=True)
class ConditionalEdge:
  """A non-driving FK edge whose candidates come from a co-parent
    joined on the driving key's overlap columns (design 2026-09-11 §4,
    ADR 0037) — e.g. a child PK's remaining member is populated from a
    second parent that shares part of the driving key."""

  id: str
  cols: tuple[str, ...]
  nullable: bool
  # Does this edge's `rest` (``cols``) supply a member of the child's
  # EFFECTIVE PK — the relationship model's `pk:` (ADR 0032), which is
  # what preflight resolves and what `EnforceUniqueness` keys on? ONLY
  # such an edge multiplies a key's capacity (`joint_key_draw`): an edge
  # the PK does not read distinguishes nothing, so counting it emitted
  # children the PK cannot tell apart (final review G1).
  #
  # The default is False — absent means "bounds nothing" — because the
  # two errors are not symmetric. Under-counting caps a key EARLY and
  # says so (`fanout_rows_capped`, a non-zero shortfall the operator
  # sees); over-counting emits rows the PK cannot represent, which land
  # or divert as `pk.duplicate` with NOTHING in the log. A payload
  # written before this field, or by a caller that does not know the PK,
  # therefore inflates no capacity.
  pk_member: bool = False

  def to_payload(self) -> dict:
    return {
        "id": self.id,
        "cols": list(self.cols),
        "nullable": bool(self.nullable),
        "pk_member": bool(self.pk_member),
    }

  @classmethod
  def from_payload(cls, payload: dict) -> ConditionalEdge:
    return cls(
        id=str(payload["id"]),
        cols=tuple(payload["cols"]),
        nullable=bool(payload["nullable"]),
        pk_member=bool(payload.get("pk_member", False)),
    )


@dataclass(frozen=True)
class FanoutPlan:
  """One driven child's relational recipe."""

  driving_cols: tuple[str, ...]
  histogram: FanoutHistogram
  cells: CellTable | None
  # True when EVERY PK member outside the driving edge is a cell column
  # — then the cells alone must key the child and are drawn without
  # replacement. False when an unbounded member (pattern, numeric,
  # temporal) completes the PK and cells only need to follow weights.
  exact_cells: bool
  # Non-driving FK edges resolved per key via `conditional_values`
  # (design 2026-09-11 §4, ADR 0037). Added at the end so positional
  # construction in existing tests/payloads keeps working.
  conditional: tuple[ConditionalEdge, ...] = ()

  @property
  def columns(self) -> frozenset[str]:
    cols = set(self.driving_cols)
    if self.cells is not None:
      cols.update(self.cells.cols)
    for edge in self.conditional:
      cols.update(edge.cols)
    return frozenset(cols)

  def to_payload(self) -> dict:
    return {
        "driving_cols": list(self.driving_cols),
        "histogram": self.histogram.to_payload(),
        "cells": self.cells.to_payload() if self.cells is not None else None,
        "exact_cells": bool(self.exact_cells),
        "conditional": [edge.to_payload() for edge in self.conditional],
    }

  @classmethod
  def from_payload(cls, payload: dict) -> FanoutPlan:
    cells = payload.get("cells")
    conditional = payload.get("conditional") or ()
    return cls(
        driving_cols=tuple(payload["driving_cols"]),
        histogram=FanoutHistogram.from_payload(payload["histogram"]),
        cells=CellTable.from_payload(cells) if cells else None,
        exact_cells=bool(payload.get("exact_cells", False)),
        conditional=tuple(ConditionalEdge.from_payload(e) for e in conditional),
    )


@dataclass(frozen=True)
class KeyDraw:
  """One parent key's children, decided JOINTLY (ADR 0037 final review,
    fix wave A1): the cell each child carries and, per conditional edge,
    the candidate tuple it carries — index-aligned, one entry per child.

    Why jointly. The cell sequence and each edge's candidate sequence
    used to be independent cyclic walks (``i % len`` each), so the
    realised number of distinct ``(cell, cand_1, …, cand_m)``
    combinations per key was ``lcm(n_cells, c_1, …, c_m)``, NOT their
    product — 2 cells and 2 candidates gave 2 combinations for 4
    children, i.e. PK duplicates on a shape preflight had just declared
    safe. And the cells were drawn by ``CellTable.draw(k, exact=True)``,
    which RAISED as soon as the fan-out exceeded the cell table, killing
    a whole key batch instead of using the candidates that make those
    children representable.

    ``capacity`` is the product of the dimensions that genuinely BOUND
    the PRIMARY KEY (`joint_key_draw`) — the cells when they key the
    child, times each conditional edge whose ``rest`` supplies a PK
    member; ``requested`` is the fan-out the histogram drew.
    ``shortfall`` is what a capped key could not emit —
    the DoFn-visible number behind `fanout_rows_capped`. It is a per-key
    CEILING only for an ``exact_cells`` plan: an inexact PK is completed
    by an unbounded member, so nothing caps and ``shortfall`` is 0.
    """

  cells: tuple[tuple, ...]
  values: dict[str, list[tuple]]
  requested: int
  capacity: int

  @property
  def n_children(self) -> int:
    return len(self.cells)

  @property
  def shortfall(self) -> int:
    return max(0, self.requested - len(self.cells))


def _mixed_radix(index: int, radices: Sequence[int]) -> list[int]:
  """``index`` decomposed over ``radices``, FIRST radix varying fastest.

    Injective on ``[0, prod(radices))``, which is what makes every
    child's combination distinct. An ``exact_cells`` plan puts the cells
    first deliberately: for the ADR 0036 regime (``k <= n_cells``) each
    child then takes a different cell, exactly as
    `CellTable.draw(exact=True)` did, and the candidate digits only start
    advancing once the cells are exhausted — the regime where the old
    code raised. An inexact plan passes the PK-supplying candidate radices
    alone; its cells are not a dimension of the walk at all. The edges
    the PK does not read walk their own radices (`joint_key_draw`), so
    they vary per child without bounding anything.
    """
  digits: list[int] = []
  rest = index
  for radix in radices:
    digits.append(rest % radix)
    rest //= radix
  return digits


def _walk_dimensions(
    plan: FanoutPlan, orders: Sequence[Sequence[tuple]]
) -> tuple[list[int], list[int], list[tuple[bool, int]]]:
  """``(bounding radices, free radices, per-edge (bounded, digit))`` —
    the TWO walks `joint_key_draw` runs over one key's children.

    The BOUNDING walk enumerates the dimensions that DISTINGUISH THE PK:
    the cells when they key the child (leading, so ADR 0036's regime is
    unchanged), then every conditional edge whose ``rest`` supplies a PK
    member. Its period IS the capacity.

    The FREE walk carries the edges the PK does not read. They still hand
    each child its own candidate, but they distinguish nothing, so
    multiplying them into the cap emitted rows the PK cannot tell apart
    (final review G1). Keeping them on their own walk also stops a capped
    key freezing every child on the first lookup candidate, which riding
    the slow end of one joint walk would have done.

    A candidate-less edge contributes a radix of 1 (the NULL fill), NOT a
    skipped cap: the whole combination used to stop being capped as soon
    as one nullable edge came back empty, and the cell dimension then
    wrapped into PK duplicates with ``shortfall == 0`` (final review, E2).
    """
  bound: list[int] = []
  if plan.exact_cells:
    bound.append(plan.cells.size if plan.cells is not None else 1)
  free: list[int] = []
  digit_of: list[tuple[bool, int]] = []
  for edge, order in zip(plan.conditional, orders, strict=True):
    radix = max(1, len(order))
    if edge.pk_member:
      digit_of.append((True, len(bound)))
      bound.append(radix)
    else:
      digit_of.append((False, len(free)))
      free.append(radix)
  return bound, free, digit_of


def joint_key_draw(
    plan: FanoutPlan,
    key: tuple,
    run_id: str,
    candidates: Sequence[Sequence[Sequence]],
) -> KeyDraw:
  """One key's children over the CROSS PRODUCT of its BOUNDED
    dimensions (design 2026-09-11 §4, ADR 0037).

    ``candidates[j]`` is edge ``plan.conditional[j]``'s candidate list
    for this key (empty = no candidates; the NULL policy is the caller's).

    The cells follow ADR 0036's rule, which turns on EXACTNESS and not on
    the presence of conditional edges (ADR 0037 final review, E1):

    - ``exact_cells`` — the cells must KEY the child, so they are drawn
      without replacement: the leading `_mixed_radix` digit indexes a
      per-key seeded weighted PERMUTATION (`CellTable.permutation`) and
      two children of one key can never share a cell.
    - otherwise — an unbounded PK member (pattern, numeric, temporal)
      keys the child and the cells only carry their measured MARGINAL,
      so each child draws one WITH replacement
      (`CellTable.draw(exact=False)`). A permutation prefix here handed a
      99:1 cell table out 50:50 at a fan-out of 2, silently inverting
      that column's distribution for the whole table.

    Each conditional edge is shuffled per key either way, and child ``i``
    reads its candidate digits from the same `_mixed_radix` walk — the
    per-key / per-edge seeds the rest of the fan-out layer uses, so a
    re-run reproduces the children exactly.

    ``capacity`` is the product of the dimensions that genuinely bound
    the PRIMARY KEY: the cell table when it keys the child, times — per
    conditional edge whose ``rest`` supplies a PK member
    (``ConditionalEdge.pk_member``) — its ACTUAL candidate count, or 1
    when it has none, because NULL-filling an edge is exactly ONE
    combination. (A NULL is not a key member, ADR 0031: such an edge
    neither drops the key nor multiplies what it can represent.)

    An edge the PK does NOT read is a FREE dimension: it still hands
    every child its own candidate, off its own walk, but it multiplies
    NOTHING. Counting it let a key emit children the PK cannot tell
    apart (final review G1: 2 cells x 3 keying candidates x 20 LOOKUP
    candidates "represented" 100 children, of which 94 were PK
    duplicates — ``shortfall == 0``, no `fanout_rows_capped`, and the
    duplicates landed or diverted as `pk.duplicate`). `edge_roles` calls
    an edge ``conditional`` for sharing a column with the driving edge,
    which says nothing about whether its ``rest`` keys anything.

    The fan-out is capped at that capacity when, and only when,
    ``exact_cells`` — an inexact PK is completed by an unbounded member,
    so capping would drop rows the PK can represent and the candidate
    digits wrap instead.
    """
  key_t = tuple(key)
  rng = random.Random(derive_key_seed(run_id, key_t))
  k = plan.histogram.sample(rng)
  values: dict[str, list[tuple]] = {edge.id: [] for edge in plan.conditional}
  if k <= 0:
    return KeyDraw((), values, 0, 0)
  orders: list[list[tuple]] = []
  for edge, candidate_list in zip(plan.conditional, candidates, strict=True):
    order = [tuple(c) for c in candidate_list]
    random.Random(derive_key_seed(run_id, key_t, salt=edge.id)).shuffle(order)
    orders.append(order)
  cell_digit = plan.exact_cells
  bound_radices, free_radices, digit_of = _walk_dimensions(plan, orders)
  capacity = prod(bound_radices)
  free_period = prod(free_radices)
  n_children = min(k, capacity) if plan.exact_cells else k
  cell_order = plan.cells.permutation(rng) if (plan.cells and
                                               cell_digit) else []
  weighted = (
      plan.cells.draw(n_children, rng, exact=False) if
      (plan.cells is not None and not cell_digit) else [])
  cells: list[tuple] = []
  for i in range(n_children):
    bound = _mixed_radix(i % capacity, bound_radices)
    free = _mixed_radix(i % free_period, free_radices)
    if plan.cells is None:
      cells.append(())
    elif cell_digit:
      cells.append(plan.cells.rows[cell_order[bound[0]]])
    else:
      cells.append(weighted[i])
    for j, edge in enumerate(plan.conditional):
      order = orders[j]
      bounded, digit = digit_of[j]
      values[edge.id].append(order[(
          bound if bounded else free)[digit]] if order else ())
  return KeyDraw(tuple(cells), values, k, capacity)


def expand_keys(
    plan: FanoutPlan,
    keys: Sequence[tuple],
    run_id: str,
    chunk_rows: int,
    draws: Mapping[tuple, KeyDraw | None] | None = None,
) -> Iterator[list[tuple[tuple, tuple]]]:
  """``(key, cell)`` pairs for every child of every key, in chunks of at
    most ``chunk_rows`` — a hot parent never makes an oversized bundle,
    and a key's cells stay unique across the split because they are
    drawn once per key.

    ``draws`` (ADR 0037) is the per-key `joint_key_draw` result a plan
    WITH conditional edges must be expanded from: the cells then come out
    of the joint walk that also decides each child's candidates, so the
    two stay index-aligned across a chunk split. A key absent from the
    mapping, or mapped to ``None`` (dropped: no candidate on a
    non-nullable edge), emits nothing. Without it — every ADR 0036 plan —
    the cells are drawn here exactly as before.
    """
  chunk_rows = max(1, int(chunk_rows))
  chunk: list[tuple[tuple, tuple]] = []
  for key in keys:
    cells: list[tuple]
    if draws is not None:
      draw = draws.get(tuple(key))
      if draw is None:
        continue
      cells = list(draw.cells)
    else:
      rng = random.Random(derive_key_seed(run_id, tuple(key)))
      k = plan.histogram.sample(rng)
      if k <= 0:
        continue
      cells = (
          plan.cells.draw(k, rng, exact=plan.exact_cells)
          if plan.cells is not None else [()] * k)
    for cell in cells:
      chunk.append((tuple(key), tuple(cell)))
      if len(chunk) >= chunk_rows:
        yield chunk
        chunk = []
  if chunk:
    yield chunk
