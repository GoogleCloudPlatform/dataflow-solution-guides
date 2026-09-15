# ADR 0003 — Corporate JFrog as the container registry

**Status:** WITHDRAWN (2026-09-14) — superseded by public base images (`apache/beam_python3.11_sdk`, Dataflow template launcher base) and Artifact Registry (ADR 0015); see ADR 0040.

This ADR once routed the worker image through a private, organization-specific Docker registry; the open-source build pushes to Artifact Registry and needs no registry credentials.
