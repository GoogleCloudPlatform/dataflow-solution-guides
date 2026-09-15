"""Effective-model adjustments — when the SOURCE contradicts the model
(ADR 0038).

The relationship model (`config/relationships/*.yaml`, ADR 0032) is a
DECLARATION. The source table is the authority for what the data
actually IS. When a full-source measurement PROVES the declaration
wrong — the declared `pk:` is not a key of the source — the launch no
longer stops: it drops that key from the EFFECTIVE model, shouts, and
carries on, so the landing table reproduces the source's key-repeat
distribution by construction.

Only a measured conflict adjusts. A model SELF-contradiction (an unknown
column, two edges both `drives: true`, two edges writing one child
column, an ambiguous role) keeps stopping — no amount of data resolves
those.

This module is the pure-Python half: the record, the two repeat shares
that prove the copy is faithful, the operator banner, and the effective
model rendered back as YAML. The detection lives in
`sdfb_beam.cli.preflight` (it needs the fan-out measurement); the
announcement lives in `sdfb_beam.cli.run_pipeline`.
"""

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

import textwrap
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from sdfb_core.contracts.relationships import RelationshipModel

__all__ = [
    "REPEAT_SHARE_TOLERANCE",
    "ModelAdjustment",
    "adjusted_model_yaml",
    "adjusted_models",
    "adjustment_banner",
    "landed_distinct_keys",
    "landing_repeat_share",
    "pk_measurement_from_histogram",
    "pk_repeat_share",
    "repeat_share_verdict",
    "source_repeat_share",
]

# Absolute tolerance between the SOURCE key-repeat share and the one the
# landing table lands with (ADR 0038 §5).
#
# The copy is faithful BY CONSTRUCTION — every parent key draws its child
# count from the same measured histogram — so the only spread is
# sampling: the per-key draw converges at O(1/sqrt(keys)) (well under
# half a point at the 52k keys of the 2026-09-12 launch), plus the
# derived row count's rounding and, on an orphan-heavy source, the fact
# that the child copies the MATCHED share's distribution rather than the
# whole source's. Five points is loose enough to survive all three and
# tight enough that a structural break is unmissable: a fan-out capped
# to one child lands ~0.00 and a doubled one ~0.75, both far outside.
REPEAT_SHARE_TOLERANCE = 0.05


@dataclass(frozen=True)
class ModelAdjustment:
  """One change the launch made to the declared model, and why.

    Every field exists to be READ by an operator: what was declared,
    what the source measured, what changed, and what follows from it.
    ``source_repeat_share`` is half of the proof that the landing table
    copies the source (`landing_repeat_share` is the other half, measured
    at the end of the run).
    """

  table: str
  # A stable, greppable verb for the milestone. Only one today.
  change: str
  declared: str
  measured: str
  consequence: str
  # The columns the model declared as `pk:`. They are dropped from the
  # effective model but KEPT for measurement: `pk.duplicate` is still
  # counted against them, it simply stops gating (ADR 0038 §3).
  declared_pk: tuple[str, ...] = ()
  # Measured over ``declared_pk`` — the SAME columns `pk.duplicate` is
  # counted over at the end of the run (fix J), so the two shares are
  # comparable in every case and the ±tolerance verdict always means
  # what it says. None only where the launch had no measurement of the
  # declared PK itself; then no share is claimed and no verdict is
  # written, rather than a number measured over other columns (which is
  # what fix H4's `repeat_share_basis` had to disclaim).
  source_repeat_share: float | None = None

  @property
  def table_name(self) -> str:
    """Bare table name — model files key on it, tables arrive FQN."""
    return self.table.rsplit(".", 1)[-1]


def source_repeat_share(histogram: Mapping[str | int, int]) -> float | None:
  """Share of the SOURCE child's rows that repeat a key value, read off
    the fan-out histogram already in hand — ``1 - key_values / children``.

    The histogram counts rows per CHILD KEY VALUE (`measure_fanout`
    groups the source child by the driving edge's columns), so
    ``sum(n for k > 0)`` is the number of distinct key values and
    ``sum(k * n)`` the rows carrying them. For E_TABLE on launch
    2026-09-12_14_50_30 that is ``1 - 583,134 / 1,172,025 = 0.5025``.

    ``None`` when the histogram carries no mass — nothing was measured,
    so nothing is claimed.
    """
  hist = {int(k): int(n) for k, n in histogram.items()}
  children = sum(k * n for k, n in hist.items())
  if children <= 0:
    return None
  key_values = sum(n for k, n in hist.items() if k > 0)
  return 1.0 - key_values / children


