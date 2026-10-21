# Clickstream analytics

In the fast-paced digital landscape, understanding user behavior is crucial for optimizing websites, apps,
and marketing campaigns. Clickstream analytics provides a continuous stream of data on how users interact
with digital platforms. But to truly capitalize on this information, businesses need insights in real time,
not days later.

For the full version of this solution guide, please refer to:

## Documentation

- [One pager: Clickstream analytics in real-time with Dataflow (PDF)](./one_pagers/clickstream_dataflow_onepager.pdf)

## Assets included in this repository

- [Terraform code to deploy infrastructure for Clickstream Analytics](../terraform/clickstream_analytics/): Provisions Pub/Sub, Cloud Bigtable instance/table, BigQuery dataset with `wikipedia` (raw events), `sessions` (primary key constrained for upserts), and `deadletter` tables, plus IAM service accounts.
- [Java Apache Beam streaming pipeline for clickstream analytics with Dataflow](../pipelines/clickstream_analytics_java/):
  - Strongly typed event schemas using Google **AutoValue**, AutoBuilder, and Beam Schemas.
  - Low-latency enrichment via **Cloud Bigtable** v2 client.
  - User session windowing with configurable inactivity gap duration (`--sessionGapDurationMinutes`).
  - Dual BigQuery sinks: append-only for enriched events, and Storage Write API **UPSERTs** for session aggregations.
  - Unified dead-letter queue (DLQ) capturing parse/validation errors and BigQuery write failures.
  - Dataflow **Streaming Engine** execution (`--enableStreamingEngine`) for offloading session window state and low-latency processing.
  - Reference data seeder (`scripts/populate_bigtable.py`) and synthetic streaming generator (`scripts/generate_clickstream_events.py`).
  - End-to-end verification runbook: [Testing & Verification Guide](../pipelines/clickstream_analytics_java/README.md#end-to-end-cloud-deployment--testing-runbook).

## Technical benefits

Dataflow provides a robust platform for building and scaling real-time clickstream analytics solutions.
Key capabilities make it the ideal choice for extracting maximum value from user interaction data:

- Streamlined Clickstream Processing: Dataflow's Apache Beam SDK simplifies the development of complex
  clickstream pipelines. Pre-built transforms, state management, and windowing functions make it easy to
  aggregate, filter, and enrich clickstream events in real time.
- Clickstream Enrichment: Enrich raw clickstream data with external data sources (e.g., user demographics,
  product catalogs) to gain deeper insights into user behavior and preferences. Side inputs and joins in
  Dataflow enable seamless data enrichment within your pipelines.
- Real-Time Dashboards and Alerts: Integrate Dataflow with real-time visualization tools and alerting systems
  to monitor clickstream metrics, detect anomalies, and trigger actions based on user interactions. Dataflow's
  low-latency processing ensures that insights are delivered within seconds.
- Scalability and Cost Efficiency: Dataflow automatically scales to handle fluctuating clickstream volumes.
  Pay only for the resources you use, avoiding overprovisioning and unnecessary costs. Right-fitting capabilities
  allow you to allocate resources optimally across different pipeline stages.
- Flexible Deployment: Deploy clickstream pipelines on various infrastructure options, including VMs and serverless
  options like Cloud Run or Cloud Functions. This flexibility allows you to tailor your deployment to your specific
  needs and budget.
- Open-Source Ecosystem: Leverage the power of the Apache Beam ecosystem, including a vast library of I/O
  connectors for various data sources and sinks. Dataflow's compatibility with open-source tools ensures flexibility
  and avoids vendor lock-in.

## How to deploy (Agentic or Manual)

### Option 1: Deploy with an AI Coding Harness (Recommended)

Open this repository in your AI coding harness and prompt it to provision Bigtable, Pub/Sub, and BigQuery, seed reference metadata, launch the Java pipeline, and verify sessionized outputs:

> *"Deploy the Clickstream Analytics guide in project `my-gcp-project` and region `us-central1` (check if the project uses a Shared VPC). Provision the Terraform infrastructure, populate the Cloud Bigtable lookup table, launch the Java Dataflow pipeline with private IPs, generate synthetic clickstream events, and verify that both raw enriched events and session UPSERTs land in BigQuery."*

### Option 2: Manual CLI Deployment

Follow the step-by-step instructions in the [Terraform module README](../terraform/clickstream_analytics/README.md) and [Pipeline README](../pipelines/clickstream_analytics_java/README.md):

```bash
# 1. Provision Pub/Sub, Cloud Bigtable & BigQuery infrastructure
cd terraform/clickstream_analytics
terraform init && terraform apply

# 2. Seed Bigtable, launch the Java pipeline & publish synthetic clickstream events
cd ../../pipelines/clickstream_analytics_java
source scripts/00_set_variables.sh
python3 scripts/populate_bigtable.py
./scripts/01_launch_pipeline.sh
python3 scripts/generate_clickstream_events.py
```

To tear down the deployment, cancel the active Dataflow job first, then run `terraform destroy` in `terraform/clickstream_analytics`.
