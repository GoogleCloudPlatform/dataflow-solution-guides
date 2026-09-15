# ADR 0032 — Relationships are config, not table descriptions

**Status:** ACCEPTED (2026-08-24) — DirectRunner-proven (TDD, 1300+ tests); first Dataflow launch on the new source is the acceptance gate
**Design:** [`docs/designs/2026-08-24-relationships-as-config.md`](../designs/2026-08-24-relationships-as-config.md)
**User guide:** [`config/relationships/README.md`](../../config/relationships/README.md)
**Supersedes:** [ADR 0021](0021-relational-contract-in-descriptions.md) entirely (the `{"sdfb": 1, …}` table-description contract is removed, not deprecated) · ADR 0029 D3 (`informational: true` → `enforced: false` in the model file)
**Keeps:** [ADR 0024](0024-structured-prompt-constraint-templates.md) — per-column `llm_prompt_constraint` stays in COLUMN descriptions

## Context

ADR 0021 put the relational contract (`pk`, `fk`, `identity`) inside each
BigQuery **table description**, because the Terraform module in use
exposed no primary-key block and BigQuery constraints are unenforced
metadata. It worked, and three runs exposed what it costs:

- **The truth was scattered.** A model spanning N tables lived in N
  descriptions. Nothing could answer "what is related to what" without
  scanning a dataset — the launcher did exactly that, one `get_table` per
  table, every launch.
- **Every change was a production metadata write.** Detaching one table
  from a run, or fixing a typo'd `ref`, meant `bq update` /
  `terraform apply` against the live dataset — slow, privileged, and
  invisible to code review.
- **It was not configurable in the ways operators actually needed.** There
  was no way to say "generate this model, but leave that branch out today"
  short of editing production descriptions.
- **It was hard to see.** The 2026-08-23 run spent 49 GPU-minutes on a
  model whose only edge was display-only; the state was knowable at launch
  but scattered across two table descriptions.

## Decision

**D1 — `config/relationships/<model>.yaml` is the single source of truth.**
One file per relational model: tables as keys, each with `pk`, `identity`,
`fk`, `enabled`. It is versioned with the code that reads it, reviewed like
code, and diffable. `sdfb_core/contracts/relationships.py` owns the schema,
the loader, and every graph question (component, waves, order, card,
diagram) — one implementation, no second grapher to drift.

**D2 — table descriptions are NEVER read for relational structure.**
`parse_relational_contract`, `TableSchema.relational_contract()`, the
extractor's `relational` mirror and the launcher's dataset scan are
deleted, not deprecated. A legacy `{"sdfb": 1, …}` object left in a
description is inert, and a test pins that it stays inert. Column-level
`llm_prompt_constraint` is untouched: it steers ONE field's generation and
belongs next to that field.

**D3 — two flags, two jobs.** `enabled: false` on a table DETACHES it: it
leaves the graph, and anything that reached the model only through it
detaches with it — one flag prunes a branch without deleting a line. It
never blocks generating that table directly; the flag governs
participation, not permission. `enforced: false` on an edge documents a
relationship without drawing keys from it (this replaces ADR 0029's
`informational: true`, in the positive sense the enable/disable vocabulary
already uses).

**D4 — delivery is a folder, overridable by URI.** `--relationships_uri`
defaults to `config/relationships`, which ships inside the flex-template
image (already `COPY`d, like `config/thresholds.yml`). Pointing it at a
`gs://` path overrides the packaged models for one launch — a relationship
change becomes a file upload, with no image rebuild and no production
metadata edit. Declared in the flex-template metadata and the Composer DAG,
because an undeclared param is rejected at launch (the 1ea4516 lesson).
The match is one level deep (`<uri>/*.yaml`; Beam's `*` does not cross
`/`). Absence is legitimate ONLY at the packaged default: an explicit URI
that cannot be listed, or that holds no models, is a loud stop — a typo'd
bucket must never read as "no relationships declared", which would
generate every table alone and still report PASSED.

**D5 — the model is the source of truth for keys; the CLI fills gaps.**
A table the model declares takes its `pk`/`identity` from it, and a
conflicting `--pk_cols` is IGNORED with a WARNING. A table no model
declares generates alone with the CLI flags — dozens of unrelated one-off
tables need no config at all.

**D6 — one card, both logs.** The launcher renders a `relationship_model`
card (model name, source file, sha, tables with PK/identity, every edge
with `-->` enforced / `..>` documented / `[DISABLED — detached]`, and the
generation waves), then carries it verbatim to the workers via
`GenerationContext.relationship_card`. The model is resolved ONCE,
driver-side; driver and worker logs cannot disagree. Fenced mermaid rides
below the card for report recycling by sha.

**D7 — the models are checked before they cost anything.** The loader
validates refs, arity, single ownership and cycles at load; preflight P2
checks every declared column against the real schema (a YAML typo stops the
launch, not the GPU); `deployment_prerequisites.py` step 12 reports model
tables that do not exist yet.

**D8 — committed models are alias-only.** `config/relationships/` is
gitignored apart from `README.md` and `example_*.yaml`, so a real production
model dropped in the folder ships in that clone's image build and can never
be committed. The `gs://` override keeps real names off the filesystem
entirely.

## Alternatives considered

- **Keep descriptions, add a config overlay.** Two sources of truth, with
  precedence rules — exactly the ambiguity this removes.
- **One big `relationships.yaml`.** Simple, but a model per file is what
  makes ownership, review and `enabled` toggles readable at a glance.
- **JSON instead of YAML.** No comments, and the file's job is to be read
  by a human deciding what a run will do. `config/thresholds.yml` set the
  precedent.
- **A BigQuery table holding the model.** Another live dependency to read,
  no code review, no diff — the problem restated.
- **Keeping `informational` as the flag name.** Two vocabularies
  (informational vs enabled) for one mental model; `enforced: false` reads
  as the negation of what it disables.

## Consequences

- Relationship changes are code review, `git diff` and a file upload —
  never a `terraform apply` against production metadata.
- The launcher no longer scans the landing dataset (N `get_table` calls per
  launch) to discover contracts; it reads one folder.
- `scripts/relationships/card.py` renders exactly what a launch would plan,
  offline, so an edit is checked in seconds.
- `run_tableset.py` plans waves from the same registry, so its dry run and
  the pipeline can no longer disagree about what is related.
- **Migration is manual and explicit**: existing `{"sdfb": 1, …}` objects in
  table descriptions must be transcribed into a model file. Nothing reads
  them any more, so a forgotten one silently loses its PK/FK — the
  relationship card (`model=none`) and preflight are where that shows up.
- ADR 0021's rationale is history: it explains why the contract ever lived
  in descriptions, which is still the right context for reading this one.