def pk_repeat_share(measurement: Mapping[str, Any] | None) -> float | None:
  """Share of the SOURCE child's rows that repeat a DECLARED PK tuple —
    ``1 - key_tuples / rows`` over the measurement
    `sdfb_beam.io.fanout_stats.measure_pk_uniqueness` takes (ADR 0038
    fix J).

    This is the authority for "is the declared `pk:` a key of this
    data". The fan-out histogram answers the same question for ONE
    column set — the driving edge — so a key with a member outside that
    edge (F_TABLE's `[D_COL_001, CONTINUOUS_NR]`, launch
    …-12600311608685394436) had no measured evidence at all until this
    one existed, and the BLOCKER gate discovered the answer after the
    GPU hours were spent: 179,853 duplicates, 0.2908 against a 0.2 gate.

    ``None`` when nothing was measured — no rows, or no measurement.
    """
  if not measurement:
    return None
  rows = int(measurement.get("rows") or 0)
  if rows <= 0:
    return None
  return 1.0 - int(measurement.get("key_tuples") or 0) / rows


def pk_measurement_from_histogram(histogram: Mapping[str | int, int],
                                  cols: Sequence[str]) -> dict | None:
  """The same three numbers, read off the fan-out histogram, for a
    declared PK that IS the driving edge (ADR 0038 fix J).

    The histogram counts rows per driving-edge VALUE, so for that PK it
    already measures the key tuple: ``sum(k*n)`` rows over
    ``sum(n for k>0)`` tuples, the largest carrying ``max(k)``. Reading
    it here is what subsumes the old driving-edge-only rule into the one
    declared-PK decision — and it costs no BigQuery at all.

    ``None`` when the histogram carries no mass.
    """
  hist = {int(k): int(n) for k, n in histogram.items()}
  rows = sum(k * n for k, n in hist.items())
  if rows <= 0:
    return None
  return {
      "cols": list(cols),
      "rows": rows,
      "key_tuples": sum(n for k, n in hist.items() if k > 0),
      "max_rows_per_key": max(hist) if hist else 0,
  }


def landed_distinct_keys(rows: int, repeat_share: float | None) -> int:
  """How many DISTINCT key values a table LANDS (ADR 0038, fix H3).

    A table whose PK the run enforces lands one row per key, so its rows
    and its distinct keys are the same number. An ADJUSTED table does
    not: it reproduces the source's key repeats, so
    ``rows x (1 - source_repeat_share)`` of its rows carry a key value no
    other row carries.

    This is the multiplier a DESCENDANT must be sized from. The composer
    fans a child out from its parent's DISTINCT keys (`parent_pk=()`
    arms `FanoutDistinct` on an adjusted parent), and the fan-out
    histogram's own denominator is the number of distinct SOURCE key
    values — so multiplying the parent's ROWS instead asks the child for
    rows it cannot produce: 220,215 requested against ~109,556
    producible on the 2026-09-12 shape, a 50.25% shortfall recorded as a
    missed request.
    """
  if rows <= 0 or repeat_share is None:
    return max(0, int(rows))
  return max(1, round(rows * (1.0 - repeat_share)))


def landing_repeat_share(*, valid_count: int,
                         dlq_by_rule: Mapping[str, int]) -> float | None:
  """Share of the LANDED rows that repeat a PK tuple.

    Only meaningful in ``streaming`` uniqueness mode, which is what an
    adjusted table runs in: nothing is removed, so ``valid_count`` is the
    DISTINCT row-digest count and ``valid_count + row.duplicate`` is the
    number of rows generated. ``pk.duplicate`` over that is the share.
    (The two rules are independent branches, so the pk excess must NOT
    go in the denominator — it is already inside the row count.)
    """
  rows = int(valid_count) + int(dlq_by_rule.get("row.duplicate", 0))
  if rows <= 0:
    return None
  return int(dlq_by_rule.get("pk.duplicate", 0)) / rows


def repeat_share_verdict(
    source: float | None,
    landing: float | None,
    tolerance: float = REPEAT_SHARE_TOLERANCE,
) -> tuple[float | None, bool | None]:
  """``(delta, within_tolerance)`` — ``(None, None)`` when either share
    is missing, because "no measurement" is not "they agree"."""
  if source is None or landing is None:
    return None, None
  delta = landing - source
  return delta, abs(delta) <= tolerance


