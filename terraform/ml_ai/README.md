# GenAI & ML inference project deployment

This directory contains the Terraform code to spawn a Google Cloud project
with all the necessary infrastructure and configuration required for running
the GenAI & ML inference solution guide.

These deployment scripts are part of the
[Dataflow Gen AI & ML solution guide](../../use_cases/GenAI_ML.md).

## Bill of resources created by this script

The scripts will create the following resources

| Resource             |         Name          | Description                                                                                                                                                                                                                                                                                      |
|:---------------------|:---------------------:|:-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Project              |      Set by user      | Optional, you may reuse an existing project. If the project is created by Terraform, it will enable the APIs for Cloud Build, Dataflow,  Monitoring, Pub/Sub, Dataflow Autoscaling, and Artifact Registry                                                                                        |
| Docker registry      | `dataflow-containers` | An Artifact Registry Docker repo for the custom Dataflow container used in the pipeline. The Cloud Build service agent is granted admin role in this repository. The Dataflow service account is granted reader role. By default, only the 3 latest versions of each image are kept in the repo. |
| Bucket               |  Same as project id   | Using the standard storage class, this is a regional bucket in the region specified by the user.                                                                                                                                                                                                 |
| Pub/Sub topic        |      `messages`       | The input Pub/Sub topic for the sample pipeline.                                                                                                                                                                                                                                                 |
| Pub/Sub topic        |     `predictions`     | The output Pub/Sub topic for the sample pipeline.                                                                                                                                                                                                                                                |
| Pub/Sub subscription |    `messages-sub`     | The subscription to the `messages` topic that is actually used by the Dataflow pipeline.                                                                                                                                                                                                         |
| Pub/Sub subscription |   `predictions-sub`   | The subscription to the `predictions` topic, useful to visualize the messages produced by the pipeline.                                                                                                                                                                                          |
| Service account      |   `my-dataflow-sa`    | Dataflow worker servive account. It has storage admin, Dataflow worker, metrics writer and Pub/Sub editor roles assigned at project level.                                                                                                                                                       |
| VPC network          |    `dataflow-net`     | If the project is created from scratch, the default network is removed and this network is re-created with a single regional sub-network.                                                                                                                                                        |
| Cloud NAT            |    `dataflow-nat`     | Cloud NAT in the region specified by the user, in case the Dataflow workers need to reach the Internet. This is not necessary for the sample pipeline provided.                                                                                                                                  |
| Firewall rules       |     Several rules     | Ingress and egress rules to remove unnecessary traffic, and to ensure the traffice required by Dataflow. If you want to access a VM using SSH, apply the network tag `ssh` to that instance. Same for `http-server` and for `https-server`                                                       |

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

The default values of all the optional configuration variables are set for development projects.
**For a production project, you should change `destroy_all_resources` to false.**

## How to deploy

1. **Set the configuration variables:**
    - Create a file named `terraform.tfvars` in the current directory.
    - Add the following configuration variables to the file, replacing the values with your own:
      ```
      billing_account = "YOUR_BILLING_ACCOUNT"
      organization = "YOUR_ORGANIZATION_ID"
      project_create = true/false
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
