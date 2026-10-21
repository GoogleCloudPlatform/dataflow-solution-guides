#  Copyright 2026 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""A standalone mirror of `sdfb_core.contracts.relationships` — the PK/FK
graph a relational launch generates from (`config/relationships/*.yaml`).

This package cannot import `sdfb_core` (independence is enforced by an AST
test), so the pieces the evaluator needs from `RelationshipRegistry` are
reimplemented here as plain, frozen dataclasses plus free functions taking
a `models: Sequence[RelModel]` — the parsed file set — rather than as
methods on a stateful registry object. The shape mirrored is deliberately
narrower than the original: PK/FK graph traversal (`component`,
`generation_order`), what a table actually draws keys from
(`enforced_edges`, including the same column-widening `derived_widenings`
applies), and the content hash (`sha12`) used to key cached diagrams and
reports. Edge ROLES (`driving`/`implied`/`independent`/`conditional`),
the launch card and the mermaid renderer stay on the generator side — the
evaluator never needs them.

A two-sided golden-file test pins this module's output against the
original's, over the three example models this repository ships
(`config/relationships/{example_retail,example_star_diamond,
gcp_public_fk_example}.yaml`): `make_parity_goldens.py` (run in the root
environment, against the originals) writes
`tests/fixtures/parity/goldens.json`; this package's
`tests/unit/test_parity_goldens.py` recomputes the same values from THIS
module and asserts equality; the root
`packages/sdfb-tests/tests/unit/evaluation_parity/test_goldens.py`
recomputes them from the originals, so neither side can silently drift.

Design: docs/designs/2026-07-07-evaluation-framework-design.md

Design: docs/DESIGN.md §4 Relational generation; §8 Configuration
(ADR 0032, 0036).
"""

from __future__ import annotations

import glob as _glob
import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "Edge",
    "RelModel",
    "RelationshipError",
    "TableRel",
    "component",
    "derived_widenings",
    "enforced_edges",
    "from_sources",
    "generation_order",
    "generation_waves",
    "is_sample_model",
    "load_models",
    "model_for",
    "parse_model",
    "sha12",
    "widened",
]

_SUFFIXES = (".yaml", ".yml")


class RelationshipError(ValueError):
  """A model file or graph is unusable: bad shape, unknown ref, or an FK
  cycle. Mirrors `sdfb_core.contracts.relationships.RelationshipError`
  closely enough for this module's own validation, without reproducing
  its pydantic validation surface.
  """


@dataclass(frozen=True)
class Edge:
  """One FK edge: this table's `cols` reference `ref`'s `ref_cols`."""

  cols: tuple[str, ...]
  ref: str
  ref_cols: tuple[str, ...]
  enforced: bool = True
  drives: bool = False

  @property
  def external(self) -> bool:
    """True when `ref` names a table outside this model (dataset-qualified,
    already landed by another run)."""
    return "." in self.ref

  @property
  def ref_name(self) -> str:
    """The bare table name `ref` resolves to, dataset prefix stripped."""
    return self.ref.rsplit(".", 1)[-1]

  def label(self, child: str) -> str:
    """A one-line `child(cols) -> ref(ref_cols)` label, for logging and
    assertions — this module has no card/mermaid renderer of its own."""
    # Hoisted out of the f-string: a `','.join(...)` call inline would nest
    # the same quote character pylint's py3.14 (PEP 701) run treats as
    # inconsistent with the rest of this double-quoted file (W1405).
    cols = ",".join(self.cols)
    ref_cols = ",".join(self.ref_cols)
    return f"{child}({cols}) -> {self.ref}({ref_cols})"


@dataclass(frozen=True)
class TableRel:
  """One table's relational facts."""

  name: str
  pk: tuple[str, ...] = ()
  identity: tuple[str, ...] = ()
  fk: tuple[Edge, ...] = ()
  enabled: bool = True


@dataclass(frozen=True)
class RelModel:
  """One model file: a named set of tables and the edges between them."""

  model: str
  tables: Mapping[str, TableRel] = field(default_factory=dict)
  source: str = ""


def _name(table: str) -> str:
  """Bare table name: model files key on it, targets arrive as FQNs."""
  return table.rsplit(".", 1)[-1]


