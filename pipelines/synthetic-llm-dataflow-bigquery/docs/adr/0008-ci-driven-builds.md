# ADR 0008 — Container image builds happen in CI, not on developer laptops

- **Status**: accepted (2026-05-19)
- **Supersedes**: nothing; refines [ADR 0003](0003-jfrog-image-registry.md) (since withdrawn — the registry decision is now [ADR 0015](0015-worker-image-via-artifact-registry.md)) on the *where* of the build.

## Context

[ADR 0003](0003-jfrog-image-registry.md) locked the **registry** but left the **build location** undefined. Earlier iteration assumed M4-local docker build → `gsutil` push, with `scripts/build_gpu_image.sh` and `scripts/push_gpu_image.sh` as the laptop-side workflow.

That approach has three real problems in a regulated environment:
1. **Provenance**: developer-local builds bypass the audit trail. Production images must trace to a CI workflow run, with the commit SHA in the image tag and the change record linked to that run.
2. **Determinism**: M4 Apple-Silicon emulating `linux/amd64` produces images bit-different from real Linux runners; reproducibility suffers.
3. **Speed**: full CUDA + cuDNN + uv-sync rebuild on M4 takes 15–25 minutes; on CI runners with a registry layer cache (`cache-from`), under 5 minutes.

## Decision

**All container builds and pushes happen in CI workflows.** CI authenticates to Google Cloud with short-lived federated credentials, and any registry credentials stay in CI — developers never hold them. The two scripts (`build_gpu_image.sh`, `push_gpu_image.sh`) are removed; developers cannot push images directly.

Launch tooling submits Dataflow jobs using an image already pushed by CI (`--sdk_container_image=<registry>/...:<sha>`); nothing on the launch path builds anything. On a personal project, Cloud Build plays the CI role ([ADR 0016](0016-personal-gcp-cloud-build.md)).

## Consequences

- **Enables**: audited provenance (workflow run + SHA in image tag), reproducible builds across machines, faster iteration via the registry layer cache, CI-held registry credentials (devs never see them).
- **Costs**: no fully-offline dev loop — a fresh image requires a workflow_dispatch. Smoke testing against a real model on M4 falls back to MLX (see [ADR 0010](0010-m4-local-smoke-mlx.md)).
- **Forbids**: committing `docker build` or `docker push` shell scripts; bypassing CI to publish images; treating manually-built images as deployable.

## Related

- `public_cloud/deploy/gcp/06_build_image.sh` / `07_build_flex_template.sh` — the Cloud Build implementation shipped in this repository ([`public_cloud/deploy/gcp/README.md`](https://github.com/albertols/synthetic-llm-dataflow-bigquery/blob/d32c34743d80c8481445956939b6bbde9c4d4a3c/public_cloud/deploy/gcp/README.md)).
- [ADR 0015](0015-worker-image-via-artifact-registry.md) — where the image is pushed and pulled from.
- [ADR 0009](0009-single-flex-template-image.md) — what the image actually contains.
