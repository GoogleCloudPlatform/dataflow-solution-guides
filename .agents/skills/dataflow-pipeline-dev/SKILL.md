---
name: dataflow-pipeline-dev
description: >-
  Develop, format, lint, package, and test Apache Beam Dataflow pipelines in Python and Java within this repository.
  Use when modifying existing pipeline DoFns, creating new Beam pipelines, configuring pipeline options,
  running local tests with DirectRunner, building custom SDK container images with Cloud Build, or fixing PyLint and Spotless style violations.
---

# Dataflow Pipeline Development Skill

This skill guides the agent through developing, modifying, testing, linting, packaging, and building Apache Beam streaming pipelines in Python and Java for Google Cloud Dataflow.

---

## 1. Python Pipeline Development Workflow

Python pipelines are located in `pipelines/<use_case>/`.

### Step 1: Environment & Dependencies
Ensure Python 3.11+ is used. In the pipeline directory:
```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install requirements
pip install -r requirements.txt
if [ -f "requirements-dev.txt" ]; then
  pip install -r requirements-dev.txt
fi
```

### Step 2: Code Formatting (Google Style)
Run `yapf` across the pipeline directory:
```bash
yapf -i -r --style yapf .
```

### Step 3: Linting with Google PyLint Configuration
Lint the code against the shared root `pipelines/pylintrc` config:
```bash
pylint --rcfile ../pylintrc .
```
Fix all lint errors (e.g. docstrings, naming conventions, import ordering).

### Step 4: Package Validation
Verify that the `setup.py` packages all sub-modules correctly for Dataflow workers:
```bash
python setup.py sdist
```

### Step 5: Local Execution with DirectRunner
Test pipeline execution locally before submitting to the cloud:
```bash
python main.py \
  --runner=DirectRunner \
  --project=test-project \
  --temp_location=/tmp/dataflow-temp
```

### Step 6: Custom SDK Container Build (if required)
For pipelines using GPU acceleration, custom C/Python libraries, or specialized base images (e.g. `ml_ai_python`, `anomaly_detection`, `cdp`, `iot_analytics`, `marketing_intelligence`):
- **SDK Version Parity**: Verify that the `apache/beam_python3.11_sdk:<version>` tag in `Dockerfile` matches `requirements.txt` (`apache-beam[gcp]==<version>`).
```bash
# Set required image tag
export CONTAINER_URI="gcr.io/$PROJECT/dataflow-ml-custom:latest"

# Build and push using Cloud Build
gcloud builds submit \
  --region=$REGION \
  --default-buckets-behavior=regional-user-owned-bucket \
  --substitutions _TAG=$CONTAINER_URI \
  .
```

---

## 2. Java Pipeline Development Workflow

Java pipelines are located in `pipelines/<use_case>_java/`.

### Step 1: Gradle Build & Test
Verify code compilation and test execution:
```bash
./gradlew build
```

### Step 2: Code Formatting (Spotless)
Apply Google Java Style automatically:
```bash
./gradlew spotlessApply
```

### Step 3: Local Testing with DirectRunner
Run the pipeline locally:
```bash
./gradlew run -Pargs="--runner=DirectRunner --project=test-project --gcpTempLocation=/tmp/dataflow-temp"
```

---

## 3. Apache Beam Best Practices in this Repo

1. **SDK & Custom Container Parity**:
   The Apache Beam SDK version in the pipeline code (`requirements.txt`) and worker container (`Dockerfile`) must be identical. Version divergence leads to serialization failures, container harness mismatch, and worker initialization crashes on Dataflow.
2. **Option Classes**:
   Define pipeline arguments by subclassing `PipelineOptions` (Python) or `PipelineOptions` interface (Java) in `options.py` / `options/`.
3. **Worker Isolation**:
   Always enforce private IP communication:
   - Python: `--no_use_public_ip`
   - Java: `--usePublicIps=false`
4. **Dead-Letter Queues (Side Outputs)**:
   For unparseable or error records, route failed elements to a dead-letter output or BigQuery error table rather than crashing worker threads:
   ```python
   # Python side-output pattern
   parsed_records, errors = (
       raw_records
       | 'ParseRecords' >> beam.ParDo(ParseDoFn()).with_outputs('errors', main='valid')
   )
   ```
5. **Metrics Instrumentation**:
   Use Beam metrics to track throughput and error counts:
   ```python
   self.processed_counter = Metrics.counter(self.__class__, 'processed_elements')
   self.processed_counter.inc()
   ```
