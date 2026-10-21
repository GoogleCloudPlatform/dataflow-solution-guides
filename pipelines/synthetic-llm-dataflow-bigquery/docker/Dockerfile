#  Copyright 2026 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

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
# Two jobs, one image (ADR 0041, amendment of 2026-10-06):
#   The image also carries the evaluator (packages/sdfb-evaluation), which
#   scores a landed run against its source in a SEPARATE, CPU-only Dataflow
#   job. FLEX_TEMPLATE_PYTHON_PY_FILE is a small dispatcher
#   (docker/flex_entry.py) that reads the template parameter `sdfb_job`:
#   - absent / `generation` -> sdfb_beam.cli.run_pipeline (as before)
#   - `evaluation`          -> sdfb_evaluation.cli.run_evaluation
#   The evaluator has NO environment of its own here: its source is copied in
#   and it runs on what `uv sync` installs for the generator. Its own
#   pyproject.toml and uv.lock serve development and CI only. A tree without
#   packages/sdfb-evaluation still builds: the image then runs generation only.
#
# Base images (both public; override with --build-arg to use a mirror):
#   Beam SDK: apache/beam_python3.14_sdk:2.76.0 (Docker Hub)
#     → amd64, Debian-based, Python 3.14, /opt/apache/beam/boot included
#     → apache-beam pinned ==2.76.0 in packages/sdfb-beam to match (Runner v2
#       needs the boot binary and the wheel at the same version)
#   Launcher: gcr.io/dataflow-templates-base/python314-template-launcher-base
#     → only /opt/google/dataflow/python_template_launcher is copied from it
#
# No system CUDA install: vLLM/torch wheels bundle their own CUDA runtime libs
# (libcudart, libnccl, etc.); the NVIDIA *driver* is provided at runtime by
# Dataflow's `install-nvidia-driver` experiment (mounted at /usr/local/nvidia/).
# ---------------------------------------------------------------------------

ARG BEAM_SDK_IMAGE="apache/beam_python3.14_sdk:2.76.0"
ARG LAUNCHER_IMAGE="gcr.io/dataflow-templates-base/python314-template-launcher-base"

# ---------------------------------------------------------------------------
# Stage 1 — Flex Template launcher (just need the binary).
# ---------------------------------------------------------------------------
FROM ${LAUNCHER_IMAGE} AS flex_launcher

# ---------------------------------------------------------------------------
# Stage 2 — the evaluator's source, when this tree has it (thrown away).
#
# `COPY packages/sdfb-evaluation/src …` would fail the build in a tree without
# that directory, and the image must still build there (it then runs
# generation only). So this stage copies `packages/` (present in every tree)
# and looks for the directory in shell: /out holds `src/` when it exists and
# is empty otherwise. Plain multi-stage syntax, so any builder builds it. It
# starts from the Beam SDK image because the build pulls that one anyway (no
# third base image).
# ---------------------------------------------------------------------------
FROM ${BEAM_SDK_IMAGE} AS evaluator_source
COPY packages /stage/packages
RUN mkdir -p /out \
    && if [ -d /stage/packages/sdfb-evaluation/src ]; then \
         cp -R /stage/packages/sdfb-evaluation/src /out/src; \
       fi

# ---------------------------------------------------------------------------
# Stage 3 — Final image: Beam SDK base (amd64, Python 3.14, has /opt/apache/beam).
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
# The evaluator's source: /workspace/packages/sdfb-evaluation/src when the
# tree has it, an empty /workspace/packages/sdfb-evaluation/ otherwise. It is
# not installed (no second `uv sync`, no second venv): the two path entries
# below are what make it importable.
COPY --from=evaluator_source /out/ packages/sdfb-evaluation/

# Runtime config read at launch from WORKDIR /workspace: thresholds
# (--thresholds_uri) and the relational models (--relationships_uri
# defaults to config/relationships, ADR 0032 — a real model file dropped
# in that folder is gitignored but DOES ship in the image; a gs:// URI
# overrides it per launch). Also ships the bq_schemas.
COPY config config