def adjustment_banner(adjustments: Sequence[ModelAdjustment]) -> str:
  """The launcher's multi-line block, in the relationship card's style.

    Launcher-side only: worker milestones stay one line. A run that
    adjusted its model must be impossible to mistake for a clean one, so
    this states all four facts per table — declared, measured, changed,
    consequence — rather than a count and a rule id.
    """
  if not adjustments:
    return ""
  lines = [
      f"MODEL ADJUSTED | {len(adjustments)} table(s) — the SOURCE "
      f"contradicted the declared relationship model (ADR 0038)",
      "  the measurement is the authority for what the data IS; the "
      "model is a declaration. This run generated with the model "
      "below, NOT the one on disk.",
  ]
  for adj in adjustments:
    share = adj.source_repeat_share
    lines.append(f" {adj.table}   [{adj.change}]")
    lines.append(f"   declared    | {adj.declared}")
    lines.append(f"   measured    | {adj.measured}")
    lines.append(f"   changed     | {adj.consequence}")
    if share is not None:
      lines.append(f"   source key-repeat share | {share:.2%} over "
                   f"{list(adj.declared_pk)} — the landing table must match "
                   f"it within {REPEAT_SHARE_TOLERANCE:.0%} "
                   f"(validation_runs.repeat_share_delta)")
    else:
      # No measurement of the DECLARED PK itself (the capacity
      # ladder proved the conflict instead). Say that, rather than
      # print a share measured over other columns: a verdict across
      # two column sets is a false negative, and a false negative on
      # this banner is worse than no verdict at all.
      lines.append("   source key-repeat share | not measured over "
                   f"{list(adj.declared_pk)} this launch — no "
                   f"±{REPEAT_SHARE_TOLERANCE:.0%} verdict is written")
  lines.append(
      "  pk.duplicate on the adjusted table(s) is EXPECTED and is "
      "excluded from the BLOCKER gate; every other table's still blocks.")
  lines.append("  Re-declare the model from the emitted YAML "
               "(model_adjustment_model milestone) to make this permanent, or "
               "pass --on_model_conflict=stop to refuse the launch instead.")
  return "\n".join(lines)


def _comment(prefix: str, body: str, indent: str = "  ") -> list[str]:
  """A YAML comment, wrapped to stay readable in a terminal and a diff.

    The `measured` and `consequence` sentences are long by design (they
    are the whole explanation), and one 250-character `#` line is a line
    nobody reads.
    """
  return [
      f"{indent}# {line}" for line in textwrap.wrap(
          f"{prefix}{body}",
          width=72,
          subsequent_indent="  ",
          break_long_words=False,
          break_on_hyphens=False,
      )
  ] or [f"{indent}# {prefix}"]


def _plain(value):
  """Pydantic dumps tuples; ``yaml.safe_dump`` represents lists only."""
  if isinstance(value, tuple | list):
    return [_plain(v) for v in value]
  if isinstance(value, dict):
    return {k: _plain(v) for k, v in value.items()}
  return value


def _dump(payload: dict) -> str:
  return str(
      yaml.safe_dump(
          payload,
          sort_keys=False,
          allow_unicode=True,
          default_flow_style=False,
      ))


def adjusted_model_yaml(
    model: RelationshipModel,
    adjustments: Sequence[ModelAdjustment],
) -> str:
  """ONE model file's EFFECTIVE content, as YAML the operator can paste
    straight into ``config/relationships/<model>.yaml``.

    The declared model, with each adjusted table's ``pk:`` removed and a
    comment above it naming the measurement that removed it. Everything
    else — edges, identity, flags, notes — is carried through verbatim,
    because nothing else changed: an adjusted table still draws its
    parent keys exactly as declared, so no FK guarantee moves.

    One model per call (one YAML document per file, the shape
    `parse_relationship_model` reads back), rendered rather than
    round-tripped through ``safe_dump`` alone so the per-table reason
    survives as a real YAML comment.
    """
  reasons = {a.table_name: a for a in adjustments}
  touched = sorted(set(reasons) & set(model.tables))
  head: dict[str, str] = {"model": model.model}
  if model.description:
    head["description"] = model.description
  lines = [
      f"# Effective relationship model for {model.model} "
      f"(emitted by the launch, ADR 0038).",
  ]
  if touched:
    lines.append(f"# {len(touched)} table(s) ADJUSTED against the declared "
                 f"model: {', '.join(touched)}.")
  else:
    lines.append("# No table in this model was adjusted.")
  lines.append(_dump(head).rstrip("\n"))
  lines.append("tables:")
  for name, relations in model.tables.items():
    adjustment = reasons.get(name)
    body = _plain(relations.model_dump(exclude_defaults=True))
    if adjustment is not None:
      body.pop("pk", None)
      lines.extend(
          _comment(
              f"pk {list(adjustment.declared_pk)} REMOVED — ",
              adjustment.measured,
          ))
      lines.extend(_comment("consequence: ", adjustment.consequence))
    if not body:
      lines.append(f"  {name}: {{}}")
      continue
    lines.append(f"  {name}:")
    lines.extend(f"    {line}" if line.strip() else line
                 for line in _dump(body).rstrip("\n").splitlines())
  return "\n".join(lines) + "\n"


def adjusted_models(
    models: Iterable[RelationshipModel],
    adjustments: Sequence[ModelAdjustment],
) -> list[tuple[RelationshipModel, tuple[ModelAdjustment, ...]]]:
  """Every model that owns at least one adjusted table, with its own
    adjustments — what the launcher writes one file each for."""
  by_name = {a.table_name: a for a in adjustments}
  out = []
  for model in models:
    mine = tuple(by_name[t] for t in model.tables if t in by_name)
    if mine:
      out.append((model, mine))
  return out
