#  Copyright 2025 Google LLC
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

ARG SERVING_BUILD_IMAGE=tensorflow/tensorflow:2.18.0-gpu
FROM ${SERVING_BUILD_IMAGE}
WORKDIR /workspace

RUN apt-get update -y && apt-get install -y \
  cmake

COPY requirements.txt requirements.txt
COPY main.py main.py
COPY ml_ai_pipeline ml_ai_pipeline
COPY MANIFEST.in MANIFEST.in
COPY setup.py setup.py

RUN pip install --upgrade --no-cache-dir pip \
  && pip install --no-cache-dir -r requirements.txt \
  && pip install --no-cache-dir -e .

# Copy files from official SDK image, including script/dependencies.
COPY --from=apache/beam_python3.11_sdk:2.63.0 /opt/apache/beam /opt/apache/beam

COPY gemma_2B gemma_2B

ENV KERAS_BACKEND="tensorflow"

# Set the entrypoint to Apache Beam SDK launcher.
ENTRYPOINT ["/opt/apache/beam/boot"]