# ADR 0010 — M4 local smoke test via MLX, not DirectRunner-with-Docker

- **Status**: accepted (2026-05-19)

## Context

We want a fast iteration loop on the M4 against a real LLM, without burning Dataflow time on every prompt change. Three candidate approaches:

1. **DirectRunner with the production GPU Docker image** — would replicate production. But DirectRunner runs in the local Python process, not a container; and the production image is CUDA-based, which Apple Silicon cannot execute. Not viable.

2. **vLLM-on-M4 (CPU mode or Metal)** — vLLM officially targets CUDA. CPU mode exists but is impractically slow; Metal support is experimental at best. Not viable for daily iteration.

3. **MLX-backed `ModelClient`** — Apple's framework, fast on M-series via Metal, reads HF-layout safetensors directly. Same `ModelClient` Protocol as `VLLMModelClient`, so the engine and the rest of the pipeline don't know which one is in use.

## Decision

The M4 local smoke loop uses **`MLXModelClient`** (`packages/sdfb-beam/src/sdfb_beam/handlers/mlx_client.py`) backed by `mlx-lm`. Three layers of validation:

- **L1**: pure-Python tests (no LLM, `FakeModelClient`).
- **L2**: `scripts/hello_synthetic_mlx.py` — minimal real-LLM smoke, no Beam.
- **L3**: `sdfb_beam.cli.run_pipeline --runner DirectRunner --client_type mlx` — full DAG with real LLM, no Docker.

`mlx-lm` is gated behind a `[mlx]` extra in `packages/sdfb-beam/pyproject.toml` with `sys_platform == 'darwin' and platform_machine == 'arm64'` markers — no-op on Linux CI runners.

## Consequences

- **Enables**: ~30-second iteration loop on M4 with real LLM output, validates the engine + Pydantic + Pandera chain against actual model behavior (not just `FakeModelClient`'s deterministic stubs).
- **Costs**: model output quality on M4 (E4B 4.5B) is lower than the production target (26B-A4B MoE) — schema conformance will be worse, throughput lower, fidelity weaker. Smoke results are directional, not authoritative.
- **Forbids**: claiming production parity from M4 smoke results. The GPU image and vLLM behavior can only be validated on Dataflow (a CI-built image + a real GPU run, e.g. a `public_cloud/deploy/gcp/run_e2e.sh` tier).

## Related

- `docs/M4_LOCAL_SMOKE.md` — operational runbook.
- `docs/MODEL_LAYOUT.md` — weight layout (MLX reads the same HF layout vLLM expects).
- [ADR 0006](0006-generation-engine-abc.md) — `ModelClient` Protocol that makes vLLM ↔ MLX swappable.
- [ADR 0008](0008-ci-driven-builds.md) — why no local image build, even on M4.
