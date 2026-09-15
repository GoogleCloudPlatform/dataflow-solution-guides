# ADR 0012 — Enterprise-network constraints for the GPU image build + Dataflow run

**Status:** WITHDRAWN (2026-09-14) — superseded by public base images (`apache/beam_python3.11_sdk`, Dataflow template launcher base) and Artifact Registry (ADR 0015); see ADR 0040.

This ADR once adapted the image build to a network that could only reach private package and image mirrors; the open-source build pulls its base images from public registries and resolves Python packages from PyPI.

## Still in force

Two decisions from this ADR did not depend on the restricted network and still hold (the code cites them as ADR 0012):

- **Model warm-pull uses the `google-cloud-storage` Python client, not `gsutil`** (`sdfb_beam/gcs.py`, `handlers/vllm_client.py`). The client is a transitive dependency of `apache-beam[gcp]` and authenticates with the worker's credentials, so the image needs no Cloud SDK.
- **The NVIDIA driver is installed on the worker VM by Dataflow** (`worker_accelerator=…;install-nvidia-driver`) and mounted at `/usr/local/nvidia/`; it is never baked into the image.
