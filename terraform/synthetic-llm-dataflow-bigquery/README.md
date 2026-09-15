# Synthetic data generation

This directory contains the Terraform code to deploy the Google Cloud
infrastructure and permissions for the synthetic data generation solution
guide, into an existing project and network.

These deployment scripts are part of the
[Dataflow synthetic data generation solution guide](../../use_cases/Synthetic_Data_Generation.md).

## Bill of resources created by this script

| Resource | Name | Description |
| :-- | :-: | :-- |
| Project services | — | Artifact Registry, BigQuery (+ Storage API), Cloud Build, Compute, Dataflow, IAM, Logging, Monitoring, Secret Manager, Storage. Never disabled on destroy. |
| Docker registry | `synthetic-llm-containers` | Artifact Registry repo for the single launcher + GPU worker image. The build service account can write, the Dataflow service account can read. Keeps the 3 latest versions. |
| Service account | `synthetic-llm-dataflow-sa` | Flex Template launcher and Dataflow workers: Dataflow worker/developer, BigQuery data editor, job user and read session user, Storage object admin, metrics and log writer. |
| Service account | `synthetic-llm-build-sa` | Cloud Build identity for `01_build_and_push_container.sh` and `02_stage_models.sh`: log writer, Storage object admin, Artifact Registry writer, access to the Kaggle secrets only. |
| Secrets | `kaggle-username`, `kaggle-key` | Created with a placeholder version. Add the real values with `gcloud secrets versions add` so they never enter the Terraform state. |
| BigQuery dataset | `synthetic_source` | Snapshots of `thelook_ecommerce` `users` (without `user_geom`), `orders` and `order_items`, filtered so every child row has its parent. |
| BigQuery dataset | `synthetic_data` | Landing tables written by the job, plus the `products` catalog copied from the public dataset as an already-landed parent. |
| BigQuery dataset | `synthetic_data_quality` | `dlq` and `validation_runs` (day-partitioned) and `fk_fanout_stats`, with the schemas from the pipeline's `config/bq_schema/`. |
| BigQuery job | `sdfb-thelook-snapshot-*` | One script that creates the snapshots and the catalog at apply time. |
| Subnet IAM | — | `roles/compute.networkUser` for the Dataflow service account on `subnetwork`, only when it is set (local or Shared VPC). |
| GCS bucket | `bucket_name` (opt) | Model weights, Dataflow temp and staging, and the Flex Template spec. Created only if `create_bucket = true`. |

All datasets are in `US`, next to the public data: BigQuery cannot join
across locations.

## Configuration variables

| Variable | Type | Description |
| :-- | :-: | :-- |
| `project_id` | `string` | Required. Existing project where resources are provisioned. |
| `region` | `string` | Required. Region for Dataflow, Cloud Build, Artifact Registry and the bucket. It must offer NVIDIA L4 GPUs (for example `us-central1`). |
| `bq_location` | `string` | Optional. Default `US`, the location of `bigquery-public-data.thelook_ecommerce`. |
| `subnetwork` | `string` | Optional. Subnetwork path or URI for the workers. It needs Private Google Access, because workers have no public IP. |
| `bucket_name` | `string` | Optional. Defaults to `project_id`. |
| `create_bucket` | `bool` | Optional. Default `false`. |
| `service_account_name` | `string` | Optional. Default `synthetic-llm-dataflow-sa`. |
| `build_service_account_name` | `string` | Optional. Default `synthetic-llm-build-sa`. |
| `model` | `string` | Optional. `gemma4-e4b-it` (default, Kaggle) or `qwen3-4b` (ModelScope, no credentials). |
| `machine_type` | `string` | Optional. Default `g2-standard-8`. |
| `accelerator` | `string` | Optional. Default `type:nvidia-l4;count:1;install-nvidia-driver`. |
| `num_rows` | `number` | Optional. Rows for the root table `users`. Default `1000`. |
| `destroy_all_resources` | `bool` | Optional. Default `true`: `terraform destroy` also removes table contents and the bucket. Use `false` for anything you want to keep. |

## How to deploy

1. Create `terraform.tfvars` in this directory:
   ```hcl
   project_id    = "YOUR_PROJECT_ID"
   region        = "us-central1"
   create_bucket = true
   bucket_name   = "YOUR_PROJECT_ID-synthetic-llm"
   # subnetwork  = "regions/us-central1/subnetworks/YOUR_SUBNET"
   ```
2. Run `terraform init` and `terraform apply`.
3. Continue with the steps in the [pipeline README](../../pipelines/synthetic-llm-dataflow-bigquery/README.md).

The account that runs the scripts needs permission to act as both service
accounts (`roles/iam.serviceAccountUser`) and NVIDIA L4 quota in `region`.

## Scripts generation

Terraform writes `../../pipelines/synthetic-llm-dataflow-bigquery/scripts/00_set_variables.sh`
with every name above. The pipeline scripts source it automatically; to use
the variables in your shell:

```bash
source ./scripts/00_set_variables.sh
```

## How to remove

This is a batch pipeline, so nothing keeps running once a job finishes.

**BEWARE: THE COMMAND BELOW DESTROYS ALL THE MANAGED RESOURCES, INCLUDING THE GENERATED TABLES.**

```bash
terraform destroy
```
