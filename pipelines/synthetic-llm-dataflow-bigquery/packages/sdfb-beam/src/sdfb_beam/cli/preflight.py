"""Driver-side relational preflight — before any Beam graph is built.

The checks are metadata-only (milliseconds, zero DAG cost) and fail with
the exact fix, following the pool-table precedent in ``run_pipeline.py``
(the 2026-07-25 TEST_1 lesson: a cryptic driver NotFound costs a run).

Check ladder (2026-08-05 spec, WS-A A3; relational input per ADR 0032):
  P2  model columns exist in the schema    → SystemExit naming them
  P3  FK parents resolved (when a resolver is provided) → SystemExit
  P4  the PK's generator can cover num_rows → SystemExit (ADR 0028)
  P5  PK tuple unique in the reference sample → WARNING milestone only
      (source data may legitimately violate an undeclared PK)

The relational input is this table's entry in `config/relationships/`
(ADR 0032) — the model file is the source of truth, so its PK/identity
win and a conflicting ``--pk_cols`` is loudly IGNORED. Tables no model
declares fall back to the CLI flags, which is how a one-off table with
no relationships generates with zero config.
"""

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from sdfb_core.contracts.description_json import DescriptionJsonError
from sdfb_core.contracts.model_adjustment import (
    ModelAdjustment,
    pk_measurement_from_histogram,
    pk_repeat_share,
)
from sdfb_core.contracts.prompt_constraint import (
    parse_llm_prompt_constraint,
    parse_prompt_constraint,
    render_prompt_clause,
)
from sdfb_core.engines.b1_rag.profile import (
    ColumnKind,
    ColumnProfile,
    profile_columns,
)
from sdfb_core.engines.constraint_sampler import compile_pattern_sampler
from sdfb_core.engines.generation_plan import FREE_TEXT_POOL_MAX
from sdfb_core.engines.pk_capacity import (
    FK_KEY_SAMPLE_CEILING,
    effective_cells,
    expected_duplicate_share_cells,
    fk_key_sample_cap,
    max_rows_under_share,
)
from sdfb_core.engines.text_shapes import is_binary_class
from sdfb_core.observability import (
    log_milestone,
    log_milestone_pretty,
    sha12,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
  from sdfb_core.contracts import TableSchema
  from sdfb_core.contracts.relationships import FkEdge, TableRelations


@dataclass(frozen=True)
class PreflightResult:
  """What preflight resolved for one table: PK, identity, caps and warnings."""

  pk_cols: tuple[str, ...]
  identity_cols: tuple[str, ...]
  # This table's entry in `config/relationships/` (ADR 0032), or None
  # when no model declares it — then PK/identity come from the CLI.
  relations: TableRelations | None
  warnings: list[str] = field(default_factory=list)
  # ADR 0035 — per enforced edge whose columns sit in this table's PK,
  # the parent key-tuple sample the DAG must broadcast (child cols →
  # cap). Edges outside the PK keep the composer's floor.
  fk_key_sample_caps: dict[tuple[str, ...], int] = field(default_factory=dict)
  # ADR 0036 — for a DRIVEN child (a `fanout` payload was given): the
  # row count this table's fan-out implies, `round(parent_rows * mean
  # k)`. None when the table is not driven, or the driving parent's
  # row count is unknown.
  derived_rows: int | None = None
  # ADR 0038 — every change the full-source measurement forced on the
  # DECLARED model (today: a `pk:` the source proves is not a key).
  # `pk_cols` above is already the ADJUSTED tuple; these records carry
  # what was dropped and why, for the launcher's banner, the emitted
  # YAML, and the gate exclusion.
  adjustments: tuple[ModelAdjustment, ...] = ()


def _missing(cols: tuple[str, ...], valid: set[str]) -> list[str]:
  return [c for c in cols if c not in valid]


def _report_prompt_constraints(table_schema: TableSchema,
                               enabled: bool) -> None:
  """Say aloud whether any column carries an `llm_prompt_constraint`.

    C5 is prompt refinement, never a requirement: `--prompt_constraints=on`
    against a DDL with no constraints anywhere is a perfectly healthy
    no-op — but a silent one reads as 'did it even look?'. One informative
    milestone answers that. A column whose description carries a MARKED but
    unparseable constraint object stops loudly (the P1 posture, per column).
    """
  found: dict[str, dict] = {}
  for col in table_schema.columns:
    try:
      clause = parse_llm_prompt_constraint(col.description, column=col.name)
    except DescriptionJsonError as exc:
      raise SystemExit(
          f"[preflight P1] {table_schema.fqn}.{col.name}: column "
          f"description carries an 'llm_prompt_constraint'-marked JSON "
          f"object that does not parse.\n{exc}") from exc
    if clause:
      # The fetched clause travels with the milestone (2026-08-20
      # follow-up): a Terraform edit is verifiable from logs by
      # clause text or by diffing clause_sha12 across launches.
      found[col.name] = {
          "clause": clause,
          "clause_sha12": sha12(clause),
          "chars": len(clause),
      }
  if found:
    log_milestone(
        "prompt_constraints_found",
        columns=",".join(found),
        count=len(found),
        enabled=enabled,
        detail=json.dumps(found, separators=(",", ":")),
    )
  elif enabled:
    log_milestone(
        "prompt_constraints_none",
        note="no column description carries llm_prompt_constraint — "
        "--prompt_constraints=on is a no-op, generation unchanged",
    )


# Column types that route through the (capped) free-text machinery —
# everything else keeps its typed route and never touches a pool
# (ADR 0024; mirrors b1_rag/profile._STRINGY_BQ_TYPES).
_POOL_ROUTED_BQ_TYPES = frozenset({"STRING", "JSON", "GEOGRAPHY", "BYTES"})


def _pk_capacity_factor(
    field_,
    pc,
    profile: ColumnProfile | None = None) -> tuple[int | None, bool]:
  """One PK member's ``(unique-value capacity, draws_at_random)``;
    capacity ``None`` = unbounded.

    Unconstrained members follow the engine's profile (ADR 0035): a
    CATEGORICAL or CONSTANT member only ever re-emits its observed
    domain, drawn at random with no collision rejection — the second
    factor of the 2026-09-09 C_TABLE collapse. Any other unconstrained
    member, and a non-STRING type with a cosmetic clause, keeps its
    typed generator and is unbounded (the 2026-08-22 A_TABLE false
    stop). Bounded constrained members: an enum ``values`` clause (its
    domain, random), a samplable ``pattern`` (its language, with the
    emitted-set rejection of ADR 0028 — exact), else the
    ``FREE_TEXT_POOL_MAX`` pool cap (random)."""
  if pc is None:
    return _unconstrained_factor(profile), profile is not None
  if pc.values:
    return len(pc.values), True
  if (field_.bq_type not in _POOL_ROUTED_BQ_TYPES or field_.is_struct or
      field_.is_repeated):
    return None, False
  if pc.pattern:
    sampler = compile_pattern_sampler(pc.pattern, families=pc.families)
    if sampler is not None:
      return sampler.capacity, False
  return FREE_TEXT_POOL_MAX, True


def _unconstrained_factor(profile: ColumnProfile | None) -> int | None:
  """An unconstrained member's capacity from the engine's own profile:
    the observed domain for CATEGORICAL, 1 for CONSTANT, else unbounded."""
  if profile is None:
    return None
  if profile.kind is ColumnKind.CATEGORICAL:
    return max(1, len(profile.categories))
  if profile.kind is ColumnKind.CONSTANT:
    return 1
  return None


def _is_sampled_member(pc, profile: ColumnProfile | None) -> bool:
  """True for an unconstrained CATEGORICAL/CONSTANT member — the
    engine re-emits its observed joint distribution, so its collision
    behaviour is the reference sample's, not a uniform draw."""
  return pc is None and profile is not None and profile.kind in (
      ColumnKind.CATEGORICAL,
      ColumnKind.CONSTANT,
  )


def _joint_cell_weights(reference_rows: list[dict],
                        cols: tuple[str, ...]) -> list[float]:
  """Row counts of every observed value tuple over ``cols`` (ADR 0035
    rev): the joint cells, not the product of per-column distinct counts
    — the 2026-09-09_16_44 pair covered 12 cells of a 24-cell grid, and
    skewed, so the uniform model read 18% where the run lost 56.5%."""
  counts = Counter(tuple(str(r.get(c)) for c in cols) for r in reference_rows)
  return [float(n) for n in counts.values()] or [1.0]


# Expected pk.duplicate share above which P4 warns even under the gate.
_PK_DUPLICATE_WARN_SHARE = 0.01


@dataclass
class _PkMembers:
  """The PK's non-FK members, folded (ADR 0035): named factors, the
    product of the uniform/exact ones, the sampled (categorical) member
    names, and whether any member draws at random or is unbounded."""

  factors: dict[str, int] = field(default_factory=dict)
  uniform: int = 1
  sampled_cols: list[str] = field(default_factory=list)
  unbounded: bool = False
  random_draw: bool = False


def _fold_pk_members(
    table_schema: TableSchema,
    effective_pk: tuple[str, ...],
    skip: set[str],
    profiles: Mapping[str, ColumnProfile],
) -> _PkMembers:
  by_name = {c.name: c for c in table_schema.columns}
  m = _PkMembers()
  for col in effective_pk:
    if col in skip:
      continue
    field_ = by_name.get(col)
    if field_ is None:
      continue
    pc = parse_prompt_constraint(field_.description, column=col)
    profile = profiles.get(col)
    factor, at_random = _pk_capacity_factor(field_, pc, profile)
    if factor is None:
      m.unbounded = True  # one unbounded member covers the tuple
      return m
    m.factors[col] = factor
    m.random_draw = m.random_draw or at_random
    if _is_sampled_member(pc, profile):
      m.sampled_cols.append(col)
    else:
      m.uniform *= factor
  return m


def _random_draw_stop(
    fqn: str,
    effective_pk: tuple[str, ...],
    num_rows: int,
    product: int,
    detail: str,
    share: float,
    gate: float,
    max_rows: int | None,
    at_ceiling: bool,
) -> SystemExit:
  ceiling_note = (
      f" The FK key sample is at its {FK_KEY_SAMPLE_CEILING:,}-tuple "
      f"side-input ceiling; beyond it the parent join must move to "
      f"the co-partitioned shuffle (ADR 0031)." if at_ceiling else "")
  return SystemExit(
      f"[preflight P4] {fqn}: the declared PK tuple {list(effective_pk)} "
      f"draws at RANDOM from {product:,} possible tuples ({detail}); "
      f"{share:.1%} of num_rows={num_rows:,} would divert as "
      f"pk.duplicate — over the {gate:.0%} BLOCKER gate. Largest run "
      f"under the gate: {max_rows:,} rows. Fix one of: --num_rows <= "
      f"{max_rows:,}; a PK member with an unbounded typed route or a "
      f"samplable 'pattern'; a parent that lands more keys.{ceiling_note}")


def _check_pk_capacity(
    table_schema: TableSchema,
    effective_pk: tuple[str, ...],
    num_rows: int,
    *,
    profiles: Mapping[str, ColumnProfile] | None = None,
    reference_rows: list[dict] | None = None,
    fk_edges: tuple[FkEdge, ...] = (),
    fk_parent_rows: Mapping[str, int] | None = None,
    blocker_failure_ratio: float = 1.0,
) -> dict[tuple[str, ...], int]:
  """P4 (ADR 0028, tuple-aware since ADR 0030, FK/categorical-aware
    since ADR 0035) — the PK TUPLE's generator capacity must cover
    ``num_rows`` unique values. Returns the FK key-sample cap per
    enforced edge whose columns sit in the PK.

    Tuple capacity is the PRODUCT of per-member factors
    (`_pk_capacity_factor`); any unbounded member passes the whole
    tuple. An enforced FK edge inside the PK counts ONCE, as the number
    of parent key tuples the child will see: the composer's sample cap
    (sized here from ``num_rows`` and the sibling members) bounded by
    the parent's row count when known. Members drawn at random with no
    collision rejection make the tuple a balls-into-bins process, so
    beyond the hard ``product < num_rows`` rule the expected duplicate
    share is compared with the BLOCKER gate — over the JOINT cells the
    sampled (categorical) members cover in the reference sample, with
    their observed skew (2026-09-09: 87.9% at the flat cap; 56.5% with
    the sized sample where the per-column uniform model said 32%)."""
  fk_parent_rows = fk_parent_rows or {}
  pk_set = set(effective_pk)
  edges = tuple(fk for fk in fk_edges if fk.enforced and pk_set & set(fk.cols))
  m = _fold_pk_members(
      table_schema,
      effective_pk,
      skip={c for fk in edges for c in fk.cols},
      profiles=profiles or {},
  )
  cells = (
      _joint_cell_weights(reference_rows or [], tuple(m.sampled_cols))
      if m.sampled_cols else [1.0])
  n_eff = effective_cells(cells)
  other = None if m.unbounded else m.uniform * n_eff
  caps = {tuple(fk.cols): fk_key_sample_cap(num_rows, other) for fk in edges}
  if m.unbounded:
    return caps
  uniform = m.uniform
  for fk in edges:
    parent_rows = fk_parent_rows.get(fk.ref)
    cap = caps[tuple(fk.cols)]
    key_count = min(cap, parent_rows) if parent_rows else cap
    m.factors[f"fk({','.join(fk.cols)})->{fk.ref}"] = key_count
    uniform *= key_count
    m.random_draw = True
  if not m.factors:
    return caps
  product = uniform * len(cells)
  detail = ", ".join(f"{c}={f:,}" for c, f in m.factors.items())
  if m.sampled_cols:
    detail += (f"; cells={len(cells)} joint values of "
               f"({', '.join(m.sampled_cols)}) in the sample, effective "
               f"{n_eff:.1f} after skew")
  if product < num_rows:
    raise SystemExit(
        f"[preflight P4] {table_schema.fqn}: the declared PK tuple "
        f"{list(effective_pk)} has a bounded generator capacity of "
        f"{product:,} ({detail}) < num_rows={num_rows:,} — every excess "
        f"row would be a pk.duplicate BLOCKER. Give a PK member a "
        f"samplable 'pattern' with enough capacity, or remove the "
        f"constraint from one member so its typed route stays "
        f"unbounded.")
  if not m.random_draw:
    return caps
  share = expected_duplicate_share_cells(num_rows, uniform, cells)
  if share > blocker_failure_ratio:
    raise _random_draw_stop(
        table_schema.fqn,
        effective_pk,
        num_rows,
        product,
        detail,
        share,
        blocker_failure_ratio,
        max_rows_under_share(uniform, blocker_failure_ratio, cells),
        at_ceiling=any(c >= FK_KEY_SAMPLE_CEILING for c in caps.values()),
    )
  if share > _PK_DUPLICATE_WARN_SHARE:
    log_milestone(
        "pk_capacity_tight",
        level=logging.WARNING,
        table=table_schema.fqn,
        pk=",".join(effective_pk),
        capacity=product,
        num_rows=num_rows,
        expected_pk_duplicate_share=round(share, 4),
        detail=detail,
    )
  return caps


def _constraint_vehicle(prof, field_) -> str:
  """What this clause ACTUALLY drives (2026-08-22 operator ask): a
    clause is a generation vehicle only on the free-text path — forced
    by route:'llm' or reached by natural free-text classification.
    Everywhere else it is prompt steering at most, and the log says so
    instead of leaving the operator to infer it."""
  stringy = (
      field_.bq_type in _POOL_ROUTED_BQ_TYPES and not field_.is_struct and
      not field_.is_repeated)
  if not stringy:
    return (f"{prof.kind.value} typed route — a clause on a non-STRING "
            f"column is NEVER a generation vehicle "
            f"(prompt_constraint_route_unsupported); it will not build a "
            f"freetext pool, RAG chunks or retrieval")
  if prof.kind.value == "free_text":
    if prof.identifier_shape is not None:
      return "shaped_identifier template (no LLM)"
    if is_binary_class(prof.observed_values):
      return ("byte_template (Tier B, ADR 0028 — no LLM, never "
              "source values)")
    if prof.constraint_pattern and compile_pattern_sampler(
        prof.constraint_pattern,
        families=getattr(prof, "constraint_families", ()),
    ):
      return ("pattern_sampler (Tier P, ADR 0028 — no LLM, "
              "unlimited uniques)")
    return ("freetext_llm_pool (LLM pool + RAG retrieval; the clause "
            "pins the pool prompt, ADR 0024/0026)")
  return (f"{prof.kind.value} typed route — the clause steers prompts "
          f"ONLY on the free-text path; add route:'llm' to force this "
          f"STRING column onto freetext_llm_pool/RAG")


def _report_constraint_vehicles(table_schema: TableSchema,
                                reference_rows: list[dict]) -> None:
  """One pretty block per table: every constrained column, keyed
    TABLE.COL, with its clause, its declared ``route`` and the RESOLVED
    generation vehicle (from the real profiler over the reference
    sample) — visible at preflight, before any worker exists."""
  if not reference_rows:
    return
  profiles = profile_columns(table_schema, reference_rows)
  name = table_schema.fqn.rsplit(".", 1)[-1]
  payload: dict[str, dict] = {}
  for col in table_schema.columns:
    pc = parse_prompt_constraint(col.description, column=col.name)
    if pc is None:
      continue
    prof = profiles.get(col.name)
    if prof is None:
      continue
    clause = render_prompt_clause(pc)
    payload[f"{name}.{col.name}"] = {
        "route": pc.route,
        "vehicle": _constraint_vehicle(prof, col),
        "clause": clause,
        "clause_sha12": sha12(clause),
        "pattern": bool(pc.pattern),
        "values": len(pc.values),
        "examples": len(pc.examples),
        "length": list(pc.length) if pc.length else None,
    }
  if payload:
    log_milestone_pretty(
        "prompt_constraints_pretty",
        payload,
        table=name,
        count=len(payload),
    )


# Above this share of duplicate PK tuples in the reference sample, the
# declared PK is not a key of the data at all — it is a typo or a missing
# column, not a data-quality dent. Generation samples those same
# marginals, so almost every row will collide and divert as
# `pk.duplicate` (2026-08-25: 99.4% duplicates in the sample ->
# 999 926 of 1 000 000 rows DLQ'd, BLOCKER gate tripped).
_PK_NOT_A_KEY_RATIO = 0.5


def _check_pk_is_a_key(
    fqn: str,
    pk: tuple[str, ...],
    distinct: int,
    sample_rows: int,
    num_rows: int,
    *,
    measured: bool = False,
    driven: bool = False,
) -> None:
  """Stop when the sample proves the run cannot fill ``num_rows``.

    A key that repeats on most of its own source rows cannot key a
    larger synthetic table: the run lands about as many rows as the
    tuple has distinct values and diverts the rest. Cheap to see here,
    expensive to discover at the gate.

    ``measured`` (ADR 0038, fixes H2 and J) — this table has a
    FULL-SOURCE measurement in scope (a fan-out payload, which since fix
    J always carries the DECLARED PK's own source measurement), so the
    measurement decides and this stop must not fire. Every premise of
    the message below is false for such a table: it does not draw its PK
    from marginals (it draws its
    parent's keys times the measured fan-out), it never uses
    ``--num_rows`` (`resolve_table_rows` returns the derived count), and
    once P4 adjusts, its `pk.duplicate` is excluded from the gate. A
    10,000-row sample pre-empting that verdict is how the 2026-09-12
    launch produced zero rows for five tables — at P5, before
    ``--on_model_conflict`` was ever read. The deferral is announced, so
    the operator sees the hand-off rather than a silence.

    ``driven`` without ``measured`` is the one driven case P5 can still
    be reached in — a declared driving edge whose parent is NOT in this
    launch — and there the premises hold again (the table generates from
    marginals at ``--num_rows`` like a root), so the stop stands with a
    message that says exactly that.
    """
  duplicate_ratio = 1 - distinct / sample_rows
  if (num_rows <= 0 or duplicate_ratio < _PK_NOT_A_KEY_RATIO or
      num_rows <= distinct):
    return
  if measured:
    log_milestone(
        "preflight_pk_sample_stop_deferred",
        level=logging.WARNING,
        table=fqn,
        pk=",".join(pk),
        distinct=distinct,
        sample_rows=sample_rows,
        duplicate_ratio=round(duplicate_ratio, 4),
        note="the FULL-source measurement of the DECLARED PK "
        "decides this table's key, not the sample (ADR 0038 fix J): "
        "P4 compares that measured repeat share with the run's "
        "BLOCKER gate and adjusts the model — or, with "
        "--on_model_conflict=stop, refuses the launch — when the "
        "source repeats the key more often than the gate allows",
    )
    return
  head = (f"[preflight P5] {fqn}: the declared PK {list(pk)} is not a key of "
          f"this data — only {distinct:,} distinct tuples in {sample_rows:,} "
          f"sample rows ({duplicate_ratio:.1%} duplicates).")
  tail = (f"a {num_rows:,}-row run would land on the order of {distinct:,} "
          f"rows and divert the rest as pk.duplicate, tripping the BLOCKER "
          f"gate. Fix one of: ")
  if driven:
    raise SystemExit(
        f"{head} This table declares a DRIVING FK edge, but no "
        f"full-source fan-out was measured this launch (its driving "
        f"parent is not generated here, or "
        f"--generate_fk_relationships is off), so it generates from "
        f"marginals at --num_rows like a root table and {tail}"
        f"generate its driving parent in the SAME launch (the "
        f"measured fan-out then decides, and a source that repeats "
        f"the key ADJUSTS the model instead of stopping — ADR 0038), "
        f"the `pk:` in the relationship model (add the column that "
        f"discriminates rows), the row count (--num_rows <= the real "
        f"key space), or move the column to `identity:` if it was "
        f"never meant to be a key.")
  raise SystemExit(
      f"{head} Generation draws the same marginals, so {tail}"
      f"the `pk:` in the relationship model (add the column that "
      f"discriminates rows), the row count (--num_rows <= the real key "
      f"space), or move the column to `identity:` if it was never meant "
      f"to be a key.")


# ADR 0037 §4 — a conditional edge hands a driving key at most this many
# parent candidate tuples (`--fk_candidate_cap`). Defined HERE, not in
# `run_pipeline`, so P4 and the launcher agree on one number without
# preflight importing the launcher (the import runs the other way).
DEFAULT_FK_CANDIDATE_CAP = 64

# Immutable empty defaults (a `{}` default would be shared mutable state).
_NO_CAPS: Mapping[tuple[str, ...], int] = MappingProxyType({})
_NO_REST: Mapping[str, tuple[str, ...]] = MappingProxyType({})
# Parent landing name -> DISTINCT key values it lands (fix H3). Empty =
# no parent was adjusted, so rows and distinct keys coincide everywhere.
_NO_PARENT_KEYS: Mapping[str, int] = MappingProxyType({})


def pk_cell_columns(
    effective_pk: tuple[str, ...],
    driving_cols: tuple[str, ...],
    profiles: Mapping[str, ColumnProfile],
    known: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], bool]:
  """``(cell columns, exact)`` for a driven child (ADR 0036): the PK
    members outside the driving edge that the engine re-emits from a
    domain (CATEGORICAL / CONSTANT); ``exact`` when they are ALL the
    remaining members, so the cells alone must key the child.

    ``known`` (ADR 0037) are the PK members ANOTHER edge supplies: an
    independent edge's columns (a whole tuple drawn from its sampled key
    pool) and a conditional edge's ``rest`` (one candidate tuple per
    shared key). They are neither cells — nothing measures a cell table
    over an FK column the parent fills — nor free members that make the
    check inexact; the capacity they contribute per parent key is its
    own P4 factor (`_check_driven_pk`)."""
  supplied = set(driving_cols) | set(known)
  rest = tuple(c for c in effective_pk if c not in supplied)
  cells = tuple(
      c for c in rest if (p := profiles.get(c)) is not None and p.kind in (
          ColumnKind.CATEGORICAL, ColumnKind.CONSTANT))
  # NO PK at all (never declared, or DROPPED by an ADR 0038 adjustment)
  # keys nothing, so the cells cannot be the thing that keys the child:
  # `exact` must be False. Read the other way — `len(()) == len(())` —
  # `joint_key_draw` capped every such child at `min(k, capacity)` with
  # a capacity of 1, i.e. ONE row per parent key, silently discarding
  # the measured fan-out the table was sized from.
  return cells, bool(effective_pk) and len(cells) == len(rest)


