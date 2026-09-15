# synthetic-dataflow-bigquery — single image for BOTH the Flex Template
# launcher and the Dataflow custom-container workers.
#
# How Dataflow uses this image:
#   Dataflow runs the image ENTRYPOINT for BOTH roles and APPENDS args — it does
#   NOT override the entrypoint. So the ENTRYPOINT is a dispatch script
#   (docker/entrypoint.sh) that picks the right binary from the args:
#   - Flex Template launch:  no FnAPI flags -> exec
#     /opt/google/dataflow/python_template_launcher, which reads
#     FLEX_TEMPLATE_PYTHON_PY_FILE and submits the job.
#   - Dataflow workers:      Dataflow appends the Beam FnAPI boot flags
#     (--id / --logging_endpoint / --control_endpoint / --artifact_endpoint /
#     --provision_endpoint) -> exec /opt/apache/beam/boot. (Passing these to the
#     launcher binary is what caused the sdk-0-0 CrashLoopBackOff: "flag provided
#     but not defined: -logging_endpoint".)
#
# REFs:
#   https://cloud.google.com/dataflow/docs/guides/templates/configuring-flex-templates
#   https://cloud.google.com/dataflow/docs/guides/build-container-image
#   https://docs.vllm.ai/en/latest/getting_started/installation.html
#
# ADR-0009 (single image, two entrypoint paths), ADR-0008 (CI-driven build),
# ADR-0015 (Artifact Registry) carry the locked decisions.
#
# Base images (both public; override with --build-arg to use a mirror):
#   Beam SDK: apache/beam_python3.11_sdk:2.74.0 (Docker Hub)
#     → amd64, Debian-based, Python 3.11, /opt/apache/beam/boot included
#     → apache-beam pinned ==2.74.0 in packages/sdfb-beam to match (Runner v2
#       needs the boot binary and the wheel at the same version)
#   Launcher: gcr.io/dataflow-templates-base/python311-template-launcher-base
#     → only /opt/google/dataflow/python_template_launcher is copied from it
#
# No system CUDA install: vLLM/torch wheels bundle their own CUDA runtime libs
# (libcudart, libnccl, etc.); the NVIDIA *driver* is provided at runtime by
# Dataflow's `install-nvidia-driver` experiment (mounted at /usr/local/nvidia/).
# ---------------------------------------------------------------------------

ARG BEAM_SDK_IMAGE="apache/beam_python3.11_sdk:2.74.0"
ARG LAUNCHER_IMAGE="gcr.io/dataflow-templates-base/python311-template-launcher-base"

# ---------------------------------------------------------------------------
# Stage 1 — Flex Template launcher (just need the binary).
# ---------------------------------------------------------------------------
FROM ${LAUNCHER_IMAGE} AS flex_launcher

# ---------------------------------------------------------------------------
# Stage 2 — Final image: Beam SDK base (amd64, Python 3.11, has /opt/apache/beam).
# ---------------------------------------------------------------------------
FROM ${BEAM_SDK_IMAGE}

# Git commit this image was built from — stamped into every launcher and
# worker log as the `build_info` milestone (2026-08-21 four-run cycle:
# same-day runs were indistinguishable by build from the logs alone).
ARG GIT_COMMIT_ARG="unknown"

ENV DEBIAN_FRONTEND=noninteractive \
    SDFB_BUILD_COMMIT=$GIT_COMMIT_ARG \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_NO_CACHE=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    BEAM_PYTHON_SDK_OPTIONS="--experiments=use_runner_v2"

# Overlay Flex Template launcher from GCR image.
# /opt/apache/beam is already in the Beam SDK base — no need to copy it.
COPY --from=flex_launcher /opt/google/dataflow/python_template_launcher /opt/google/dataflow/python_template_launcher

# Bootstrap uv from PyPI (pinned; uv.lock resolves against pypi.org).
RUN pip install --no-cache-dir uv==0.11.15

WORKDIR /workspace

# Layer-cache strategy: pyprojects + lockfile first.
COPY pyproject.toml uv.lock README.md ./
COPY packages/sdfb-core/pyproject.toml packages/sdfb-core/pyproject.toml
COPY packages/sdfb-beam/pyproject.toml packages/sdfb-beam/pyproject.toml
COPY packages/sdfb-tests/pyproject.toml packages/sdfb-tests/pyproject.toml

