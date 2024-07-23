# Real-Time Anomaly Detection project deployment

This directory contains the Terraform code to spawn a Google Cloud project
with all the necessary infrastructure and configuration required for running
the Real-Time Anomaly Detection solution guide.

These deployment scripts are part of the
[Dataflow Real-Time Anomaly Detection](../../use_cases/Anomaly_Detection.md).

## Bill of resources created by this script

The scripts will create the following resources

| Resource             |           Name           | Description                                                                                                                                                                                                                                                                                      |
|:---------------------|:------------------------:|:-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Project              |       Set by user        | Optional, you may reuse an existing project. If the project is created by Terraform, it will enable the APIs for Cloud Build, Dataflow,  Monitoring, Pub/Sub, Dataflow Autoscaling, and Artifact Registry                                                                                        |
| Docker registry      |  `dataflow-containers`   | An Artifact Registry Docker repo for the custom Dataflow container used in the pipeline. The Cloud Build service agent is granted admin role in this repository. The Dataflow service account is granted reader role. By default, only the 3 latest versions of each image are kept in the repo. |
| Bucket               |    Same as project id    | Using the standard storage class, this is a regional bucket in the region specified by the user.                                                                                                                                                                                                 |
| Pub/Sub topic        |      `transactions`      | The input Pub/Sub topic for the sample pipeline.                                                                                                                                                                                                                                                 |
| Pub/Sub topic        |       `detections`       | The output Pub/Sub topic for the sample pipeline.                                                                                                                                                                                                                                                |
| Bigquery Dataset     |     `output_dataset`     | The output dataset for the sample pipeline.                                                                                                                                                                                                                                                      |
| Bigtable             | `bt-enrichment.features` | Bigtable table to store features data.                                                                                                                                                                                                                                                           |
| Pub/Sub subscription |    `transactions-sub`    | The subscription to the `messages` topic that is actually used by the Dataflow pipeline.                                                                                                                                                                                                         |
| Pub/Sub subscription |     `detections-sub`     | The subscription to the `predictions` topic, useful to visualize the messages produced by the pipeline.                                                                                                                                                                                          |
| Service account      |     `my-dataflow-sa`     | Dataflow worker service account. It has storage admin, Dataflow worker, metrics writer and Pub/Sub editor roles assigned at project level.                                                                                                                                                       |
| VPC network          |      `dataflow-net`      | If the project is created from scratch, the default network is removed and this network is re-created with a single regional sub-network.                                                                                                                                                        |
| Cloud NAT            |      `dataflow-nat`      | Cloud NAT in the region specified by the user, in case the Dataflow workers need to reach the Internet. This is not necessary for the sample pipeline provided.                                                                                                                                  |
| Firewall rules       |      Several rules       | Ingress and egress rules to remove unnecessary traffic, and to ensure the traffice required by Dataflow. If you want to access a VM using SSH, apply the network tag `ssh` to that instance. Same for `http-server` and for `https-server`                                                       |

## Configuration variables

This deployment accepts the following configuration variables:

| Variable                |   Type    | Description                                                                                                                                                                                                           |
|:------------------------|:---------:|:----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `billing_account`       | `string`  | Optional. Billing account to be used if the project is created from scratch.                                                                                                                                          |
| `organization`          | `string`  | Optional. Organization (or folder) number, <br/><br/><br/>where the project will be created. Only required if you are creating the project from scratch. Use the format `organizations/XXXXXXX` or `folder/XXXXXXXX`. |
| `project_create`        | `boolean` | Set to false to reuse an existing project. Or to true to create a new project from scratch.                                                                                                                           | 
| `project_id`            | `string`  | Project Id.                                                                                                                                                                                                           | 
| `region`                | `string`  | Region to be used for all the resources. The VPC will contain only a single sub-network in this region.                                                                                                               |
| `destroy_all_resources` |  `bool`   | Optional. Default true. Destroy buckets and the Spanner instance with `tf destroy `.                                                                                                                                  |
| `network-prefix`        | `string`  | Optional. Default "dataflow". Add a prefix to the network net, subnet, NAT                                                                                                                                            |
| `zone`                  | `string`  | Zone to create bigtable clusters                                                                                                                                                                                      |

The default values of all the optional configuration variables are set for development projects.
**For a production project, you should change `destroy_all_resources` to false.**

