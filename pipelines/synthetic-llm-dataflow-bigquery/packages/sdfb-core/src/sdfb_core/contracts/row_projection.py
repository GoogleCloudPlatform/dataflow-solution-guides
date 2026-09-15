"""What a launch will GENERATE, decided before it generates anything
(ADR 0039).

The sizing itself is not new: a root takes ``--num_rows``, a driven child
takes its parent's DISTINCT landed keys times the measured mean fan-out
([ADR 0036](../../../../../docs/adr/0036-parent-driven-fanout-generation.md)
D5, [ADR 0038](../../../../../docs/adr/0038-measured-conflicts-adjust-the-model.md)
fix H3). What is new is saying it OUT LOUD, in one block, before the graph
exists: per table the count and the measurement it came from, the total
across the launch, and a warning wherever a measured relationship makes
one of those counts untrustworthy.

Pure Python — the figures arrive as plain numbers the launcher already
holds (the resolved row counts, the fan-out histograms, the source parent
tuple count, the ADR 0038 repeat shares). Nothing here reads BigQuery and
nothing here re-measures: a projection that cannot be derived says so
rather than printing a number.
"""

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "BAR_WIDTH",
    "MATCHED_SHARE_FLOOR",
    "PROJECTION_FACTOR_CEILING",
    "ZERO_SHARE_CEILING",
    "ProjectionWarning",
    "TableProjection",
    "fanout_mean",
    "project_table",
    "projected_total",
    "projection_banner",
    "projection_warnings",
]

# --- thresholds -------------------------------------------------------
#
# Each one is a "this projection is unsound" bar, not a style preference,
# and each is deliberately loose: a warning an operator learns to ignore
# is worse than no warning. A clean launch must print no warning at all.

# A driven child projected at more than this multiple of the launch's own
# `--num_rows` is a run the operator did not ask for. The five-table
# launch 2026-09-13_06_10_16 asked for 210,958 rows and projected
# 30,937,138 on one table — 146.7x, and ~18 hours of L4 time. Two
# hops of an ordinary 2-4x fan-out land at 4-16x, so a ceiling below 10
# would fire on shapes that are working exactly as declared.
PROJECTION_FACTOR_CEILING = 10.0

# A driving edge whose source parent covers less than half of the source
# child's distinct key values. The child then generates from the matched
# share of its key space only, the fan-out's zero bucket is unmeasurable
# and its mean is an upper bound (`fk_fanout_source_orphans`). Half is
# the point past which "the source's FK is a bit dirty" stops being a
# credible reading of the measurement: the same launch measured 9%.
MATCHED_SHARE_FLOOR = 0.5

# A driving edge where more than this share of the source parent's key
# values carry NO child at all. The row count is still right (the mean
# already divides by every parent, childless ones included); what is
# wrong is any expectation that the landed child covers its parent. The
# 2026-09-13 F_TABLE edge measured 0.2024 and is not worth a word.
ZERO_SHARE_CEILING = 0.5

# Width of the share bar in the block — wide enough that 5% is one mark
# and a dominant table is unmistakable, narrow enough for an 80-column
# terminal once the name, count, share and derivation are on the line.
BAR_WIDTH = 20

_NOT_DERIVABLE = "not derivable"


@dataclass(frozen=True)
class TableProjection:
  """One table's projected record count and where it came from.

    ``rows`` is ``None`` when the derivation was not available — that is
    a statement ("this launch cannot say"), never a fallback to
    ``--num_rows``, which for a driven child is the one number that is
    certainly wrong.
    """

  table: str
  rows: int | None
  # The DRIVING parent's bare model name. Empty = a root, sized by
  # `--num_rows`.
  parent: str = ""
  # `FkEdgeSpec.edge_id` for the driving edge, e.g. `(D_COL_001)->B_TABLE`.
  edge: str = ""
  # The parent's landed rows, and the DISTINCT keys it hands out. They
  # differ only when the parent's own `pk:` was adjusted away (ADR 0038
  # fix H3) — and the DISTINCT count is the multiplier.
  parent_rows: int | None = None
  parent_keys: int | None = None
  mean_fanout: float | None = None
  # Measured on the driving edge: the share of the source parent's key
  # values with no child, and the share of the source child's key
  # values the source parent actually covers (None when the parent
  # covers all of them — nothing to warn about).
  zero_share: float | None = None
  matched_share: float | None = None
  # This table's OWN ADR 0038 adjustment: whether the effective model
  # dropped its `pk:`, and the SOURCE key-repeat share that proved it
  # (None when the capacity ladder proved the conflict instead — then
  # no share is claimed, exactly as in `adjustment_banner`).
  adjusted: bool = False
  repeat_share: float | None = None
  # Why ``rows`` is None. Empty whenever ``rows`` is set.
  undecided: str = ""

  @property
  def table_name(self) -> str:
    """Bare table name — the model and the log card key on it."""
    return self.table.rsplit(".", 1)[-1]

  @property
  def driven(self) -> bool:
    return bool(self.parent)

  @property
  def basis(self) -> str:
    """The derivation, as the operator reads it on the line."""
    if self.rows is None:
      return f"{_NOT_DERIVABLE} — {self.undecided}"
    if not self.driven:
      return f"root — sized by --num_rows={self.rows:,}"
    keys = f"{self.parent_keys:,}" if self.parent_keys is not None else "?"
    mean = f"{self.mean_fanout:.4f}" if self.mean_fanout is not None else "?"
    basis = (f"driven by {self.parent} — {keys} distinct keys x {mean} "
             f"mean fan-out on {self.edge or '(driving edge)'}")
    if (self.parent_rows is not None and self.parent_keys is not None and
        self.parent_rows != self.parent_keys):
      basis += (f" (its parent lands {self.parent_rows:,} rows over "
                f"{self.parent_keys:,} DISTINCT keys — ADR 0038)")
    return basis