# Allowed keys at each level, transcribed from the originals' pydantic
# models (`extra="forbid"`): `RelationshipModel` (plus `source`, which a
# YAML file may name but which the loader always overwrites — never
# `extra_forbidden` there), `TableRelations`, `FkEdge`.
_MODEL_KEYS = frozenset({"model", "description", "tables", "source"})
_TABLE_KEYS = frozenset({"pk", "identity", "fk", "enabled", "note"})
_EDGE_KEYS = frozenset(
    {"cols", "ref", "ref_cols", "enforced", "note", "drives"})

# Pydantic v2's lax `bool` input vocabulary (case-insensitive, no
# whitespace stripping) — empirically confirmed against
# `pydantic.TypeAdapter(bool)` rather than assumed: only the literal
# strings below, and only the literal 0/1 for int/float, coerce; every
# other value (including out-of-range ints like `2` and any leading/
# trailing space) is rejected.
_BOOL_TRUE = frozenset({"true", "yes", "on", "y", "t", "1"})
_BOOL_FALSE = frozenset({"false", "no", "off", "n", "f", "0"})


def _reject_unknown(spec: Mapping[str, Any], allowed: frozenset[str],
                    context: str) -> None:
  # `key=str`: YAML keys may be ints, bools or nulls next to strings, and
  # those must surface as a RelationshipError, not a TypeError from the
  # sort itself.
  extra = sorted(set(spec) - allowed, key=str)
  if extra:
    raise RelationshipError(f"{context}: unexpected field(s) {extra}")


def _require_str(spec: Mapping[str, Any], key: str, *, context: str) -> str:
  if key not in spec:
    raise RelationshipError(f"{context}.{key}: field required")
  value = spec[key]
  if not isinstance(value, str):
    raise RelationshipError(
        f"{context}.{key}: expected a string, got {type(value).__name__}")
  return value


def _optional_str(spec: Mapping[str, Any], key: str, *, default: str,
                  context: str) -> str:
  if key not in spec:
    return default
  value = spec[key]
  if not isinstance(value, str):
    raise RelationshipError(
        f"{context}.{key}: expected a string, got {type(value).__name__}")
  return value


def _coerce_bool(value: Any, *, context: str) -> bool:
  if isinstance(value, bool):
    return value
  if isinstance(value, int) and value in (0, 1):
    return bool(value)
  if isinstance(value, float) and value in (0.0, 1.0):
    return bool(value)
  if isinstance(value, str):
    folded = value.casefold()
    if folded in _BOOL_TRUE:
      return True
    if folded in _BOOL_FALSE:
      return False
  raise RelationshipError(f"{context}: {value!r} is not a valid boolean")


def _optional_bool(spec: Mapping[str, Any], key: str, *, default: bool,
                   context: str) -> bool:
  if key not in spec:
    return default
  return _coerce_bool(spec[key], context=f"{context}.{key}")


def _optional_str_list(spec: Mapping[str, Any], key: str, *,
                       context: str) -> tuple[str, ...]:
  """A YAML value coerced the way pydantic's `tuple[str, ...]` (no extra
  validator) does: omitted -> `()`; a `list` of `str` elements -> that
  tuple (elements MAY be empty strings — `pk`/`identity` carry no
  non-empty validator upstream, confirmed empirically); anything else
  (a bare scalar — pydantic never treats `str` as an implicit sequence
  of its own characters — a mapping, or an explicit `null`) is rejected.
  """
  if key not in spec:
    return ()
  value = spec[key]
  if not isinstance(value, list):
    raise RelationshipError(f"{context}.{key}: expected a list of strings, got "
                            f"{type(value).__name__}")
  for item in value:
    if not isinstance(item, str):
      raise RelationshipError(
          f"{context}.{key}: expected a list of strings, got a "
          f"{type(item).__name__} element")
  return tuple(value)


def _require_str_list(spec: Mapping[str, Any], key: str, *,
                      context: str) -> tuple[str, ...]:
  if key not in spec:
    raise RelationshipError(f"{context}.{key}: field required")
  return _optional_str_list(spec, key, context=context)


def _edge_str_list(spec: Mapping[str, Any], key: str, *,
                   context: str) -> tuple[str, ...]:
  """`cols`/`ref_cols`: required, and non-empty strings only — the extra
  validator `FkEdge` carries beyond the plain `tuple[str, ...]` coercion.
  """
  cols = _require_str_list(spec, key, context=context)
  if not cols or any(not c for c in cols):
    raise RelationshipError(
        f"{context}.{key}: FK column lists must be non-empty strings")
  return cols


