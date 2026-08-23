# Terraform Directory — Agent Guidelines

This directory contains Terraform infrastructure definitions for each solution guide, built using **Google Cloud Foundation Fabric** (v56.2.0) modules.

---

## 1. Directory Structure & Module Map

| Directory | Core Infrastructure Resources | Target Pipeline |
| :--- | :--- | :--- |
| `ml_ai/` | VPC, Subnet, NAT, GCS Bucket, Pub/Sub Topics (`messages`, `predictions`), Service Account | `pipelines/ml_ai_python/` |
| `etl_integration/` | Cloud Spanner (taxis DB + Change Stream), BigQuery Dataset, Service Account | `pipelines/etl_integration_java/` |
| `cdp/` | Pub/Sub Topics (`transactions`, `coupon-redemption`), BigQuery Dataset, VPC, Subnet | `pipelines/cdp/` |
| `anomaly_detection/` | Pub/Sub, BigQuery, Vertex AI Endpoint, VPC, Subnet | `pipelines/anomaly_detection/` |
| `marketing_intelligence/` | Pub/Sub, BigQuery, Vertex AI AutoML Endpoint, VPC, Subnet | `pipelines/marketing_intelligence/` |
| `clickstream_analytics/` | Cloud Bigtable (Instance & Table), Pub/Sub, BigQuery Dataset, VPC, Subnet | `pipelines/clickstream_analytics_java/` |
| `iot_analytics/` | Cloud Bigtable, Pub/Sub, GCS Bucket, VPC, Subnet | `pipelines/iot_analytics/` |
| `log_replication_splunk/` | Pub/Sub, Secret Manager / HEC token, VPC, Subnet | `pipelines/log_replication_splunk/` |

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
