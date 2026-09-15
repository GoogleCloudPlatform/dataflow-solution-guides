# ADR 0024 — Structured prompt-constraint templates in column descriptions

- **Status:** Accepted (2026-08-10)
- **Context bundle:** [wave-2 design doc](../designs/2026-08-10-prompt-constraints.md)
  (evidence, figures, template catalog); extends
  [ADR 0021](0021-relational-contract-in-descriptions.md) and
  [ADR 0022](0022-stats-driven-generation.md); prompt-shape constraints from
  [ADR 0018](0018-parallel-batched-freetext-pools.md) (prefix caching) and
  [ADR 0023](0023-source-domain-pool-rejection.md) (privacy rejection sets)
  are unchanged.

## Context

The 2026-08-09 WS8 R1 baselines (jobs `…00_19_04-7880…`, `…00_31_21-1718…`)
were memorization-clean but showed 0 % shape recall on the highest-value
free-text columns and a pool-cap diversity ceiling on 7 more. The per-column
steering available to a DDL author was a single free-prose string
(`llm_prompt_constraint`, ADR 0021) — no structured way to state a format,
pattern, closed vocabulary, affix, or length; no way to route a
mis-classified column to the LLM; and no way to inspect the prompt a run
actually used (reference values are banned from logs).

## Decision

1. The `llm_prompt_constraint` description marker now accepts an **object**
   as well as the legacy string. `contracts/prompt_constraint.py` owns the
   typed model (`PromptConstraint`: `format`, `pattern`, `examples`,
   `values`, `prefix`, `suffix`, `charset`, `length`, `units`, `locale`,
   `route`, `notes`), the single parse site, and a deterministic compact
   renderer. The legacy string parses as `notes` and renders byte-identically
   to today. Unknown keys warn (`prompt_constraint_unknown_keys`) and are
   skipped — forward and backward compatible by construction.
2. The rendered clause stays a **per-column constant suffix** after the
   shared instruction prefix, preserving
   [vLLM automatic prefix caching](https://docs.vllm.ai/en/latest/features/automatic_prefix_caching.html)
   (ADR 0018). A user `pattern` also drives **guided decoding**
   (`items.pattern`), because prompting alone does not guarantee format
   adherence — grammar-constrained decoding does
   ([Willard & Louf 2023](https://arxiv.org/abs/2307.09702);
   [vLLM structured outputs](https://docs.vllm.ai/en/latest/features/structured_outputs.html)).
   Schema/constraint-aware conditioning is the established practice in
   LLM-based tabular synthesis
   ([Borisov et al. 2023, GReaT](https://arxiv.org/abs/2210.06280)).
3. `route: "llm"` overrides a STRING column's typed classification
   (constant/categorical/temporal/identifier) into the LLM free-text route,
   carrying its rendered constraint. Non-STRING types keep their typed route
   and log `prompt_constraint_route_unsupported` (numeric fidelity is owned
   by the B.2 inverse-CDF acceptance path, ADR 0022).
4. A `--prompt_debug off|redacted|full` flag logs the built pool prompt as a
   `freetext_pool_prompt` milestone. `redacted` (the debug default) elides
   seed exemplars (`<k seeds elided>`) so the privacy rule "reference values
   never reach logs" holds; `full` is an explicit opt-in that logs verbatim
   prompts and carries a WARNING banner. A `sha12` hash supports prompt-drift
   comparison without verbatim text in both logging modes; `off` (the
   default) logs nothing at all — zero log surface, per the design doc §3c.
   *(Erratum 2026-08-19: this sentence originally claimed the `sha12` logs
   even at `off`, which the implementation never did.)*

## Alternatives rejected

- **Keep free prose only** — cannot express whitespace-exact or positional
  formats unambiguously (the COL_038 triple-space class), cannot feed guided
  decoding, and invites unbounded prose that burns prompt tokens.
- **A separate sidecar config file for constraints** — the DDL JSON
  (`field.description`) is already the single contract surface (ADR 0021);
  a second surface would drift from it.
- **Per-row LLM prompting with constraints** (GReaT-style row serialization)
  — violates the pooled-generation design (ADR 0013/0018): per-row LLM calls
  at 1M rows are cost-prohibitive on L4/T4; constraints instead steer the
  bounded pool + template/expansion machinery.
- **Fine-tuning the model per table** — out of scope for M1 self-hosted
  weights (no training loop in the serving path).

## Consequences

- DDL authors get a documented, versionable template catalog (design doc §3)
  usable directly in `field.description`; malformed marked objects still fail
  loudly (ADR 0021 parsing rule). Copy-paste-ready Terraform + `_ddl.json`
  worked examples: [`docs/DDL_CONTRACT_GUIDE.md`](../DDL_CONTRACT_GUIDE.md).
- Both engines consume constraints through one parse site; engine drift is
  structurally impossible.
- `prompt_debug=full` deliberately leaks reference exemplars into Dataflow
  logs; it is opt-in, WARNING-bannered, and documented for short-lived debug
  runs only.
- The wave ships with six sampler/gate/tooling fixes traced from the R1
  evidence (mask-table identifier sampling, literal-space expansion,
  collapsed-mask candidate gate, copy-fraction epsilon, crosscheck
  non-empty top-up sampling, categorical sparsity-mass pinning) — see
  design doc §4; acceptance is the next R-series run per design doc §5.
