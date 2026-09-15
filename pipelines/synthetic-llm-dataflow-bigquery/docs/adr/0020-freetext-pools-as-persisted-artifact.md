# ADR 0020 — Free-text pools are a persisted artifact, not per-worker work

**Status:** Accepted (2026-07-26) · **Amends:** [0018](0018-parallel-batched-freetext-pools.md)
**Design:** [WS5 — generation throughput & RAG seeding](../designs/2026-07-26-ws5-generation-throughput.md)
**Evidence:** `runs/2026-07-26_06_54_25-14348390798392809440/` (1M rows, 68 min, 4 L4 workers)

## Context

ADR 0018 made the free-text pool ladder batched, parallel across columns, and
cached — but the cache (`_POOL_CACHE`) is a module-level dict, so its lifetime
is **one worker process**. Autoscaling therefore multiplies the whole ladder.

Measured on the 1M-row run:

| Signal | Value |
|---|---|
| `freetext_pool_built` | 108 (36 rebuilds × 3 columns) |
| Total LLM service time | 68 805 s ≈ 19.1 GPU-hours |
| Excluding `COL_047` (known binary-char special case) | 42 698 s over 72 rebuilds ≈ 11.9 GPU-hours |
| `freetext_pool_cache_hit` | 21 of 129 attempts — **all inside one process**, t+11.8→13.8 min |
| `dofn_setup_done` | 43 entries, 28 982 s total, max 965 s |
| Last pool completed | **t+64 min of a 68-minute job** |

Workers spent essentially the whole run getting ready to generate. The
autoscale waves at t+37 and t+55 min got zero cache reuse because they were
new processes.

Two further findings from the same run:

- The CUDA OOM (`20 MiB` requested, `10.81 MiB` free, vLLM holding 13.80 of
  14.56 GiB) is the embedder loading during a `setup()` **retry**, while the
  vLLM this same process ignited for the first attempt still holds the card.
- `b1_index_built` totals **2 s** across the job. FAISS at ≤1024 vectors is
  exact brute force; the entire GPU embedder exists to pick 8 prompt seeds per
  column.

## Decision

**1. Pools become a BigQuery artifact** — `synthetic_rag.freetext_pools`,
keyed `(reference_digest, model_uri, column, target)` — produced by a
dedicated DAG branch and read by `Generate.setup()`. This mirrors what
`rag_chunks` already proved: the driver decides via an existence check whether
to attach a sink, and a `None` sink leaves the DAG byte-identical.

**2. A sibling table, not `chunk_kind='pool_value'`.** Pools key on
`model_uri` (which LLM produced the values); chunks key on `embedder_id` /
`embedder_version` (which vector space). One `fetch` signature cannot answer
both questions without lying about one of them.

**3. Resolution order is store → process cache → build.** The store is
cross-process and authoritative; `_POOL_CACHE` remains the intra-process tier
that survives `setup()` retries within one worker. The store is an
optimisation, never a dependency: no store, no digest, a store that raises, or
a persisted row with an empty `values` array all fall through to the ladder.

**4. Stagnation is recorded, not rediscovered.** `stagnated` and `attempts`
are columns. A pool that stopped yielding novel values below its target is
stored *as* stagnated, so a later worker reads the conclusion instead of
re-running the ladder to re-derive it.

**5. The build branch never reads its own output.** It blanks
`pool_store`/`freetext_pools_table` on its own context — otherwise it would
short-circuit itself and silently stop refreshing the artifact.

**6. Amendment to ADR 0018.** That ADR required a byte-identical prompt prefix
across attempts so vLLM prefix caching / LMCache could amortise the rebuilds.
At once-per-digest there is almost nothing left to amortise, so the constraint
is materially weaker. `--pool_seed_strategy=kcenter_rotate` deliberately
trades it away; `centroid` and `kcenter` still honour it, and tests pin that
distinction per arm.

## Consequences

- Up to 19.1 GPU-hours of redundant LLM service time removed from a 1M-row
  cold run; a warm run (pools already persisted) skips the ladder entirely.
- **vLLM ignition is already lazy** (WS1 §3b), so a store hit means the server
  never boots — which also removes one party from the CUDA OOM, structurally
  rather than by retry-tuning.
- `WRITE_APPEND` duplicate semantics are value-stable: re-running the build
  branch for a digest already present is skipped driver-side.
- Pools invalidate with the reference digest **or** the model URI. A new
  reference sample or a different LLM is a different pool, by construction.
- New operational surface: `--build_pool_layer` + `--freetext_pools_table`,
  and one BigQuery table to provision (never auto-created — the
  blast-radius rule confines `CREATE_IF_NEEDED` to the landing sink).

## Alternatives rejected

- **Make `_POOL_CACHE` cross-process** (shared memory / worker-local file): a
  cache is still per-worker-VM, so a 4-worker autoscale still pays 4×, and it
  survives nothing between runs.
- **Build pools driver-side before launch:** the driver has no GPU, and the
  ladder needs the served model.
- **Overload `rag_chunks` with a `pool_value` chunk kind:** see decision 2.
