"""Shared plumbing for the once-per-run `generation_plan` milestone.

Both engines answer the same question — which fields are LLM free-text
pools, which are shaped identifiers routed off the LLM, which are plain
samplers — from their own `ColumnProfile` types. The two profile classes
are distinct dataclasses but agree on the duck-typed surface this module
needs: ``.kind`` (a StrEnum sharing the five values) and
``.identifier_shape``. One definition of the label mapping and the
once-guard, every engine (the 2026-07-28 R1 lesson: inline copies drift).
"""

from __future__ import annotations

import threading
from typing import Any

from sdfb_core.engines.text_shapes import shape_mix_is_identifier_like
from sdfb_core.observability import (
    log_milestone,
    log_milestone_text,
    sha12,
)

# The bounded-pool ceiling shared by the engines' free-text ladders and
# the launcher's PK-capacity preflight (ADR 0028 P4): a constrained
# column with no samplable pattern can never exceed this many distinct
# values, so a PK routed there caps at it — the 2026-08-21 run DLQ'd
# 999 488 of 1M rows exactly this way.
FREE_TEXT_POOL_MAX = 512

# (engine, reference_digest, table_fqn) triples already logged by this
# worker process. Keyed per engine so a b1 + b2 comparison run on the same
# reference sample logs BOTH plans.
_LOGGED: set[tuple[str, str, str]] = set()
_LOGGED_LOCK = threading.Lock()


def clear_generation_plan_log() -> None:
  """Forget which plans were logged (tests / maintenance only)."""
  with _LOGGED_LOCK:
    _LOGGED.clear()


def should_log_plan(engine: str, reference_digest: str, table_fqn: str) -> bool:
  """True exactly once per (engine, digest, table) per worker process."""
  key = (engine, reference_digest or "", table_fqn)
  with _LOGGED_LOCK:
    if key in _LOGGED:
      return False
    _LOGGED.add(key)
    return True


def build_plan_detail(profiles: dict[str, Any]) -> dict[str, dict]:
  """column → compact fidelity detail for the generation_plan milestone.

    The 2026-08-05 spec (WS-B): the plan line should answer "what will this
    column's sparsity and shape handling be" without a postmortem re-derive.
    Duck-typed like :func:`build_plan`; fields absent on an engine's profile
    default safely (b1/b2 parity guaranteed by the shared tests).
    """
  detail: dict[str, dict] = {}
  labels = {
      col: label for label, cols in build_plan(profiles).items() for col in cols
  }
  for name, prof in profiles.items():
    detail[name] = {
        "kind":
            labels[name],
        "null_fraction":
            round(float(prof.null_fraction), 4),
        "empty_fraction":
            round(float(getattr(prof, "empty_fraction", 0.0)), 4),
        "shapes":
            len(getattr(prof, "shape_mix", None) or ()),
        "constraint":
            bool(getattr(prof, "llm_prompt_constraint", "")),
        # Whether the default (`identifiers`) expansion draws this
        # column from its shape mix instead of the bounded pool — the
        # 2026-08-11 R1 postmortems reverse-engineered this per column.
        "expandable":
            bool(
                getattr(prof, "identifier_shape", None) is not None or
                shape_mix_is_identifier_like(getattr(prof, "shape_mix", None))),
    }
  return dict(sorted(detail.items()))


def build_constraints_detail(profiles: dict[str, Any]) -> dict[str, dict]:
  """column → the `llm_prompt_constraint` actually fetched from the DDL.

    The 2026-08-20 follow-up: the launcher preflight named constrained
    columns and the plan detail said `constraint: true`, but nothing in the
    worker logs showed WHAT was fetched — a Terraform description edit was
    unverifiable without a `--prompt_debug` run. The rendered clause is
    config (Terraform/git-owned; real values are banned from constraints by
    ADR 0024's privacy rule), so it logs in full; `clause_sha12` is the
    drift-comparison key shared with `freetext_pool_prompt`. Duck-typed
    like :func:`build_plan_detail` (b1/b2 parity).
    """
  detail: dict[str, dict] = {}
  for name, prof in profiles.items():
    clause = getattr(prof, "llm_prompt_constraint", "") or ""
    pattern = getattr(prof, "constraint_pattern", "") or ""
    examples = getattr(prof, "constraint_examples", ()) or ()
    sets_length = bool(getattr(prof, "constraint_sets_length", False))
    if not clause and not pattern and not examples:
      continue
    detail[name] = {
        "clause": clause,
        "clause_sha12": sha12(clause),
        "chars": len(clause),
        "pattern": bool(pattern),
        "sets_length": sets_length,
        "examples": len(examples),
    }
  return dict(sorted(detail.items()))


def build_plan(profiles: dict[str, Any]) -> dict[str, list[str]]:
  """column-profile map → {generation_type: sorted [columns]}.

    FREE_TEXT splits on ``identifier_shape``: a shaped identifier generates
    from its per-position template and never reaches the LLM (both
    engines); everything else is an LLM free-text pool.
    """
  plan: dict[str, list[str]] = {}
  for name, prof in profiles.items():
    kind = str(prof.kind.value)
    if kind == "free_text":
      label = ("freetext_llm_pool"
               if prof.identifier_shape is None else "shaped_identifier")
    else:
      label = kind
    plan.setdefault(label, []).append(name)
  return {k: sorted(v) for k, v in sorted(plan.items())}


