# Marketing Intelligence sample pipeline (Python)

This directory contains the Terraform code to provision application-level infrastructure and configuration required for running the Market Intelligence solution guide on Google Cloud.

These deployment scripts are part of the [Dataflow Marketing Intelligence Solution Guide](../../use_cases/Marketing_Intelligence.md).

## Bill of resources created by this script

The scripts will create the following application-level resources:

| Resource | Name | Description |
| :--- | :---: | :--- |
| **Docker registry** | `dataflow-containers` | An Artifact Registry Docker repository for the custom Dataflow container image. Cloud Build is granted admin role and the Dataflow worker service account is granted reader role. By default, the 3 latest image versions are retained. |
| **GCS Bucket** *(Optional)* | `var.bucket_name` or `var.project_id` | Optional standard regional GCS bucket for Dataflow temp and staging files (created when `create_bucket = true`). |
| **Pub/Sub topic (Input)** | `dataflow-solutions-guide-market-intelligence-input` | The input Pub/Sub topic for streaming customer interaction events. |
| **Pub/Sub subscription (Input)** | `dataflow-solutions-guide-market-intelligence-input-sub` | Pub/Sub subscription consumed by the Dataflow streaming pipeline. |
| **Pub/Sub topic (Output)** | `dataflow-solutions-guide-market-intelligence-output` | The output Pub/Sub topic for high-propensity coupon and discount activations ($\ge 0.80$). |
| **Pub/Sub subscription (Output)** | `dataflow-solutions-guide-market-intelligence-output-sub` | Subscription to the output topic for downstream systems and verification. |
| **Firestore Database** | `(default)` | Cloud Firestore in Native Mode for low-latency customer profile enrichment. |
| **BigQuery Dataset** | `output_dataset` | The destination BigQuery dataset containing the `predictions` table for real-time analytics. |
| **Service Account** | `marketing-intel-sa` (configurable) | Dedicated Dataflow worker service account with least-privilege roles (`roles/storage.objectAdmin`, `roles/dataflow.worker`, `roles/monitoring.metricWriter`, `roles/pubsub.editor`, `roles/datastore.user`, `roles/bigquery.dataEditor`). |

## Configuration variables

| Variable | Type | Default | Description |
| :--- | :---: | :---: | :--- |
| `project_id` | `string` | *(Required)* | Project ID of the existing GCP project where resources will be provisioned. |
| `region` | `string` | *(Required)* | GCP region for application resources and Dataflow jobs (e.g. `us-central1`, `europe-west1`). |
| `subnetwork` | `string` | `null` | Optional subnetwork URL or path for Dataflow workers (e.g. `regions/europe-west1/subnetworks/dev-subnet` or full Shared VPC URI `https://www.googleapis.com/compute/v1/projects/HOST_PROJECT/regions/REGION/subnetworks/SUBNET_NAME`). If omitted, the default network is used. |
| `bucket_name` | `string` | `null` | Optional GCS bucket name for Dataflow temp/staging files. Defaults to `project_id`. |
| `service_account_name` | `string` | `"marketing-intel-sa"` | Dedicated Dataflow worker service account ID. |
| `create_bucket` | `bool` | `false` | Set to `true` to provision a new GCS bucket, or `false` to reuse an existing bucket. |
| `destroy_all_resources` | `bool` | `true` | When `true`, enables deletion of Firestore database and BigQuery dataset contents on `terraform destroy`. Set to `false` for production environments. |

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
   project_id            = "ihr-pipelines"
   region                = "europe-west1"
   subnetwork            = "https://www.googleapis.com/compute/v1/projects/HOST_PROJECT_ID/regions/europe-west1/subnetworks/shared-dataflow-subnet"
   bucket_name           = "ihr-pipelines-dataflow-staging"
   create_bucket         = false
   service_account_name  = "marketing-intel-sa"
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
   Terraform will automatically generate `pipelines/marketing_intelligence/scripts/00_set_variables.sh` with all required environment variables.

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
