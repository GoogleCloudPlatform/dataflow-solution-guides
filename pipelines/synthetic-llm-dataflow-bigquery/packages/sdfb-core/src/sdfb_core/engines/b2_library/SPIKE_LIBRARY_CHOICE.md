# B.2 library choice — spike write-up (M1 §6)

> **Status**: decided. `sdgx` is locked. This file is the spike record; once
> the choice is fully validated on the M4 it folds into this package's
> `README.md` (charter AC #2 / DRY policy). No bake-off was run — the
> decision was made on license + maintenance + spine-fit grounds (see
> [ADR 0013](../../../../../../docs/adr/0013-distribution-estimator-spine.md)).

## Decision

**Use `sdgx` (hitsz-ids/synthetic-data-generator), CTGAN family, pinned in
the `[library]` extra (`sdgx>=0.2.0`).**

| Candidate | License | Verdict |
|---|---|---|
| **`sdgx`** (hitsz-ids) | **Apache-2.0** | **Selected** — license-clean for production use; active; ships CTGAN + statistical models; in-memory `DataFrameConnector` fits the no-disk Beam-worker path. |
| SDV / `GaussianCopula` | BSL-1.1 | **Deferred upgrade path.** Technically stronger (richer constraint API, more deterministic sampling) but the Business Source License needs a legal review before production adoption. Recorded here, not adopted now. |
| DataDreamer | Apache-2.0/MIT | **Not selected** for the tabular spine — it is an LLM-orchestration framework (prompt/workflow), not a statistical tabular fitter. Its niche overlaps the free-text hook, which we already cover via the `ModelClient` Protocol; pulling a second LLM stack inside the Beam DoFn is redundant. |

Rationale in one line: of the candidates, `sdgx` is the only **license-clean,
statistical, in-memory-fittable** tabular library, and it slots directly into
the FASTGEN spine (fit-once in `setup`, vectorized `sample` in
`generate_batch`) without violating the no-Vertex / no-external-API
constraints.

## Why no per-row LLM generation (the spine)

Per ADR 0013: prompting the LLM per row is ~9,500× slower than statistical
sampling and collapses distributions toward linguistic-token frequency. So
the LLM is used **O(1)** (free-text columns only); `sdgx` provides the O(N)
statistical bulk.

## How `sdgx` is wired (this package)

- `backends.py::SdgxBackend` — deferred `import sdgx` inside `fit()` (heavy:
  pulls torch). Fits `Synthesizer(model=CTGANSynthesizerModel(epochs=...))`
  on an in-memory `DataFrameConnector` over `reference_rows`. The fitted
  `Synthesizer` pickles across the Beam worker boundary.
- Free-text / JSON / very-high-cardinality string columns are **dropped**
  before the CTGAN fit (CTGAN models them poorly) and patched by the
  `ModelClient` free-text hook instead (`freetext.py`).
- `sdgx`'s metadata/constraint API is thinner than SDV's, so range / enum /
  constant fidelity is enforced **post-hoc** locally in `fidelity.py`
  (defense-in-depth on top of the Mode-A Pandera contract).
- `EmpiricalBackend` (pure NumPy) is the deterministic CPU baseline + the
  laptop/offline fallback when the `[library]` extra is not installed. The
  ABC reproducibility contract pins this backend (CTGAN RNG is not
  bit-for-bit reproducible across machines).

## Measurements (charter AC #2 / #3)

These require the `[library]` extra (`uv sync --group dev --extra library`)
and are an **M4 task** — the laptop in this worktree had no reachable package index at the
time, so `sdgx` could not be installed or fitted here. To record on the M4:

| Metric | How to measure | Threshold |
|---|---|---|
| **Fit time, 10k reference rows** | time `SdgxBackend.fit()` with `epochs=100` on the real wide table | **> 5 min ⇒ spike review** (AC #3). Mitigations if breached: lower `epochs`, sub-sample reference, or switch this table to `EmpiricalBackend`. |
| **RAM (peak RSS during fit)** | `resource.getrusage` / `tracemalloc` around `fit()` | fits in the L4 worker's host RAM alongside vLLM (size the CTGAN batch down if contended). |
| **Sample-quality eyeball** | sample 1k rows, eyeball marginals vs. reference; confirm constants stayed constant, numerics in-range, categoricals plausible | qualitative pass; fidelity is also enforced by `fidelity.py` post-hoc. |

Empirical-backend timings (the laptop baseline, no fit cost beyond
profiling) are O(rows × columns) and sub-second for 10k rows; profiling is a
single pass over `reference_rows`.

## sdgx pin

Recorded in `config/models.yml` under `b2_library`, and in
`packages/sdfb-beam/pyproject.toml` `[project.optional-dependencies].library`.