@dataclass(frozen=True)
class EdgeSupply:
  """What a driven child's NON-driving edges hand the engine
    (ADR 0037 §6): the columns they fill, and which of them bound the
    PK. Built only by :func:`edge_supplied_members`."""

  # Every column a non-driving edge writes, PK member or not.
  known: tuple[str, ...]
  # Independent edges whose columns touch the PK — each contributes its
  # sampled key pool as a per-key factor.
  independent: tuple[FkEdge, ...]
  # ``edge_id`` of every conditional edge whose ``rest`` touches the PK
  # — each contributes ``--fk_candidate_cap``.
  conditional: tuple[str, ...]


def edge_supplied_members(
    effective_pk: tuple[str, ...],
    edge_roles: Mapping[FkEdge, str] | None,
    conditional_rest: Mapping[str, tuple[str, ...]] | None,
) -> EdgeSupply:
  """The ONE rule for what a non-driving edge supplies and when it
    counts (ADR 0037 §6; ruling 12 of the 2026-09-11 review). Both
    readers call THIS — `resolve_fanout` at measure time and
    `_check_driven_pk` at check time — because two spellings of the rule
    disagree on some model and the disagreement lands as a launch stop.

    SUPPLIED (``known``) — an independent edge's whole column tuple (the
    engine overwrites them from the sampled key pool) and a conditional
    edge's ``rest`` (overwritten from the candidate draw). Supplied is
    supplied whether or not the PK holds those columns: the engine does
    not consult the PK before writing them, so a cell table measured
    over one of them asks BigQuery for a domain nothing ever draws from.
    This is `pk_cell_columns`'s ``known``.

    COUNTS (``independent`` / ``conditional``) — an edge whose supplied
    columns INTERSECT the PK contributes its factor. Partial containment
    counts: a pool of ``cap`` whole tuples yields at most ``cap``
    distinct values on any SUBSET of its columns, so the factor stays an
    upper bound on what the edge adds to the key."""
  pk = set(effective_pk)
  known: list[str] = []
  independent: list[FkEdge] = []
  for edge, role in (edge_roles or {}).items():
    if role != "independent":
      continue
    known.extend(edge.cols)
    if pk & set(edge.cols):
      independent.append(edge)
  # Conditional edges are read off the MAPPING, never off `edge_roles`:
  # its key is `edge_id` (`conditional_edge_id`, `(cols)->parent`) — the
  # same string the plan entries and the request payload's `matches`
  # use. The PARENT is part of it, so two conditional edges with the
  # same child columns to different parents (declarable: the parse-time
  # duplicate check only rejects an identical (cols, ref, ref_cols))
  # keep separate entries and each contributes its own factor. Under
  # the old columns-only id they collapsed into one here, and collided
  # in `matches` besides (ruling 14).
  conditional: list[str] = []
  for edge_id, rest in (conditional_rest or {}).items():
    known.extend(rest)
    if pk & set(rest):
      conditional.append(edge_id)
  return EdgeSupply(tuple(known), tuple(independent), tuple(conditional))


