FROM apache/beam_python3.12_sdk:2.57.0
WORKDIR /workspace

RUN apt-get update -y && apt-get install -y \
    cmake

COPY requirements.txt requirements.txt
COPY main.py main.py
COPY cdp_pipeline cdp_pipeline
COPY schema schema
COPY MANIFEST.in MANIFEST.in
COPY setup.py setup.py

RUN pip install --upgrade --no-cache-dir pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -e .

# Copy files from official SDK image, including script/dependencies.
COPY --from=apache/beam_python3.12_sdk:2.57.0 /opt/apache/beam /opt/apache/beam

# Set the entrypoint to Apache Beam SDK launcher.
ENTRYPOINT ["/opt/apache/beam/boot"]