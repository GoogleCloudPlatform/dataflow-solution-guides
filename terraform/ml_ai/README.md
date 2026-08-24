# GenAI & ML Inference

This directory contains the Terraform code to deploy the minimum necessary Google Cloud
infrastructure and permissions required for running the GenAI & ML inference solution guide.

These deployment scripts are part of the
[Dataflow Gen AI & ML solution guide](../../use_cases/GenAI_ML.md).

## Bill of resources created by this script

The scripts will create the following resources:

| Resource             |         Name          | Description                                                                                                                                                                                                                                                          |
|:---------------------|:---------------------:|:---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Docker registry      | `dataflow-containers` | An Artifact Registry Docker repo for the custom Dataflow container used in the pipeline. The Cloud Build service agent is granted admin role in this repository. The Dataflow service account is granted reader role. By default, only the 3 latest versions of each image are kept in the repo. |
| Pub/Sub topic        |      `messages`       | The input Pub/Sub topic for the sample pipeline.                                                                                                                                                                                                                     |
| Pub/Sub topic        |     `predictions`     | The output Pub/Sub topic for the sample pipeline.                                                                                                                                                                                                                    |
| Pub/Sub subscription |    `messages-sub`     | The subscription to the `messages` topic that is used by the Dataflow pipeline.                                                                                                                                                                                      |
| Pub/Sub subscription |   `predictions-sub`   | The subscription to the `predictions` topic, useful to inspect and visualize the predictions produced by the pipeline.                                                                                                                                              |
| Service account      |  `ml-ai-dataflow-sa`  | Dedicated Dataflow worker service account. It has Dataflow worker, Storage object admin, metrics writer, Pub/Sub editor, and Artifact Registry reader roles assigned.                                                                                                |
| GCS Bucket           |   Set by user (opt)   | Optional. A regional bucket for Dataflow temp and staging files, and for storing the Gemma model weights (created only if `create_bucket = true`).                                                                                                                  |

## Configuration variables

This deployment accepts the following configuration variables:

| Variable                |   Type    | Description                                                                                                                                                                                                           |
|:------------------------|:---------:|:----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `project_id`            | `string`  | Required. Existing Google Cloud project ID where resources and IAM roles will be provisioned.                                                                                                                        |
| `region`                | `string`  | Required. Region to be used for application resources, Artifact Registry, and Dataflow workers (e.g. `us-central1`).                                                                                                   |
| `subnetwork`            | `string`  | Optional. Subnetwork URL or path for Dataflow workers (e.g., `regions/us-central1/subnetworks/dev-default` or full URI). If omitted, the default network is used.                                                      |
| `service_account_name`  | `string`  | Optional. Name of the Dataflow worker service account to create. Defaults to `ml-ai-dataflow-sa`.                                                                                                                      |
| `bucket_name`           | `string`  | Optional. Existing GCS bucket name for Dataflow temp/staging files and Gemma model weights. Defaults to `project_id` if omitted.                                                                                     |
| `create_bucket`         |  `bool`   | Optional. Default `false`. Set to `true` if you want Terraform to create a new GCS bucket named `bucket_name` (or `project_id`).                                                                                      |
| `destroy_all_resources` |  `bool`   | Optional. Default `true`. Set to `true` for test/demo environments to destroy all managed resources with `terraform destroy`. For production, set to `false`.                                                       |

## How to deploy

1. **Set the configuration variables:**
    - Create a file named `terraform.tfvars` in this directory:
      ```hcl
      project_id            = "YOUR_PROJECT_ID"
      region                = "us-central1"
      subnetwork            = "regions/us-central1/subnetworks/YOUR_SUBNET" # Optional
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
with [the accompanying pipeline in this solution guide](../../pipelines/ml_ai_python/README.md).

The script is written in the location `../../pipelines/ml_ai_python/scripts/00_set_variables.sh`, and should be sourced as follows:

```bash
source ./scripts/00_set_variables.sh
```

## How to remove

The setup will be continuously consuming as this is a streaming architecture, running without stop.

**BEWARE: THE COMMAND BELOW WILL DESTROY AND REMOVE ALL THE MANAGED RESOURCES**.

To destroy and stop all the resources, run:

```bash
terraform destroy
```