def parse_model(text: str, source: str = "") -> RelModel:
  """One YAML document -> a validated `RelModel`.

  Mirrors the shape `sdfb_core.contracts.relationships
  .parse_relationship_model` enforces: unknown keys rejected at every
  level (model/table/edge), `model` a required non-empty-type (but
  possibly empty-VALUE, e.g. `model: ''`) string, booleans read the way
  pydantic's lax mode reads them, list fields require an actual list of
  strings (a bare scalar or an explicit `null` is rejected even where the
  field has a default), a table may not reference itself, `cols`/
  `ref_cols` must have equal, non-zero arity, an internal `ref` must name
  a table in this model, and one `fk:` entry per `(cols, ref, ref_cols)`
  — all without pydantic itself.
  """
  try:
    raw = yaml.safe_load(text) or {}
  except yaml.YAMLError as exc:
    raise RelationshipError(f"{source}: not valid YAML — {exc}") from exc
  if not isinstance(raw, Mapping):
    raise RelationshipError(
        f"{source}: expected a mapping at the top level, got "
        f"{type(raw).__name__}")
  _reject_unknown(raw, _MODEL_KEYS, source)
  model_name = _require_str(raw, "model", context=source)
  _optional_str(raw, "description", default="", context=source)
  if "tables" in raw and not isinstance(raw["tables"], Mapping):
    # Hoisted local: an inline `raw['tables']` would nest the same quote
    # character the py3.14 pylint gate treats as inconsistent (W1405).
    tables_type = type(raw["tables"]).__name__
    raise RelationshipError(
        f"{source}.tables: expected a mapping, got {tables_type}")
  tables_raw: Mapping[str, Any] = raw.get("tables") or {}
  tables: dict[str, TableRel] = {}
  for name, table_spec in tables_raw.items():
    if not isinstance(name, str):
      # `tables: dict[str, ...]` upstream: pydantic does not coerce a
      # YAML int/bool/null/float key to a string.
      raise RelationshipError(f"{source}.tables: table name {name!r} is a "
                              f"{type(name).__name__}, expected a string")
    table_context = f"{source}: {name}"
    if not isinstance(table_spec, Mapping):
      raise RelationshipError(f"{table_context}: expected a mapping, got "
                              f"{type(table_spec).__name__}")
    _reject_unknown(table_spec, _TABLE_KEYS, table_context)
    pk = _optional_str_list(table_spec, "pk", context=table_context)
    identity = _optional_str_list(table_spec, "identity", context=table_context)
    if "fk" in table_spec and not isinstance(table_spec["fk"], list):
      fk_type = type(table_spec["fk"]).__name__
      raise RelationshipError(f"{table_context}.fk: expected a list, got "
                              f"{fk_type}")
    fk_raw = table_spec.get("fk") or []
    fk = tuple(_parse_edge(source, name, e) for e in fk_raw)
    enabled = _optional_bool(
        table_spec, "enabled", default=True, context=table_context)
    _optional_str(table_spec, "note", default="", context=table_context)
    tables[name] = TableRel(
        name=name, pk=pk, identity=identity, fk=fk, enabled=enabled)
  model = RelModel(model=model_name, tables=tables, source=source)
  _validate_refs(model)
  return model


def _parse_edge(source: str, table: str, spec: Any) -> Edge:
  context = f"{source}: {table}.fk"
  if not isinstance(spec, Mapping):
    raise RelationshipError(
        f"{context}: expected a mapping, got {type(spec).__name__}")
  _reject_unknown(spec, _EDGE_KEYS, context)
  cols = _edge_str_list(spec, "cols", context=context)
  ref = _require_str(spec, "ref", context=context)
  ref_cols = _edge_str_list(spec, "ref_cols", context=context)
  enforced = _optional_bool(spec, "enforced", default=True, context=context)
  drives = _optional_bool(spec, "drives", default=False, context=context)
  # "note" is validated (type only) but not retained — this mirror has no
  # card/mermaid renderer that would ever read it back.
  _optional_str(spec, "note", default="", context=context)
  return Edge(
      cols=cols, ref=ref, ref_cols=ref_cols, enforced=enforced, drives=drives)


