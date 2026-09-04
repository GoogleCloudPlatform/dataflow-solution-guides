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
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.txt
COPY main.py main.py
COPY scripts scripts
COPY iot_analytics_pipeline iot_analytics_pipeline
COPY MANIFEST.in MANIFEST.in
COPY setup.py setup.py
COPY maintenance_model.pkl* ./

RUN pip install --upgrade --no-cache-dir pip \
  && pip install --no-cache-dir -r requirements.txt \
  && pip install --no-cache-dir . \
  && python scripts/model.py --output_path /workspace/maintenance_model.pkl

ENV PYTHONPATH="/workspace:${PYTHONPATH}"

# Copy Apache Beam boot entrypoint binaries
COPY --from=apache/beam_python3.14_sdk:2.76.0 /opt/apache/beam /opt/apache/beam

# Set the entrypoint to Apache Beam SDK launcher.
ENTRYPOINT ["/opt/apache/beam/boot"]