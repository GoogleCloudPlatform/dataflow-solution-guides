"""Relationship models — the single source of truth for PK/FK/identity.

`config/relationships/<model>.yaml`, one file per relational model,
versioned with the code that reads it (ADR 0032). Before this, the
table-level contract lived inside each BigQuery table DESCRIPTION, so
the truth was scattered across N tables and every change meant a
`bq update` / `terraform apply` against production metadata. A launch
now reads one file and knows the whole model.

A model file, whole::

    model: retail                     # id, shown in the log card
    description: orders chain         # optional prose
    tables:
      A_TABLE:
        pk: [A_COL_001, A_COL_002]
        identity: [A_COL_009]
      B_TABLE:
        pk: [B_COL_001]
        enabled: true                 # false = detach from the model
        fk:
          - cols:     [B_COL_006, B_COL_007]
            ref:      A_TABLE         # bare name = in this model
            ref_cols: [A_COL_001, A_COL_002]
            enforced: true            # false = documented, never generated from
            drives:   true            # this edge generates the child (ADR 0036)

Three flags, three different jobs:

* ``enabled: false`` (table) — the table leaves the GRAPH. Anything that
  reached the rest of the model only through it detaches with it, so one
  flag prunes a whole branch without deleting a line. The table itself
  still generates when it is the explicit target: the flag governs
  relationship participation, never permission.
* ``enforced: false`` (edge) — the relationship is real and drawn, but
  no keys are drawn from it. For join keys that exist in the business
  model and not in the DDL.
* ``drives: true`` (edge, ADR 0036) — this is the edge the child is
  GENERATED from: its parent's landed keys become the child's request
  stream, one child row per source fan-out draw. Needed only to
  disambiguate when a table has several enforced in-model edges (a lone
  one drives by itself, and when no marker and no ancestry decides it,
  the first declared edge defaults to driving — ADR 0037 ruling A).
  Every other enforced edge is then `implied` (a subset of the driving
  columns, carried transitively), `conditional` (shares columns with
  the driving edge) or `independent` (shares none) — see
  :meth:`RelationshipRegistry.edge_roles`. Only more than one edge
  marked `drives: true` still stops the launch.

Column-level ``llm_prompt_constraint`` stays in COLUMN descriptions
(:mod:`sdfb_core.contracts.prompt_constraint`) — that is per-column
generation steering, not relational structure, and it belongs next to
the column it steers.
"""

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from sdfb_core.observability import sha12

__all__ = [
    "FkEdge",
    "RelationshipError",
    "RelationshipModel",
    "RelationshipRegistry",
    "TableRelations",
    "parse_relationship_model",
]


class RelationshipError(ValueError):
  """A model file is unusable: bad shape, unknown ref, cycle, or a
    table claimed by two models. Never degraded silently — a half-read
    relational model generates the wrong data."""


def _name(table: str) -> str:
  """Bare table name: model files key on it, targets arrive as FQNs."""
  return table.rsplit(".", 1)[-1]


class FkEdge(BaseModel):
  """One FK edge: this table's ``cols`` reference ``ref``'s ``ref_cols``."""

  model_config = ConfigDict(frozen=True, extra="forbid")

  cols: tuple[str, ...]
  ref: str
  ref_cols: tuple[str, ...]
  # False = documented-only: drawn in the card, never a source of keys.
  enforced: bool = True
  note: str = ""
  # Design 2026-09-10 (ADR 0036): the edge whose parent keys this table
  # is generated FROM. Needed only when a child has several enforced
  # in-model edges; a lone edge drives by itself.
  drives: bool = False

  @field_validator("cols", "ref_cols")
  @classmethod
  def _non_empty(cls, v: tuple[str, ...]) -> tuple[str, ...]:
    if not v or any(not c for c in v):
      raise ValueError("FK column lists must be non-empty strings")
    return v

  @property
  def external(self) -> bool:
    """True when ``ref`` names a table outside this model (it must be
        dataset-qualified, and must already be landed)."""
    return "." in self.ref


class TableRelations(BaseModel):
  """One table's relational facts."""

  model_config = ConfigDict(frozen=True, extra="forbid")

  pk: tuple[str, ...] = ()
  identity: tuple[str, ...] = ()
  fk: tuple[FkEdge, ...] = ()
  # False = detached from the model (see module docstring).
  enabled: bool = True
  note: str = ""


