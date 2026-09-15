"""`ModelClient` / `ModelHandler` implementations.

Production (Linux + L4 + CUDA):
  - `vllm_client.py` — vLLM-backed (stub today; real impl in M1 §9).

M4 local smoke (Apple Silicon):
  - `mlx_client.py` — `mlx-lm`-backed (gated by `[mlx]` extra).

Test / development:
  - `fake_client.py` — deterministic `ModelClient` for CI and DirectRunner.

All three satisfy the `ModelClient` Protocol; engines never know which one
is in use. See ADR 0006 and ADR 0010.
"""

from sdfb_beam.handlers.fake_client import FakeModelClient

# vllm_client and mlx_client are intentionally NOT imported at package level —
# they have heavy / platform-specific runtime deps. Import them only at the
# call site (or via the factory in `sdfb_beam.cli.run_pipeline`).

__all__ = ["FakeModelClient"]