# Two execution contexts import sdfb-core/sdfb-beam, both from /workspace/.venv:
#   - LAUNCHER (`python flex_entry.py`): honors PYTHONPATH, so the venv
#     site-packages + the editable src dirs go on it directly.
#   - WORKER: Beam >= 2.44 builds its OWN --system-site-packages venv at startup
#     and DISCARDS the container's venv + PYTHONPATH (Google "Build custom
#     container images for Dataflow"). It does inherit the GLOBAL site dir and
#     processes its .pth files — so a bridge .pth there runs site.addsitedir() on
#     our venv, activating the deps AND the editable sdfb-* finders. That is what
#     resolves the worker `ModuleNotFoundError: No module named 'sdfb_core'`.
# The evaluator is not installed in the venv, so it has no finder there: its
# src dir is named in both places, LAST on PYTHONPATH and as a second line of
# the bridge (a plain path line of a .pth file; `site` adds it only when the
# directory exists, so it does nothing in an image built without the evaluator).
ENV PYTHONPATH="/workspace/.venv/lib/python3.14/site-packages:/workspace/packages/sdfb-core/src:/workspace/packages/sdfb-beam/src:/workspace/packages/sdfb-evaluation/src"
RUN SITE="$(python -c 'import site; print(site.getsitepackages()[0])')" \
    && printf "import site; site.addsitedir('/workspace/.venv/lib/python3.14/site-packages')\n/workspace/packages/sdfb-evaluation/src\n" \
        > "${SITE}/zz_sdfb_bridge.pth" \
    && echo "Wrote worker bridge .pth -> ${SITE}/zz_sdfb_bridge.pth"

# Build-time check. The evaluator runs on the GENERATOR's environment, and three
# of its dependencies (numpy, scipy, scikit-learn) are there only through the
# `uv sync` line above (scipy and scikit-learn through an extra). When this tree
# has the evaluator, the image must import its two entry modules now, so an
# image that lost something the evaluator needs fails at BUILD, not at the first
# evaluation launch:
#   - the launcher's: ENV PYTHONPATH as set above;
#   - the worker's:   PYTHONPATH empty, the bridge .pth alone;
#   - the venv's own: `/workspace/.venv/bin/python`, PYTHONPATH empty. The
#     first two run the base image's global interpreter, which may ship those
#     packages itself; this one cannot be satisfied by the base image, so an
#     image built without the extra fails whatever the base holds.
# Each prints the version and file of numpy, scipy and scikit-learn it
# resolved, so the first build log shows what a worker will import. Without the
# evaluator the step only says so.
RUN if [ -d /workspace/packages/sdfb-evaluation/src/sdfb_evaluation ]; then \
      SHOW='import numpy, scipy, sklearn; [print("  " + m.__name__ + " " + m.__version__ + " " + m.__file__) for m in (numpy, scipy, sklearn)]' \
      && echo "sdfb-evaluation: importing its entry modules (launcher context)" \
      && python -c "import sdfb_evaluation.cli.run_evaluation, sdfb_evaluation.beam.pipeline; ${SHOW}" \
      && echo "sdfb-evaluation: importing its entry modules (worker context)" \
      && PYTHONPATH= python -c "import sdfb_evaluation.cli.run_evaluation, sdfb_evaluation.beam.pipeline; ${SHOW}" \
      && echo "sdfb-evaluation: importing numpy, scipy, scikit-learn (the image's own venv)" \
      && PYTHONPATH= /workspace/.venv/bin/python -c "${SHOW}" \
      && echo "sdfb-evaluation: importable in both contexts and in the image's own venv"; \
    else \
      echo "no packages/sdfb-evaluation in the build context: this image runs generation only"; \
    fi

# NVIDIA driver mount point (populated by Dataflow's install-nvidia-driver).
ENV LD_LIBRARY_PATH="/usr/local/nvidia/lib64:${LD_LIBRARY_PATH}" \
    PATH="/usr/local/nvidia/bin:${PATH}"

# Flex Template launcher contract: one entry for both jobs (copied below,
# beside the dispatch entrypoint). Without the template parameter `sdfb_job`
# it calls sdfb_beam.cli.run_pipeline.main with the launch's arguments.
ENV FLEX_TEMPLATE_PYTHON_PY_FILE="/opt/sdfb/flex_entry.py"

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
# The evaluator's driver reads its own variable for the same purpose. Same
# value, same build argument: an evaluation job's workers run THIS image too.
ENV SDFB_EVAL_SDK_CONTAINER_IMAGE=$SDFB_SDK_CONTAINER_IMAGE_ARG

# Dispatch entrypoint: launcher binary for the Flex launch role, /opt/apache/beam/boot
# for the worker harness role. See docker/entrypoint.sh and the header comment.
COPY docker/entrypoint.sh /opt/sdfb/entrypoint.sh
RUN chmod +x /opt/sdfb/entrypoint.sh

# The Flex Template entry named by FLEX_TEMPLATE_PYTHON_PY_FILE above.
COPY docker/flex_entry.py /opt/sdfb/flex_entry.py

ENTRYPOINT ["/opt/sdfb/entrypoint.sh"]
