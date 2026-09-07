# Customer Data Platform (CDP) Infrastructure Deployment

This directory contains the Terraform code to provision application-level infrastructure and configuration required for running the Customer Data Platform solution guide on Google Cloud.

These deployment scripts are part of the [Dataflow Customer Data Platform Solution Guide](../../use_cases/CDP.md).

## Bill of resources created by this script

The scripts will create the following application-level resources:

| Resource | Name | Description |
| :--- | :---: | :--- |
| **Docker registry** | `cdp-containers` | An Artifact Registry Docker repository for the custom Dataflow container image. Cloud Build is granted admin role and the Dataflow worker service account is granted reader role. By default, the 3 latest image versions are retained. |
| **GCS Bucket** *(Optional)* | `var.bucket_name` or `var.project_id` | Optional standard regional GCS bucket for Dataflow temp and staging files (created when `create_bucket = true`). |
| **Pub/Sub topic** | `cdp-transactions` | The first input Pub/Sub topic for streaming customer transaction events. |
| **Pub/Sub topic** | `cdp-coupon-redemption` | The second input Pub/Sub topic for streaming coupon redemption events. |
| **BigQuery Dataset** | `cdp_dataset` | The destination BigQuery dataset for customer data unification. |
| **BigQuery Table** | `unified_customer_data` | The destination BigQuery table storing joined transaction and coupon redemption records. |
| **Service Account** | `cdp-dataflow-sa` (configurable) | Dedicated Dataflow worker service account with least-privilege roles (`roles/storage.objectAdmin`, `roles/dataflow.worker`, `roles/monitoring.metricWriter`, `roles/pubsub.editor`, `roles/bigquery.dataEditor`, `roles/bigquery.jobUser`). |

## Configuration variables

| Variable | Type | Default | Description |
| :--- | :---: | :---: | :--- |
| `project_id` | `string` | *(Required)* | Project ID of the existing GCP project where resources will be provisioned. |
| `region` | `string` | *(Required)* | GCP region for application resources and Dataflow jobs (e.g. `us-central1`, `europe-west1`). |
| `subnetwork` | `string` | `null` | Optional subnetwork URL or path for Dataflow workers (e.g. `regions/europe-west1/subnetworks/dev-subnet` or full Shared VPC URI `https://www.googleapis.com/compute/v1/projects/HOST_PROJECT/regions/REGION/subnetworks/SUBNET_NAME`). If omitted, the default network is used. |
| `bucket_name` | `string` | `null` | Optional GCS bucket name for Dataflow temp/staging files. Defaults to `project_id`. |
| `service_account_name` | `string` | `"cdp-dataflow-sa"` | Dedicated Dataflow worker service account ID. |
| `artifact_registry_name` | `string` | `"cdp-containers"` | Name of the Artifact Registry repository for custom Dataflow worker containers. |
| `pubsub_transactions_topic` | `string` | `"cdp-transactions"` | Name of the Pub/Sub topic for streaming customer transaction events. |
| `pubsub_coupon_redemption_topic` | `string` | `"cdp-coupon-redemption"` | Name of the Pub/Sub topic for streaming coupon redemption events. |
| `create_bucket` | `bool` | `false` | Set to `true` to provision a new GCS bucket, or `false` to reuse an existing bucket. |
| `destroy_all_resources` | `bool` | `true` | When `true`, enables deletion of BigQuery dataset contents and tables on `terraform destroy`. Set to `false` for production environments. |
| `bq_dataset` | `string` | `"cdp_dataset"` | The BigQuery output dataset name for customer data unification. |
| `bq_table` | `string` | `"unified_customer_data"` | The BigQuery output table name for unified customer data. |

## How to deploy

1. **Set configuration variables:**

   Create a `terraform.tfvars` file in this directory.

   **Standard Deployment (Default Network / Same Project):**
   ```hcl
   project_id            = "YOUR_PROJECT_ID"
   region                = "us-central1"
   destroy_all_resources = true
   ```

   **Shared VPC Deployment (Dataflow in Service Project, Network in Host Project):**
   ```hcl
   project_id            = "YOUR_SERVICE_PROJECT_ID"
   region                = "europe-west1"
   subnetwork            = "https://www.googleapis.com/compute/v1/projects/HOST_PROJECT_ID/regions/europe-west1/subnetworks/shared-dataflow-subnet"
   bucket_name           = "YOUR_STAGING_BUCKET"
   create_bucket         = false
   service_account_name  = "cdp-dataflow-sa"
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
   Terraform will automatically generate `pipelines/cdp/scripts/00_set_environment.sh` with all required environment variables.

   Proceed to the pipeline directory to build the container and launch the streaming pipeline:
   ```bash
   cd ../../pipelines/cdp
   source scripts/00_set_environment.sh
   ./scripts/01_build_and_push_container.sh
   ./scripts/02_run_dataflow.sh
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

