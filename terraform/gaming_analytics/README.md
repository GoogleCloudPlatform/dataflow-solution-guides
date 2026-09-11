# Gaming Analytics project deployment

This directory contains the Terraform code to provision the application-level infrastructure required to run the Gaming Analytics solution guide on Google Cloud.

These deployment scripts are part of the
[Dataflow Gaming Analytics solution guide](../../use_cases/guides/gaming_analytics_dataflow_guide.pdf).

The architecture ingests gameplay events from the gaming platform servers into Pub/Sub, enriches them with player features stored in Cloud Bigtable (Apache Beam `Enrichment` transform), scores them with `RunInference`, and writes the result to a Pub/Sub topic for immediate in-game activation plus a BigQuery dataset for analytics.

## Bill of resources created by this script

The scripts will create the following application-level resources:

| Resource | Name | Description |
| :--- | :---: | :--- |
| **Pub/Sub topic (Input)** | `gaming-events` (configurable) | The input Pub/Sub topic for raw gameplay events published by the gaming servers. |
| **Pub/Sub subscription (Input)** | `gaming-events-sub` | The subscription consumed by the Dataflow streaming pipeline. |
| **Pub/Sub topic (Output)** | `gaming-recommendations` (configurable) | Buffers in-game recommendations to be sent back to the gaming platform. |
| **Pub/Sub subscription (Output)** | `gaming-recommendations-sub` | Downstream subscription for the activation consumer. |
| **Pub/Sub topic (Dead-letter)** | `gaming-analytics-errors` | Dead-letter topic for elements the pipeline cannot process. |
| **Pub/Sub subscription (Dead-letter)** | `gaming-analytics-errors-sub` | Subscription used to inspect and reprocess failed elements. |
| **Bigtable Instance** | `gaming-analytics` | Single-node Cloud Bigtable instance acting as the low-latency feature store. |
| **Bigtable Table** | `player_features` | Feature table with column family `features`, used by the Beam `Enrichment` transform. |
| **BigQuery Dataset** | `gaming_analytics` | Dataset holding the analytics output of the pipeline. |
| **BigQuery Table** | `player_recommendations` | Scored gameplay events, day-partitioned on `event_timestamp` and clustered on `player_id`, `event_type`. |
| **Artifact Registry** | `gaming-analytics-containers` | Docker repository hosting the custom Dataflow worker container image. |
| **Service account** | `gaming-analytics-sa` (configurable) | Dedicated Dataflow worker identity with least-privilege roles (`roles/dataflow.worker`, `roles/monitoring.metricWriter`, `roles/storage.objectAdmin`, `roles/pubsub.editor`, `roles/bigquery.dataEditor`, `roles/bigquery.jobUser`), plus a table-scoped `roles/bigtable.reader` binding on `player_features`. |
| **GCS Bucket** *(Optional)* | `var.bucket_name` or `var.project_id` | Optional regional standard bucket for Dataflow temp/staging files (created when `create_bucket = true`). |

This module deliberately does **not** create the project, VPC network, subnet, firewall rules or Cloud NAT. It deploys into an existing project and network (default, custom or Shared VPC).

## Inference

The Java pipeline shipped with this guide scores events with a **scikit-learn** model that runs on the worker itself, inside the Python SDK harness, through the cross-language [`RunInference`](https://beam.apache.org/documentation/ml/multi-language-inference/) transform. There is no remote endpoint and no per-element network call, so this module provisions plain CPU workers: no accelerator, no `aiplatform.googleapis.com`, and no Vertex AI IAM.

| | Value |
| :--- | :--- |
| Worker machine type | `n1-standard-2` (override with `machine_type`) |
| Accelerator | none — scikit-learn does not use a GPU |
| Worker disk | 50 GB |

> [!NOTE]
> A Runner v2 multi-language job runs a Java and a Python SDK harness side by side on every worker. `n1-standard-2` is sized for the guide's demo throughput; raise `machine_type` before pushing real traffic through it.

> [!NOTE]
> No Dataflow job is created by Terraform. The model artifact is not created either: `pipelines/gaming_analytics_java/scripts/train_model.py` trains it and uploads it to GCS, and the pipeline is pointed at it with `MODEL_URI`.

## Configuration variables

This deployment accepts the following configuration variables:

| Variable | Type | Default | Description |
| :--- | :---: | :---: | :--- |
| `project_id` | `string` | *(Required)* | Existing GCP project ID where resources and IAM roles will be provisioned. |
| `region` | `string` | *(Required)* | GCP region for Bigtable, BigQuery, Pub/Sub, Artifact Registry and Dataflow resources. |
| `zone` | `string` | `"a"` | Zone suffix used for the Bigtable cluster (e.g. `a` yields `us-central1-a`). |
| `subnetwork` | `string` | `null` | Optional subnetwork URL or path for Dataflow workers (e.g. `regions/europe-southwest1/subnetworks/dev-default` or a full Shared VPC URI). If omitted, the default network is used. |
| `bucket_name` | `string` | `null` | Optional GCS bucket name for Dataflow temp/staging files. Defaults to `project_id` if not specified. |
| `create_bucket` | `bool` | `false` | Set to `true` to provision a new GCS bucket, or `false` to reuse an existing one. |
| `service_account_name` | `string` | `"gaming-analytics-sa"` | Name of the dedicated Dataflow worker service account to create. |
| `machine_type` | `string` | `null` | Overrides the worker machine type. Defaults to `n1-standard-2`. |
| `input_topic` | `string` | `"gaming-events"` | Name for the input Pub/Sub topic. |
| `output_topic` | `string` | `"gaming-recommendations"` | Name for the output Pub/Sub topic. |
| `destroy_all_resources` | `bool` | `true` | When `true`, allows deletion of the Bigtable instance and BigQuery dataset contents on `terraform destroy`. Set to `false` for production. |

## How to deploy

1. **Set configuration variables:**

   Create a file named `terraform.tfvars` in this directory:

   **Standard deployment (default network / same project):**
   ```hcl
   project_id            = "YOUR_PROJECT_ID"
   region                = "us-central1"
   destroy_all_resources = true
   ```

   **Shared VPC deployment (Dataflow in service project, network in host project):**
   ```hcl
   project_id            = "YOUR_PROJECT_ID"
   region                = "europe-southwest1"
   subnetwork            = "https://www.googleapis.com/compute/v1/projects/HOST_PROJECT_ID/regions/europe-southwest1/subnetworks/shared-dataflow-subnet"
   bucket_name           = "YOUR_BUCKET_NAME"
   create_bucket         = false
   service_account_name  = "gaming-analytics-sa"
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
   Terraform generates `pipelines/gaming_analytics_java/scripts/00_set_environment.sh` with all required environment variables.

> [!IMPORTANT]
> If you deploy into an existing network with `--no_use_public_ips` workers, make sure Private Google Access is enabled on the subnet, that TCP ports `12345` and `12346` are allowed between workers, and that Cloud NAT is configured if the workers need internet access.

## Scripts generation

The Terraform code generates an environment configuration script with all variable values to be used by the pipeline:

```bash
source ../../pipelines/gaming_analytics_java/scripts/00_set_environment.sh
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
