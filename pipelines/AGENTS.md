# Pipelines Directory — Agent Guidelines

This directory contains Apache Beam streaming pipeline implementations in **Python**, **Java**, and **Dataflow Templates** for the Dataflow Solution Guides.

---

## 1. Directory Structure & Pipeline Map

| Pipeline Directory | Language | Primary Technologies & Transforms | Target Solution Guide |
| :--- | :--- | :--- | :--- |
| `ml_ai_python/` | Python | `RunInference`, Gemma 4, vLLM, NVIDIA L4 GPU | [GenAI_ML.md](../use_cases/GenAI_ML.md) |
| `etl_integration_java/` | Java | Pub/Sub to Cloud Spanner, Spanner Change Streams to BigQuery | [ETL_integration.md](../use_cases/ETL_integration.md) |
| `cdp/` | Python | Multi-topic Pub/Sub streaming join, BigQuery streaming insert | [CDP.md](../use_cases/CDP.md) |
| `anomaly_detection/` | Python | External Vertex AI endpoint prediction, Pub/Sub; Bigtable/BigQuery reserved for extensions | [Anomaly_Detection.md](../use_cases/Anomaly_Detection.md) |
| `marketing_intelligence/` | Python | Firestore enrichment, Scikit-Learn RunInference, BigQuery, Pub/Sub | [Marketing_Intelligence.md](../use_cases/Marketing_Intelligence.md) |
| `clickstream_analytics_java/` | Java | Cloud Bigtable enrichment / hydration lookup, BigQueryIO | [Clickstream_Analytics.md](../use_cases/Clickstream_Analytics.md) |
| `iot_analytics/` | Python | Sensor telemetry aggregation, Bigtable hydration, Vertex AI | [IoT_Analytics.md](../use_cases/IoT_Analytics.md) |
| `log_replication_splunk/` | Flex Template | Pub/Sub to Splunk HTTP Event Collector (HEC) | [Log_replication.md](../use_cases/Log_replication.md) |

---

## 2. Python Pipeline Development Standards

### Pipeline Layout Pattern
Every Python pipeline package follows this standard layout:
```
pipelines/<name>/
├── Dockerfile                   # Custom worker image definition
├── cloudbuild.yaml              # Cloud Build build & push configuration
├── setup.py                     # Package definition for Dataflow workers
├── main.py                      # Main pipeline entrypoint & option parsing
├── <name>_pipeline/             # Package directory containing pipeline DoFns
│   ├── __init__.py
│   ├── options.py               # Custom PipelineOptions subclasses
│   └── pipeline.py              # Beam pipeline graph definition
├── requirements.txt             # Pipeline runtime dependencies
├── requirements-dev.txt         # Dev / test dependencies
└── scripts/                     # Launch and automation scripts
    ├── 01_build_and_push_container.sh
    └── 02_run_dataflow.sh
```

### Formatting & Linting
- **Yapf**: Run from the pipeline subdirectory:
  ```bash
  yapf -i -r --style yapf .
  ```
- **Pylint**: Must use the configuration at `pipelines/pylintrc`:
  ```bash
  pylint --rcfile ../pylintrc .
  ```
- **Package verification**:
  ```bash
  python setup.py sdist
  ```

### Local Testing with DirectRunner
Always verify pipeline graph construction and transformation logic locally before submitting to Dataflow:
```bash
python main.py \
  --runner=DirectRunner \
  --project=test-project \
  --temp_location=/tmp/beam-temp
```

---

## 3. Java Pipeline Development Standards

### Gradle Wrapper
Java pipelines use Gradle with standard Google Cloud Dataflow plugins and spotless code formatting.
- **Compile and Test**:
  ```bash
  ./gradlew build
  ```
- **Apply Code Formatting**:
  ```bash
  ./gradlew spotlessApply
  ```
- **Local Run**:
  ```bash
  ./gradlew run -Pargs="--runner=DirectRunner [arguments...]"
  ```

---

## 4. Custom Containers & Cloud Build

For pipelines leveraging custom dependencies or machine learning models on GPU workers (e.g. `ml_ai_python`):
1. **SDK and Container Version Parity (Mandatory)**:
   The `apache/beam_python3.13_sdk:<version>` or `apache/beam_python3.14_sdk:<version>` tag in `Dockerfile` (both `FROM` and `COPY --from=...`) **must strictly match** the `apache-beam[gcp]==<version>` dependency pinned in `requirements.txt`.
   - Never upgrade container image tags without verifying that the matching stable `apache-beam` release is available on PyPI and updating `requirements.txt` in tandem.
   - A version mismatch between the pipeline submission environment and the worker container harness will cause serialization errors or worker startup failures.
2. The `Dockerfile` builds on top of `apache/beam_python3.13_sdk` or `apache/beam_python3.14_sdk`.
3. Cloud Build builds and registers the container image in Google Artifact Registry / Google Container Registry:
   ```bash
   gcloud builds submit \
     --region=$REGION \
     --default-buckets-behavior=regional-user-owned-bucket \
     --substitutions _TAG=$CONTAINER_URI \
     .
   ```
4. When submitting the pipeline to Dataflow, specify:
   ```bash
   --sdk_container_image=$CONTAINER_URI
   ```

---

## 5. Security & Worker Best Practices

- **Private Networking**: Always pass `--no_use_public_ip` (Python) or `--usePublicIps=false` (Java).
- **Service Accounts**: Always specify a dedicated service account via `--service_account_email` or `--serviceAccount`.
- **Worker Scaling**: Set conservative bounds (`--num_workers=1`, `--max_num_workers=...`) for demo and development environments.
- **Streaming Engine**: Enable streaming engine with `--enableStreamingEngine` (or `--experiments=enable_streaming_engine`) for low-latency streaming pipelines.

### Anomaly detection deployment

Anomaly detection uses an existing project and network, an existing bucket by default, and a user-supplied Vertex AI endpoint with a deployed model. Source generated `scripts/00_set_variables.sh`, set `MODEL_ENDPOINT` (optional `MODEL_LOCATION`, default `REGION`), then run the build and launch scripts. Input is `transactions` via `transactions-sub`; output is `detections`. `SUBNETWORK` is optional with legacy `NETWORK` fallback. Existing networks must provide Private Google Access, worker TCP 12345/12346 communication and NAT where needed. Before migrating existing state, follow `terraform/anomaly_detection/README.md` to transfer foundation and bucket ownership safely.
