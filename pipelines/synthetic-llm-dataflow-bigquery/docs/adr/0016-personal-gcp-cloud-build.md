# 0016 — Personal-project images build on Cloud Build (ADR 0008 carve-out)

Date: 2026-07-14
Status: Accepted

## Context

ADR 0008 makes CI the only sanctioned image builder — its rationale is
audited provenance, a shared layer cache and reproducible Linux runners. A
personal, cost-capped GCP project (`public_cloud/deploy/gcp/`, see its
[README](https://github.com/albertols/synthetic-llm-dataflow-bigquery/blob/d32c34743d80c8481445956939b6bbde9c4d4a3c/public_cloud/deploy/gcp/README.md)) now runs the T4 E2E
matrix on its own. The dev laptop has no Docker (Intel Mac, macOS 12), the
image is ~15–20 GB, and the organization's CI cannot push to a personal
Artifact Registry.

## Decision

For the **personal path only**, `gcloud builds submit` (Cloud Build) is the
sanctioned builder (`public_cloud/deploy/gcp/06_build_image.sh` +
`cloudbuild/build_image.yaml`). The mainline `docker/Dockerfile` is reused
unmodified: it builds from public base images and bootstraps uv from PyPI,
and `uv.lock` pins public `files.pythonhosted.org` URLs, so dependency
resolution is unchanged. (An earlier revision retargeted a private
package-index bootstrap line with a `sed` in the ephemeral Cloud Build
workspace; the Dockerfile no longer needs it.) Local `docker build`/`push`
remains forbidden on both paths.

## Consequences

- ADR 0008's rule is unchanged for the CI path; zero mainline edits
  (drift-guard test `test_tiers_matrix.py` protects the template contract
  instead).
- Personal images live only in the personal GAR repo (keep-newest-1 policy).
- Two accepted deviations from the CI path's least-privilege defaults,
  scoped to the personal path only: `run.googleapis.com` +
  `eventarc.googleapis.com` are enabled (`01_bootstrap_project.sh`) because
  the gen2 `billing-killswitch` Cloud Function requires them; and `BUILD_SA`
  holds project-level `roles/storage.objectAdmin` (`02_iam.sh`) rather than
  a bucket-scoped grant, because Cloud Build's auto-created source-staging
  bucket doesn't exist yet when IAM is provisioned.
