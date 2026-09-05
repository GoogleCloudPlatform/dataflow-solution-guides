# Terraform Directory — Agent Guidelines

This directory contains Terraform infrastructure definitions for each solution guide, built using **Google Cloud Foundation Fabric** (v56.2.0) modules.

---

## 1. Directory Structure & Module Map

| Directory | Core Infrastructure Resources | Target Pipeline |
| :--- | :--- | :--- |
| `ml_ai/` | Pub/Sub Topics (`messages`, `predictions`), Artifact Registry (`dataflow-containers`), GCS Bucket, Service Account | `pipelines/ml_ai_python/` |
| `etl_integration/` | Cloud Spanner (taxis DB + Change Stream), BigQuery Dataset, Service Account | `pipelines/etl_integration_java/` |
| `cdp/` | Pub/Sub Topics (`transactions`, `coupon-redemption`), BigQuery Dataset, VPC, Subnet | `pipelines/cdp/` |
| `anomaly_detection/` | Pub/Sub, Bigtable, BigQuery, Artifact Registry, optional GCS, Service Account; external Vertex AI endpoint | `pipelines/anomaly_detection/` |
| `marketing_intelligence/` | Pub/Sub Topics (`input`, `output`), Cloud Firestore (Native Mode), BigQuery Dataset, Artifact Registry, Service Account | `pipelines/marketing_intelligence/` |
| `clickstream_analytics/` | Cloud Bigtable (Instance & Table), Pub/Sub Topic, BigQuery Dataset, Service Account | `pipelines/clickstream_analytics_java/` |
| `iot_analytics/` | Cloud Bigtable (Instance & Table), BigQuery Dataset & Table, Pub/Sub Topic, Artifact Registry, Service Account | `pipelines/iot_analytics/` |
| `log_replication_splunk/` | Pub/Sub Topics (`all-logs`, `deadletter-topic`), Cloud Logging Sink, Secret Manager (Splunk HEC token), Service Account, Optional Splunk Demo VM | `pipelines/log_replication_splunk/` |

---

## 2. The `local_file.variables_script` Pattern

Every Terraform module includes a `resource "local_file" "variables_script"` block. When Terraform applies successfully, it renders all computed infrastructure outputs (project ID, region, subnet path, service account email, topic IDs, table names) into a shell script inside the corresponding pipeline's `scripts/` directory:

```hcl
resource "local_file" "variables_script" {
  filename        = "${path.module}/../../pipelines/<use_case>/scripts/01_set_variables.sh"
  file_permission = "0644"
  content         = <<FILE
export PROJECT=${module.google_cloud_project.project_id}
export REGION=${var.region}
export NETWORK=regions/${var.region}/subnetworks/${var.network_prefix}-subnet
export SERVICE_ACCOUNT=${module.dataflow_sa.email}
...
FILE
}
```

> [!IMPORTANT]
> Do not manually edit these generated variable files. Always update the Terraform variables or definitions and re-run `terraform apply` to regenerate them.

---

## 3. Infrastructure & Security Standards

1. **Cloud Foundation Fabric Modules**:
   Always use standard Fabric module sources:
   - `github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/project?ref=v56.2.0`
   - `github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs?ref=v56.2.0`
   - `github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-vpc?ref=v56.2.0`
   - `github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-vpc-firewall?ref=v56.2.0`
   - `github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-cloudnat?ref=v56.2.0`
   - `github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v56.2.0`
   - `github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/bigquery-dataset?ref=v56.2.0`

2. **Network Security**:
   - Every VPC module creates a private subnet with `enable_private_access = true`.
   - Firewall rules must allow TCP `12345` and `12346` ingress/egress for the `dataflow` target tag.
   - Cloud NAT is provisioned when `var.internet_access` is `true`.

3. **Lifecycle & Deletion Protection**:
   - Resources respect `var.destroy_all_resources` for demo/test environments (e.g. `force_destroy = var.destroy_all_resources`, `deletion_protection = !var.destroy_all_resources`).

---

## 4. Terraform Development Commands

From any use case subfolder:
```bash
# Format code
terraform fmt

# Initialize providers and modules
terraform init

# Validate configuration syntax
terraform validate

# Create an execution plan
terraform plan -out=tfplan

# Apply the plan
terraform apply tfplan

# Teardown resources
terraform destroy
```

### Anomaly detection deployment

Anomaly detection uses an existing project and network, an existing bucket by default, and a user-supplied Vertex AI endpoint with a deployed model. Source generated `scripts/00_set_variables.sh`, set `MODEL_ENDPOINT` (optional `MODEL_LOCATION`, default `REGION`), then run the build and launch scripts. Input is `transactions` via `transactions-sub`; output is `detections`. `SUBNETWORK` is optional with legacy `NETWORK` fallback. Existing networks must provide Private Google Access, worker TCP 12345/12346 communication and NAT where needed. Before migrating existing state, follow `terraform/anomaly_detection/README.md` to transfer foundation and bucket ownership safely.