@dataclass(frozen=True)
class ProjectionWarning:
  """One reason part of the projection is unsound.

    ``measurement`` names the figure that triggered it (and the milestone
    that carries it) so the operator can go and read it; ``consequence``
    says what it means for the LANDED table. A warning without both is a
    warning nobody acts on.
    """

  code: str
  table: str
  measurement: str
  consequence: str

  @property
  def table_name(self) -> str:
    return self.table.rsplit(".", 1)[-1]


def _histogram(histogram: Mapping[str | int, int] | None) -> dict[int, int]:
  return {int(k): int(n) for k, n in (histogram or {}).items()}


def fanout_mean(histogram: Mapping[str | int, int] | None) -> float | None:
  """Children per parent key value — ``sum(k*n) / sum(n)``, the zero
    bucket INCLUDED in the denominator, exactly as `FanoutHistogram.mean`
    and `_derived_rows` compute it.

    ``None`` when the histogram carries no mass (no parents measured, or
    every measured parent has zero children): nothing was measured, so
    nothing is claimed.
    """
  hist = _histogram(histogram)
  total = sum(hist.values())
  children = sum(k * n for k, n in hist.items())
  if total <= 0 or children <= 0:
    return None
  return children / total


def _zero_share(histogram: Mapping[str | int, int] | None) -> float | None:
  hist = _histogram(histogram)
  total = sum(hist.values())
  return hist.get(0, 0) / total if total > 0 else None


def _matched_share(histogram: Mapping[str | int, int] | None,
                   source_parent_tuples: int | None) -> float | None:
  """The share of the source CHILD's distinct key values the source
    PARENT actually covers — the `fk_fanout_source_orphans` figure, read
    back off the two numbers the measurement already carries.

    ``None`` when the parent covers all of them (the ordinary case: the
    zero bucket is then real and there is nothing to say) or when the
    parent tuple count was not carried.
    """
  if not source_parent_tuples:
    return None
  key_values = sum(n for k, n in _histogram(histogram).items() if k > 0)
  if key_values <= source_parent_tuples:
    return None
  return source_parent_tuples / key_values


def project_table(
    table: str,
    *,
    launch_rows: int,
    parent: str = "",
    edge: str = "",
    parent_rows: int | None = None,
    parent_keys: int | None = None,
    histogram: Mapping[str | int, int] | None = None,
    source_parent_tuples: int | None = None,
    adjusted: bool = False,
    repeat_share: float | None = None,
) -> TableProjection:
  """One table's projection, from figures the launcher already holds.

    A table with no driving parent is a ROOT and lands ``launch_rows``.
    A DRIVEN child lands ``round(parent_keys x mean fan-out)`` — the same
    expression, over the same two numbers, that `preflight._derived_rows`
    sized it with, so the block can never disagree with the count the run
    actually requests.
    """
  if not parent:
    return TableProjection(
        table=table,
        rows=max(0, int(launch_rows)),
        adjusted=adjusted or repeat_share is not None,
        repeat_share=repeat_share,
    )
  mean = fanout_mean(histogram)
  keys = parent_keys if parent_keys is not None else parent_rows
  common = {
      "table": table,
      "parent": parent,
      "edge": edge,
      "parent_rows": parent_rows,
      "parent_keys": keys,
      "mean_fanout": mean,
      "zero_share": _zero_share(histogram),
      "matched_share": _matched_share(histogram, source_parent_tuples),
      "adjusted": adjusted or repeat_share is not None,
      "repeat_share": repeat_share,
  }
  if mean is None:
    return TableProjection(
        rows=None,
        undecided=(f"the source fan-out histogram for its driving edge "
                   f"{edge or '(driving edge)'} carries no mass, so the "
                   f"mean fan-out this table is sized from does not exist"),
        **common,  # type: ignore[arg-type]
    )
  if not keys:
    return TableProjection(
        rows=None,
        undecided=(f"the distinct keys its driving parent {parent} lands are "
                   f"unknown here, so there is nothing to multiply the "
                   f"measured mean fan-out by"),
        **common,  # type: ignore[arg-type]
    )
  return TableProjection(
      rows=round(keys * mean), **common)  # type: ignore[arg-type]