def _validate_refs(model: RelModel) -> None:
  known = set(model.tables)
  for table, relations in model.tables.items():
    seen: set[tuple[tuple[str, ...], str, tuple[str, ...]]] = set()
    for edge in relations.fk:
      key = (edge.cols, edge.ref, edge.ref_cols)
      if key in seen:
        raise RelationshipError(
            f"{model.source}: {table}: edge {edge.label(table)} declared "
            f"twice — one fk entry per (cols, ref, ref_cols)")
      seen.add(key)
      if edge.ref == table or _name(edge.ref) == table:
        raise RelationshipError(f"{model.source}: {table} references itself "
                                f"({edge.ref}) — an FK edge needs two tables")
      if len(edge.cols) != len(edge.ref_cols):
        raise RelationshipError(f"{model.source}: {table} FK arity mismatch — "
                                f"{list(edge.cols)} vs {list(edge.ref_cols)}")
      if not edge.external and edge.ref not in known:
        raise RelationshipError(
            f"{model.source}: {table}'s fk.ref {edge.ref!r} does not name "
            f"a table in model {model.model!r} ({sorted(known)}). Qualify "
            f"it as dataset.table if the parent lives outside this model.")


def is_sample_model(path: str) -> bool:
  """True for a documentation sample (`example_*`, `*.example.*`,
  `*_example.*`) — a VERBATIM port of
  `sdfb_beam.io.relationships.is_sample_model`, so a directory scan on
  both sides skips exactly the same files.
  """
  name = path.rsplit("/", 1)[-1]
  stem = name.rsplit(".", 1)[0]
  return (name.startswith("example_") or ".example." in name or
          stem.endswith("_example"))


def _default_lister(pattern: str) -> list[str]:
  """Local or `gs://` paths matching one glob `pattern` (one level, no
  recursion — the `*` never crosses `/`, matching the ADR 0032 loader).

  `glob.glob` hides dotfiles behind a bare `*` by default; Beam's
  `FileSystems.match` does not, so `include_hidden=True` keeps the two
  branches agreeing on what `*.yaml` means.
  """
  if pattern.startswith("gs://"):
    # Lazy import: apache_beam is a real dependency of this package, but
    # only a gs:// URI needs it, and a local-only caller (every test in
    # this package) never pays for importing it.
    from apache_beam.io.filesystems import FileSystems  # pylint: disable=import-outside-toplevel

    return sorted(
        metadata.path
        for match in FileSystems.match([pattern])
        for metadata in match.metadata_list)
  return sorted(_glob.glob(pattern, include_hidden=True))


def _default_reader(path: str) -> str:
  """One local or `gs://` file's text content."""
  if path.startswith("gs://"):
    from apache_beam.io.filesystems import FileSystems  # pylint: disable=import-outside-toplevel

    with FileSystems.open(path) as handle:
      return handle.read().decode("utf-8")
  return Path(path).read_text(encoding="utf-8")


def from_sources(sources: Sequence[tuple[str, str]]) -> tuple[RelModel, ...]:
  """`[(source, yaml text), ...]` -> validated models, cross-checked.

  Mirrors `RelationshipRegistry.from_sources`: each source is parsed
  independently (`parse_model`), then two graph-wide checks run before
  the tuple is returned. A table may be declared in only one model — two
  DIFFERENT sources claiming it is an error even when they happen to
  share the same `model:` name, since "one source of truth" is about the
  file, not the label. And every table's own component must have a
  valid (acyclic) `generation_order` over enforced edges, so an FK cycle
  is caught here, at load time, rather than lazily on first use.
  """
  models = tuple(parse_model(text, source=source) for source, text in sources)
  owners: dict[str, list[str]] = {}
  for model in models:
    for table in model.tables:
      owners.setdefault(table, []).append(model.model)
  clashes = {t: ms for t, ms in owners.items() if len(ms) > 1}
  if clashes:
    detail = "; ".join(
        f"{t} in {sorted(set(ms))}" for t, ms in sorted(clashes.items()))
    raise RelationshipError(
        f"table declared in 2 models or more — one source of truth per "
        f"table: {detail}")
  for model in models:
    for table in model.tables:
      generation_order(models, component(models, table))
  return models


