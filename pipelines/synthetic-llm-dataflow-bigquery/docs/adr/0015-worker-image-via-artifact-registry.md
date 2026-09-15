# ADR 0015 — Dataflow worker image served via Artifact Registry (amends ADR 0003)

- **Status**: accepted (2026-05-26), amended (2026-05-26 — dual-push) — amends [ADR 0003](0003-jfrog-image-registry.md) (withdrawn); relates to [ADR 0009](0009-single-flex-template-image.md)

## Amendment (2026-05-26) — optional archival copy in a private registry

The original decision below pushed the image to **Artifact Registry only**. The build may also push the *same* image to a private, third-party registry of record (archival, the immutable `:<branch>-<sha>` tag) **and** Artifact Registry, in a single `docker build` with two tags.

This does **not** change the runtime-pull conclusion that is the whole point of this ADR: the Dataflow **worker** (`sdk_container_image`) and the Flex Template **launcher** (`--image`) still pull from **AR only**, via the runtime SA's IAM — a third-party registry pull still 403s on the worker kubelet pod (see Context). The archival copy is an additional artifact store, never a runtime pull source. So everywhere the Decision/Consequences below say the image lives "only" in AR, read it as **"the runtime pull source is AR only"**; storage may be dual.

Added prerequisite when dual-pushing: the CI credentials for the archival registry must keep write access (write-once — only fresh `:<branch>-<sha>` tags, never `:latest`).

## Context

ADR 0003 made a private third-party registry the single image registry, and ADR 0009 made one image serve both the Flex Template **launcher** and the Dataflow **workers**. At §11 the launcher pulls the image fine, but every worker fails the SDK-harness pull:

```
StartContainer for "sdk-0-0" … ImagePullBackOff … HEAD …/sdfb-python/manifests/<tag>: 403 Forbidden
```

Root cause (proven by the worker VM's kubelet static-pod manifest `google-container-manifest`): Dataflow Runner v2 runs each SDK harness as a **kubelet pod**, and that pod has **no `imagePullSecret`**. Google's own worker containers (`vmmonitor`/`healthchecker`/`harness`) in the same pod pull from `*-artifactregistry.gcr.io` and succeed via kubelet's **cloud credential provider** (`DisableKubeletCloudCredentialProviders: false`) using the worker SA's IAM. That provider only mints tokens for GCR/Artifact Registry — **never a third-party registry**. So the third-party `sdk-0-0` image is pulled anonymously → 403.

What does **not** fix it (all verified):
- The Flex Template `ContainerSpec.imageRepository{Username,Password}SecretId` — authenticates only the **launcher** VM, and is absent from the worker pod manifest. Worker-SA access to the secret + valid creds (a manual `docker pull` with them succeeds) still 403, because the kubelet pod never attaches them.
- Network — the harness VM carries the network tags that open egress to the registry and the egress firewall shows live hits; the 403 is an HTTP auth response, not a connectivity failure.
- Baking creds into the image / a worker startup hook — the pull precedes the container, so neither can authenticate it.
- The worker VM user-data is managed (no pre-pull/`IfNotPresent` injection point).

Dataflow's only supported worker-pull auth for a custom container is **GCR/Artifact Registry + worker-SA IAM** ([Dataflow custom containers](https://cloud.google.com/dataflow/docs/guides/using-custom-containers)).

## Decision

Build the image in CI and push it to **Artifact Registry** in the project that runs the jobs. Both the Flex Template **launcher** (`--image`) and the Dataflow **workers** (`sdk_container_image`) run that single AR image, pulled via the Dataflow worker service account's IAM (`roles/artifactregistry.reader`) — exactly like Google's own worker containers. A private registry may still serve the Beam SDK **base** image pulled *during the Docker build*.

- The image build pushes the runtime image to AR (`${AR_LOCATION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${IMAGE_NAME}:$RELEASE_VERSION`). The baked `SDFB_SDK_CONTAINER_IMAGE` (worker, via `run_pipeline` → `WorkerOptions.sdk_container_image`) is that AR coordinate.
- The Flex Template build uses `--image=<AR coordinate>` and **no** `imageRepository*SecretId` — AR auth is IAM, not a registry password.
- One-time setup: an AR Docker repo (Terraform or `gcloud artifacts repositories create`); `roles/artifactregistry.writer` for the CI build SA; `roles/artifactregistry.reader` for the Dataflow worker SA (covers the launcher VM pull AND the worker pod pull). `public_cloud/deploy/gcp/` provisions exactly this on a personal project ([ADR 0016](0016-personal-gcp-cloud-build.md)).

## Consequences

- **Unblocks** the §11 worker SDK-harness pull — AR-via-IAM is the only Dataflow-supported mechanism for a custom worker container; a non-Google registry cannot authenticate the worker pod.
- **Amends ADR 0003**: the **runtime image lives in Artifact Registry**, not a third-party registry. One registry for the runtime image avoids the launcher-vs-worker split.
- **Simplifies auth**: no `imageRepository*SecretId`, no baked registry creds, no Secret Manager in the pull path. One IAM grant (`artifactregistry.reader` for the worker SA) covers both the launcher VM and worker pod pulls; CI needs `artifactregistry.writer`.
- **Newly exercised**: the launcher VM now pulls `--image` from AR (it previously pulled from the third-party registry). This relies on the launch SA's AR IAM — verify on the first run. (An earlier interim kept the launcher on the third-party registry while only the worker moved to AR; consolidated to all-AR to remove the confusion.)
- **Costs**: AR storage; the immutable `:<branch>-<sha>` tag now lives in AR (still never `:latest`). The GCS `sdfb-latest-template.json` pointer indirection in the Flex Template deploy step is unchanged.