def projected_total(projections: Sequence[TableProjection]) -> int:
  """Rows across every table whose projection IS derivable. A table
    that could not be derived is left out of the sum and named in the
    block — it is not counted as zero."""
  return sum(p.rows or 0 for p in projections)


def _chain(projection: TableProjection,
           by_name: Mapping[str, TableProjection]) -> list[str]:
  """``[root, …, this table]`` — the parent chain that produced a
    driven child's count, walked up by bare name and cycle-safe."""
  names = [projection.table_name]
  seen = {projection.table_name}
  current = projection
  while current.parent and current.parent not in seen:
    names.append(current.parent)
    seen.add(current.parent)
    nxt = by_name.get(current.parent)
    if nxt is None:
      break
    current = nxt
  return list(reversed(names))


def projection_warnings(
    projections: Sequence[TableProjection],
    *,
    launch_rows: int,
    factor_ceiling: float = PROJECTION_FACTOR_CEILING,
    matched_share_floor: float = MATCHED_SHARE_FLOOR,
    zero_share_ceiling: float = ZERO_SHARE_CEILING,
) -> tuple[ProjectionWarning, ...]:
  """Every measured reason this projection is unsound, in generation
    order. Empty on a clean launch — by design, and the reason each
    threshold above is loose."""
  by_name = {p.table_name: p for p in projections}
  total = projected_total(projections)
  out: list[ProjectionWarning] = []
  for p in projections:
    if p.matched_share is not None and p.matched_share < matched_share_floor:
      out.append(
          ProjectionWarning(
              code="fk_source_orphans",
              table=p.table,
              measurement=(f"the source parent covers only "
                           f"{p.matched_share:.1%} of the distinct key values "
                           f"the source child holds on the driving edge "
                           f"{p.edge or '(driving edge)'} "
                           f"(fk_fanout_source_orphans matched_share="
                           f"{p.matched_share:.4f}, under the "
                           f"{matched_share_floor:.0%} floor)"),
              consequence=(f"{p.table_name} generates from the matched "
                           f"{p.matched_share:.1%} of its key space only, so "
                           f"the projected count is honest but the MODEL is "
                           f"suspect: the landed table reproduces that slice "
                           f"of the source's key range and nothing outside "
                           f"it, and the fan-out's zero bucket is "
                           f"unmeasurable, so the mean is an UPPER bound. "
                           f"Check the edge names the columns you meant."),
          ))
    if p.driven and p.rows is not None and launch_rows > 0:
      factor = p.rows / launch_rows
      if factor > factor_ceiling:
        share = p.rows / total if total else 0.0
        out.append(
            ProjectionWarning(
                code="projection_explodes",
                table=p.table,
                measurement=(
                    f"{p.rows:,} projected rows is {factor:,.1f}x the "
                    f"launch's --num_rows={launch_rows:,} (over the "
                    f"{factor_ceiling:,.0f}x ceiling), down the chain " +
                    " -> ".join(_chain(p, by_name))),
                consequence=(f"this one table is {share:.1%} of the run's "
                             f"{total:,} rows and sizes the whole job — GPU "
                             f"hours, shuffle and landing cost scale with it. "
                             f"It is not a defect: it is the measured "
                             f"fan-out, compounded. Lower --num_rows, or "
                             f"take the table out of the launch, if that is "
                             f"not the run you meant to pay for."),
            ))
    if p.zero_share is not None and p.zero_share > zero_share_ceiling:
      out.append(
          ProjectionWarning(
              code="fanout_zero_share",
              table=p.table,
              measurement=(f"{p.zero_share:.1%} of the source parent's key "
                           f"values carry NO child on the driving edge "
                           f"{p.edge or '(driving edge)'} (fk_fanout_measured "
                           f"zero_share={p.zero_share:.4f}, over the "
                           f"{zero_share_ceiling:.0%} ceiling)"),
              consequence=(f"the count is right — the mean already divides by "
                           f"every parent, childless ones included — but the "
                           f"landed {p.table_name} covers only about "
                           f"{1 - p.zero_share:.1%} of its parent's keys, so "
                           f"a join from the parent is mostly empty by "
                           f"construction, exactly as in the source."),
          ))
    if p.adjusted:
      keys = (
          round(p.rows * (1 - p.repeat_share))
          if p.rows is not None and p.repeat_share is not None else None)
      children = [c.table_name for c in projections if c.parent == p.table_name]
      out.append(
          ProjectionWarning(
              code="adjusted_key_projection",
              table=p.table,
              measurement=(
                  "this table's declared `pk:` was ADJUSTED away "
                  "(model_adjusted, ADR 0038)" +
                  (f" because {p.repeat_share:.2%} of the SOURCE's "
                   f"rows repeat a key value" if p.repeat_share is not None else
                   ", with no key-repeat share measured over "
                   "the declared PK this launch")),
              consequence=("its projection assumes that repeat distribution "
                           "reproduces: " +
                           (f"{p.rows:,} rows over about {keys:,} DISTINCT "
                            f"key values" if keys is not None else
                            "its rows carry repeated key values") +
                           (f", which is what {', '.join(children)} "
                            f"{'is' if len(children) == 1 else 'are'} sized "
                            f"from, not the row count" if children else "") +
                           ". Read model_adjustment_repeat_share at the end "
                           "of the run: outside tolerance, this table AND "
                           "every descendant landed a count this block did "
                           "not project."),
          ))
  return tuple(out)


