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

# Custom Python SDK harness container for the gaming analytics pipeline.
#
# The pipeline itself is Java, but it scores events with the Apache Beam
# RunInference transform through its cross-language wrapper, so a Dataflow
# Runner v2 worker runs a Java harness and a Python harness side by side. This
# image replaces only the Python one, selected at launch time with
#   --sdkHarnessContainerImageOverrides=".*python.*,<this image>"
# (see scripts/03_launch_pipeline.sh).
#
# Two things are baked in:
#
#  1. The scikit-learn model artifact, at MODEL_PATH below. The pipeline reads
#     it from that local path instead of downloading it from Cloud Storage on
#     every worker.
#
#  2. Its dependencies, pinned. The model is trained *in this image*, by the
#     RUN step below, so the interpreter that pickles the artifact is by
#     construction the same one that later unpickles it in the harness.
#     scikit-learn does not guarantee cross-version pickle compatibility, and
#     this removes that entire class of failure rather than testing for it.
#
# The base image tag must track the Beam version in build.gradle, and its
# Python minor version must match the environment that the expansion service
# declares at pipeline construction time, otherwise the override above will not
# match and Dataflow will silently fall back to the stock Docker Hub harness.
FROM apache/beam_python3.13_sdk:2.76.0

ARG MODEL_PATH=/opt/gaming_analytics/recommender.pkl
ARG TRAINING_SAMPLES=20000
ARG RANDOM_SEED=42

# Installed before the training step so that the dependency layer is cached
# independently of changes to the training script.
COPY scripts/requirements-model.txt /tmp/requirements-model.txt
RUN pip install --no-cache-dir -r /tmp/requirements-model.txt \
    && rm /tmp/requirements-model.txt

COPY scripts/train_model.py /tmp/train_model.py
RUN mkdir -p "$(dirname "${MODEL_PATH}")" \
    && python /tmp/train_model.py \
        --output_path="${MODEL_PATH}" \
        --num_samples="${TRAINING_SAMPLES}" \
        --random_seed="${RANDOM_SEED}" \
    && rm /tmp/train_model.py

# Verifies the artifact in the same environment that will unpickle it on the
# worker, and fails the build if it cannot be loaded or does not return one
# propensity per recommendation label. Without this a silently broken model
# would only surface as a crash-looping Dataflow worker.
RUN python -c "\
import pickle, numpy, sklearn; \
path = '${MODEL_PATH}'; \
model = pickle.load(open(path, 'rb')); \
shape = model.predict(numpy.zeros((1, 7))).shape; \
assert shape == (1, 5), 'unexpected prediction shape: %s' % (shape,); \
print('verified', type(model).__name__, 'at', path); \
print('scikit-learn', sklearn.__version__, 'numpy', numpy.__version__)"

# Recorded so that the image is self-describing: `docker inspect` reveals where
# the artifact lives without having to read this file.
LABEL org.opencontainers.image.title="gaming-analytics-python-harness"
LABEL com.google.dataflow.gaming-analytics.model-path="${MODEL_PATH}"

# The entrypoint (/opt/apache/beam/boot) is inherited from the base image and
# must not be overridden: it is what makes this a usable SDK harness.
