# IoT Analytics project deployment

This directory contains the Terraform code to provision application-level infrastructure and configuration required for running the IoT Analytics solution guide on Google Cloud.

These deployment scripts are part of the
[Dataflow IoT Analytics solution guide](../../use_cases/IoT_Analytics.md).

## Bill of resources created by this script

The scripts will create the following application-level resources:

| Resource | Name | Description |
| :--- | :---: | :--- |
| **Pub/Sub topic** | `maintenance-data` (configurable) | The input Pub/Sub topic for streaming vehicle telemetry events. |
| **Pub/Sub subscription** | `maintenance-data-sub` | The subscription to the input topic. |
| **Bigtable Instance** | `iot-analytics` | Cloud Bigtable instance to store vehicle maintenance enrichment metadata. |
| **Bigtable Table** | `maintenance_data` | Cloud Bigtable table with column family `maintenance` for real-time metadata lookup. |
| **BigQuery Dataset** | `iot` | BigQuery dataset where processed records and inference results reside. |
| **BigQuery Table** | `maintenance_analytics` | Stores real-time enriched IoT telemetry records and maintenance predictions. |
| **Artifact Registry** | `dataflow-containers` | Docker repository to host the custom Dataflow worker container image. |
| **Service account** | `iot-analytics-sa` (configurable) | Dedicated Dataflow worker service account with least-privilege roles (`roles/storage.objectAdmin`, `roles/dataflow.worker`, `roles/monitoring.metricWriter`, `roles/pubsub.editor`, `roles/bigtable.reader`, `roles/bigquery.dataEditor`, `roles/bigquery.jobUser`). |
| **GCS Bucket** *(Optional)* | `var.bucket_name` or `var.project_id` | Optional regional standard GCS bucket for Dataflow temp/staging files (created when `create_bucket = true`). |

## Configuration variables

This deployment accepts the following configuration variables:

| Variable | Type | Default | Description |
| :--- | :---: | :---: | :--- |
| `project_id` | `string` | *(Required)* | Existing GCP project ID where resources and IAM roles will be provisioned. |
| `region` | `string` | *(Required)* | GCP region for Bigtable, BigQuery dataset, Pub/Sub, and Dataflow resources (e.g. `us-central1` or `europe-southwest1`). |
| `subnetwork` | `string` | `null` | Optional subnetwork URL or path for Dataflow workers (e.g. `regions/europe-southwest1/subnetworks/dev-default` or full Shared VPC URI). If omitted, the default network is used. |
| `bucket_name` | `string` | `null` | Optional GCS bucket name for Dataflow temp/staging files. Defaults to `project_id` if not specified. |
| `service_account_name` | `string` | `"iot-analytics-sa"` | Name of the dedicated Dataflow worker service account to create. |
| `pubsub_topic` | `string` | `"maintenance-data"` | Name for the input Pub/Sub topic. |
| `create_bucket` | `bool` | `false` | Set to `true` to provision a new GCS bucket, or `false` to reuse an existing bucket. |
| `destroy_all_resources` | `bool` | `true` | When `true`, enables deletion of Bigtable instances and BigQuery dataset contents on `terraform destroy`. For production environments, set to `false`. |

## How to deploy

1. **Set configuration variables:**

   Create a file named `terraform.tfvars` in this directory:

   **Standard Deployment (Default Network / Same Project):**
   ```hcl
   project_id            = "YOUR_PROJECT_ID"
   region                = "us-central1"
   destroy_all_resources = true
   ```

   **Shared VPC Deployment (Dataflow in Service Project, Network in Host Project):**
   ```hcl
   project_id            = "YOUR_PROJECT_ID"
   region                = "europe-southwest1"
   subnetwork            = "https://www.googleapis.com/compute/v1/projects/HOST_PROJECT_ID/regions/europe-southwest1/subnetworks/shared-dataflow-subnet"
   bucket_name           = "YOUR_BUCKET_NAME"
   create_bucket         = false
   service_account_name  = "iot-analytics-sa"
   destroy_all_resources = true
   ```

2. **Initialize Terraform:**
   ```bash
   terraform init
   ```

3. **Apply the configuration:**
   ```bash
   terraform plan -out=tfplan
   terraform apply tfplan
   ```

4. **Access the deployed resources:**
   Terraform will automatically generate `pipelines/iot_analytics/scripts/00_set_environment.sh` with all required environment variables.

## Scripts generation

The Terraform code will generate an environment configuration script with all variable values to be used by the pipeline:

```bash
source ../../pipelines/iot_analytics/scripts/00_set_environment.sh
```

## How to remove

To destroy all provisioned infrastructure:

1. Cancel any active Dataflow streaming jobs first:
   ```bash
   gcloud dataflow jobs list --region=YOUR_REGION --status=active
   gcloud dataflow jobs cancel JOB_ID --region=YOUR_REGION
   ```

2. Run `terraform destroy`:
   ```bash
   terraform destroy
   ```
