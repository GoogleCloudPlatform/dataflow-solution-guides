# ETL / Integration

This directory contains the Terraform code to deploy the minimum necessary Google Cloud
infrastructure and permissions required for running the ETL / integration solution guide.

These deployment scripts are part of the
[Dataflow ETL Integration solution guide](../../use_cases/ETL_integration.md).

## Bill of resources created by this script

The scripts will create the following resources:

| Resource         |          Name           | Description                                                                                                                                                                                                                                |
|:-----------------|:-----------------------:|:-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Spanner instance | `test-spanner-instance` | A Spanner instance that will be replicated to BigQuery                                                                                                                                                                                     |           
| Spanner database |         `taxis`         | The main database for the data being received and replicated. An `events` table and an `events_stream` change stream are created in this database.                                                                                        |
| Spanner database |       `metadata`        | A metadata database used for tracking change streams and keeping track of the replication checkpoints to BigQuery                                                                                                                           | 
| BigQuery dataset |        `replica`        | A BigQuery dataset where the data coming from the Spanner change stream will be replicated to. Access is granted to the Dataflow service account.                                                                                          |
| Service account  |    `my-dataflow-sa`     | Dedicated Dataflow worker service account. It has Dataflow worker, Storage object admin, metrics writer, BigQuery data editor/job user, Pub/Sub editor, and Spanner database user roles assigned.                                        |
| GCS Bucket       |   Set by user (opt)     | Optional. A regional bucket for Dataflow temp and staging files (created only if `create_bucket = true`).                                                                                                                                 |

## Configuration variables

This deployment accepts the following configuration variables:

| Variable                |   Type    | Description                                                                                                                                                                                                           |
|:------------------------|:---------:|:----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `project_id`            | `string`  | Required. Existing Google Cloud project ID where resources and IAM roles will be provisioned.                                                                                                                        | 
| `region`                | `string`  | Required. Region to be used for Spanner, BigQuery dataset, and Dataflow resources (e.g. `europe-southwest1` or `us-central1`).                                                                                        |
| `subnetwork`            | `string`  | Optional. Subnetwork URL or path for Dataflow workers (e.g., `regions/europe-southwest1/subnetworks/dev-default` or full URI). If omitted, the default network is used.                                                |
| `bucket_name`           | `string`  | Optional. Existing GCS bucket name for Dataflow temp/staging files (`TEMP_LOCATION`). Defaults to `project_id` if omitted.                                                                                            |
| `create_bucket`         |  `bool`   | Optional. Default `false`. Set to `true` if you want Terraform to create a new GCS bucket named `bucket_name` (or `project_id`).                                                                                      |
| `destroy_all_resources` |  `bool`   | Optional. Default `true`. Set to `true` for test/demo environments to destroy Spanner instances and datasets with `terraform destroy`. For production, set to `false`.                                               |

### Spanner instance size and configuration

The Spanner instance in this guide is configured to use `1000` processing units, which is the
equivalent of a 1 node instance.

The configuration is regional, using the same region as the rest of resources (`regional-YOUR_REGION`).

To change those settings (increase capacity, multi-regional setups, etc), you can adapt
the `main.tf` file. For more details see the following links:

* https://cloud.google.com/spanner/docs/compute-capacity
* https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/spanner_instance

## How to deploy

1. **Set the configuration variables:**
    - Create a file named `terraform.tfvars` in this directory:
      ```hcl
      project_id            = "YOUR_PROJECT_ID"
      region                = "YOUR_REGION"
      subnetwork            = "regions/YOUR_REGION/subnetworks/YOUR_SUBNET" # Optional
      bucket_name           = "YOUR_BUCKET_NAME"                           # Optional
      destroy_all_resources = true
      ```
2. **Initialize Terraform:**
    - Run the following command to initialize Terraform:
      ```bash
      terraform init
      ```
3. **Apply the configuration:**
    - Run the following command to apply the Terraform configuration:
      ```bash
      terraform apply
      ```
4. **Wait for the deployment to complete:**
    - Terraform will output the status of the deployment. Wait for it to complete successfully.

## Scripts generation

The Terraform code will generate a script with variable values, to be used
with [the accompanying pipelines in this solution guide](../../pipelines/etl_integration_java/README.md).

The script is written in the location `../../pipelines/etl_integration_java/scripts/01_set_variables.sh`, and should be sourced as follows:

```bash
source ./scripts/01_set_variables.sh
```

## How to remove

**BEWARE: THE COMMAND BELOW WILL DESTROY AND REMOVE ALL THE MANAGED RESOURCES**.

To destroy and stop all the resources, run:

```bash
terraform destroy
```