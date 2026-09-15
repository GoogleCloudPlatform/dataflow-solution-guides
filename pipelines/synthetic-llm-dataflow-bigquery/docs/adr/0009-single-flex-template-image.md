# ADR 0009 — Single image for Flex Template launcher AND Dataflow workers

- **Status**: accepted (2026-05-19)

## Context

Dataflow has two distinct runtime contracts:
1. **Flex Template launcher containers** expect `ENTRYPOINT = /opt/google/dataflow/python_template_launcher`. The launcher reads `FLEX_TEMPLATE_PYTHON_PY_FILE` and submits the pipeline.
2. **Dataflow worker containers** (custom container path) expect `/opt/apache/beam/boot` as their entrypoint.

Naive read: two contracts → two images. That doubles registry storage, CI time, and creates drift risk.

Less-naive read: both roles can share one image because `/opt/apache/beam/boot` and `/opt/google/dataflow/python_template_launcher` can both be present.

> **Correction (2026-06-30):** an earlier version of this ADR claimed Dataflow starts workers with an explicit `--entrypoint=/opt/apache/beam/boot` override, making the image ENTRYPOINT irrelevant. **That is wrong.** Dataflow runs the image's ENTRYPOINT for *both* roles and merely *appends* args; for workers it appends the Beam FnAPI boot flags (`--id`, `--logging_endpoint`, `--control_endpoint`, `--artifact_endpoint`, `--provision_endpoint`). With `ENTRYPOINT = python_template_launcher`, the worker launched the launcher binary with those flags and crash-looped (`flag provided but not defined: -logging_endpoint`). The fix is a **dispatch ENTRYPOINT** (`docker/entrypoint.sh`) that execs `boot` when any FnAPI `*_endpoint`/`--id` flag is present, else the launcher. See https://cloud.google.com/dataflow/docs/guides/build-container-image.

## Decision

**One image** (`sdfb-python`) serves both contracts:
- `ENTRYPOINT` is a dispatch script (`docker/entrypoint.sh`) that inspects the appended args: FnAPI `*_endpoint`/`--id` flags present → exec `/opt/apache/beam/boot` (worker); otherwise → exec `/opt/google/dataflow/python_template_launcher` (Flex Template launcher).
- Both binaries are present at their canonical paths, pulled from official Google base images (`apache/beam_python3.11_sdk:2.73.0` and `gcr.io/dataflow-templates-base/python311-template-launcher-base`) via multi-stage `COPY --from`.

## Consequences

- **Enables**: one registry tag per release, no chained-build CI dependency, single cache layer. Workers and launcher share the exact same Python deps and source — zero drift.
- **Costs**: the image is slightly larger (one extra `/opt/google/dataflow/python_template_launcher` binary, ~tens of MB on top of the CUDA + uv-synced workspace).
- **Forbids**: assuming Dataflow overrides the image `ENTRYPOINT` for workers (it does not — it appends FnAPI flags to *your* entrypoint). The dispatch script is load-bearing; anyone editing it must preserve the FnAPI-flag discriminator. The Dockerfile comment block makes this explicit.

## Related

- `docker/Dockerfile` — implementation.
- [ADR 0015](0015-worker-image-via-artifact-registry.md) — where this image is pushed and pulled from; [`public_cloud/deploy/gcp/README.md`](https://github.com/albertols/synthetic-llm-dataflow-bigquery/blob/a01e54abd46e676b53f344bd8ba5d10af231de21/public_cloud/deploy/gcp/README.md) — a runnable build/deploy path.
- [ADR 0008](0008-ci-driven-builds.md) — where the build runs.
