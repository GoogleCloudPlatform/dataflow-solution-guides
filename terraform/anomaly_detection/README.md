# Anomaly detection infrastructure

Deploy application resources into an **existing project and network** using the
[complete walkthrough](../../use_cases/Anomaly_Detection.md). Terraform does not
create a project, billing association, VPC, firewall or NAT. Vertex training,
model upload and endpoint deployment are managed by the separate Python workflow;
compatible externally supplied endpoints remain supported.

The existing Fabric module pins remain at v58.0.0.

| Resource | Configuration |
|---|---|
| Artifact Registry | `anomaly-detection-containers`; worker/trainer reader and legacy Cloud Build repository writer |
| Pub/Sub | `anomaly-detection-transactions` / `anomaly-detection-transactions-sub`, `anomaly-detection-detections` / `anomaly-detection-detections-sub`, `anomaly-detection-errors` / `anomaly-detection-errors-sub` |
| Bigtable | `anomaly-detection`, one-node cluster, `customer_profiles` with `profile` column family |
| BigQuery | `anomaly_detection.detections`, explicit schema, daily timestamp partitions, customer/transaction clustering |
| Bucket | Reused by default; optional creation in the chosen region |
| Worker | `anomaly-detection-sa`, default `n1-standard-2` CPU machine |
| Trainer | `anomaly-training-sa`, isolated artifact access beneath `anomaly-training/` |
| Prediction role | `anomalyDetectionPredictor`, containing only `aiplatform.endpoints.predict`; workflow grants it on its endpoint |

Worker application permissions are additive and scoped to its input subscription,
output/error topics, profile table, detections table, bucket and image repository.
Only Dataflow worker/metrics roles are project-scoped. Terraform grants subnet
network-user access when `subnetwork` is supplied. The trainer does not receive
worker or endpoint-management roles. Workflow operators need separate seeding,
publication, model-deployment, endpoint-IAM and smoke-query permissions; see the
walkthrough. Projects using a non-legacy Cloud Build identity must grant the
actual execution identity source, log and repository permissions.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `project_id` | Required | Existing billing-enabled project |
| `region` | Required | Application, training and Dataflow region |
| `zone` | `a` | Bigtable zone suffix |
| `subnetwork` | `null` | Local subnet path or full Shared VPC URL; omitted uses the default network |
| `bucket_name` | `null` | Defaults to the project ID |
| `create_bucket` | `false` | Whether Terraform manages the bucket |
| `service_account_name` | `anomaly-detection-sa` | Dedicated worker account |
| `training_service_account_name` | `anomaly-training-sa` | Dedicated training account |
| `destroy_all_resources` | `true` | Allow deletion of managed bucket/dataset contents and disable table/instance deletion protection |

Use `destroy_all_resources = false` for retained environments. It is not blanket
protection for every resource. Reused buckets are never deleted by Terraform.
The existing subnet must provide Private Google Access, enough IP addresses,
worker TCP 12345/12346 communication and NAT where internet access is required.
Shared VPC attachment and service-agent access remain foundation prerequisites;
the Terraform caller needs subnet IAM permissions in the host project.

```hcl
project_id = "YOUR_PROJECT_ID"
region = "us-central1"
subnetwork = "regions/us-central1/subnetworks/YOUR_SUBNET"
bucket_name = "YOUR_EXISTING_BUCKET"
create_bucket = false
```

Save as ignored `terraform.tfvars`, then:

```bash
terraform init
terraform fmt -check
terraform validate
terraform test  # mocked provider contract; Terraform 1.7+
terraform plan -out=tfplan
terraform apply tfplan
```

The API resources remain enabled after destroy. The generated
`../../pipelines/anomaly_detection/scripts/00_set_variables.sh` supplies pipeline
and workflow settings; the Python deployment writes endpoint settings separately.
Bigtable and BigQuery are active parts of the pipeline.

## Cleanup

Stop the Dataflow job and wait for termination, then run the Python workflow's
ownership-aware cleanup before `terraform destroy`. Terraform removes only the
application resources it manages, subject to deletion controls. External
endpoints, reused buckets, project and network remain externally managed.
