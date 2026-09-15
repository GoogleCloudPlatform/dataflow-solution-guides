# ADR 0004 — europe-west3 (Frankfurt) as the M1 Dataflow region

- **Status**: accepted (2026-05-19)

## Context

L4 GPUs and `g2-standard-*` machine types are not available in every region. The reference BigQuery data sits in the EU, so keeping workers in an EU region keeps reads and egress to googleapis.com in-region. Frankfurt is the closest GCP region with broad L4 availability and matches an EU data-residency posture.

## Decision

M1 Dataflow jobs run in **`europe-west3`** with worker zone **`europe-west3-b`** by default. Both encoded in `.envrc` as `GCP_REGION` / `GCP_ZONE`; consumed by the launch tooling (the Composer DAG and the `public_cloud/deploy/gcp/` run driver).

## Consequences

- **Enables**: same-region image pulls (once an AR fallback exists), same-region BQ reads, EU-data-residency alignment.
- **Costs**: ties the M1 deliverable to a single region. M3 will add at least `us-central1` parity (see `docs/ROADMAP.md`).
- **Forbids**: hardcoding the region into Python sources or Dockerfiles. All region-dependent flags come from env vars or argparse.

## Related

- [ADR 0015](0015-worker-image-via-artifact-registry.md) — worker image in a same-region Artifact Registry repo.
- L4 quota check: `gcloud compute project-info describe --flatten='quotas[]' | grep NVIDIA_L4_GPUS`.
