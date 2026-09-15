# B.2 library-wrapper engine (`b2_library`)

`B2LibraryEngine` — one of the two `GenerationEngine` implementations behind
the shared ABC (M1 §6). Library choice: **`sdgx`** (Apache-2.0). See
[`SPIKE_LIBRARY_CHOICE.md`](SPIKE_LIBRARY_CHOICE.md) for the decision record
(sdgx selected; SDV/GaussianCopula deferred pending BSL-1.1 sign-off) and the
fit-time / RAM / sample-quality measurement plan.

Design of record: [ADR 0013](../../../../../../docs/adr/0013-distribution-estimator-spine.md).

## The spine (fit-once, vectorized-sample, LLM-only-for-free-text)

```
setup(model_client, ctx):       # once per Beam worker; idempotent
    profile reference_rows → per-column ColumnProfile          (fidelity.py)
    fit the statistical backend ONCE                            (backends.py)
generate_batch(n, cfg):         # many times; cheap, CPU
    vectorized-sample n rows (seeded)                           (backends.py)
    enforce fidelity: constant / range / enum clamps            (fidelity.py)
    patch FREE_TEXT / JSON / high-cardinality cols via the LLM   (freetext.py)
    yield GeneratedRecord (Pydantic; Pandera + DLQ downstream)
teardown(): release the fitted model
```

The LLM runs **O(1)** in N (free-text columns only); the bulk N is sampled
vectorized on CPU. Fidelity is preserved *by construction* (constants copied,
numerics clipped to observed bounds, categoricals drawn from the empirical
table) on top of the Mode-A Pandera contract.

## Modules

| File | Responsibility |
|---|---|
| `engine.py` | `B2LibraryEngine` — lifecycle, orchestration, pickling hooks. Registers as `"b2_library"`. |
| `fidelity.py` | Column profiling (`ColumnKind`, `ColumnProfile`) + post-hoc enforcement (`enforce_value`). LOCAL to B.2; may consolidate to `engines/_fidelity.py` post-merge. |
| `backends.py` | Sampling-backend seam: `SdgxBackend` (deferred `sdgx` import; production) + `EmpiricalBackend` (pure NumPy; deterministic; laptop/offline fallback + reproducibility-contract backend). |
| `freetext.py` | Per-column free-text LLM hook via the `ModelClient` Protocol. Bounded unique pool, sample-with-replacement, honors `cfg.similarity`. |

## `similarity` mapping

- **Backend categorical sampling** (`engine._similarity_to_sampling_temperature`):
  similarity→1 ⇒ temp→0 (collapse to the empirical mode, max mimicry);
  similarity→0 ⇒ temp→~2 (flatten toward uniform within the observed
  support, max diversity).
- **Free-text hook** (`freetext.similarity_to_temperature` + `_blend_pools`):
  similarity sets the LLM sampling temperature *and* biases the
  sample-with-replacement draw toward the observed reference pool (high
  similarity) vs. the LLM-generated pool (low similarity).

## Constraints honored

- Pure `sdfb-core`: no `apache_beam`, no `vllm`, no GCP. NumPy is the only
  always-on heavy dep (the vectorized-sampling backbone). `sdgx` (+ torch) is
  imported **lazily** inside `SdgxBackend.fit()` — importing this package
  works on a laptop with only base deps.
- Engines call only the `ModelClient` Protocol; never import vLLM.
- The fitted engine pickles across the Beam worker boundary (the dynamic
  Pydantic record model is rebuilt from `ctx` on unpickle — see
  `engine.__getstate__/__setstate__`).

## Running the contract + engine tests

```bash
uv sync --group dev --extra library          # sdgx; offline → empirical fallback
uv run pytest -m "not gpu and not gcp" -q     # 5 ABC contract tests + B.2 unit tests
```

The ABC contract pins the deterministic NumPy backend (`use_sdgx=False`);
`sdgx`/CTGAN fit + timing is an M4 task (see the spike doc).