# Install all deps into the uv venv (/workspace/.venv). vLLM/torch wheels bundle
# their own CUDA runtime libs — no system CUDA needed; the NVIDIA driver is
# mounted by Dataflow at runtime. FLEX_TEMPLATE_PYTHON_REQUIREMENTS_FILE stays
# UNSET so the launcher reuses this pre-baked env. The venv is bridged into the
# GLOBAL site dir further down so the worker harness sees it too (bridge .pth).
RUN uv sync --frozen --no-dev --all-packages \
        --extra gpu --extra embedding --extra library

# Source last (changes here don't invalidate the deps layer).
COPY packages/sdfb-core/src packages/sdfb-core/src
COPY packages/sdfb-beam/src packages/sdfb-beam/src

# Runtime config read at launch from WORKDIR /workspace: thresholds
# (--thresholds_uri) and the relational models (--relationships_uri
# defaults to config/relationships, ADR 0032 — a real model file dropped
# in that folder is gitignored but DOES ship in the image; a gs:// URI
# overrides it per launch). Also ships the bq_schemas.
COPY config config

# Two execution contexts import sdfb-core/sdfb-beam, both from /workspace/.venv:
#   - LAUNCHER (`python run_pipeline.py`): honors PYTHONPATH, so the venv
#     site-packages + the editable src dirs go on it directly.
#   - WORKER: Beam >= 2.44 builds its OWN --system-site-packages venv at startup
#     and DISCARDS the container's venv + PYTHONPATH (Google "Build custom
#     container images for Dataflow"). It does inherit the GLOBAL site dir and
#     processes its .pth files — so a bridge .pth there runs site.addsitedir() on
#     our venv, activating the deps AND the editable sdfb-* finders. That is what
#     resolves the worker `ModuleNotFoundError: No module named 'sdfb_core'`.
ENV PYTHONPATH="/workspace/.venv/lib/python3.11/site-packages:/workspace/packages/sdfb-core/src:/workspace/packages/sdfb-beam/src"
RUN SITE="$(python -c 'import site; print(site.getsitepackages()[0])')" \
    && printf "import site; site.addsitedir('/workspace/.venv/lib/python3.11/site-packages')\n" \
        > "${SITE}/zz_sdfb_bridge.pth" \
    && echo "Wrote worker bridge .pth -> ${SITE}/zz_sdfb_bridge.pth"

# NVIDIA driver mount point (populated by Dataflow's install-nvidia-driver).
ENV LD_LIBRARY_PATH="/usr/local/nvidia/lib64:${LD_LIBRARY_PATH}" \
    PATH="/usr/local/nvidia/bin:${PATH}"

# Flex Template launcher contract.
ENV FLEX_TEMPLATE_PYTHON_PY_FILE="/workspace/packages/sdfb-beam/src/sdfb_beam/cli/run_pipeline.py"

# Worker harness image for Runner v2. The Flex Template's `--image` sets only
# the LAUNCHER; the worker harness image must be set separately and a Flex
# Template can't bake a default (Google "Configure Flex Templates"). So the
# image self-describes its own pushed coordinate and run_pipeline.py applies it
# (WorkerOptions.sdk_container_image) — keeping the sha-free template + DAG
# untouched. Without it, workers boot the stock Beam SDK container (no
# sdfb_core) and DoFn unpickling fails: ModuleNotFoundError: 'sdfb_core'.
# Workflow 1 passes `$IMG:$RELEASE_VERSION`. Declared late so this per-commit
# value never invalidates the cached `uv sync` layer above.
ARG SDFB_SDK_CONTAINER_IMAGE_ARG=""
ENV SDFB_SDK_CONTAINER_IMAGE=$SDFB_SDK_CONTAINER_IMAGE_ARG

# Dispatch entrypoint: launcher binary for the Flex launch role, /opt/apache/beam/boot
# for the worker harness role. See docker/entrypoint.sh and the header comment.
COPY docker/entrypoint.sh /opt/sdfb/entrypoint.sh
RUN chmod +x /opt/sdfb/entrypoint.sh

ENTRYPOINT ["/opt/sdfb/entrypoint.sh"]
