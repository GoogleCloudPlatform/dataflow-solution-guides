# Real-Time IoT Analytics & Predictive Maintenance

Organizations employ Internet of Things (IoT) sensors to monitor connected vehicle fleets and manufacturing equipment in real-time. These sensors gather critical telemetry on various operating conditions—such as engine temperatures, vibration levels, speed, and mileage. To convert raw operational telemetry into predictive intelligence, enterprises need low-latency stream processing to detect early warning signs of equipment failure and trigger maintenance before costly breakdowns occur.

This reference architecture demonstrates how to ingest streaming vehicle telemetry via **Cloud Pub/Sub**, perform stateful metric aggregation over sliding time windows, enrich operational records with historical maintenance data from **Cloud Bigtable**, apply on-worker **Scikit-Learn Machine Learning inference** using Apache Beam's `RunInference` to predict maintenance needs, persist analytical records to **BigQuery** via the Storage Write API, and emit real-time alert notifications to a **Cloud Pub/Sub** alert topic for immediate intervention.

## Documentation

- [One pager: IoT analytics in real-time with Dataflow (PDF)](./one_pagers/iot_analytics_dataflowonepager.pdf)
- [IoT Analytics Solution Guide & Architecture (PDF)](./guides/iot_analytics_dataflow_guide.pdf)

## Assets included in this repository

- [Terraform code to deploy infrastructure for IoT Analytics](../terraform/iot_analytics/)
- [Sample streaming pipeline in Python with Bigtable enrichment and Scikit-Learn RunInference](../pipelines/iot_analytics/)

## Technical benefits

Dataflow is the premier platform for building real-time IoT stream processing and predictive maintenance applications:

- **Stateful Windowed Stream Processing**:
  - Apache Beam's state and timer APIs (`StateSpec`, `TimerSpec`) group telemetry streams by asset (`vehicle_id`) and track rolling metric extremes (maximum temperature, peak vibration) with deterministic cleanup on window expiration, preventing memory leaks in 24/7 streaming environments.
- **Ultra-Low Latency Metadata Enrichment with Cloud Bigtable**:
  - Cloud Bigtable delivers single-digit millisecond row lookups at petabyte scale, allowing Dataflow workers to enrich streaming sensor readings with historical maintenance records (last service date, past repair types, engine model) without bottlenecking throughput.
- **In-Worker ML Inference (`RunInference`)**:
  - [RunInference](https://beam.apache.org/documentation/ml/about-ml/#use-runinference) with `SklearnModelHandlerNumpy` executes the trained predictive maintenance model directly on worker threads, eliminating remote network RPC overhead, avoiding API quota limits, and minimizing worker compute costs on standard `n2-standard-4` CPU instances.
- **Dual-Sink Real-Time Analytics & Alerting**:
  - Processed records and inference predictions are streamed into **BigQuery** using the high-performance Storage Write API for fleet analytics and dashboards, while immediate maintenance flags (`needs_maintenance == 1`) are simultaneously routed to a dedicated **Pub/Sub alert topic** for downstream work-order dispatching and telemetry monitors.
- **Extensible & Portable Architecture**:
  - The pipeline uses standard Apache Beam constructs that run seamlessly on Google Cloud Dataflow or locally with `DirectRunner` for rapid prototyping and unit testing.

## How to deploy (Agentic or Manual)

### Option 1: Deploy with an AI Coding Harness (Recommended)

Open this repository in your AI coding harness and prompt it to provision the infrastructure, seed Bigtable maintenance history, launch the streaming pipeline, and verify predictive maintenance alerts:

> *"Deploy the IoT Analytics guide in project `my-gcp-project` and region `us-central1` (auto-discovering any Shared VPC subnet and existing staging bucket). Build the custom worker container, populate Cloud Bigtable with historical vehicle maintenance records, launch the Dataflow pipeline with private IPs, publish synthetic vehicle sensor telemetry, and verify that predictions and maintenance alerts land in BigQuery and Pub/Sub."*

### Option 2: Manual CLI Deployment

Follow the step-by-step instructions in the [Terraform module README](../terraform/iot_analytics/README.md) and [Pipeline README](../pipelines/iot_analytics/README.md):

```bash
# 1. Provision Cloud Bigtable, BigQuery, Pub/Sub & Artifact Registry
cd terraform/iot_analytics
terraform init && terraform apply

# 2. Build container, seed Bigtable & submit the Dataflow job
cd ../../pipelines/iot_analytics
source scripts/00_set_environment.sh
./scripts/01_cloud_build_and_push.sh
python3 scripts/create_and_populate_bigtable.py
./scripts/02_submit_job.sh

# 3. Publish simulated vehicle sensor telemetry
python3 scripts/publish_on_pubsub.py
```

To tear down the deployment, cancel the active Dataflow job first, then run `terraform destroy` in `terraform/iot_analytics`.
