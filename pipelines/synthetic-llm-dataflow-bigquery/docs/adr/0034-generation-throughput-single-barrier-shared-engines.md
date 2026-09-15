# ADR 0034 — Generation throughput: one dedup barrier, one engine per process, a fleet that starts full (and the multi-process experiment)

**Status:** ACCEPTED (2026-09-07) — D1–D3, D5–D7 verified on the 2026-09-07 R7 pair (single + multi SDK topology, 10M rows/table); D4 amended the same day after the pair exposed its fallback defect and verified on the 09-07/09-08 multi pair; D8 revised (rev. 2) and D9 added 2026-09-08 after that pair (see *Acceptance evidence*)
**Design:** [`docs/designs/2026-09-07-generation-throughput-where-time-goes.md`](../designs/2026-09-07-generation-throughput-where-time-goes.md)
**Evidence:** the 2026-08-29 R6 pair — cold `2026-08-29_07_33_36-13355700596190055276` (93.8 min) and its immediate warm re-trigger `2026-08-29_09_49_17-12681434869969021419` (86.1 min), both `C_TABLE ◄═ A_TABLE`, 10M rows/table, PK 1.0, 0/10M orphans, `worker_logs.jsonl` + `_full_report.md` in each
**Amends:** [ADR 0019](0019-rag-population-scoped-to-consumers.md) (embedder lifecycle) · [ADR 0030](0030-single-job-relational-generation.md) (launch topology) · [ADR 0033](0033-pool-ladder-integrity-at-scale.md) (prose ceiling) · the WS6 uniqueness modes ([design](../designs/2026-07-27-ws6-pipeline-shape.md))
**Keeps:** [ADR 0020](0020-freetext-pools-as-persisted-artifact.md) · [ADR 0023](0023-source-domain-pool-rejection.md) · [ADR 0031](0031-joint-fk-key-draws.md) · [ADR 0032](0032-relationships-as-config.md) — every fidelity, privacy and referential-integrity guarantee is unchanged

## Context

The R6 pair is the first cold + warm twin at 10M rows per table, and its
headline is clean and reproducible: PK uniqueness 1.0, 0 orphans by an
independent full-table join, `dlq_by_rule={}`, 67/67 + 44/45 stats
columns `ok`, `copy_fraction = 0` — and 93.8 min against 92.9 min for the
2026-08-26 10M run (+1 %). There is no regression. There is a ceiling,
and the worker logs say where it is:

1. **Warming buys 8 minutes.** The warm twin ignited vLLM zero times and
   still ran 86 min. The pool branch is the only phase warming removes;
   generation (23.5 + 19.2 min) and the dedup barriers (14.0 + 12.2 min)
   are the same in both runs. The GPU was busy (embeds + pool ladders)
   for ~11 of 94 minutes and was billed 274 GPU-minutes.
2. **Generation is bound by one interpreter per worker.** The GPU tiers
   pin `no_use_multiple_sdk_containers` (RUN_PLAYBOOK §3), so each
   `n1-highmem-8` runs ONE Python SDK process with 8 harness threads
   (`--dax_workflow_worker_num_threads_per_worker=8`). Every batch of 10k
   rows takes 26–29 s under that contention and the fleet's steady state
   is ~10.5k rows/s (C_TABLE) / ~12k (A_TABLE): four interpreters for
   32 vCPUs. `TotalVcpuTime` bills the other 28.
3. **The fleet started half-size.** Both jobs launched on 2 workers and
   the autoscaler added 2 more ~4 min into the first generate stage
   (three harness boots at 15:01 / 17:08; their SDK harnesses registered
   3–5 min later). C_TABLE's first 8 minutes ran at ~2.5k rows/s.
4. **Thirty-two engine builds per table.** 4 workers × 8 threads =
   32 `GenerateRecordsDoFn` instances, each building its own engine:
   chunk-store read, FAISS index, five pool-store reads, per-column
   source-domain and k-anonymity fetches, the FK key-pool IPF fit —
   2,940 thread-seconds on C_TABLE cold (p50 75 s, max 444 s), 2,824
   warm, serialized on process-level single-flight locks for state the
   sibling threads already held. A B.2 run pays its CTGAN fit and lazy
   pool ladders the same 32 times.