def load_models(
    uri: str,
    *,
    reader: Callable[[str], str] | None = None,
    lister: Callable[[str], list[str]] | None = None,
) -> tuple[RelModel, ...]:
  """Every model file under `uri` — local path or `gs://`, one level,
  `.yaml`/`.yml` only — parsed, validated, and cross-checked
  (`from_sources`).

  Mirrors `sdfb_beam.io.relationships.load_relationship_registry`'s file
  discovery: `uri=""` means relationships are off (`()`); a URI ending in
  `.yaml`/`.yml` loads that one file directly, sample or not; anything
  else is a one-level directory scan that skips documentation samples
  (`is_sample_model`). Unlike the original, this mirror has no "packaged
  default directory" concept, so ANY non-empty `uri` that resolves to no
  files — an explicit directory with nothing in it, or a direct file
  that does not exist — is an error, never a silently empty `()`; only
  `uri=""` itself means "off". `reader`/`lister` are injectable so a
  test never touches a real filesystem or GCS; by default `gs://` reads
  go through `apache_beam.io.filesystems.FileSystems` (imported lazily)
  and anything else through the local filesystem.
  """
  if not uri:
    return ()
  reader = reader or _default_reader
  lister = lister or _default_lister
  direct = uri.rstrip("/").endswith(_SUFFIXES)
  if direct:
    patterns = [uri]
  else:
    base = uri.rstrip("/")
    patterns = [f"{base}/*{suffix}" for suffix in _SUFFIXES]
  paths: list[str] = []
  for pattern in patterns:
    paths.extend(lister(pattern))
  candidates = sorted(set(paths))
  if not direct:
    candidates = [p for p in candidates if not is_sample_model(p)]
  if not candidates:
    raise RelationshipError(
        f"{uri}: no model files there (checked {patterns}) — an explicit "
        f"relationships URI must resolve to at least one .yaml/.yml file")
  return from_sources([(p, reader(p)) for p in candidates])


def model_for(models: Sequence[RelModel], table: str) -> RelModel | None:
  """The model that declares `table` (bare name or FQN), or `None`."""
  key = _name(table)
  for model in models:
    if key in model.tables:
      return model
  return None


def _relations(models: Sequence[RelModel], table: str) -> TableRel | None:
  model = model_for(models, table)
  return model.tables[_name(table)] if model else None


def _enabled(models: Sequence[RelModel], table: str) -> bool:
  relations = _relations(models, table)
  return True if relations is None else relations.enabled


def _raw_enforced(models: Sequence[RelModel], table: str) -> tuple[Edge, ...]:
  """Enforced edges exactly as declared: both ends enabled."""
  relations = _relations(models, table)
  if relations is None or not relations.enabled:
    return ()
  return tuple(
      edge for edge in relations.fk
      if edge.enforced and (edge.external or _enabled(models, edge.ref)))


def _widenings(models: Sequence[RelModel]) -> list[dict[str, Any]]:
  """Column pairs a parent's edge to a grandparent must carry, pinned by
  a child that references the SAME columns in both (the model asserts
  the correspondence). Ports `RelationshipRegistry._widenings`
  (ADR 0036 rev 2).
  """
  records: list[dict[str, Any]] = []
  for model in models:
    for child, relations in model.tables.items():
      if not relations.enabled:
        continue
      edges = [e for e in _raw_enforced(models, child) if not e.external]
      for i, e1 in enumerate(edges):
        for e2 in edges[i + 1:]:
          if e1.cols != e2.cols or e1.ref == e2.ref:
            continue
          for near, far in ((e1, e2), (e2, e1)):
            # `near.ref` must itself hold a direct edge to `far.ref`.
            for up in _raw_enforced(models, near.ref):
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
                    "added": added,
                })
  return records


def derived_widenings(models: Sequence[RelModel]) -> list[dict[str, Any]]:
  """`[{"table", "ref", "via", "added": [(col, ref_col), ...]}, ...]` —
  every edge widened relative to what its model file declares."""
  return _widenings(models)


def widened(models: Sequence[RelModel], table: str, edge: Edge) -> Edge:
  """`edge` as a launch actually draws it: widened with the column pairs
  the model's own children pin (`derived_widenings`), or `edge` unchanged
  when nothing widens it. A documented edge (`enforced=False`) is
  returned as-is — it is never drawn, so it keeps its declared columns.
  """
  if not edge.enforced:
    return edge
  added = [
      pair for rec in _widenings(models)
      if rec["table"] == _name(table) and rec["ref"] == edge.ref
      for pair in rec["added"] if pair[0] not in edge.cols
  ]
  if not added:
    return edge
  return Edge(
      cols=edge.cols + tuple(c for c, _ in added),
      ref=edge.ref,
      ref_cols=edge.ref_cols + tuple(r for _, r in added),
      enforced=edge.enforced,
      drives=edge.drives,
  )