def log_plan_pretty(
    engine: str,
    ctx: Any,
    profiles: dict[str, Any],
    pool_sources: dict[str, str] | None = None,
) -> None:
  """The once-per-plan relational entries (ADR 0028 follow-up; compact
    since ADR 0035 rev).

    Emitted by both engines right after their compact ``generation_plan``
    milestone, under the same once-guard: ONE single-line
    ``relational_e2e`` (landing table, PK, identity, edge and clause
    counts) and ONE single-line ``relational_fk_edge`` per edge (its
    parent landing table, key-tuple count, activation and — ADR 0037,
    design 2026-09-11 §8 — the DAG path it took: ``mode=fanout|implied|
    side_input|conditional``, plus ``overlap=`` for a conditional edge's
    shared columns). One glance answers "did the whole relational
    contract reach this run", which the 2026-08-21 job could not (its FK
    was silently inactive). The indent-2 ``generation_plan_pretty`` JSON
    is gone: eight engine instances per table echoed it and on the
    2026-09-09 three-table runs the pretty entries were 40% of the
    worker log by bytes. ``pool_sources`` rides on ``generation_plan``;
    it is accepted here for the engines' unchanged call shape."""
  del pool_sources  # on `generation_plan` already
  table = ctx.table_schema.fqn

  fk_pools: dict[str, tuple] = getattr(ctx, "fk_pools", {}) or {}
  edges: list[dict] = list(getattr(ctx, "fk_edges", []) or [])
  if not edges and fk_pools:
    edges = [{"cols": [c]} for c in sorted(fk_pools)]
  # Joint key pools (ADR 0031) are the sampling truth; the per-column
  # projection is only a fallback for a legacy single-column pool. A
  # composite edge MUST report its key-tuple count — the first
  # column's distinct count can be 1 for a pool of a million tuples.
  key_pools: list[dict] = list(getattr(ctx, "fk_key_pools", []) or [])
  tuples_by_cols = {
      tuple(p.get("cols") or ()): len(p.get("keys") or ()) for p in key_pools
  }
  constraints = build_constraints_detail(profiles)
  log_milestone(
      "relational_e2e",
      engine=engine,
      table=table,
      landing=getattr(ctx, "landing_table", "") or "",
      pk=",".join(getattr(ctx, "pk_columns", []) or []),
      identity=",".join(getattr(ctx, "identity_columns", []) or []),
      fk_edges=len(edges),
      constraints=len(constraints),
  )
  for edge in edges:
    cols = tuple(edge.get("cols") or ())
    fields: dict[str, Any] = {
        "cols": ",".join(cols),
        "ref": edge.get("ref", ""),
        "ref_cols": ",".join(edge.get("ref_cols") or ()),
        "parent_landing": edge.get("parent_landing", ""),
        "enforced": edge.get("enforced", True),
        # ADR 0037 (design §8): the DAG path this edge took —
        # "fanout" (drives), "implied", "side_input" (independent,
        # the pre-ADR-0037 default) or "conditional". Legacy/partial
        # metadata (no "mode" key yet) defaults to "side_input", the
        # path every edge took before this design.
        "mode": edge.get("mode", "side_input"),
    }
    overlap = edge.get("overlap")
    if overlap:
      fields["overlap"] = ",".join(overlap)
    key_tuples = tuples_by_cols.get(cols)
    if key_tuples is None:
      pool_size = len(fk_pools.get(cols[0] if cols else "", ()))
      fields.update(pool_size=pool_size, active=pool_size > 0)
    else:
      fields.update(key_tuples=key_tuples, joint=True, active=key_tuples > 0)
    log_milestone("relational_fk_edge", engine=engine, table=table, **fields)
  _log_relationship_card(engine, table, ctx)


_MERMAID_FENCE = "\n```mermaid"


def _log_relationship_card(engine: str, table: str, ctx) -> None:
  """The launcher's relationship card, echoed once per plan in the
    WORKER log (ADR 0032) — pipes and arrows only.

    Workers are where a run is debugged, and a card the driver rendered
    is the same card — the model was resolved once, from
    `config/relationships/`, and travels as text. The fenced mermaid
    source stays in the LAUNCHER entry for the report tooling; here it
    was one 30-line block per engine instance nobody could read past
    (2026-09-09), so a fence is stripped even if the driver sent one.
    """
  card = getattr(ctx, "relationship_card", "") or ""
  card = card.split(_MERMAID_FENCE, 1)[0].rstrip()
  if not card.strip():
    return
  log_milestone_text("relationship_model", card, engine=engine, table=table)


__all__ = [
    "build_constraints_detail",
    "build_plan",
    "build_plan_detail",
    "clear_generation_plan_log",
    "log_plan_pretty",
    "should_log_plan",
]