5. **Three full-row shuffle barriers per table.** `EnforceUniqueness`
   chains `CombineByRowDigest → CombineByPk → CombineByIdentity`; every
   row crosses Dataflow Shuffle three times as a keyed dict —
   123 GB per job, `Will retry read due to resource exhausted` storms
   during `CombineByPk`, a harness restart inside each barrier phase
   (15:29 / 17:36). 26 of 94 minutes.
6. **The report misread two gaps.** The "27.4-min FK parent-key-cap
   computation" is the child wave waiting for the parent wave
   (`fk_key_pool_capped` fires 25 s after C_TABLE's load job triggers);
   the "1,078 s generation stall" is `_fetch_identifier_domains` paging
   944,582 distinct `A_COL_005` values through the BigQuery REST row
   iterator at ~2.9k rows/s inside `DoFn.setup()`.
7. **One ADR 0033 acceptance miss.** `A_COL_019` is mask-gated in this
   run (`gate_lengths=mask`), so the prose-only length ceiling never
   engaged: pool values reached 48 characters against a 35-character
   source (p95 42 vs 35).

## Decision

**D1 — `uniqueness_mode=exact` runs ONE full-row barrier.** Rows cross
the shuffle once, keyed by digest (`CombineByRowDigest`), packed as value
tuples in schema order (about half the bytes of a keyed dict). The PK and
identity rules are resolved from key-only collision groups —
`(pk tuple → sorted digests)` for keys shared by ≥ 2 distinct rows,
combined map-side in the same fused stage — delivered as `AsDict` side
inputs to the barrier's read stage (`ResolveUniqueness`). Semantics are
preserved: one survivor per digest with `seen − 1` `row.duplicate`
envelopes; among digest-unique rows one survivor per PK tuple
(`pk.duplicate`); among PK survivors one survivor per identity tuple
(`identity.unique`). The survivor is the smallest digest — deterministic
where the chain was arbitrary — and PK/identity envelopes carry the
dropped row's own payload. The chain stays as `exact_chained` for A/B
runs; `streaming` is untouched.

**D2 — One engine per (engine, run, landing table, reference digest) per
worker process.** `GenerateRecordsDoFn` acquires its engine from a
process-level registry: the first DoFn builds (under a per-key lock;
siblings wait for one build instead of racing eight), every sibling
shares it, holders are refcounted, and the engine — with the
`ModelClient` it was built with — is torn down by the last holder. A
lone DoFn keeps the historical `engine_setup → engine_teardown →
client_teardown` order; a failed build is never shared. `engine_shared
holders=N` is the evidence line. B.2 gets the same seam for free: one
CTGAN fit and one lazy pool ladder per process instead of eight.

**D3 — The embedder loads on first use.** `BgeEmbedder` construction
records the path and the requested device; `ensure_loaded()` (called by
`embed()`) imports the HF stack, resolves the device, loads the weights
and moves them, once, process-serialized. `demote_to_cpu()` before any
load pins the eventual load to CPU. A store-warm generate setup never
embeds, so it never imports transformers, never loads 130 MB of weights
and never opens a CUDA context beside vLLM. The cold path (bulk embed on
CUDA → demote) is byte-identical.

**D4 — Source-domain fetches use the BigQuery Storage Read API.**
`BigQuerySourceValueStore` reads its result through
`RowIterator.to_arrow()`: large results stream through the Storage API
(the `google-cloud-bigquery-storage` client is on the worker image),
small ones come from the cached first page, and any Arrow-path failure
falls back to row iteration with `source_values_arrow_fallback`. Same
SQL, same cap semantics, same process cache. *Amended 2026-09-07:* the
fallback takes a **fresh** `QueryJob.result()` — a `RowIterator` is
one-shot and the failed Storage attempt has already started it — and a
`PermissionDenied` / `Forbidden` disables the Storage attempt for the
rest of the process (`source_values_storage_api_disabled`, once). The
worker SA needs `roles/bigquery.readSessionUser`
(`bigquery.readsessions.create`); `02_iam.sh` and
`DEPLOYMENT_PREREQUISITES.md` grant and document it.

**D5 — A scale run starts at its worker ceiling.** `initial_workers` is
a Flex Template parameter and Composer `Param`, pinned by the launcher
through `WorkerOptions.num_workers` (the `disk_size_gb` channel); an
explicit Beam `--num_workers` wins. `run_e2e.sh` tiers carry it as
`job.num_workers` (`R7`: 10M rows, 4 workers from the first second).

**D6 — The multi-process SDK topology is launchable and safe, as an
experiment.** `sdk_containers=multi` (Composer `Param`, `run_e2e.sh`
`job.sdk_containers`, tier `R7m`) lifts `no_use_multiple_sdk_containers`.
The launcher detects the topology from the experiments
(`sdk_container_topology` milestone) and builds the vLLM client with
`cross_process=True`: the pull → spawn → ready window is serialized
across processes by a bound loopback port (`_PortMutex`, `spawn_lock_port`
= 8001 — SDK containers share the host network, the same fact the reuse
probe relies on), losers wait and bind to the winner's server through the
reuse probe (`vllm_spawn_lock_wait` / `vllm_spawn_lock_acquired`), and
teardown keeps the server alive (`vllm_server_kept_alive`) because a
sibling process's refcount is invisible. D3 keeps the seven non-spawning
processes off the GPU. The default stays `single` until the R7m
acceptance run reads clean. Both topologies, side by side — threads and
GILs per vCPU, the engine registry per process, the spawn mutex and
reuse, the bounded population embed — are drawn once in the design
doc's `assets/sdk-containers-topology.png` (drawio source committed
beside it).