def enforced_edges(models: Sequence[RelModel], table: str) -> tuple[Edge, ...]:
  """Edges `table` actually draws keys from: enforced, both ends
  enabled, and WIDENED with the column pairs the model's own children pin
  (`derived_widenings`) — mirrors `RelationshipRegistry.enforced_edges`.
  """
  return tuple(
      widened(models, table, edge) for edge in _raw_enforced(models, table))


def _adjacency(models: Sequence[RelModel]) -> dict[str, set[str]]:
  """Undirected neighbours over ENABLED tables only. Documented edges
  (`enforced=False`) count here: they still say "these tables belong
  together"."""
  adjacent: dict[str, set[str]] = {}
  for model in models:
    for table, relations in model.tables.items():
      adjacent.setdefault(table, set())
      if not relations.enabled:
        continue
      for edge in relations.fk:
        if edge.external or not _enabled(models, edge.ref):
          continue
        adjacent[table].add(edge.ref)
        adjacent.setdefault(edge.ref, set()).add(table)
  return adjacent


def component(models: Sequence[RelModel], table: str) -> tuple[str, ...]:
  """`table` plus every table still reachable from it.

  A disabled table is not traversed, so anything that reached the model
  only through it is no longer part of this component. A disabled TARGET
  is returned alone — the flag detaches, it does not forbid.
  """
  key = _name(table)
  if _relations(models, key) is None:
    return (key,)
  adjacent = _adjacency(models)
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
  ordered = [t for m in models for t in m.tables if t in seen]
  return tuple(ordered)


def generation_waves(
    models: Sequence[RelModel],
    tables: tuple[str, ...],
) -> tuple[tuple[str, ...], ...]:
  """Parents-first WAVES over ENFORCED edges, stable in model order.

  Every table inside a wave is independent of the others; the waves
  themselves stay ordered — a child never precedes its parent.
  """
  members = {_name(t) for t in tables}
  parents = {
      t: {
          edge.ref
          for edge in enforced_edges(models, t)
          if not edge.external and edge.ref in members
      } for t in members
  }
  order_hint = [t for m in models for t in m.tables if t in members]
  order_hint += [t for t in sorted(members) if t not in order_hint]
  waves: list[tuple[str, ...]] = []
  placed: set[str] = set()
  remaining = dict(parents)
  while remaining:
    ready = tuple(
        t for t in order_hint if t in remaining and remaining[t] <= placed)
    if not ready:
      raise RelationshipError(
          f"FK cycle among {sorted(remaining)} — enforced edges must form "
          f"a DAG (a child cannot be its own ancestor)")
    for table in ready:
      del remaining[table]
    placed.update(ready)
    waves.append(ready)
  return tuple(waves)


def generation_order(
    models: Sequence[RelModel],
    tables: tuple[str, ...],
) -> tuple[str, ...]:
  """Parents first — `generation_waves` flattened."""
  return tuple(t for wave in generation_waves(models, tables) for t in wave)


def _edge_canon(edge: Edge) -> str:
  """One edge's slice of `sha12`'s canonical string."""
  # Hoisted locals: an inline `','.join(...)` would nest the same quote
  # character the py3.14 pylint gate treats as inconsistent (W1405).
  cols = ",".join(edge.cols)
  ref_cols = ",".join(edge.ref_cols)
  return f"{cols}->{edge.ref}:{ref_cols}:{int(edge.enforced)}:{int(edge.drives)}"


def _table_canon(model_name: str, table: str, relations: TableRel) -> str:
  """One table's slice of `sha12`'s canonical string — the EXACT format
  `RelationshipRegistry.sha12` builds for it."""
  pk = ",".join(relations.pk)
  identity = ",".join(relations.identity)
  edges = "|".join(_edge_canon(edge) for edge in relations.fk)
  return f"{model_name}:{table}:{pk}:{identity}:{int(relations.enabled)}:{edges}"


def sha12(models: Sequence[RelModel]) -> str:
  """Content hash of `models` — the EXACT canonical string
  `RelationshipRegistry.sha12` builds, hashed the same way
  (`sdfb_core.observability.sha12`: first 12 hex chars of the sha256),
  so identical model content gives the identical hash on both sides of
  the parity test.
  """
  canon = ";".join(
      sorted(
          _table_canon(m.model, t, r)
          for m in models
          for t, r in m.tables.items()))
  return hashlib.sha256(canon.encode()).hexdigest()[:12]
