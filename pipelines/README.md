# Dataflow Pipelines for the Solution Guides

This directory contains production-ready Apache Beam pipelines for the Dataflow Solution Guides, demonstrating real-time streaming and batch AI processing patterns on Google Cloud Dataflow.

The pipelines are implemented in **Python 3.14**, **Java 25**, and **Dataflow Flex Templates**. Each pipeline directory includes a detailed `README.md` covering its architecture, inputs, outputs, custom containers, data generators, and verification queries.

---

## Running & Developing with an AI Coding Harness vs. Manually

### Option 1: Agentic Execution, Development & Troubleshooting (Recommended)

When working in an AI coding harness, the repository's [`pipelines/AGENTS.md`](./AGENTS.md) instructions and workspace skills in [`../.agents/skills/`](../.agents/skills/) can build, test, launch, and debug pipelines for you:

- **[`use-case-deployment`](../.agents/skills/use-case-deployment/SKILL.md)**: Sources the Terraform-generated environment script, builds custom SDK/training/serving containers in Cloud Build, submits the Dataflow job with private IPs, runs synthetic data generators, and validates live outputs in BigQuery, Spanner, Bigtable, or Pub/Sub.
- **[`dataflow-pipeline-dev`](../.agents/skills/dataflow-pipeline-dev/SKILL.md)**: Runs local unit tests (`pytest` or `./gradlew test`), verifies local execution with `DirectRunner`, formats code (`yapf` or `./gradlew spotlessApply`), and runs style checks (`pylint --rcfile ../pylintrc .`).
- **[`dataflow-troubleshooting`](../.agents/skills/dataflow-troubleshooting/SKILL.md)**: Diagnoses worker startup issues, IAM errors, Private Google Access / Cloud NAT / firewall issues, GPU quota errors, or Beam SDK/container version parity mismatches.

**Example prompts:**
> *"Build the custom container and launch the Customer Data Platform pipeline in my project, run the transaction generator for 60 seconds, and verify that session profiles land in BigQuery."*
>
> *"Run the unit tests, formatter, and linter across `pipelines/iot_analytics` to make sure my changes pass CI."*

### Option 2: Manual Step-by-Step Execution

1. **Provision Infrastructure First**:
   Make sure you have applied the corresponding Terraform module in [`../terraform/<use_case>/`](../terraform/). Applying Terraform automatically generates an environment script (`scripts/00_set_environment.sh`, `scripts/00_set_variables.sh`, or `scripts/01_set_variables.sh`) inside the pipeline's `scripts/` directory.
2. **Load Environment Variables**:
   ```bash
   cd <pipeline_directory>
   source scripts/00_set_environment.sh   # (or scripts/00_set_variables.sh / 01_set_variables.sh)
   ```
3. **Build Custom Containers (if applicable)**:
   ```bash
   ./scripts/01_build_and_push_container.sh
   ```
4. **Launch the Pipeline & Simulate Data**:
   Follow the pipeline's `README.md` to run the launch script (e.g., `./scripts/02_run_dataflow.sh` or `./scripts/01_launch_pipeline.sh`) and synthetic event publisher.

---

## Pipelines Catalog

| Use case | Programming language / Runtime | Key Technologies & Beam Transforms | Location |
| :--- | :---: | :--- | :---: |
| **GenAI & ML Inference** | Python 3.14 | `RunInference`, Gemma 4, vLLM on NVIDIA L4 GPU, Pub/Sub | [`ml_ai_python`](./ml_ai_python/) |
| **ETL & Integration** | Java 25 & Flex Template | Pub/Sub to Cloud Spanner, Spanner Change Streams CDC to BigQuery | [`etl_integration_java`](./etl_integration_java/) |
| **Customer Data Platform** | Python 3.14 | Multi-stream Pub/Sub join, `Sessions` windowing, BigQuery Storage Write API & DLQ | [`cdp`](./cdp/) |
| **Anomaly Detection** | Python 3.14 | Custom Python 3.14 Vertex AI training/serving containers, Bigtable enrichment, keyed `RunInference` | [`anomaly_detection`](./anomaly_detection/) |
| **Marketing Intelligence** | Python 3.14 | Cloud Firestore enrichment, local Scikit-Learn `RunInference`, BigQuery & Pub/Sub | [`marketing_intelligence`](./marketing_intelligence/) |
| **Log Replication** | Dataflow Flex Template | Cloud Logging & Pub/Sub to Splunk HTTP Event Collector (HEC) | [`log_replication_splunk`](./log_replication_splunk/) |
| **Clickstream Analytics** | Java 25 | Cloud Bigtable lookup enrichment, session windowing, BigQuery Storage Write API UPSERTs & DLQ | [`clickstream_analytics_java`](./clickstream_analytics_java/) |
| **IoT Analytics** | Python 3.14 | Stateful `DoFn` timers & windowed aggregation, Bigtable enrichment, Scikit-Learn `RunInference` | [`iot_analytics`](./iot_analytics/) |
| **Gaming Analytics** | Java 25 (Cross-Language Python) | Cloud Bigtable feature store lookup, cross-language `RunInference` (Portable Runner), Pub/Sub & BigQuery | [`gaming_analytics_java`](./gaming_analytics_java/) |
| **Synthetic Data Generation** | Python 3.14 (Flex Template) | Batch relational synthetic data generation, in-worker vLLM on NVIDIA L4/T4 GPU, BigQuery validation | [`synthetic-llm-dataflow-bigquery`](./synthetic-llm-dataflow-bigquery/) |