**D7 — The fixed-width ceiling applies to mask-gated columns.**
`_FormatGate.length_blind` (prose OR collapsed-mask gate) is where
`length_ceiling` clamps candidates before the format and novelty checks
(ADR 0033 D5 covered prose only).

**D8 — Bound the cold population embed and let the pool branch wait
for it (rev. 2, 2026-09-08).** The rag_chunks population branch embeds
the ≤10k reference rows and the distinct free-text values with
`BgeEmbedder`. Under `sdk_containers=multi` the acceptance pair fanned
it out (`Reshuffle`) to every SDK process — eight CUDA contexts and
eight weight loads per worker beside the vLLM spawn: 8 × 20 s
`vllm_unfittable_wait`, 344 s ignition against 190 s on `single`.
Rev. 1 (2026-09-07) moved those embeds to CPU; the 2026-09-08 cold run
showed that 56 CPU embedders starve the rest of the VM instead: the
model pull took 177.6 s (52.5 s the day before), the vLLM engine init
598.7 s, and the population stage itself 15.4 min instead of 2.9. Rev. 2
keeps the GPU (`PipelineConfig.rag_embed_device="auto"` on every
topology) and bounds the *fan-out*: `PipelineConfig.rag_embed_shards`
(default 2) keys the chunks into that many groups (`RagShard` →
`RagShardGroup`) so at most two embedders exist per job whatever the
topology, and the pool branch's trigger takes the embedded chunks as a
side input (`AwaitRagPopulation`, only when a rag sink is configured) so
the branch's first LLM call — the one that spawns vLLM — meets a free
card and idle cores. The pool branch's own embedder (one process) is
unchanged. Also from the acceptance pair: the launcher pins
`max_cache_memory_usage_mb` to 512 when unset (the single-barrier read
stage fetched its PK/identity group side inputs per bundle — "Retrieving
state 62 times costed 60 seconds"), and `vllm_server_kept_alive` fires
only from a client that was bound to or spawned a server (40 lines from
38 idle clients on R7m).

**D9 — A fleet you size by hand stays that size.** `autoscaling` =
`auto` | `throughput` | `fixed` (template + DAG param,
`run_pipeline --autoscaling`). `auto` (default) pins Dataflow's
`autoscaling_algorithm=NONE` exactly when `initial_workers` is given;
`fixed` pins it and refuses to launch without `initial_workers`;
`throughput` keeps THROUGHPUT_BASED. An explicit Beam
`--autoscaling_algorithm` always wins over the mode. Evidence: on both
09-07 15:53 and 09-08 01:28 the autoscaler went 1 → 4 for the parent's
generate stage, dropped to 1–2 workers during the parent's load and the
child's pool phase, then re-provisioned VMs for the child's generate
stage — A_TABLE ran its first minutes short-handed and each job paid
≈ 4 min against the 09-07 07:33 run, whose fleet happened to stay up.
The cost is a fixed 4-worker bill through load and cleanup, minutes on
a 10M run; the `R7`/`R7m` tiers now pass `initial_workers=4` +
`autoscaling=fixed`.

**Evaluated, not adopted — NVIDIA MPS.** Dataflow's
[NVIDIA Multi-Process Service](https://docs.cloud.google.com/dataflow/docs/gpu/use-nvidia-mps)
(`worker_accelerator=…;use_nvidia_mps`, retrieved 2026-09-07) lets
several SDK processes share one GPU's CUDA context and scheduler; Google
recommends it for `RunInference` with `model_copies > 1`, it forbids
`no_use_multiple_sdk_containers`, and it warns against exceeding GPU
memory with large models. Our GPU work is one vLLM **server** per
worker reached over HTTP by every process (ADR 0014) — the only
multi-process CUDA use is the cold population embed, which D8 bounds to
`rag_embed_shards` processes that finish before vLLM spawns. MPS would
not change the KV budget that bounds the pool
ladders, adds a control daemon between vLLM and the driver, and the
[driver guidance](https://docs.cloud.google.com/dataflow/docs/gpu/use-gpus#drivers)
keeps `install-nvidia-driver:5xx` unchanged either way. Revisit only if
the design ever runs more than one model process per GPU (e.g. two vLLM
replicas on an L4 for parallel pool ladders).

## Acceptance evidence — the 2026-09-07 R7 pair and the 09-07/09-08 multi pair

Four 10M-row launches of the same pair, `initial_workers` left empty on
every one (all started on **1** worker), read from `worker_logs.jsonl`
and the console autoscaling chart; figure `assets/throughput-evolution.png`.
The first two are on image `oss-pk-ready-00fc613`; the last two carry
the D4 amendment and D8 rev. 1 (CPU population embeds). The 09-07 15:53
launch was meant as a warm replay, but the taint preflight found the
pools the R7 pair had persisted without source filters and rebuilt them
(`pool_source_overlap` → delete + rebuild, as designed); its RAG store
was warm. The 09-08 logs were exported with an exclusion filter (no
`batch_start`/`batch_done`), so its per-table rates are stage spans:

| | R6 cold (08-29) | R7 `single` | R7 `multi` | multi, pools rebuilt (09-07 15:53) | multi cold, CPU embeds (09-08) |
|---|---|---|---|---|---|
| wall time | 93.8 min | ≈ 76.5 min | ≈ 50.5 min | ≈ 52.5 min | 56.9 min |
| C_TABLE / A_TABLE generation | 23.5 / 19.2 min | 21.5 / 14.9 | **10.4 / 6.7** | 12.2 / 8.9 | ≈ 6 / ≈ 11 (A short-handed, D9) |
| 10k-row batch at steady state | 26–29 s | 26–29 s | **5.6–6.7 s** | 6.6 s | (filtered out of the export) |
| dedup + load, C / A | 14.0 / 12.2 min | **7.8 / 6.5** | **3.4 / 3.0** | 7.5 (both) | 8.2 (both) |
| cold pool branch (critical path) | 7.9 min | 10.1 | 12.9 | 9.7 | 14.2 |
| population embed stage | — | — | 2.9 min (GPU × 8 processes) | warm (`b1_chunks_reused`) | **15.4 min** (CPU × 56 processes) |
| model pull / vLLM ignition | — / 226 s | — / 190 s | — / 344 s (8 unfittable waits) | 52.5 s / **183 s**, no wait | **177.6 s / 598.7 s** (CPU-starved) |
| engine builds per table | 32 | **4** (`engine_shared holders=2..8`) | one per process | one per process | one per process |
| cross-process spawn lock | n/a | n/a | 1 acquired + 1 wait + reuse, no OOM | 1 per worker, no OOM | 1 per worker, no OOM |
| workers | 2 → 4 at +27 min | 1 → 4 at +26 min | 1 → 2 at +29, 4 at +37 min | 1 → 4 at +32, **dip to 2 at +37** | 1 → 4 at +20, **dip to 1 at +40** |
| source-domain fetches | OK (REST, 5.5 min) | **107/107 failed** | **459/459 failed** | OK — `identifier_source_filter size=944582` | OK — same; Storage denied once per process (IAM role still ungranted), REST 22 s |

The last row is the defect D4's amendment fixes: `PermissionDenied` on
the Storage attempt, then `ValueError` ("Iterator has already started")
on the fallback. For those two runs the ADR 0023 rejection sets, the
identifier and numeric source domains, and the ADR 0033 filter-sized
pool target (`A_COL_015` back to 94) were all inactive; the pools they
persisted are candidates for the launcher's taint preflight
(`pool_source_overlap` → delete + rebuild) and their landed rows need the
E2E probe's `copy_ratio` before they count as clean.

The 09-07/09-08 multi pair then verified that amendment (every fetch
`size=944582`, the Storage attempt denied and disabled once per process
because `roles/bigquery.readSessionUser` is still to be granted on the
worker SA) and falsified D8 rev. 1: the CPU population embed is the
whole difference between the 09-08 cold run's 56.9 min and a ≈ 45-min
cold run — a 15.4-min population stage that also starved the model pull
and the vLLM init on the same VM. Both runs show the same autoscaler
shape (up for the parent, down between stages, up again for the child),
which is D9's evidence. Launcher/boot variance accounts for the rest
(11.6 vs 15.5 min).

## Consequences

- The dedup phase shrinks from six full-row shuffle passes per table to
  two, with the same envelopes and exact counts; the `resource exhausted`
  retry storms lose their cause. Expected: ~26 → ~10 min per job, to be
  read from the next run's `dominant_stages` and
  `TotalShuffleDataProcessed` (123 GB → ~24 GB if value tuples halve the
  per-row bytes as designed, ~44 GB if they do not — the figure hatches
  the projection).
- The first generate stage stops ramping for 8 minutes: 4 workers from
  launch (D5) and one engine build per process (D2) — the C_TABLE stage
  starts at its steady state; ~6 min per job. `dofn_setup_done`
  collapses from 32 entries per table to 4, `engine_shared holders=8` ×
  4 appears.
- `_fetch_identifier_domains` on a 944k-value domain drops from ~5.5 min
  to seconds (D4) — off the critical path in this pair, on it for any
  standalone child or cold parent with a large identifier domain.
- Under `sdk_containers=multi` the fleet gets 8 interpreters per worker
  for the CPU-bound generate stages (the pair's 10.5k rows/s was ~4
  interpreters); the ceiling moves to the shuffle write and BigQuery
  load. This is the one change that is **not laptop-provable**: the
  R7m run must show exactly one `vllm_spawn_lock_acquired` per worker,
  `vllm_spawn_lock_wait` on the others, no CUDA OOM, and a higher
  `batch_done` rate before the default flips.
- What this ADR does **not** do: it does not touch the launcher's 10-min
  flex-template phase or the 5-min worker boot (a slimmer launcher image
  is the next infra step, ADR 0009 territory); it does not change the T4
  KV budget that makes a 512-value pool ladder cost ~4.4 min (a
  quantized checkpoint is a `config/models.yml` decision); it does not
  alter the FK-column marginal trade-off (`C_COL_007` warn, ADR 0031);
  it leaves the A_COL_037 clause example (28 chars on a 31-char column,
  `prompt_constraint_example_off_format` fired again) to the operator.
- The multi topology is validated on three launches (3× the fleet rate
  at four workers, 5× per worker at two) and the D4 amendment on two,
  but `multi` stays opt-in (`sdk_containers=multi`) for one more
  launch: D8 rev. 2 and D9 are the untested changes. The next multi cold
  start must show at most two population embedders per job
  (`embedder_device` from `RagEmbedChunks`), a population stage back
  under 3 min, a model pull under 60 s and vLLM ignition ≈ 180–200 s
  with zero `vllm_unfittable_wait`, and — with `initial_workers=4` +
  `autoscaling=fixed` — no `Starting Unified Worker` after
  `workers_ready` and a worker count that never drops between the
  parent and child stages. Expected wall time for that cold run:
  ≈ 44–46 min (56.9 − 12.5 population/ignition − ≈ 4 autoscaler dip,
  ± launcher variance).
- Original acceptance list (kept for the record; status in the table above):
  `CombineByPk` absent from `dominant_stages`; `engine_shared` present
  and `dofn_setup_done` ≤ 4 per table; `initial_workers=4` → no harness
  boots after `workers_ready`; `identifier_source_filter` for
  `A_COL_005` under 30 s; `freetext_pool_length_clamped` on
  `A_COL_019` and `len_max == 35` in the crosscheck; PK 1.0 and 0
  orphans unchanged.
