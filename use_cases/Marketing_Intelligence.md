# Marketing Intelligence & Real-Time Personalization

Real-time marketing intelligence describes the practice of collecting and analyzing data about your market, customers, and competitors as it happens. This enables you to make informed, agile decisions and respond swiftly to emerging trends, customer behaviors, and personalized marketing opportunities.

This reference architecture demonstrates how to combine streaming interaction events with historical customer data from **Cloud Firestore (Native Mode)**, apply on-worker **Scikit-Learn Machine Learning inference** via Apache Beam's `RunInference` to score purchase propensity and determine next-best-offers, persist structured analytical records to **BigQuery** via the Storage Write API, and emit instant discount triggers to **Pub/Sub**.

## Documentation

- [One pager: Marketing intelligence in real-time with Dataflow (PDF)](./one_pagers/market_intel_dataflow_onepager.pdf)
- [Marketing Intelligence Solution Guide and Architecture (PDF)](./guides/market_intel_dataflow_guide.pdf)

## Assets included in this repository

- [Terraform code to deploy infrastructure for Marketing Intelligence](../terraform/marketing_intelligence/)
- [Sample streaming pipeline in Python with Firestore enrichment and Scikit-Learn RunInference](../pipelines/marketing_intelligence/)

## Technical benefits

Dataflow is the premier platform for building real-time ML & streaming personalization applications:

- **Turnkey ML Inference (`RunInference`)**:
  - [RunInference](https://beam.apache.org/documentation/ml/about-ml/#use-runinference)
    lets you efficiently execute pre-trained Scikit-Learn, PyTorch, or TensorFlow models directly on Dataflow worker threads with zero remote network RPC latency, automated batching, and memory sharing across threads.
- **Serverless Low-Latency Enrichment**:
  - Serverless **Cloud Firestore (Native Mode)** provides single-digit millisecond document lookups with zero standing cluster cost when idle, backed by in-memory worker-side LRU caching (`cachetools.TTLCache`).
- **High-Throughput Analytics & Activation**:
  - Stream all scored interactions directly into **BigQuery** using the high-performance Storage Write API for real-time Looker dashboards, while concurrently streaming high-propensity events to **Pub/Sub** for immediate marketing activation (email, SMS, push notification, dynamic web modal).
- **Hermetic Container Builds**:
  - Pre-bake dependencies and serialized model artifacts into lightweight CPU container images for instant worker boot times and deterministic autoscaling.
- **Advanced Stream Processing**:
  - Implemented using the open-source [Apache Beam SDK](https://beam.apache.org/get-started/), providing state & timer APIs, streaming joins, dead-letter routing, and portable execution across runners.

## How to deploy (Agentic or Manual)

### Option 1: Deploy with an AI Coding Harness (Recommended)

Open this repository in your AI coding harness and prompt it to provision the infrastructure, seed Firestore profiles, run the pipeline, and verify predictions in BigQuery:

> *"Deploy the Marketing Intelligence guide in project `my-gcp-project` and region `us-central1` (auto-discovering any Shared VPC subnet and existing staging bucket). Build the custom container, seed the Firestore customer profiles, launch the Dataflow pipeline with private IPs, publish synthetic clickstream events, and verify that enriched conversion propensity scores land in BigQuery with zero dead-letter errors."*

### Option 2: Manual CLI Deployment

Follow the step-by-step instructions in the [Terraform module README](../terraform/marketing_intelligence/README.md) and [Pipeline README](../pipelines/marketing_intelligence/README.md):

```bash
# 1. Provision Pub/Sub, Firestore, BigQuery & Artifact Registry
cd terraform/marketing_intelligence
terraform init && terraform apply

# 2. Build container, seed Firestore & launch the Dataflow pipeline
cd ../../pipelines/marketing_intelligence
source scripts/00_set_environment.sh
./scripts/01_build_and_push_container.sh
./scripts/02_run_dataflow.sh

# 3. Publish synthetic events & verify BigQuery predictions
python3 ./scripts/03_publish_events.py
```

To tear down the deployment, cancel the active Dataflow job first, then run `terraform destroy` in `terraform/marketing_intelligence`.