## How to deploy

1. **Set the configuration variables:**
    - Create a file named `terraform.tfvars` in the current directory.
    - Add the following configuration variables to the file, replacing the values with your own:
      ```
      billing_account = "YOUR_BILLING_ACCOUNT"
      organization = "YOUR_ORGANIZATION_ID"
      project_create = TRUE_OR_FALSE
      project_id = "YOUR_PROJECT_ID"
      region = "YOUR_REGION"
      ```
    - If this is a production deployment, make sure you change also the optional variables.
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
5. **Access the deployed resources:** You are now ready to launch the sample pipeline in this
   solution guide.

## How to remove

The setup will be continuously consuming as this is a streaming architecture, running without stop.

**BEWARE: THE COMMAND BELOW WILL DESTROY AND REMOVE ALL THE RESOURCES**.

To destroy and stop all the resources, run:

```bash
terraform destroy
```
<!-- BEGIN_TF_DOCS -->
## Requirements

No requirements.

## Providers

| Name | Version |
|------|---------|
| <a name="provider_local"></a> [local](#provider\_local) | 2.5.1 |

## Modules

| Name | Source | Version |
|------|--------|---------|
| <a name="module_buckets"></a> [buckets](#module\_buckets) | github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs | v32.0.0 |
| <a name="module_dataflow_sa"></a> [dataflow\_sa](#module\_dataflow\_sa) | github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account | v32.0.0 |
| <a name="module_enrichment_table"></a> [enrichment\_table](#module\_enrichment\_table) | github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/bigtable-instance | v32.0.0 |
| <a name="module_firewall_rules"></a> [firewall\_rules](#module\_firewall\_rules) | github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-vpc-firewall | v32.0.0 |
| <a name="module_google_cloud_project"></a> [google\_cloud\_project](#module\_google\_cloud\_project) | github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/project | v32.0.0 |
| <a name="module_input_topic"></a> [input\_topic](#module\_input\_topic) | github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub | v32.0.0 |
| <a name="module_output_dataset"></a> [output\_dataset](#module\_output\_dataset) | github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/bigquery-dataset | v32.0.0 |
| <a name="module_output_topic"></a> [output\_topic](#module\_output\_topic) | github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/pubsub | v32.0.0 |
| <a name="module_regional_nat"></a> [regional\_nat](#module\_regional\_nat) | github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-cloudnat | v32.0.0 |
| <a name="module_registry_docker"></a> [registry\_docker](#module\_registry\_docker) | github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/artifact-registry | v32.0.0 |
| <a name="module_vpc_network"></a> [vpc\_network](#module\_vpc\_network) | github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/net-vpc | v32.0.0 |

## Resources

| Name | Type |
|------|------|
| [local_file.variables_script](https://registry.terraform.io/providers/hashicorp/local/latest/docs/resources/file) | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_billing_account"></a> [billing\_account](#input\_billing\_account) | Billing account for the projects/resources | `string` | `null` | no |
| <a name="input_destroy_all_resources"></a> [destroy\_all\_resources](#input\_destroy\_all\_resources) | Destroy all resources when calling tf destroy. Use false for production deployments. For test environments, set to true to remove all buckets and bigtable instances. | `bool` | `true` | no |
| <a name="input_internet_access"></a> [internet\_access](#input\_internet\_access) | Set to true to create a NAT for Dataflow workers to access Internet. | `bool` | `false` | no |
| <a name="input_network_prefix"></a> [network\_prefix](#input\_network\_prefix) | Prefix to be used for networks and subnetworks | `string` | `"dataflow"` | no |
| <a name="input_organization"></a> [organization](#input\_organization) | Organization for the project/resources | `string` | `null` | no |
| <a name="input_project_create"></a> [project\_create](#input\_project\_create) | True if you want to create a new project. False to reuse an existing project. | `bool` | n/a | yes |
| <a name="input_project_id"></a> [project\_id](#input\_project\_id) | Project ID for the project/resources | `string` | n/a | yes |
| <a name="input_region"></a> [region](#input\_region) | The region for resources and networking | `string` | n/a | yes |
| <a name="input_zone"></a> [zone](#input\_zone) | The zone for big table | `string` | n/a | yes |

## Outputs

No outputs.
<!-- END_TF_DOCS -->