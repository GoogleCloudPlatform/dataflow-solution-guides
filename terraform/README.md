# Terraform Infrastructure for the Solution Guides

This directory contains the Terraform infrastructure definitions for each Dataflow solution guide, built using [Google Cloud Foundation Fabric](https://github.com/GoogleCloudPlatform/cloud-foundation-fabric) modules.

Refer to the [main repository README](../README.md) for the full catalog of use cases and end-to-end architecture guides.

---

## Lightweight, Enterprise-Ready Infrastructure Design

Each Terraform module in this directory provisions the **application-level Google Cloud resources** required by its solution guide (such as Pub/Sub topics and subscriptions, BigQuery datasets and tables, Cloud Spanner or Cloud Bigtable instances, Firestore databases, Artifact Registry repositories, and dedicated least-privilege service accounts) while integrating seamlessly into existing enterprise environments:

- **Existing Project & Network Reuse by Default**:
  - Modules target an **existing Google Cloud project** (`project_create = false` by default) and reuse an existing **local VPC subnetwork** or **Shared VPC subnetwork** (`network_create = false` by default).
  - For **Shared VPC** deployments, pass the full subnetwork self-link or URI in `subnetwork` (for example, `projects/HOST_PROJECT/regions/REGION/subnetworks/SUBNET_NAME`).
- **Identity and Access Management (IAM)**:
  - Every deployment creates a dedicated, domain-scoped Dataflow worker service account (and training/build service accounts where applicable) granted only the minimum required roles.
  - Default Compute Engine service accounts are never used.
- **Network Security**:
  - Dataflow jobs launched from these modules always run with **private worker IPs only** (`--no_use_public_ip`, `--usePublicIps=false`, or `--disable-public-ips`).
  - Subnetworks require Private Google Access (`enable_private_access = true`), internal Dataflow worker firewall rules (`tcp:12345-12346` on tag `dataflow`), and Cloud NAT when external package/model downloads are needed.
- **Automatic Pipeline Environment Generation (`local_file.variables_script`)**:
  - Running `terraform apply` automatically generates an environment script (`scripts/00_set_environment.sh`, `scripts/00_set_variables.sh`, or `scripts/01_set_variables.sh`) inside the corresponding [`../pipelines/<use_case>/scripts/`](../pipelines/) directory with all resolved resource names, URIs, and service account emails.
- **Clean Demo Teardown (`destroy_all_resources`)**:
  - Setting `destroy_all_resources = true` (default for demos) disables deletion protection on ephemeral demo resources so `terraform destroy` leaves the target project clean without touching pre-existing VPCs or reused buckets.

---

## Deploying with an AI Coding Harness vs. Manually

### Option 1: Agentic Deployment (Recommended)

When using this repository with an AI coding harness, the [`terraform-deploy`](../.agents/skills/terraform-deploy/SKILL.md) and [`use-case-deployment`](../.agents/skills/use-case-deployment/SKILL.md) skills automate pre-flight discovery and provisioning:

- Inspects your project for existing local VPCs or **Shared VPC** host projects and usable subnets in your target region.
- Checks for existing GCS staging buckets (`gs://<project_id>`) and Service Account naming collisions before running `terraform plan`.
- Generates `terraform.tfvars`, runs `terraform init`, `terraform validate`, and `terraform apply`, and verifies that the pipeline environment script was rendered.

**Example prompt:**
> *"Provision the Terraform infrastructure for the Clickstream Analytics guide in project `my-gcp-project` and region `us-central1`. Check if the project uses a Shared VPC and whether a staging bucket already exists."*

### Option 2: Manual CLI Deployment

From any use-case subfolder (`terraform/<use_case>/`):

```bash
# 1. Create terraform.tfvars (see each module's README.md for full variable reference)
cat <<EOF > terraform.tfvars
project_id            = "my-gcp-project"
region                = "us-central1"
# Optional: full subnet path for custom or Shared VPCs
# subnetwork          = "projects/HOST_PROJECT/regions/us-central1/subnetworks/SUBNET_NAME"
# Optional: reuse an existing GCS bucket
# bucket_name         = "my-existing-bucket"
# create_bucket       = false
destroy_all_resources = true
EOF

# 2. Initialize, validate, and apply
terraform init
terraform validate
terraform plan -out=tfplan
terraform apply tfplan
```

To tear down the infrastructure after cancelling any active Dataflow jobs:

```bash
terraform destroy
```

---

## Terraform Modules by Use Case

| Use Case | Terraform Directory | Target Pipeline Directory |
| :--- | :--- | :--- |
| **GenAI & Machine Learning Inference** | [`ml_ai/`](./ml_ai/) | [`../pipelines/ml_ai_python/`](../pipelines/ml_ai_python/) |
| **ETL / Integration (Spanner CDC)** | [`etl_integration/`](./etl_integration/) | [`../pipelines/etl_integration_java/`](../pipelines/etl_integration_java/) |
| **Log Replication & Analytics (Splunk)** | [`log_replication_splunk/`](./log_replication_splunk/) | [`../pipelines/log_replication_splunk/`](../pipelines/log_replication_splunk/) |
| **Marketing Intelligence** | [`marketing_intelligence/`](./marketing_intelligence/) | [`../pipelines/marketing_intelligence/`](../pipelines/marketing_intelligence/) |
| **Clickstream Analytics** | [`clickstream_analytics/`](./clickstream_analytics/) | [`../pipelines/clickstream_analytics_java/`](../pipelines/clickstream_analytics_java/) |
| **IoT Analytics** | [`iot_analytics/`](./iot_analytics/) | [`../pipelines/iot_analytics/`](../pipelines/iot_analytics/) |
| **Anomaly Detection** | [`anomaly_detection/`](./anomaly_detection/) | [`../pipelines/anomaly_detection/`](../pipelines/anomaly_detection/) |
| **Customer Data Platform (CDP)** | [`cdp/`](./cdp/) | [`../pipelines/cdp/`](../pipelines/cdp/) |
| **Gaming Analytics** | [`gaming_analytics/`](./gaming_analytics/) | [`../pipelines/gaming_analytics_java/`](../pipelines/gaming_analytics_java/) |
| **Synthetic Data Generation** | [`synthetic-llm-dataflow-bigquery/`](./synthetic-llm-dataflow-bigquery/) | [`../pipelines/synthetic-llm-dataflow-bigquery/`](../pipelines/synthetic-llm-dataflow-bigquery/) |