def _sufficient_candidate_cap(max_k: int, other_factors: int,
                              n_conditional: int) -> int:
  """Smallest ``--fk_candidate_cap`` whose capacity reaches ``max_k``.

    ``other_factors`` is the capacity WITHOUT the conditional term, so
    the conditional edges must jointly supply ``ceil(max_k /
    other_factors)`` combinations and each one contributes the same cap:
    the ``n_conditional``-th root, rounded up. Computed on integers —
    a float root is off by one exactly where the operator would retry."""
  needed = -(-max_k // max(1, other_factors))
  cap = max(1, int(needed**(1.0 / max(1, n_conditional))))
  while cap**n_conditional < needed:
    cap += 1
  while cap > 1 and (cap - 1)**n_conditional >= needed:
    cap -= 1
  return cap


def _driven_pk_stop(
    table_schema: TableSchema,
    effective_pk: tuple[str, ...],
    driving: tuple[str, ...],
    max_k: int,
    capacity: int,
    cells: tuple[str, ...],
    n_cells: int,
    independent: tuple[tuple[str, ...], ...],
    n_conditional: int,
    candidate_cap: int,
) -> SystemExit:
  """The P4 stop for a driven child whose per-key capacity is short,
    naming every factor that bounds it and the knob that moves it.

    Every listed factor is a genuine lever (capacity is their product),
    and the cap advice is the value that actually WORKS — "above 64" is
    not a fix when 65 fails too. An INDEPENDENT edge is named too, but as
    what it is: a per-ROW draw with replacement, which bounds nothing per
    key (fix wave A2) — so "a parent that lands more keys" is no longer
    offered as a remedy, because it never was one."""
  parts: list[str] = []
  if cells:
    parts.append(f"the PK-completing members {list(cells)} cover only "
                 f"{n_cells} cells")
  for cols in independent:
    parts.append(f"the independent edge ({','.join(cols)}) is drawn per ROW "
                 f"with replacement, so it bounds nothing per key")
  if n_conditional:
    parts.append(f"{n_conditional} conditional edge(s) contribute at most "
                 f"--fk_candidate_cap={candidate_cap:,} candidates each (an "
                 f"upper bound: a key whose parent offers fewer candidates "
                 f"emits fewer children)")
  fixes: list[str] = []
  if n_conditional:
    sufficient = _sufficient_candidate_cap(
        max_k, capacity // candidate_cap**n_conditional, n_conditional)
    fixes.append(f"raise --fk_candidate_cap to at least {sufficient:,} "
                 f"(it is {candidate_cap:,})")
  fixes.append("the `pk:` in the relationship model")
  fix = (f"Fix one of: {'; '.join(fixes)}."
         if len(fixes) > 1 else "Fix the `pk:` in the relationship model.")
  return SystemExit(
      f"[preflight P4] {table_schema.fqn}: one value of the driving "
      f"edge ({','.join(driving)}) appears up to {max_k} times in the "
      f"source child, but the PK completes to only {capacity:,} rows "
      f"per key value ({'; '.join(parts)}) — the declared PK "
      f"{list(effective_pk)} is not a key of the source. {fix}")


# ADR 0038 — what a MEASURED model conflict does. `stop` is the
# pre-0038 behaviour, message for message (`--on_model_conflict`).
ON_CONFLICT_ADJUST = "adjust"
ON_CONFLICT_STOP = "stop"
ON_MODEL_CONFLICT_MODES = (ON_CONFLICT_ADJUST, ON_CONFLICT_STOP)

_MEDIAN_QUANTILE = 0.5


def _histogram_p50(hist: Mapping[int, int]) -> int:
  """Median children-per-key over a fan-out histogram (the same
    definition `log_fanout_measured` prints as ``p50=``)."""
  total = sum(hist.values())
  if not total:
    return 0
  cumulative = 0
  for k in sorted(hist):
    cumulative += hist[k]
    if cumulative / total >= _MEDIAN_QUANTILE:
      return k
  return max(hist)


def _pk_not_a_key_adjustment(
    table_schema: TableSchema,
    effective_pk: tuple[str, ...],
    driving: tuple[str, ...],
    fanout: Mapping,
    detail: str,
    *,
    share: float | None = None,
    measurement: Mapping | None = None,
) -> ModelAdjustment:
  """The ADR 0038 record for "the source proves this `pk:` is not a
    key": drop it, say so, and carry the SOURCE repeat share the landing
    table now has to reproduce.

    Nothing else about the table changes — the fan-out histogram is
    untouched, so the child still lands its measured children per parent
    key and copies the source's key-repeat distribution by construction.
    Capping, narrowing or synthesizing a key would all break exactly that.

    ``share`` / ``measurement`` (fix J) are the DECLARED PK's own
    measurement on the source — the same columns `pk.duplicate` is
    counted over, which is what makes the end-of-run faithfulness verdict
    like-for-like in EVERY case (it replaces fix H4's driving-edge share,
    which was comparable only when the PK happened to equal the edge).
    A record built without one carries no share at all rather than a
    number measured over different columns.
    """
  hist = {int(k): int(n) for k, n in (fanout.get("histogram") or {}).items()}
  max_k = max(hist) if hist else 0
  evidence = (
      f"the full source repeats the declared PK — "
      f"{int(measurement.get('key_tuples') or 0):,} distinct tuples in "
      f"{int(measurement.get('rows') or 0):,} rows, {share:.2%} of them "
      f"repeating one (the largest carries "
      f"{int(measurement.get('max_rows_per_key') or 0)} rows)"
      if measurement is not None and share is not None else
      (f"the full source repeats it — one value of the driving edge "
       f"({','.join(driving)}) carries up to {max_k} rows "
       f"(p50={_histogram_p50(hist)})"))
  return ModelAdjustment(
      table=table_schema.fqn,
      change="pk_dropped",
      declared=f"pk {list(effective_pk)}",
      measured=f"{evidence}; {detail}",
      consequence=(
          f"`pk:` DROPPED from the effective model for "
          f"{table_schema.fqn}; the table still generates from its "
          f"driving edge with the measured fan-out untouched, so it "
          f"reproduces the source's key repeats. pk.duplicate is still "
          f"MEASURED on it (uniqueness_mode=streaming) but no longer "
          f"counts toward the BLOCKER gate"),
      declared_pk=tuple(effective_pk),
      source_repeat_share=share,
  )


def source_pk_measurement(
    effective_pk: tuple[str, ...],
    driving: tuple[str, ...],
    fanout: Mapping,
) -> Mapping | None:
  """The DECLARED PK's measurement on the SOURCE child, or None
    (ADR 0038 fix J).

    Two ways to the SAME three numbers, never two rules:

    * ``fanout["pk_source"]`` — the extra GROUP BY `measure_pk_uniqueness`
      paid for, taken over exactly these columns (a stale payload whose
      ``cols`` no longer match the declared PK is ignored, not trusted);
    * the fan-out histogram, when the declared PK IS the driving edge —
      it already groups the source child by those very columns, so the
      key tuple is measured and no second scan exists to pay for.

    None only where no measurement is in scope at all, which is where the
    pre-fix-J capacity ladder remains the only evidence.
    """
  stats = fanout.get("pk_source")
  if stats and tuple(stats.get("cols") or ()) == tuple(effective_pk):
    return stats
  if effective_pk and set(effective_pk) == set(driving):
    return pk_measurement_from_histogram(
        fanout.get("histogram") or {}, effective_pk)
  return None


def _measured_pk_stop(
    table_schema: TableSchema,
    effective_pk: tuple[str, ...],
    measurement: Mapping,
    share: float,
    gate: float,
) -> SystemExit:
  """``--on_model_conflict=stop`` on a MEASURED declared-PK conflict:
    the refusal states the measurement that produced the verdict, so the
    operator can check it against the source themselves."""
  return SystemExit(
      f"[preflight P4] {table_schema.fqn}: the declared PK "
      f"{list(effective_pk)} is not a key of the source — "
      f"{int(measurement.get('key_tuples') or 0):,} distinct tuples in "
      f"{int(measurement.get('rows') or 0):,} rows, so {share:.1%} of "
      f"them repeat a key, above this run's BLOCKER gate of {gate:.1%}. "
      f"Generation reproduces the source, so that share would land as "
      f"pk.duplicate and trip the gate. Fix one of: the `pk:` in the "
      f"relationship model (add the column that discriminates rows, or "
      f"drop it if the source has no key), or re-launch with "
      f"--on_model_conflict=adjust (the default), which drops the key "
      f"and excludes this table's pk.duplicate from the gate.")


def _one_to_one_stop(
    table_schema: TableSchema,
    effective_pk: tuple[str, ...],
    driving: tuple[str, ...],
    max_k: int,
) -> SystemExit:
  """``--on_model_conflict=stop`` on a 1:1 child whose source fans out:
    the pre-0038 message, word for word."""
  return SystemExit(f"[preflight P4] {table_schema.fqn}: one value of the "
                    f"driving edge ({','.join(driving)}) appears up to {max_k} "
                    f"times in the source child, but the declared PK "
                    f"{list(effective_pk)} equals the driving edge exactly — "
                    f"no completing members, so only ONE row per key value is "
                    f"representable and the rest would be pk.duplicate. Add a "
                    f"discriminating column to the `pk:` in the relationship "
                    f"model (the sibling table's own PK usually names one), "
                    f"drop the `pk:` if the source has no key, or confirm the "
                    f"source relationship really is 1:1 and the measurement is "
                    f"stale.")


def _check_driven_pk(
    table_schema: TableSchema,
    effective_pk: tuple[str, ...],
    fanout: Mapping,
    profiles: Mapping[str, ColumnProfile],
    *,
    # Passed by `_driven_child_rows`; deliberately NOT a capacity factor (see body).
    independent_caps: Mapping[tuple[str, ...], int] = _NO_CAPS,  # pylint: disable=unused-argument
    conditional_rest: Mapping[str, tuple[str, ...]] = _NO_REST,
    candidate_cap: int | None = DEFAULT_FK_CANDIDATE_CAP,
    edge_roles: Mapping[FkEdge, str] | None = None,
    on_conflict: str = ON_CONFLICT_ADJUST,
    gate: float = 1.0,
) -> ModelAdjustment | None:
  """P4 for a DRIVEN child: the largest source fan-out must fit in the
    per-key capacity the PK's completing members offer, else the declared
    PK is not a key in the source.

    That capacity is a PRODUCT (ADR 0037 §6, corrected by the final
    review): the measured cell table x ``--fk_candidate_cap`` per
    CONDITIONAL edge — each one a member the engine fills per child
    without repeating itself (`joint_key_draw` walks their cross
    product). A PK with none of them has capacity 1 and is the ADR 0036
    1:1 case, which keeps its own message.

    An INDEPENDENT edge is NOT a factor. Its pool is drawn per ROW WITH
    REPLACEMENT (`_draw_fk_columns`), so two children of one parent can
    and do draw the same parent key: counting it declared PKs safe that
    the engine then duplicated. Its columns stay in ``known`` — the
    engine still writes them, so no cell table is measured over them.

    The conditional factor is an UPPER bound, not a promise: a key whose
    co-parent offers fewer than ``--fk_candidate_cap`` candidates emits
    fewer children, and `joint_key_draw` counts the shortfall
    (`fanout_rows_capped`).

    What each edge supplies, and whether it counts, is
    `edge_supplied_members` — the same call `resolve_fanout` makes when
    it decides which columns to measure a cell table over.

    The ENGINE spells the same rule: only a conditional edge whose
    ``rest`` supplies a PK member multiplies a key's capacity there
    (``ConditionalEdge.pk_member``, set by the launcher from this same
    effective PK). Before fix wave G1 it multiplied EVERY conditional
    edge, so a model this check passed — counting the PK-touching edges
    only — still emitted children the PK could not tell apart.

    ADR 0038: a "the declared PK is not a key of the source" verdict is
    PROVEN by a full-source measurement, so under the default
    ``on_conflict="adjust"`` it returns a :class:`ModelAdjustment`
    instead of raising — the caller drops the `pk:` and shouts. With
    ``"stop"`` the SystemExit comes back. A missing cell table is NOT a
    proven conflict — it is a missing MEASUREMENT — and keeps stopping
    either way.

    ADR 0038 fix J — ONE decision path. The DECLARED PK's own measurement
    on the source (`source_pk_measurement`) is the authority for "is this
    a key of this data", and it is compared with the run's BLOCKER GATE,
    not with any repetition: a share ABOVE the gate cannot survive
    generation (it lands as `pk.duplicate` and fails the run), a share at
    or below it is a dirty source whose few duplicates today's machinery
    diverts — a 0.4% source must not lose its key. The driving-edge rule
    is SUBSUMED, not duplicated: a PK that equals its driving edge is the
    case where the histogram already measures that key. The capacity
    ladder below stays as the evidence of LAST resort, for a table with
    no measurement at all; where a measurement exists it only warns,
    because the measurement has already answered the question the
    capacity model estimates."""
  driving = tuple(fanout.get("driving_cols") or ())
  cap = (
      DEFAULT_FK_CANDIDATE_CAP if candidate_cap is None else int(candidate_cap))
  supply = edge_supplied_members(effective_pk, edge_roles, conditional_rest)
  # Named in the stop (so the operator sees why the edge does NOT help),
  # never multiplied into the capacity. `independent_caps` still sizes
  # the composer's side input — that is `_independent_pool_caps`' job.
  independent = tuple(tuple(e.cols) for e in supply.independent)
  conditional = supply.conditional
  known = supply.known
  cells, exact = pk_cell_columns(effective_pk, driving, profiles, known=known)
  max_k = max(int(k) for k in (fanout.get("histogram") or {"0": 0}))
  n_cells = len((fanout.get("cells") or {}).get("rows") or ()) if cells else 0
  capacity = (n_cells or 1) * cap**len(conditional)
  # Fix J — the DECLARED PK's own measurement, and the ONE comparison
  # that decides: the run's BLOCKER gate.
  measurement = source_pk_measurement(effective_pk, driving, fanout)
  share = pk_repeat_share(measurement)
  # The gate divides the diverted rows by the rows that REACH it —
  # generated PLUS diverted — so a source that repeats a share `s` of
  # its rows lands `s / (1 + s)`, not `s`. Compare what the gate will
  # actually compute: at a 0.2 gate the true boundary is a source
  # share of 0.25, and every source in (0.20, 0.25] keeps its key on a
  # run that would have PASSED with it enforced (2026-09-13).
  predicted = share / (1.0 + share) if share is not None else None
  if measurement is not None and predicted is not None and predicted > gate:
    completing = tuple(c for c in effective_pk if c not in driving)
    detail = (f"one value of the driving edge ({','.join(driving)}) carries "
              f"up to {max_k} rows and the completing member(s) "
              f"{list(completing)} do not tell them apart" if completing else
              "the declared PK equals the driving edge exactly, so it "
              "has no completing member that could tell those rows apart")
    if on_conflict == ON_CONFLICT_STOP:
      raise (_measured_pk_stop(table_schema, effective_pk, measurement, share,
                               gate) if completing else _one_to_one_stop(
                                   table_schema, effective_pk, driving, max_k))
    return _pk_not_a_key_adjustment(
        table_schema,
        effective_pk,
        driving,
        fanout,
        detail,
        share=share,
        measurement=measurement,
    )
  if exact and cells and max_k > 0 and n_cells == 0:
    # Fail CLOSED: the PK needs these members to be a key, and the
    # measurement that would supply them is missing entirely — a
    # different fault from "measured, and too small" below, and not a
    # proven conflict, so it stops under both settings (ADR 0038 D5).
    raise SystemExit(
        f"[preflight P4] {table_schema.fqn}: the declared PK "
        f"{list(effective_pk)} is completed by {list(cells)} "
        f"outside the driving edge ({','.join(driving)}), but no "
        f"cell table was measured for {list(cells)} — the per-key "
        f"draw has nothing to draw from. Re-measure the source "
        f"fan-out (clear the `fk_fanout_stats` cache entry) or fix "
        f"the `pk:` in the relationship model.")
  if measurement is not None and share is not None:
    # The source says the declared PK IS a key of this data, within
    # the gate. It is KEPT, and nothing below may take it away.
    log_milestone(
        "preflight_pk_source_repeats",
        level=logging.WARNING if share > 0 else logging.INFO,
        table=table_schema.fqn,
        pk=",".join(effective_pk),
        rows=int(measurement.get("rows") or 0),
        key_tuples=int(measurement.get("key_tuples") or 0),
        max_rows_per_key=int(measurement.get("max_rows_per_key") or 0),
        repeat_share=round(share, 4),
        predicted_observed=round(share / (1.0 + share), 4),
        gate=gate,
        note="the declared PK is a key of this source within the "
        "run's BLOCKER gate, so it is KEPT; the repeats that remain "
        "divert as pk.duplicate like any other table's",
    )
    if exact and max_k > capacity:
      # The capacity model ESTIMATES what a key can represent; the
      # measurement MEASURED it. Say the two disagree — never act
      # on it, or a 0.4%-dirty source loses its key to an estimate.
      log_milestone(
          "preflight_pk_capacity_below_fanout",
          level=logging.WARNING,
          table=table_schema.fqn,
          pk=",".join(effective_pk),
          max_fanout=max_k,
          capacity=capacity,
          candidate_cap=cap,
          note="the per-key capacity model sits below the source's "
          "largest fan-out, but the DECLARED PK's own source "
          "measurement kept the key (ADR 0038 fix J): the expected "
          "duplicates are the measured share above, under the gate. "
          "Raise --fk_candidate_cap if pk.duplicate overshoots it",
      )
    return None
  # No measurement over the declared PK: the pre-fix-J capacity ladder
  # is the only evidence there is.
  if not exact or max_k <= capacity:
    return None
  if not cells and not known:
    # The declared PK IS the driving edge (a true 1:1 child): there
    # are no completing members, so `measure_fanout` never builds a
    # cell table — by design, not by omission. The single trivial
    # cell holds exactly one child per parent key; a source fan-out
    # above 1 is the PK not being a key of the source (2026-09-11,
    # E_TABLE stopped with "no cell table was measured for []").
    if on_conflict == ON_CONFLICT_STOP:
      raise _one_to_one_stop(table_schema, effective_pk, driving, max_k)
    return _pk_not_a_key_adjustment(
        table_schema,
        effective_pk,
        driving,
        fanout,
        "the declared PK equals the driving edge exactly, so it has "
        "no completing member that could tell those rows apart",
    )
  if on_conflict == ON_CONFLICT_STOP:
    raise _driven_pk_stop(
        table_schema,
        effective_pk,
        driving,
        max_k,
        capacity,
        cells,
        n_cells,
        tuple(independent),
        len(conditional),
        cap,
    )
  return _pk_not_a_key_adjustment(
      table_schema,
      effective_pk,
      driving,
      fanout,
      f"the PK's completing members represent only {capacity:,} rows "
      f"per key value",
  )


def _derived_rows(
    fqn: str,
    fanout: Mapping,
    edge_roles: Mapping[FkEdge, str] | None,
    fk_parent_rows: Mapping[str, int],
    fk_parent_distinct_keys: Mapping[str, int] = _NO_PARENT_KEYS,
) -> int | None:
  """A DRIVEN child's row count (ADR 0036): ``round(driving parent's
    DISTINCT landed keys x mean fan-out)``. ``None`` when the histogram
    carries no mass or the driving parent's row count is unknown.

    The multiplier is the parent's distinct KEY VALUES, not its rows (fix
    H3). The composer fans a child out from its parent's distinct keys —
    an adjusted parent's ``parent_pk=()`` arms `FanoutDistinct` — and the
    histogram's own denominator is the number of distinct SOURCE key
    values, so the two agree only on a parent that lands one row per key.
    For a parent whose PK the run ENFORCES that is its row count and
    nothing changes; for an ADJUSTED parent it is
    ``rows x (1 - source_repeat_share)`` (`landed_distinct_keys`), and
    asking for the larger number lands a permanent shortfall: 220,215
    requested against ~109,556 producible on the 2026-09-12 shape,
    written to `validation_runs` as a missed request with no milestone
    naming the cause. Hence the milestone.
    """
  hist = {int(k): int(n) for k, n in (fanout.get("histogram") or {}).items()}
  total = sum(hist.values())
  driving_ref = next(
      (e.ref for e, r in (edge_roles or {}).items() if r == "driving"),
      None,
  )
  parent_rows = fk_parent_rows.get(driving_ref) if driving_ref else None
  if not (total and parent_rows):
    return None
  mean_fanout = sum(k * n for k, n in hist.items()) / total
  keys = fk_parent_distinct_keys.get(driving_ref) or parent_rows
  derived = round(keys * mean_fanout)
  if keys < parent_rows:
    log_milestone(
        "model_adjustment_descendant_rows",
        level=logging.WARNING,
        table=fqn,
        parent=driving_ref,
        parent_rows=parent_rows,
        parent_distinct_keys=keys,
        mean_fanout=round(mean_fanout, 4),
        derived_rows=derived,
        unadjusted_rows=round(parent_rows * mean_fanout),
        note="the driving parent's `pk:` was ADJUSTED away (ADR "
        "0038), so it lands repeated keys and this table fans out "
        "from its DISTINCT ones — sizing it off the parent's rows "
        "would request rows the fan-out cannot produce",
    )
  return derived


def _independent_pool_caps(
    supply: EdgeSupply, rows: int,
    fk_parent_rows: Mapping[str, int]) -> dict[tuple[str, ...], int]:
  """Per INDEPENDENT edge touching the PK, the parent key sample the
    composer will broadcast (ADR 0037 §6; ruling 13). P4 counts it as a
    per-key factor and `in_set_parent_edges` sizes the side input from
    the SAME number, which reaches it through
    ``PreflightResult.fk_key_sample_caps``.

    ``rows`` is the child's DERIVED count when the fan-out gives one: a
    driven child lands ``parent_rows x mean_fanout``, NEVER the
    launch-wide ``--num_rows`` (ADR 0036 D5, `resolve_table_rows`). The
    launch count is the fallback, and there it is a LOWER bound, not an
    upper one — what keeps P4 honest in that case is the
    ``[FLOOR, CEILING]`` clamp inside `fk_key_sample_cap`, not the count
    (2026-09-11 review, finding B-2).

    ``other=1``: the PK's cells and its sibling edges are counted in
    `_check_driven_pk`, not folded in here. ``fk_parent_rows`` holds
    IN-SET parents only, so an edge to an out-of-set parent keeps the
    unclamped cap — unreachable today, since `plan_launch` expands to
    the whole component and every enabled parent is in-set."""
  caps: dict[tuple[str, ...], int] = {}
  for edge in supply.independent:
    cap = fk_key_sample_cap(rows, 1)
    parent_rows = fk_parent_rows.get(edge.ref)
    caps[tuple(edge.cols)] = min(cap, parent_rows) if parent_rows else cap
  return caps


def _driven_child_rows(
    table_schema: TableSchema,
    effective_pk: tuple[str, ...],
    num_rows: int,
    fanout: Mapping,
    profiles: Mapping[str, ColumnProfile],
    edge_roles: Mapping[FkEdge, str] | None,
    fk_parent_rows: Mapping[str, int] | None,
    *,
    conditional_rest: Mapping[str, tuple[str, ...]] = _NO_REST,
    candidate_cap: int | None = DEFAULT_FK_CANDIDATE_CAP,
    on_conflict: str = ON_CONFLICT_ADJUST,
    fk_parent_distinct_keys: Mapping[str, int] = _NO_PARENT_KEYS,
    gate: float = 1.0,
) -> tuple[int | None, dict[tuple[str, ...], int], ModelAdjustment | None]:
  """``(derived rows, independent pool caps, adjustment)`` for a DRIVEN
    child.

    ORDER MATTERS (ruling 13): the row count derives FIRST, because the
    independent key pools are sized from it, and P4 then reads those
    pools as per-key PK factors. `_check_driven_pk` may stop the launch;
    when it does not, both values travel out on ``PreflightResult``.

    ADR 0038: when it returns an ADJUSTMENT instead, the derived row
    count and the pool caps stay EXACTLY as computed — the caps from the
    DECLARED PK, which is the larger (upper-bound) sizing, so dropping
    the key never narrows what the composer broadcasts and no FK
    guarantee moves. Only the key itself goes."""
  parent_rows = fk_parent_rows or {}
  derived = _derived_rows(
      table_schema.fqn,
      fanout,
      edge_roles,
      parent_rows,
      fk_parent_distinct_keys,
  )
  supply = edge_supplied_members(effective_pk, edge_roles, conditional_rest)
  caps = _independent_pool_caps(supply, derived or num_rows, parent_rows)
  adjustment = None
  if num_rows > 0 and effective_pk:
    adjustment = _check_driven_pk(
        table_schema,
        effective_pk,
        fanout,
        profiles,
        independent_caps=caps,
        conditional_rest=conditional_rest,
        candidate_cap=candidate_cap,
        edge_roles=edge_roles,
        on_conflict=on_conflict,
        gate=gate,
    )
  return derived, caps, adjustment


def _drawn_edges(relations: TableRelations,
                 enforced_fk: tuple[FkEdge, ...] | None) -> tuple[FkEdge, ...]:
  """The edges P2/P3/P4 reason about: the ones the launch DRAWS.

    Documented edges (`enforced: false`) describe a relationship whose
    join key need not be in the DDL — they never draw keys, so their
    columns are exempt by definition. So is an edge whose parent is
    DISABLED (`registry.enforced_edges` drops it): the member keeps its
    own route, exactly as the card's "not drawn" says. Callers without
    a registry fall back to the declared `enforced: true` edges."""
  if enforced_fk is not None:
    return enforced_fk
  return tuple(fk for fk in relations.fk if fk.enforced)


def _run_p4(
    table_schema: TableSchema,
    effective_pk: tuple[str, ...],
    num_rows: int,
    *,
    profiles: Mapping[str, ColumnProfile],
    reference_rows: list[dict],
    enforced_fk: tuple[FkEdge, ...],
    fk_parent_rows: Mapping[str, int] | None,
    blocker_failure_ratio: float,
    fanout: Mapping | None,
    edge_roles: Mapping[FkEdge, str] | None,
    conditional_rest: Mapping[str, tuple[str, ...]] | None,
    candidate_cap: int | None,
    on_model_conflict: str,
    fk_parent_distinct_keys: Mapping[str, int] | None = None,
) -> tuple[
    tuple[str, ...],
    int | None,
    dict[tuple[str, ...], int],
    tuple[ModelAdjustment, ...],
]:
  """P4 in one place: ``(effective pk, derived rows, key-sample caps,
    adjustments)``.

    Two regimes, never both. A DRIVEN child (``fanout`` given, ADR 0036)
    is checked per key against the source fan-out; everyone else against
    the random-draw capacity model (ADR 0028/0035). Only the first can
    ADJUST (ADR 0038) — the second's verdict is a GENERATOR limit with a
    row-count remedy, not the source contradicting the model, so it keeps
    stopping under both `--on_model_conflict` settings.
    """
  if fanout is None:
    caps: dict[tuple[str, ...], int] = {}
    if num_rows > 0 and effective_pk:
      caps = _check_pk_capacity(
          table_schema,
          effective_pk,
          num_rows,
          profiles=profiles,
          reference_rows=reference_rows,
          fk_edges=enforced_fk,
          fk_parent_rows=fk_parent_rows,
          blocker_failure_ratio=blocker_failure_ratio,
      )
    return effective_pk, None, caps, ()
  # ADR 0037: an independent edge touching the PK is BOTH a P4 factor
  # and a side input the composer must size — one cap, sized from the
  # derived row count and leaving on the result so `in_set_parent_edges`
  # broadcasts that same number.
  derived_rows, caps, adjustment = _driven_child_rows(
      table_schema,
      effective_pk,
      num_rows,
      fanout,
      profiles,
      edge_roles,
      fk_parent_rows,
      conditional_rest=conditional_rest or _NO_REST,
      candidate_cap=candidate_cap,
      on_conflict=on_model_conflict,
      fk_parent_distinct_keys=fk_parent_distinct_keys or _NO_PARENT_KEYS,
      gate=blocker_failure_ratio,
  )
  if adjustment is None:
    return effective_pk, derived_rows, caps, ()
  # ADR 0038 — the source won. The declared members survive on the
  # adjustment record (`declared_pk`) so `pk.duplicate` is still
  # MEASURED against them; the EFFECTIVE key is empty, so nothing keys,
  # caps or dedupes on it any more. That empties the P4 cell exactness
  # too (`pk_cell_columns`), which is what keeps `joint_key_draw` from
  # capping the fan-out to one row per parent key.
  return (), derived_rows, caps, (adjustment,)


def preflight(
    table_schema: TableSchema,
    pk_cols: tuple[str, ...],
    identity_cols: tuple[str, ...],
    reference_rows: list[dict],
    relations: TableRelations | None = None,
    fk_parents_resolved: dict[str, bool] | None = None,
    prompt_constraints_enabled: bool = True,
    num_rows: int = 0,
    fk_parent_rows: Mapping[str, int] | None = None,
    blocker_failure_ratio: float = 1.0,
    fanout: Mapping | None = None,
    edge_roles: Mapping[FkEdge, str] | None = None,
    enforced_fk: tuple[FkEdge, ...] | None = None,
    edge_overlaps: Mapping[FkEdge, tuple[str, ...]] | None = None,
    conditional_rest: Mapping[str, tuple[str, ...]] | None = None,
    candidate_cap: int | None = DEFAULT_FK_CANDIDATE_CAP,
    on_model_conflict: str = ON_CONFLICT_ADJUST,
    fk_parent_distinct_keys: Mapping[str, int] | None = None,
) -> PreflightResult:
  """Run P1-P5 + P4; returns the effective pk/identity columns.

    ``num_rows`` > 0 arms the P4 PK-capacity check. ``fk_parent_rows``
    (FK ``ref`` → rows the parent lands, when known) and
    ``blocker_failure_ratio`` (the run's BLOCKER gate) arm its ADR 0035
    random-draw branch. FK activation is no longer a preflight concern
    (ADR 0029 rev B): fk_parent_landing derives from the landing table,
    and an unlanded/empty parent stops loudly at pool-load time
    instead.

    ``fanout`` (the Task 8 payload, ADR 0036) marks this table a DRIVEN
    child: P4 switches from the random-draw model to
    ``_check_driven_pk`` (the source fan-out must fit the PK-completing
    cells), and ``PreflightResult.derived_rows`` is set from the mean
    fan-out and the driving parent's row count. ``edge_roles`` (from
    ``RelationshipRegistry.edge_roles``) logs one ``fk_edge_role``
    milestone per enforced edge and locates the driving parent.

    ``enforced_fk`` is the set of edges the launch actually DRAWS —
    ``RelationshipRegistry.enforced_edges`` (both ends enabled, widened,
    ADR 0032/0036). Without it, preflight falls back to the declared
    ``enforced: true`` edges, which cannot see a DISABLED parent: the
    2026-09-10 launch counted C_TABLE's undrawn edge to B_TABLE at the
    1M key-sample ceiling and stopped a root table at P4.

    ``edge_overlaps`` (ADR 0037, the launcher's
    ``RelationshipRegistry.edge_overlap`` per CONDITIONAL edge) adds
    ``overlap=`` to that edge's ``fk_edge_role`` line — the columns it
    is co-partitioned on. It is reporting only: no capacity check reads
    it.

    ``conditional_rest`` / ``candidate_cap`` (ADR 0037 §6) are what P4
    DOES read on a driven child: each CONDITIONAL edge's ``rest``
    columns keyed by its ``edge_id`` (`conditional_edge_id` —
    ``(cols)->parent``), and the Top-M
    candidate cap (``None`` means the default). Together with every
    INDEPENDENT edge's sampled key pool — sized HERE, from the derived
    row count this same call produces (ruling 13), not by the caller —
    they are per-key PK factors next to the measured cells. The pool
    caps travel back out on ``PreflightResult.fk_key_sample_caps``, so
    the composer broadcasts exactly the number P4 counted, exactly as
    the ADR 0035 random-draw path returns its own.

    ``fk_parent_distinct_keys`` (FK ``ref`` → the DISTINCT key values
    that parent LANDS, fix H3) is what a driven child's row count is
    actually derived from, because that is what the composer fans out
    over. It equals the parent's rows whenever the parent's PK is
    enforced, and ``rows x (1 - source_repeat_share)`` when the parent's
    own `pk:` was adjusted away — omit it and an adjusted parent's child
    is asked for roughly twice the rows it can produce.

    ``on_model_conflict`` (ADR 0038, ``--on_model_conflict``) decides
    what a MEASURED contradiction does. ``adjust`` (default): a declared
    `pk:` the full-source fan-out proves is not a key is DROPPED from
    the effective model, recorded on ``PreflightResult.adjustments``, and
    the launch carries on. ``stop``: the pre-0038 SystemExit, message for
    message. Only a full-source measurement adjusts — the P2 column
    check, the P3 closure check, an ambiguous/contradictory edge set and
    the ADR 0035 capacity gate all keep stopping, because no data
    resolves a model that contradicts itself or a generator that cannot
    cover the requested rows."""
  warnings: list[str] = []
  fqn = table_schema.fqn
  _report_prompt_constraints(table_schema, prompt_constraints_enabled)
  _report_constraint_vehicles(table_schema, reference_rows)
  profiles = (
      profile_columns(table_schema, reference_rows)
      if num_rows > 0 and reference_rows else {})

  if relations is None:
    log_milestone("relationships_absent_for_table", table=fqn)
    if num_rows > 0 and pk_cols:
      _check_pk_capacity(
          table_schema,
          pk_cols,
          num_rows,
          profiles=profiles,
          reference_rows=reference_rows,
          blocker_failure_ratio=blocker_failure_ratio,
      )
    return PreflightResult(pk_cols, identity_cols, None, warnings)

  # P2 — every column the model names must exist in the schema. This is
  # where a typo in `config/relationships/*.yaml` stops the launch, at
  # the cost of one comparison, instead of generating the wrong shape.
  valid = {c.name for c in table_schema.columns}
  enforced_fk = _drawn_edges(relations, enforced_fk)
  fk_cols = tuple(c for fk in enforced_fk for c in fk.cols)
  for label, cols in (
      ("pk", relations.pk),
      ("identity", relations.identity),
      ("fk.cols", fk_cols),
  ):
    missing = _missing(cols, valid)
    if missing:
      raise SystemExit(
          f"[preflight P2] {fqn}: the relationship model's {label} "
          f"names unknown columns {missing}. Fix the model file (or "
          f"the table). Schema columns: {sorted(valid)}")

  # P3 — FK closure, when the caller resolved parents (multi-table runs).
  if fk_parents_resolved is not None and enforced_fk:
    unresolved = sorted(
        {fk.ref for fk in enforced_fk if not fk_parents_resolved.get(fk.ref)})
    if unresolved:
      raise SystemExit(
          f"[preflight P3] {fqn}: FK parents not resolved: {unresolved}. "
          f"Generate parents first (run_tableset orders this) or land "
          f"their synthetic tables before this run.")

  # The model is the source of truth; CLI flags fill the gaps it leaves.
  effective_pk = relations.pk or pk_cols
  effective_identity = relations.identity or identity_cols
  if pk_cols and relations.pk and tuple(pk_cols) != relations.pk:
    warnings.append(
        f"--pk_cols {list(pk_cols)} IGNORED — the relationship model "
        f"declares pk {list(relations.pk)} and is the source of truth")
    log_milestone(
        "relationships_pk_override_ignored",
        level=logging.WARNING,
        table=fqn,
        cli_pk=",".join(pk_cols),
        model_pk=",".join(relations.pk),
    )

  # P5 — is the declared PK actually a key of this data?
  #
  # ADR 0038 boundary: this reads the 10,000-row REFERENCE SAMPLE, and
  # a sample never adjusts the model. On launch 2026-09-12_14_50_30 it
  # reported 40 duplicate tuples of 10,000 rows for E_TABLE — 0.4%,
  # far under the `_PK_NOT_A_KEY_RATIO` bar — on a table whose FULL
  # source repeats the median key value twice. A signal that weak
  # neither proves a key nor disproves one, so it stays exactly what it
  # is: a warning (plus the existing stop when the sample alone already
  # proves the run cannot fill `num_rows`). Only the full-source
  # fan-out measurement below adjusts anything.
  #
  # Fix H2: and where that measurement EXISTS (`fanout`), it also owns
  # the verdict — the sample's stop defers to it, so P4 (adjust, or
  # refuse under `--on_model_conflict=stop`) is what the operator sees.
  # A table with no measurement keeps P5 exactly as it was.
  #
  # Fix J closes the gap that deference left: P4 used to reason only
  # about the DRIVING EDGE's histogram, so a declared PK with a member
  # outside that edge was deferred to a check that never looked at it.
  # The payload now carries the declared PK's OWN source measurement,
  # which is what P4 decides on — so the deference is total, and P5's
  # sample is only ever the last word where nothing was measured.
  if effective_pk and reference_rows:
    tuples = {tuple(r.get(c) for c in effective_pk) for r in reference_rows}
    if len(tuples) < len(reference_rows):
      dupes = len(reference_rows) - len(tuples)
      _check_pk_is_a_key(
          fqn,
          effective_pk,
          len(tuples),
          len(reference_rows),
          num_rows,
          measured=fanout is not None,
          driven=any(role == "driving" for role in (edge_roles or {}).values()),
      )
      warnings.append(
          f"PK {list(effective_pk)} not unique in the reference sample "
          f"({dupes} duplicate tuples of {len(reference_rows)} rows)")
      log_milestone(
          "preflight_pk_not_unique_in_sample",
          level=logging.WARNING,
          table=fqn,
          pk=",".join(effective_pk),
          duplicates=dupes,
          sample_rows=len(reference_rows),
      )

  # ADR 0036 — one milestone per enforced edge, naming its role
  # (driving / implied / external) so a launch log answers "which
  # parent is this table generated FROM" without opening the model.
  # ADR 0037 adds independent / conditional, and the shared columns a
  # conditional edge is joined on.
  for edge, role in (edge_roles or {}).items():
    overlap = (edge_overlaps or {}).get(edge) or ()
    log_milestone(
        "fk_edge_role",
        table=fqn,
        edge=f"({','.join(edge.cols)})->{edge.ref}",
        role=role,
        **({
            "overlap": ",".join(overlap)
        } if overlap else {}),
    )

  # P4 — PK generation capacity. A DRIVEN child (``fanout`` given, ADR
  # 0036) is checked per key against the source fan-out instead of the
  # random-draw model (ADR 0028/0035), and its row count derives from
  # the driving parent's rows and the mean fan-out.
  effective_pk, derived_rows, fk_key_sample_caps, adjustments = _run_p4(
      table_schema,
      tuple(effective_pk),
      num_rows,
      profiles=profiles,
      reference_rows=reference_rows,
      enforced_fk=enforced_fk,
      fk_parent_rows=fk_parent_rows,
      blocker_failure_ratio=blocker_failure_ratio,
      fanout=fanout,
      edge_roles=edge_roles,
      conditional_rest=conditional_rest,
      candidate_cap=candidate_cap,
      on_model_conflict=on_model_conflict,
      fk_parent_distinct_keys=fk_parent_distinct_keys,
  )

  log_milestone(
      "relations_loaded",
      table=fqn,
      pk=",".join(relations.pk),
      fk_count=len(relations.fk),
      enforced_fk=len(enforced_fk),
      identity=",".join(relations.identity),
      enabled=relations.enabled,
  )
  return PreflightResult(
      tuple(effective_pk),
      tuple(effective_identity),
      relations,
      warnings,
      fk_key_sample_caps=fk_key_sample_caps,
      derived_rows=derived_rows,
      adjustments=adjustments,
  )


__all__ = [
    "DEFAULT_FK_CANDIDATE_CAP",
    "ON_CONFLICT_ADJUST",
    "ON_CONFLICT_STOP",
    "ON_MODEL_CONFLICT_MODES",
    "EdgeSupply",
    "PreflightResult",
    "edge_supplied_members",
    "pk_cell_columns",
    "preflight",
    "source_pk_measurement",
]