class RelationshipModel(BaseModel):
  """One model file: a named set of tables and the edges between them."""

  model_config = ConfigDict(frozen=True, extra="forbid")

  model: str
  description: str = ""
  tables: dict[str, TableRelations] = {}
  # Where this came from — printed in the log card so an operator can
  # go straight to the file that decided the run.
  source: str = ""


def parse_relationship_model(text: str, source: str = "") -> RelationshipModel:
  """One YAML document → a validated :class:`RelationshipModel`."""
  try:
    raw = yaml.safe_load(text) or {}
  except yaml.YAMLError as exc:
    raise RelationshipError(f"{source}: not valid YAML — {exc}") from exc
  if not isinstance(raw, dict):
    raise RelationshipError(
        f"{source}: expected a mapping at the top level, got "
        f"{type(raw).__name__}")
  try:
    model = RelationshipModel.model_validate({**raw, "source": source})
  except ValidationError as exc:
    raise RelationshipError(f"{source}: {exc}") from exc
  _validate_refs(model)
  return model


def _validate_refs(model: RelationshipModel) -> None:
  known = set(model.tables)
  for table, relations in model.tables.items():
    seen: set[tuple[tuple[str, ...], str, tuple[str, ...]]] = set()
    for edge in relations.fk:
      # Two fk: entries with the same (cols, ref, ref_cols) are a
      # copy-paste mistake, not two edges: under value equality
      # they collapse silently downstream (edge_roles' dict keying,
      # edge_overlap/edge_rest's driving-edge comparison) instead
      # of being caught here, at load time.
      key = (edge.cols, edge.ref, edge.ref_cols)
      if key in seen:
        raise RelationshipError(
            f"{model.source}: {table}: edge "
            f"({','.join(edge.cols)})->{edge.ref} declared twice "
            f"— one fk entry per (cols, ref, ref_cols)")
      seen.add(key)
      if edge.ref == table or _name(edge.ref) == table:
        raise RelationshipError(f"{model.source}: {table} references itself "
                                f"({edge.ref}) — an FK edge needs two tables")
      if len(edge.cols) != len(edge.ref_cols):
        raise RelationshipError(f"{model.source}: {table} FK arity mismatch — "
                                f"{list(edge.cols)} vs {list(edge.ref_cols)}")
      if not edge.external and edge.ref not in known:
        raise RelationshipError(
            f"{model.source}: {table}'s fk.ref {edge.ref!r} does "
            f"not name a table in model {model.model!r} "
            f"({sorted(known)}). Qualify it as dataset.table if "
            f"the parent lives outside this model.")


