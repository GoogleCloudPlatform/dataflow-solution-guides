# ADR 0002 — Gemma 4 family as the M1 model shortlist

- **Status**: accepted (2026-05-18, supersedes the pre-2026-04-02 Gemma 2 / Phi-3 / Qwen 2.5 shortlist)

## Context

The brief originally suggested Gemma 2 / Qwen 2.5 / Phi-3 Mini. On 2026-04-02 Google released Gemma 4 under Apache 2.0 with materially different architecture: native JSON-schema function calling trained into pretraining (not patched), 256K context on the larger variants, MoE option (26B A4B activating ~3.8B/token) sized to fit on a single L4 24GB at Q4-AWQ, and Multi-Token Prediction speculative decoding built in.

The user's pain point — incomplete fields, broken timestamps, repetitive rows from Gemma-2-2B raw prompting — is exactly what guided JSON + native function-calling targets. Gemma 4 obsoletes the prior shortlist on every axis that matters for structured tabular synthesis.

## Decision

M1 production target is **Gemma 4 26B A4B MoE, Q4-AWQ** (primary). Dev / cost-floor is **Gemma 4 E4B**. Optional cross-family check: **Qwen 2.5 7B Instruct** (Apache-2.0). Weights pulled from **Kaggle** (Google-hosted, license-clean), staged to `gs://{project}-models/{family}/{model}/{version}/`. No HuggingFace Hub at runtime (`HF_HUB_OFFLINE=1`).

Dropped from the shortlist: Gemma 2 family (superseded), Phi-3 Mini (superseded by Gemma 4 E4B), Gemma 4 31B Dense + Llama 70B (no headroom on single L4 — A100 lane is M3+).

> **Amendment (2026-05-21):** the GCS layout convention above evolved to `gs://{bucket}/synthetic/models/{family}/{model}/{version}/` (shared bucket + `synthetic/` app-prefix) to match the deploy workflows; the `{project}-models` path is superseded. Also: the dev model is the **`-it`** (instruction-tuned) variant — the base ships no chat template. The model *selection* (this ADR's decision) is unchanged; see `docs/MODEL_LAYOUT.md` + `config/models.yml` for the current layout.

## Consequences

- **Enables**: schema-conformant generation with much less prompt engineering; the 256K context unlocks RAG-with-many-exemplars for B.1.
- **Costs**: dependency on Kaggle for the Gemma 4 download path (no Hub fallback); user must accept the Gemma license once per model.
- **Forbids**: silently bumping to Hub-only models. New model entries land in `config/models.yml` with their GCS URI, license, and download hint.

## Related

- `docs/MODEL_LAYOUT.md` for storage layout and the Kaggle download procedure.
- `config/models.yml` for the registry of models with their vLLM args.
