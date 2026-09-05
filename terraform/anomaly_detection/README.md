# Real-time anomaly detection infrastructure

Deploy application resources into an **existing project and network**. Terraform does not create a project, configure billing, or manage a VPC, firewall or NAT. A Vertex AI endpoint with an already deployed model must be supplied separately at pipeline launch, in the deployment project.

## Resources

All Fabric modules retain their v58.0.0 pins.

| Resource | Name / behavior |
|---|---|
| Artifact Registry | `dataflow-containers`; worker reader, legacy Cloud Build account admin; keep three image versions |
| Pub/Sub | `transactions` / `transactions-sub` input; `detections` / `detections-sub` output |
| Bigtable | `bt-enrichment`, three-node cluster, `features` table |
| BigQuery | `output_dataset`, located in `region` |
| Staging bucket | Existing by default; optionally created in `region` |
| Worker identity | `anomaly-detection-sa` by default |

Bigtable and BigQuery are retained for extensions; the current Python pipeline only reads Pub/Sub, calls Vertex AI, and publishes detections. No model or endpoint is created here.

Terraform enables Vertex AI, IAM, Compute, Storage, Dataflow, Monitoring, Cloud Build, Artifact Registry, Pub/Sub, Bigtable and BigQuery APIs. These remain enabled on destroy. Worker project roles cover Storage Object Admin, Dataflow worker, metrics writer, Pub/Sub editor, Vertex AI user (prediction), Bigtable reader and BigQuery data editor; container read access is granted on the repository.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `project_id` | Required | Existing project |
| `region` | Required | Application and Dataflow region |
| `zone` | `a` | Bigtable zone suffix within `region` |
| `subnetwork` | `null` | Local path or full Shared VPC URL; omitted/empty uses Dataflow's default network |
| `bucket_name` | `null` | Bucket name; defaults to project ID |
| `create_bucket` | `false` | Create and manage the selected bucket |
| `service_account_name` | `anomaly-detection-sa` | Dedicated worker account ID |
| `destroy_all_resources` | `true` | Allow bucket contents and BigQuery contents deletion; disable Bigtable instance deletion protection |

Use `destroy_all_resources = false` for production. This flag is not blanket protection for every resource. Existing buckets are never managed or deleted by this configuration when `create_bucket = false`. For a bucket in another project, its owner must grant the worker object access separately.

The existing subnet must have Private Google Access, sufficient free IPs, and ingress/egress TCP 12345 and 12346 for Dataflow workers tagged `dataflow`. Provide Cloud NAT where workers need internet access. Workers always use private IPs. With `subnetwork` configured, Terraform grants additive subnet-level `roles/compute.networkUser` to the worker, resolving the host project from Shared VPC URLs. The Terraform caller needs subnet IAM permissions in that host project; Shared VPC attachment and any required service-agent permissions remain foundation prerequisites.

```hcl
project_id = "YOUR_PROJECT_ID"
region = "us-central1"
subnetwork = "regions/us-central1/subnetworks/YOUR_SUBNET"
# Shared VPC: https://www.googleapis.com/compute/v1/projects/HOST_PROJECT/regions/us-central1/subnetworks/YOUR_SUBNET
bucket_name = "YOUR_EXISTING_BUCKET"
create_bucket = false
```

Save these values in `terraform.tfvars`, then run:

```bash
terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

The deployer needs permissions to enable APIs, manage application resources and IAM, and read project metadata. The pipeline submitter needs permission to act as the worker service account. Ensure the actual Cloud Build execution identity has build, staging bucket and repository push permissions; projects using a different build identity must grant those externally.

Terraform generates `../../pipelines/anomaly_detection/scripts/00_set_variables.sh`. Follow the [pipeline instructions](../../pipelines/anomaly_detection/README.md) to build and launch.

## Cleanup

Cancel or drain running Dataflow jobs first, then run `terraform destroy`. Application resources managed here are removed subject to their deletion controls; external project, network, endpoint and reused bucket remain externally managed.