@dataclass(frozen=True)
class RelationshipRegistry:
  """Every model the launch loaded, indexed by table.

    The registry is the ONLY thing the pipeline asks about relational
    structure. It answers four questions: what are this table's keys,
    which tables travel with it, in what order, and what should the log
    show.
    """

  models: tuple[RelationshipModel, ...] = ()

  @classmethod
  def from_sources(cls, sources: list[tuple[str, str]]) -> RelationshipRegistry:
    """``[(source, yaml text), …]`` → a validated registry.

        A table may appear in exactly ONE model: two models claiming it
        would make "the single source of truth" a question of file
        ordering.
        """
    models = tuple(
        parse_relationship_model(text, source=source)
        for source, text in sources)
    owners: dict[str, list[str]] = {}
    for model in models:
      for table in model.tables:
        owners.setdefault(table, []).append(model.model)
    clashes = {t: m for t, m in owners.items() if len(m) > 1}
    if clashes:
      detail = "; ".join(
          f"{t} in {sorted(set(ms))}" for t, ms in sorted(clashes.items()))
      raise RelationshipError(
          f"table declared in 2 models or more — one source of "
          f"truth per table: {detail}")
    registry = cls(models=models)
    for model in models:
      for table in model.tables:
        # Surfaces enforced-edge cycles at LOAD time, with the
        # file name, instead of mid-launch.
        registry.generation_order(registry.component(table))
    return registry

  # -- lookups -----------------------------------------------------------

  def model_for(self, table: str) -> RelationshipModel | None:
    key = _name(table)
    for model in self.models:
      if key in model.tables:
        return model
    return None

  def relations(self, table: str) -> TableRelations | None:
    model = self.model_for(table)
    return model.tables[_name(table)] if model else None

  def enabled(self, table: str) -> bool:
    relations = self.relations(table)
    return True if relations is None else relations.enabled

  def _raw_enforced(self, table: str) -> tuple[FkEdge, ...]:
    """Enforced edges exactly as declared: both ends enabled."""
    relations = self.relations(table)
    if relations is None or not relations.enabled:
      return ()
    return tuple(
        edge for edge in relations.fk
        if edge.enforced and (edge.external or self.enabled(edge.ref)))

  def enforced_edges(self, table: str) -> tuple[FkEdge, ...]:
    """Edges this table actually draws keys from: enforced, both ends
        enabled, and WIDENED with the column pairs the model's own
        children pin (`derived_widenings`, ADR 0036 rev): when a child
        references the same columns in this table AND in this table's
        parent, those columns are inherited from the parent, so this
        table's edge to it carries them."""
    return tuple(
        self.widened(table, edge) for edge in self._raw_enforced(table))

  def widened(self, table: str, edge: FkEdge) -> FkEdge:
    """``edge`` as the launch actually DRAWS it: widened with the
        column pairs the model's own children pin (`derived_widenings`,
        ADR 0036 rev 2), or the very same object back when nothing
        widens it. A documented edge (``enforced: false``) is returned
        unchanged — the launch never draws it, so it keeps the columns
        the model declares. Public because resolving a DECLARATION to
        the edge `enforced_edges` returns is what the launcher's
        `fk_edge_metadata` does to look an edge's role up, and guessing
        that correspondence from a column prefix mis-stamped a
        documented edge once already (ADR 0037 review round 1)."""
    if not edge.enforced:
      return edge
    added = [
        pair for rec in self._widenings()
        if rec["table"] == _name(table) and rec["ref"] == edge.ref
        for pair in rec["added"] if pair[0] not in edge.cols
    ]
    if not added:
      return edge
    return edge.model_copy(
        update={
            "cols": edge.cols + tuple(c for c, _ in added),
            "ref_cols": edge.ref_cols + tuple(r for _, r in added),
        })

  def _widenings(self) -> list[dict]:
    """Column pairs a parent's edge to a grandparent must carry, pinned
        by a child that references the SAME columns in both (the model
        asserts the correspondence). Derived, never written back."""
    records: list[dict] = []
    for model in self.models:
      for child, relations in model.tables.items():
        if not relations.enabled:
          continue
        edges = [e for e in self._raw_enforced(child) if not e.external]
        for i, e1 in enumerate(edges):
          for e2 in edges[i + 1:]:
            if e1.cols != e2.cols or e1.ref == e2.ref:
              continue
            for near, far in ((e1, e2), (e2, e1)):
              # `near.ref` must itself hold a direct edge to `far.ref`.
              for up in self._raw_enforced(near.ref):
                if up.external or up.ref != far.ref:
                  continue
                known = set(zip(up.cols, up.ref_cols, strict=True))
                added = [
                    (n, f)
                    for n, f in zip(near.ref_cols, far.ref_cols, strict=True)
                    if (n, f) not in known and n not in up.cols
                ]
                if added:
                  records.append({
                      "table": near.ref,
                      "ref": far.ref,
                      "via": child,
                      "added": added
                  })
    return records

  def derived_widenings(self) -> list[dict]:
    """``[{"table", "ref", "via", "added": [(col, ref_col), …]}, …]`` —
        every edge the registry widened, for the launcher to announce."""
    return self._widenings()

  def _descends(self,
                table: str,
                ancestor: str,
                seen: set[str] | None = None) -> bool:
    """True when ``table`` reaches ``ancestor`` over enforced edges."""
    seen = seen if seen is not None else set()
    if table == ancestor:
      return True
    if table in seen:
      return False
    seen.add(table)
    return any(not e.external and self._descends(e.ref, ancestor, seen)
               for e in self._raw_enforced(table))

  def edge_roles(self, table: str) -> dict[FkEdge, str]:
    """Role of every enforced edge of ``table`` (ADR 0036/0037): the
        ``driving`` edge is the parent whose keys the child is generated
        from (:meth:`driving_choice` says how it was picked);
        ``implied`` — a subset of the driving edge's columns, carried by
        the driving parent from that parent through its own enforced
        edges, transitively: nothing to draw; ``conditional`` — shares
        at least one column with the driving edge (co-drawn from a
        joint pool on the shared columns, ADR 0037 §4); ``independent``
        — shares no column with the driving edge (drawn from a
        side-input key pool, ADR 0037 §5); ``external`` — the parent is
        outside this model. Two launch stops remain: more than
        one edge marked ``drives: true`` (rule 5), and two NON-DRIVING
        edges writing the same child column
        (:meth:`_check_column_ownership`)."""
    edges = self.enforced_edges(table)
    roles: dict[FkEdge, str] = {e: "external" for e in edges if e.external}
    internal = [e for e in edges if not e.external]
    if internal:
      driving, _ = self._pick_driving(table, internal)
      roles[driving] = "driving"
      for edge in internal:
        if edge is driving:
          continue
        if self._implied(edge, driving):
          roles[edge] = "implied"
          continue
        overlap = tuple(c for c in edge.cols if c in driving.cols)
        roles[edge] = "conditional" if overlap else "independent"
    self._check_column_ownership(table, edges, roles)
    return roles

  def _written_cols(self, table: str, edge: FkEdge,
                    role: str) -> tuple[str, ...]:
    """The child columns this edge actually WRITES into a generated
        row: nothing for ``implied`` (the driving key already carries
        them), only :meth:`edge_rest` for ``conditional`` (the shared
        columns come from the driving key), and the whole ``cols`` tuple
        for ``driving``, ``independent`` and ``external`` — a driving key
        or a whole pool draw."""
    if role == "implied":
      return ()
    if role == "conditional":
      return self.edge_rest(table, edge)
    return edge.cols

  def _check_column_ownership(self, table: str, edges: tuple[FkEdge, ...],
                              roles: dict[FkEdge, str]) -> None:
    """No child column may be WRITTEN by two NON-DRIVING edges
        (ADR 0037 final review).

        :meth:`edge_roles` gives each non-driving edge a role from its
        overlap with the DRIVING edge alone, and never compares the
        non-driving edges with each other — so two of them could claim
        one column and the last writer silently won. Two ``independent``
        edges write whole pool tuples in declaration order
        (``_draw_fk_columns`` in B.1, the pool loop in B.2), so the first
        edge's tuple is destroyed and nearly every row is diverted as
        ``fk.orphan`` — after the GPU has already generated it. Two
        ``conditional`` edges whose ``rest``s overlap are written in plan
        order by ``apply_conditional_overrides``, and conditional edges
        are not gated at all (the gate only sees side-input pools), so
        those referentially broken rows LAND. Both shapes raised here
        before ADR 0037 (D4's blanket "neither driving nor implied"
        stop); this is that stop, by name.

        The DRIVING edge is deliberately outside the pairing:
        ``implied`` / ``independent`` / ``conditional`` are disjoint from
        it by construction.

        So is every ``external`` edge (fix wave F3). Design 2026-09-11 §9
        names an external overlap as a LIMITATION, not a stop, and none
        of the remedies above can be applied to one: an external edge
        never becomes ``implied`` (:meth:`edge_roles` assigns the role
        before any subset analysis), ``drives: true`` is inert for it
        (:meth:`_pick_driving` only considers in-model edges), and an
        external parent has no ``tables:`` entry to disable. Stopping
        there refused the classic denormalised child — every parent
        external, all on one ancestry line — which the identical shape
        with IN-MODEL parents ships today as ``implied``. Those pairs are
        reported by :meth:`external_overlaps` and logged as
        ``fk_edge_overlap_external`` (WARNING) instead.
        """
    owners: list[tuple[FkEdge, str, tuple[str, ...]]] = []
    for edge in edges:
      role = roles.get(edge)
      if role is None or role in ("driving", "external"):
        continue
      owners.append((edge, role, self._written_cols(table, edge, role)))
    for index, (first, first_role, first_cols) in enumerate(owners):
      for second, second_role, second_cols in owners[index + 1:]:
        shared = tuple(c for c in first_cols if c in second_cols)
        if not shared:
          continue
        raise RelationshipError(
            f"{_name(table)}: edges "
            f"({','.join(first.cols)})->{first.ref} [{first_role}] "
            f"and ({','.join(second.cols)})->{second.ref} "
            f"[{second_role}] both write ({','.join(shared)}) — one "
            f"child column cannot be owned by two edges: the second "
            f"draw overwrites the first, landing a tuple its parent "
            f"never held. Make one edge's columns a SUBSET of the "
            f"other's so it is implied, mark the edge this table is "
            f"generated from `drives: true`, document one edge "
            f"(`enforced: false`, so no keys are drawn from it), or "
            f"disable one parent (`enabled: false`).")

  def external_overlaps(
      self,
      table: str,
      roles: Mapping[FkEdge, str] | None = None
  ) -> tuple[tuple[FkEdge, FkEdge, tuple[str, ...]], ...]:
    """``(external edge, other edge, shared columns)`` for every pair
        of ``table``'s enforced edges that WRITE a column in common and
        has at least one EXTERNAL parent — the launcher's
        ``fk_edge_overlap_external`` WARNING source (fix wave F3).

        Covers the pre-ADR-0037 case (``driving`` n ``external``) and the
        two the ownership stop must not be fatal on: ``external`` n
        ``external`` and ``external`` n any non-driving edge. The launch
        writes the edges in declaration order, so the last writer keeps
        the shared columns and the loser's tuple need not exist in its
        parent — a real risk, reported rather than stopped, because
        nothing the operator can declare resolves it while the parent
        stays outside the launch.

        ``roles`` is the mapping the caller already resolved (it is keyed
        on the WIDENED edges :meth:`enforced_edges` returns); omitting it
        resolves them here, which may raise the ownership stop.
        """
    resolved = self.edge_roles(table) if roles is None else roles
    written: list[tuple[FkEdge, str, tuple[str, ...]]] = []
    for edge in self.enforced_edges(table):
      role = resolved.get(edge)
      if role is None:
        continue
      written.append((edge, role, self._written_cols(table, edge, role)))
    pairs: list[tuple[FkEdge, FkEdge, tuple[str, ...]]] = []
    for index, (first, first_role, first_cols) in enumerate(written):
      for second, second_role, second_cols in written[index + 1:]:
        if "external" not in (first_role, second_role):
          continue
        shared = tuple(c for c in first_cols if c in second_cols)
        if not shared:
          continue
        # The EXTERNAL edge is named first: it is the one the
        # operator cannot route, so it leads the milestone.
        pairs.append((second, first,
                      shared) if first_role != "external" else (first, second,
                                                                shared))
    return tuple(pairs)

  def driving_edge(self, table: str) -> FkEdge | None:
    return next(
        (e for e, r in self.edge_roles(table).items() if r == "driving"), None)

  def _pick_driving(self, table: str,
                    internal: list[FkEdge]) -> tuple[FkEdge, str]:
    """The driving edge and how it was chosen (ADR 0037 §3, in
        order): a lone internal edge drives itself (``"single"``);
        exactly one edge marked ``drives: true`` (``"marked"``); with
        at least two DISTINCT candidate parents, the parent that
        descends from every other one, widened with the child's pins
        (``"derived"``, ADR 0036 rev 2); else — no
        marker and no ancestry between the parents — the first declared
        edge drives (``"first_declared"``, ruling A, 2026-09-11). More
        than one edge marked ``drives: true`` is the only stop left."""
    if len(internal) == 1:
      return internal[0], "single"
    marked = [e for e in internal if e.drives]
    if len(marked) > 1:
      names = ", ".join(f"({','.join(e.cols)})->{e.ref}" for e in internal)
      raise RelationshipError(
          f"{_name(table)}: {len(internal)} enforced edges [{names}] "
          f"and {len(marked)} marked `drives: true` — mark exactly "
          f"one edge `drives: true` (the parent whose keys this "
          f"table is generated from)")
    if len(marked) == 1:
      return marked[0], "marked"
    # Unmarked (the operator only toggled `enabled`): the DAG
    # decides — the parent that itself descends from every other
    # candidate parent is the most-derived one and drives. That needs
    # a real choice, so at least two DISTINCT candidate parents: with
    # two edges to the SAME parent the `all(...)` below runs over an
    # EMPTY set and is vacuously true for every edge, so rule 3
    # returned the first declared edge labelled "derived" though
    # nothing was derived and nothing was widened — and the operator
    # lost rule 4's `fk_driving_edge_defaulted` WARNING and its
    # `DRIVES (first declared …)` card tag, the only hint that
    # `drives: true` was theirs to set (ADR 0037 final review).
    parents = {e.ref for e in internal}
    if len(parents) > 1:
      lowest = [
          e for e in internal if all(
              self._descends(e.ref, other) for other in parents
              if other != e.ref)
      ]
      if len({e.ref for e in lowest}) == 1:
        return lowest[0], "derived"
    # No marker, no ancestry between the parents (ADR 0037 ruling A,
    # 2026-09-11): the first declared internal enforced edge drives.
    return internal[0], "first_declared"

  def _driving_edge_and_choice(self,
                               table: str) -> tuple[FkEdge | None, str | None]:
    internal = [e for e in self.enforced_edges(table) if not e.external]
    if not internal:
      return None, None
    return self._pick_driving(table, internal)

  def driving_choice(self, table: str) -> str | None:
    """How the driving edge was picked (ADR 0037 §3): ``"single"``,
        ``"marked"``, ``"derived"`` or ``"first_declared"`` — see
        :meth:`_pick_driving`. ``None`` when ``table`` has no internal
        enforced edge (a root, a disabled table, or a table outside
        every model)."""
    return self._driving_edge_and_choice(table)[1]

  def _overlap_with_driving(self, table: str,
                            edge: FkEdge) -> tuple[str, ...] | None:
    """``edge``'s overlap with the driving edge, in ``edge.cols``
        order — or ``None`` when ``edge`` IS the driving edge, or
        ``table`` has no driving edge at all. Compared by VALUE
        (``==``), not identity: `widened()` returns a fresh `FkEdge`
        via `model_copy()` on every call, so a widened driving edge
        obtained from an earlier `enforced_edges()`/`edge_roles()` call
        is never the same Python object as the one this method derives
        — but it is always equal (frozen pydantic model, structural
        `__eq__`), as :meth:`mermaid`'s `roles.get(widened)` already
        relies on elsewhere in this file."""
    driving, _ = self._driving_edge_and_choice(table)
    if driving is None or edge == driving:
      return None
    return tuple(c for c in edge.cols if c in driving.cols)

  def edge_overlap(self, table: str, edge: FkEdge) -> tuple[str, ...]:
    """Child column names of ``edge`` that also appear in the
        driving edge's columns, in ``edge.cols`` order. Empty for the
        driving edge itself, and when ``table`` has no driving edge."""
    return self._overlap_with_driving(table, edge) or ()

  def edge_rest(self, table: str, edge: FkEdge) -> tuple[str, ...]:
    """``edge.cols`` outside :meth:`edge_overlap`. Empty for the
        driving edge itself, and when ``table`` has no driving edge."""
    overlap = self._overlap_with_driving(table, edge)
    if overlap is None:
      return ()
    return tuple(c for c in edge.cols if c not in overlap)

  def _implied(self, edge: FkEdge, driving: FkEdge) -> bool:
    """``edge`` is satisfied by construction when its columns ride on
        the driving edge AND the driving parent obtains them from
        ``edge.ref`` (transitively over enforced edges)."""
    if not set(edge.cols) <= set(driving.cols):
      return False
    # The parent-side names of edge.cols on the driving parent.
    pos = {c: i for i, c in enumerate(driving.cols)}
    parent_cols = {driving.ref_cols[pos[c]] for c in edge.cols}
    return self._carries(driving.ref, edge.ref, parent_cols, seen=set())

  def _carries(
      self,
      table: str,
      target: str,
      cols: set[str],
      seen: set[tuple[str, frozenset[str]]],
  ) -> bool:
    if table == target:
      return True
    key = (table, frozenset(cols))
    if key in seen:
      return False
    seen.add(key)
    for up in self.enforced_edges(table):
      if up.external or not cols <= set(up.cols):
        continue
      pos = {c: i for i, c in enumerate(up.cols)}
      upstream = {up.ref_cols[pos[c]] for c in cols}
      if self._carries(up.ref, target, upstream, seen):
        return True
    return False

  # -- graph -------------------------------------------------------------

  def _adjacency(self) -> dict[str, set[str]]:
    """Undirected neighbours over ENABLED tables only. Documented
        edges count here: they still say "these tables belong together",
        which is what a scenario-2 launch is asking about."""
    adjacent: dict[str, set[str]] = {}
    for model in self.models:
      for table, relations in model.tables.items():
        adjacent.setdefault(table, set())
        if not relations.enabled:
          continue
        for edge in relations.fk:
          if edge.external or not self.enabled(edge.ref):
            continue
          adjacent[table].add(edge.ref)
          adjacent.setdefault(edge.ref, set()).add(table)
    return adjacent

  def component(self, table: str) -> tuple[str, ...]:
    """``table`` plus every table still reachable from it.

        A disabled table is not traversed, so anything that reached the
        model only through it is no longer part of this launch. A
        disabled TARGET is returned alone — the flag detaches, it does
        not forbid.
        """
    key = _name(table)
    if self.relations(key) is None:
      return (key,)
    adjacent = self._adjacency()
    seen = {key}
    frontier = [key]
    while frontier:
      nxt = []
      for current in frontier:
        for neighbour in adjacent.get(current, ()):
          if neighbour not in seen:
            seen.add(neighbour)
            nxt.append(neighbour)
      frontier = nxt
    ordered = [t for m in self.models for t in m.tables if t in seen]
    return tuple(ordered)

  def generation_waves(self, tables: tuple[str,
                                           ...]) -> tuple[tuple[str, ...], ...]:
    """Parents-first WAVES over ENFORCED edges, stable in model order.

        Every table inside a wave is independent of the others, so a wave
        may run in parallel while the waves themselves stay ordered — a
        child never starts before its parent has landed.
        """
    members = {_name(t) for t in tables}
    parents = {
        t: {
            edge.ref
            for edge in self.enforced_edges(t)
            if not edge.external and edge.ref in members
        } for t in members
    }
    order_hint = [t for m in self.models for t in m.tables if t in members]
    order_hint += [t for t in sorted(members) if t not in order_hint]
    waves: list[tuple[str, ...]] = []
    placed: set[str] = set()
    remaining = dict(parents)
    while remaining:
      ready = tuple(
          t for t in order_hint if t in remaining and remaining[t] <= placed)
      if not ready:
        raise RelationshipError(
            f"FK cycle among {sorted(remaining)} — enforced edges "
            f"must form a DAG (a child cannot be its own ancestor)")
      for table in ready:
        del remaining[table]
      placed.update(ready)
      waves.append(ready)
    return tuple(waves)

  def generation_order(self, tables: tuple[str, ...]) -> tuple[str, ...]:
    """Parents first — :meth:`generation_waves` flattened."""
    return tuple(t for wave in self.generation_waves(tables) for t in wave)

  def sha12(self) -> str:
    """Content hash of every loaded model — same models, same sha, so
        a report can recycle a diagram instead of redrawing it."""
    canon = ";".join(
        sorted(f"{m.model}:{t}:{','.join(r.pk)}:{','.join(r.identity)}:"
               f"{int(r.enabled)}:" +
               "|".join(f"{','.join(e.cols)}->{e.ref}:{','.join(e.ref_cols)}:"
                        f"{int(e.enforced)}:{int(e.drives)}"
                        for e in r.fk)
               for m in self.models
               for t, r in m.tables.items()))
    return sha12(canon)

  # -- the log card ------------------------------------------------------

  def card(self, table: str) -> str:
    """One glanceable block: the model, where it came from, and every
        table's keys, edges and state.

        Written for Cloud Logging at 3am: no renderer, no copy-paste,
        waves state the generation order, ``-->`` is an enforced edge and
        ``..>`` a documented one, and a disabled table says so on its own
        line.
        """
    model = self.model_for(table)
    if model is None:
      return (f"RELATIONSHIP MODEL | none — {_name(table)} is not in any "
              f"model file; generating it alone (PK/identity from CLI "
              f"flags if given)")
    component = self.component(table)
    order = self.generation_order(component)
    disabled = [t for t in model.tables if not model.tables[t].enabled]
    enforced = sum(len(self.enforced_edges(t)) for t in order)
    documented = sum(
        1 for t in model.tables for e in model.tables[t].fk if not e.enforced)
    head = (f"RELATIONSHIP MODEL {model.model} | source {model.source} | "
            f"sha {self.sha12()}")
    counts = (f"  {len(model.tables)} tables declared · {len(order)} in this "
              f"launch · {enforced} enforced + {documented} documented edges" +
              (f" · {len(disabled)} DISABLED" if disabled else ""))
    lines = [head, counts]
    if model.description:
      lines.append(f"  \"{model.description}\"")
    for wave, name in enumerate(order):
      lines.extend(self._table_lines(model, name, f"wave {wave}"))
    for name in model.tables:
      if name not in order:
        lines.extend(self._table_lines(model, name, "  --  "))
    return "\n".join(lines)

  def mermaid(self, table: str) -> str:
    """House-style diagram source for the table's model — the ONE
        renderer reports embed (visual-first docs rule). Stores are
        cylinders, enforced edges solid, documented edges dashed, and a
        disabled table is dimmed like an external one. Driving and
        implied edges keep their plain ``cols → ref_cols`` label
        unchanged; independent and conditional edges (ADR 0037) get an
        extra suffix."""
    model = self.model_for(table)
    if model is None:
      return ""

    def node_id(name: str) -> str:
      return name.replace(".", "_").replace("-", "_")

    lines = ["flowchart BT"]
    external: list[str] = []
    for name, relations in model.tables.items():
      icon = "🗄️" if relations.enabled else "🚫"
      suffix = "" if relations.enabled else " (disabled)"
      lines.append(f'  {node_id(name)}[("{icon} {name}{suffix}")]')
      for edge in relations.fk:
        if edge.external and edge.ref not in external:
          external.append(edge.ref)
    for ref in external:
      lines.append(f'  {node_id(ref)}[("⚪ {ref} (external)")]')
    for name, relations in model.tables.items():
      try:
        roles = self.edge_roles(name) if relations.enabled else {}
      except RelationshipError:
        roles = {}
      for edge in relations.fk:
        arrow = "-->" if edge.enforced else "-.->"
        label = f"{','.join(edge.cols)} → {','.join(edge.ref_cols)}"
        widened = self.widened(name, edge)
        role = roles.get(widened)
        if role == "independent":
          label += " -- independent"
        elif role == "conditional":
          overlap = self.edge_overlap(name, widened)
          label += f" -- conditional on {','.join(overlap)}"
        lines.append(f'  {node_id(name)} {arrow}|"{label}"| '
                     f"{node_id(edge.ref)}")
    lines.append("  classDef store fill:#2a78d6,color:#fff,stroke:#1d5599")
    lines.append("  classDef data fill:#6b7280,color:#fff,stroke:#4b5563")
    live = [t for t, r in model.tables.items() if r.enabled]
    dim = [t for t, r in model.tables.items() if not r.enabled] + external
    if live:
      lines.append("  class " + ",".join(node_id(t) for t in live) + " store")
    if dim:
      lines.append("  class " + ",".join(node_id(t) for t in dim) + " data")
    return "\n".join(lines)

  def log_body(self, table: str) -> str:
    """What the launcher and the workers both print: the card for
        humans, the fenced mermaid below it for the report tooling to
        lift by sha. One entry, both audiences."""
    diagram = self.mermaid(table)
    card = self.card(table)
    if not diagram:
      return card
    return card + "\n\n```mermaid\n" + diagram + "\n```"

  def _role_tag(self, name: str, edge: FkEdge, roles: dict[FkEdge, str],
                choice: str | None) -> str | None:
    """The ``[enforced, ...]`` suffix for one edge's role (card tags,
        ADR 0036/0037), or ``None`` to keep the caller's plain
        ``"enforced"`` tag (an edge :meth:`edge_roles` didn't classify —
        should not happen once ``roles`` is non-empty)."""
    role = roles.get(edge)
    if role == "driving":
      if choice == "first_declared":
        return "enforced, DRIVES (first declared — mark drives: true to choose)"
      return "enforced, DRIVES"
    if role == "implied":
      driving = next((e for e, r in roles.items() if r == "driving"), None)
      return f"enforced, implied via {_name(driving.ref)}" if driving else None
    if role == "independent":
      return "enforced, independent"
    if role == "conditional":
      overlap = self.edge_overlap(name, edge)
      return f"enforced, conditional on ({','.join(overlap)})"
    return None

  def _table_lines(self, model: RelationshipModel, name: str,
                   prefix: str) -> list[str]:
    relations = model.tables[name]
    keys = []
    if relations.pk:
      keys.append(f"pk({','.join(relations.pk)})")
    if relations.identity:
      keys.append(f"identity({','.join(relations.identity)})")
    state = "" if relations.enabled else "   [DISABLED — detached]"
    lines = [
        f" {prefix} | {name:<24} {' '.join(keys) or '(no keys declared)'}"
        f"{state}"
    ]
    # Compute roles once per table (ADR 0036/0037).
    try:
      roles = self.edge_roles(name)
      choice = self.driving_choice(name)
    except RelationshipError:
      roles = {}
      choice = None
    widened_by = {
        (rec["ref"], tuple(rec["added"])): rec["via"]
        for rec in self._widenings()
        if rec["table"] == name
    }
    for declared in relations.fk:
      edge = self.widened(name, declared)
      arrow = "-->" if edge.enforced else "..>"
      tag = "enforced" if edge.enforced else "documented, never drawn"
      if edge.enforced and not relations.enabled:
        tag = "this table DISABLED — not drawn"
      elif edge.enforced and not (edge.external or self.enabled(edge.ref)):
        tag = "parent DISABLED — not drawn"
      # Render edge roles (ADR 0036/0037).
      elif edge.enforced and tag == "enforced" and roles:
        tag = self._role_tag(name, edge, roles, choice) or tag
      if edge is not declared:
        added = tuple(
            zip(edge.cols[len(declared.cols):],
                edge.ref_cols[len(declared.ref_cols):],
                strict=True))
        via = widened_by.get((edge.ref, added), "?")
        tag += f", widened via {via} (+{','.join(c for c, _ in added)})"
      lines.append(f"        |   +- ({','.join(edge.cols)}) {arrow} "
                   f"{edge.ref} ({','.join(edge.ref_cols)})   [{tag}]")
    return lines
