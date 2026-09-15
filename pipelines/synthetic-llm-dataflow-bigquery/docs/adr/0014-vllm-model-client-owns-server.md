# ADR 0014 — `VLLMModelClient` owns the vLLM OpenAI server (amends ADR 0011)

- **Status**: accepted (2026-05-21) — amends [ADR 0011](0011-adopt-beam-vllm-model-handler.md)

## Context

ADR 0011 chose Beam's `apache_beam.ml.inference.vllm_inference.VLLMCompletionsModelHandler` for the §9 serving path. That handler is a **`RunInference` component** — it's driven by a Beam `PTransform` over a `PCollection`.

But the engines (B.1/B.2, ADR 0013) do **not** call the LLM via RunInference. They call `model_client.generate_json(prompt, json_schema, …)` **synchronously**, inside `generate_batch()`, inside the DoFn — and only O(1) times (free-text pools / distribution inference), not per row. There is no PCollection of prompts to run inference over. So a RunInference handler is the wrong shape for the `ModelClient` Protocol.

Two further constraints landed after ADR 0011:
- Gemma 4 IT emits a chain-of-thought channel that must be suppressed via the **chat** template (`chat_template_kwargs={"enable_thinking": False}`) — the completions endpoint doesn't apply the chat template (see ADR 0013).
- Gemma 4 needs vLLM ≥ 0.21 + transformers ≥ 5.5.0 (pinned in `packages/sdfb-beam/pyproject.toml`); weights are pulled via the `google-cloud-storage` client, not gcloud.

## Decision

`VLLMModelClient` (`packages/sdfb-beam/src/sdfb_beam/handlers/vllm_client.py`) **owns a vLLM OpenAI-compatible server directly**, instead of going through Beam's RunInference handler:

- `setup()` (once per worker): pull weights GCS→`/local-ssd/model` via the `google-cloud-storage` client; launch `python -m vllm.entrypoints.openai.api_server --model /local-ssd/model <vllm_server_kwargs>` as a subprocess (poll `/v1/models` for readiness); create an `openai` client at `http://localhost:<port>/v1`.
- `generate_json(prompt, json_schema, *, max_tokens, temperature, n, seed)`: call the **chat** endpoint — `client.chat.completions.create(messages=[{role:user, content:prompt}], …, extra_body={"guided_json": json_schema, "chat_template_kwargs": {"enable_thinking": False}, "guided_decoding_backend": "outlines"})` — and parse `choices[*].message.content` (JSON) into `list[dict]`.
- `teardown()`: terminate the server subprocess.

This is the productionized form of the (now-deleted) `vllm_spike.py` logic, fitted to the `ModelClient` Protocol. `vllm_server_kwargs` (quantization, max-model-len, gpu-memory-utilization, max-num-seqs) come from `config/models.yml`.

## Consequences

- **Enables**: the synchronous `ModelClient.generate_json` contract the engines actually use; server-side thinking suppression + guided JSON in one call; reuse of vLLM's batched OpenAI server without the RunInference wrapper.
- **Supersedes** (of ADR 0011): the choice of `VLLMCompletionsModelHandler`/RunInference. We keep ADR 0011's *substance* — vLLM's OpenAI-compatible server + guided JSON via `extra_body` — but the client manages the server itself and uses the **chat** endpoint.
- **Costs**: the client owns subprocess lifecycle (spawn/health-check/teardown) — code the Beam handler would otherwise have provided. Offline-untestable (CUDA-only); validated at §11 on L4 (unit-tested with a mocked `openai` client on the laptop).
- **Unchanged**: ADR 0013's distribution-estimator spine — the LLM is still O(1); this client serves only the free-text path.

## Related
- [ADR 0011](0011-adopt-beam-vllm-model-handler.md) (amended), [ADR 0013](0013-distribution-estimator-spine.md) (spine).
- `.claude/skills/model-handler.md`, `config/models.yml` (`vllm_server_kwargs`).
