# ADR 0006 — `GenerationEngine` ABC + `ModelClient` Protocol shape

- **Status**: accepted (2026-05-18)

## Context

Two distinct synthesis strategies coexist in M1: B.1 (RAG-driven prompting) and B.2 (tabular library fit, e.g. CTGAN / TVAE, with LLM augmentation on free-text columns). Multiple engines could share a single Beam DoFn without sharing implementation if the seam is right; the wrong seam forces the DoFn to know about every engine internally.

Additionally, the LLM call needs to be *swappable*: real vLLM on Dataflow GPU workers; deterministic fake on the laptop DirectRunner. The DoFn shouldn't care which is in use.

## Decision

Two seams:

1. **`GenerationEngine` ABC** in `sdfb_core.engines.base` — `setup(model_client, ctx)`, `generate_batch(n, cfg) → Iterator[GeneratedRecord]`, `teardown()`. Engines auto-register their string name in `ENGINE_REGISTRY` at import time; the Beam DoFn carries the name (string) and looks up the class via `get_engine(name)`.
2. **`ModelClient` Protocol** — single method `generate_json(prompt, json_schema, *, max_tokens, temperature, n, seed) → list[dict]`. Real impl: `VLLMModelClient`. Test impl: `FakeModelClient` (canned-cycle or echo-from-pool modes).

Engines never `import vllm` directly. The DoFn never directly references engine classes.

## Consequences

- **Enables**: parallel B.1 / B.2 work in separate worktrees behind one contract. The same Beam DAG runs both engines without changes. Laptop tests of every engine using `FakeModelClient` (no GPU). Future engines (e.g. diffusion-based tabular for B.3) slot in by registering their name.
- **Costs**: extra indirection. The contract's surface (3 methods + 1 Protocol) must be honored even by experimental engines — see the five contract tests in `packages/sdfb-tests/tests/unit/engines/test_abc_contract.py`.
- **Forbids**: engine implementations that bypass the Protocol (direct vLLM import) or construct heavy state inside `generate_batch` (anti-pattern: must be in `setup()`).

## Related

- ABC: `packages/sdfb-core/src/sdfb_core/engines/base.py`.
- Registry: `ENGINE_REGISTRY` in `packages/sdfb-core/src/sdfb_core/engines/__init__.py`.
- Tests: `packages/sdfb-tests/tests/unit/engines/test_abc_contract.py`.
- Skill: `.claude/skills/engine-contract.md`.
