# ETL / integration

This directory contains the Terraform code to spawn a Google Cloud project
with all the necessary infrastructure and configuration required for running
the ETL / integration solution guide.

These deployment scripts are part of the
[Dataflow ETL Integration solution guide](../../use_cases/ETL_integration.md).

## Bill of resources created by this script

The scripts will create the following resources

| Resource         |          Name           | Description                                                                                                                                                                                                                                |
|:-----------------|:-----------------------:|:-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Project          |       Set by user       | Optional, you may reuse an existing project. If the project is created by Terraform, it will enable the APIs for Cloud Build, Dataflow,  Monitoring, Pub/Sub, Dataflow Autoscaling, and Artifact Registry                                  |
| Bucket           |   Same as project id    | Using the standard storage class, this is a regional bucket in the region specified by the user.                                                                                                                                           |
| BigQuery dataset |        `replica`        | A dataset where the data coming from Spanner will be replicated to.                                                                                                                                                                        |
| Spanner instance | `test-spanner-instance` | A Spanner instance that will be replicated to BigQuery                                                                                                                                                                                     |           
| Spanner database |         `taxis`         | The main database for the data being received and replicated. A table `events` will be created in this database.                                                                                                                           |
| Spanner database |       `metadata`        | A metadata database used for tracking change streams and keeping track of the replication to BigQuery                                                                                                                                      | 
| Service account  |    `my-dataflow-sa`     | Dataflow worker service account. It has storage admin, Dataflow worker, metrics writer and Pub/Sub editor roles assigned at project level.                                                                                                 |
| VPC network      |     `dataflow-net`      | If the project is created from scratch, the default network is removed and this network is re-created with a single regional sub-network.                                                                                                  |
| Firewall rules   |      Several rules      | Ingress and egress rules to remove unnecessary traffic, and to ensure the traffice required by Dataflow. If you want to access a VM using SSH, apply the network tag `ssh` to that instance. Same for `http-server` and for `https-server` |
| Cloud NAT        |     `dataflow-nat`      | Optional. Cloud NAT in the region specified by the user, in case the Dataflow workers need to reach the Internet. This is not necessary for the sample pipeline provided.                                                                  |

## Configuration variables

This deployment accepts the following configuration variables:

| Variable                |   Type    | Description                                                                                                                                                                                                           |
|:------------------------|:---------:|:----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `billing_account`       | `string`  | Optional. Billing account to be used if the project is created from scratch.                                                                                                                                          |
| `organization`          | `string`  | Optional. Organization (or folder) number, where the project will be created. Only required if you are creating the project from scratch. Use the format `organizations/XXXXXXX` or `folder/XXXXXXXX`. |
| `project_create`        | `boolean` | Set to false to reuse an existing project. Or to true to create a new project from scratch.                                                                                                                           | 
| `project_id`            | `string`  | Project Id.                                                                                                                                                                                                           | 
| `region`                | `string`  | Region to be used for all the resources. The VPC will contain only a single sub-network in this region.                                                                                                               |
| `destroy_all_resources` |  `bool`   | Optional. Default true. Destroy buckets and the Spanner instance with `tf destroy `.                                                                                                                                  |
| `internet_access`       |  `bool`   | Optional. Default false. Create a NAT for the Dataflow workers to access the Internet.                                                                                                                                |
| `network-prefix`        | `string`  | Optional. Default "dataflow". Add a prefix to the network net, subnet, NAT                                                                                                                                            |

The default values of all the optional configuration variables are set for development projects.
**For a production project, you should change `destroy_all_resources` to false.**

For production settings, you will want to adjust the Spanner instance settings in `main.tf`. The
default values create a very small instance for testing purposes only.

### Other potential configuration variables

The file `main.tf` has some local variables for the names of the Spanner instance, database, etc.
These are not configurable externally, but they can be changed in the `main.tf` file if necessary.

## Spanner instance size and configuration

The Spanner instance in this guide is configured to use `1000` processing units, which is the
equivalent of a 1 node instance.

The configuration is regional, using the same region as the rest of resources, and it assumes the
name of the configuration is `regional-YOUR_REGION`.

To change those settings (increase capacity, multi-regional setups, etc), you will need to adapt
the `main.tf` file. For more details see the following links:

* https://cloud.google.com/spanner/docs/compute-capacity
* https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/spanner_instance

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

## Scripts generation

The Terraform code will generate a script with variable values, to be used
with [the accompanying pipelines in this solution guide](../../pipelines/etl_integration_java/README.md).

The script is written in the location `scripts/01_set_variables.sh`, and should be executed as follows:

```bash
source ./scripts/01_set_variables.sh
```

## How to remove

The setup will be continuously consuming as this is a streaming architecture, running without stop.

**BEWARE: THE COMMAND BELOW WILL DESTROY AND REMOVE ALL THE RESOURCES**.

To destroy and stop all the resources, run:

```bash
terraform destroy
```