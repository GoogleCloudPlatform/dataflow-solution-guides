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

FROM python:3.14-slim
WORKDIR /workspace

RUN apt-get update -y && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.txt
COPY main.py main.py
COPY ml_ai_pipeline ml_ai_pipeline
COPY MANIFEST.in MANIFEST.in
COPY setup.py setup.py

RUN pip install --upgrade --no-cache-dir pip \
  && pip install --no-cache-dir -r requirements.txt \
  && pip install --no-cache-dir --no-deps git+https://github.com/keras-team/keras-hub.git \
  && pip install --no-cache-dir -e .

ARG MODEL_PRESET="gemma4_instruct_2b"
ARG KAGGLE_USERNAME=""
ARG KAGGLE_KEY=""
ARG HF_TOKEN=""

ENV MODEL_PRESET=${MODEL_PRESET}
ENV MODEL_DIR="/opt/models/${MODEL_PRESET}"

# Copy pre-staged model directory if present
COPY gemma/ /opt/models/gemma/

# Pre-download and bake model weights into container image during build
RUN if [ -f "/opt/models/gemma/config.json" ]; then \
      echo "Model weights found in build context."; \
    elif [ -n "${KAGGLE_USERNAME}" ] || [ -n "${HF_TOKEN}" ]; then \
      echo "Downloading and baking model preset ${MODEL_PRESET} into ${MODEL_DIR}..."; \
      KAGGLE_USERNAME=${KAGGLE_USERNAME} KAGGLE_KEY=${KAGGLE_KEY} HF_TOKEN=${HF_TOKEN} \
      python -m ml_ai_pipeline.download_model "${MODEL_DIR}"; \
    else \
      echo "Attempting anonymous pre-download of ${MODEL_PRESET} into ${MODEL_DIR}..."; \
      python -m ml_ai_pipeline.download_model "${MODEL_DIR}" || echo "Pre-download skipped (credentials required for gated models)."; \
    fi

# Copy files from official SDK image, including script/dependencies.
COPY --from=apache/beam_python3.14_sdk:2.75.0 /opt/apache/beam /opt/apache/beam

ENV KERAS_BACKEND="jax"
ENV XLA_PYTHON_CLIENT_PREALLOCATE="false"
ENV XLA_PYTHON_CLIENT_MEM_FRACTION="0.75"

# Set the entrypoint to Apache Beam SDK launcher.
ENTRYPOINT ["/opt/apache/beam/boot"]