def _bar(share: float) -> str:
  return "#" * round(max(0.0, share) * BAR_WIDTH)


def projection_banner(
    projections: Sequence[TableProjection],
    *,
    launch_rows: int,
    warnings: Sequence[ProjectionWarning] = (),
) -> str:
  """The launcher's multi-line block, in the relationship card's and
    the adjustment banner's style.

    Launcher-side only: worker milestones stay one line. The per-table
    milestones (`row_projection_table`) carry the same figures for log
    mining; this is the surface an operator sizing a run reads.
    """
  del launch_rows  # Unused: root projections already carry it; kept for callers.
  if not projections:
    return ""
  total = projected_total(projections)
  undecided = [p for p in projections if p.rows is None]
  name_w = max(len(p.table_name) for p in projections)
  name_w = max(name_w, len("TOTAL"))
  rows_w = max([len(f"{p.rows:,}") for p in projections if p.rows is not None] +
               [len(f"{total:,}"), len(_NOT_DERIVABLE)])
  dominant = max(
      (p for p in projections if p.rows is not None),
      key=lambda p: p.rows or 0,
      default=None,
  )
  lines = [
      f"ROW PROJECTION | {len(projections)} table(s), {total:,} rows — "
      f"what this launch WILL generate, decided before the graph is "
      f"built (ADR 0039)",
      "  every count is derived from figures this launch already "
      "measured: --num_rows for a root, the driving parent's DISTINCT "
      "landed keys x the measured mean fan-out for a driven child "
      "(ADR 0036, ADR 0038). Nothing is generated yet.",
  ]
  for p in projections:
    count = f"{p.rows:,}" if p.rows is not None else _NOT_DERIVABLE
    share = (p.rows or 0) / total if total else 0.0
    pct = f"{share:.1%}" if p.rows is not None else "—"
    lines.append(f" {p.table_name:<{name_w}} | {count:>{rows_w}} | {pct:>6} | "
                 f"{_bar(share) if p.rows is not None else '':<{BAR_WIDTH}} | "
                 f"{p.basis}")
  tail = (f"across {len(projections)} table(s)" if dominant is None or not total
          else (f"across {len(projections)} table(s) — "
                f"{(dominant.rows or 0) / total:.1%} of the run is "
                f"{dominant.table_name}"))
  if undecided:
    tail += f"; {len(undecided)} table(s) not derivable, excluded"
  lines.append(f" {'TOTAL':<{name_w}} | {total:>{rows_w},} | {'100.0%':>6} | "
               f"{'':<{BAR_WIDTH}} | {tail}")
  if warnings:
    lines.append(f" WARNINGS | {len(warnings)} — a measured relationship makes "
                 f"part of this projection unsound")
    for w in warnings:
      lines.append(f"  {w.table_name}   [{w.code}]")
      lines.append(f"    measured    | {w.measurement}")
      lines.append(f"    consequence | {w.consequence}")
  return "\n".join(lines)
