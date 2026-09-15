# ADR 0007 — DRY across documentation and code

- **Status**: accepted (2026-05-19)

## Context

The repo carries three layers of documentation (`CLAUDE.md`, `docs/*.md`, `.claude/skills/*.md`) plus per-session memory and code-level docstrings. As content gets added, the same information naturally appears in multiple places (e.g. the GPU image layout in `MODEL_LAYOUT.md` *and* `GPU_CONTAINER.md` *and* a skill recipe). Each copy is a chance for drift. The user flagged this explicitly on 2026-05-19.

## Decision

Single source of truth per concern, links between docs/code, never re-stating. Applies to:

- **Documentation** — `M4_SETUP.md` carries the "Cross-doc map" that points at the authoritative home of each concern. Other docs link there, don't restate.
- **Skills** — recipes link to the production code; production code stays as the source of truth for "how this is implemented today."
- **Code** — type maps live once (`sdfb_core/codegen/types.py`); pinned versions in one place (Dockerfile *or* `pyproject.toml`, not both); engine names register through `ENGINE_REGISTRY`; config values flow through `config/*.yml`, not duplicated into scripts.

When duplication is unavoidable (a value must appear in both a Dockerfile and a Python script), a comment in *both* points at the canonical source so the next editor updates both.

## Consequences

- **Enables**: future edits don't have to find every parallel copy of a fact. New docs link instead of re-stating. Reviewers can spot DRY violations easily.
- **Costs**: more inter-doc navigation. Reading a single doc gives less of the full picture than a self-contained one would; the Cross-doc map exists to mitigate.
- **Forbids**: composing docs by copy-paste. When the same content seems needed twice, link or table-row it. When two docs cover overlapping ground, one wins and the other defers.

## Related

- Live example: `docs/M4_SETUP.md` defers to `docs/MODEL_LAYOUT.md` and the ADRs instead of restating.
