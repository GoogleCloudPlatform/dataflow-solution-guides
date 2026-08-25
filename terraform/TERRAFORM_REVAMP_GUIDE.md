# Terraform Revamp Guide: Modernizing Solution Guides to Lightweight Infrastructure

This guide outlines the standard architecture, step-by-step procedure, code templates, and quality checklist used to revamp the **ETL / Integration** solution guide. Use this playbook as a reference when modernizing the remaining solution guides in this repository (`ml_ai`, `cdp`, `anomaly_detection`, `marketing_intelligence`, `clickstream_analytics`, `iot_analytics`, and `log_replication_splunk`).

---

## 1. Architectural Philosophy

### The Shift: Monolithic vs. Application-Level IaC

```
❌ OLD PATTERN (Heavyweight & Inflexible):
├── Project Creation / Reuse (module "google_cloud_project")
├── Cloud Resource Manager API Enablement (google_project_service.crm)
├── Organization / Billing Account Bindings
├── Dedicated VPC Network (module "vpc_network")
├── Custom Subnet with Pods/Services Secondary Ranges
├── Custom Firewall Rules (module "firewall_rules")
├── Cloud NAT (module "regional_nat")
└── Application Resources (Spanner, BigQuery, Bigtable, PubSub, etc.)

✅ MODERN PATTERN (Lightweight & Portable):
├── Assumes Pre-Existing Project & Network (Shared VPC, Default, or Custom)
├── Dedicated Dataflow Worker Service Account (Least-Privilege IAM)
├── Application Resources (Spanner, BigQuery, Bigtable, PubSub Topics, Vertex AI, etc.)
├── Optional Subnetwork & GCS Bucket Configuration
└── Environment Configuration File Generator (local_file.variables_script)
```

### Key Principles
1. **Never create or mutate organizational foundations**: Do not manage projects, billing accounts, organizational folders, or VPC networks. Assume developers deploy into existing projects and networks (e.g. shared VPCs).
2. **Dedicated, Domain-Specific Service Accounts**: Always keep `module "dataflow_sa"` to ensure workers run with an isolated identity and least-privilege IAM roles. Never use generic placeholder names like `my-dataflow-sa` (which collide in shared environments); instead, use unique domain-specific defaults (e.g., `spanner-cdc-dataflow-sa`, `cdp-dataflow-sa`) and make the name configurable via `var.service_account_name`.
3. **Flexible Subnetwork Support**: Accept an optional `subnetwork` variable (e.g. `regions/REGION/subnetworks/SUBNET_NAME` or full Shared VPC URI) without assuming a local subnet exists.
4. **Decoupled GCS Bucket**: Support referencing an existing bucket (`var.bucket_name`), with an optional toggle (`var.create_bucket`) if provisioning a new bucket is desired.
5. **Dynamic Pipeline Launch Scripts**: Ensure runner shell scripts dynamically pass `--subnetwork` only when configured, and remove legacy firewall experiment tags (`use_network_tags=...`).
6. **Toolchain & Runtime Compatibility**: For Java pipelines, configure `build.gradle` with supported LTS toolchains (`JavaLanguageVersion.of(21)` or `17`).

---

## 2. Step-by-Step Refactoring Procedure

### Step 1: Revamp `terraform/<use_case>/variables.tf`

#### ❌ Remove These Variables:
- `billing_account`
- `organization`
- `project_create`
- `internet_access`
- `network_prefix`

#### ✅ Standard Variable Definitions:
```hcl
variable "project_id" {
  description = "Project ID of the existing GCP project where resources will be provisioned."
  type        = string
}

variable "region" {
  description = "The GCP region for application resources and Dataflow jobs."
  type        = string
}

variable "subnetwork" {
  description = "Optional subnetwork URL or path for Dataflow workers (e.g. regions/europe-southwest1/subnetworks/dev-default or full URI). If omitted, the default network is used."
  type        = string
  default     = null
}

variable "bucket_name" {
  description = "Optional GCS bucket name for Dataflow temp/staging files. Defaults to project_id if not specified."
  type        = string
  default     = null
}

variable "service_account_name" {
  description = "Name of the dedicated Dataflow worker service account to create."
  type        = string
  default     = "<use-case>-dataflow-sa" # e.g. spanner-cdc-dataflow-sa, cdp-dataflow-sa
}

variable "create_bucket" {
  description = "Whether to create a new GCS bucket for temp/staging files. Set to false if using an existing bucket."
  type        = bool
  default     = false
}

variable "destroy_all_resources" {
  description = "Destroy all resources when calling tf destroy. Use false for production deployments. For test environments, set to true to remove all resources."
  type        = bool
  default     = true
}
```

> [!IMPORTANT]
> **Service Account Naming Rules**:
> - GCP Service Account IDs must be **6 to 30 characters**, lowercase letters, numbers, and hyphens (`^[a-z]([a-z0-9-]*[a-z0-9])$`).
> - Provide a specific domain default (e.g. `clickstream-dataflow-sa` instead of `my-dataflow-sa`) to prevent 409 `alreadyExists` conflicts when multiple pipelines or developers share the same GCP project.

---

### Step 2: Revamp `terraform/<use_case>/main.tf`

#### ❌ Delete These Blocks:
1. `resource "google_project_service" "crm"`
2. `module "google_cloud_project"`
3. `module "vpc_network"`
4. `module "firewall_rules"`
5. `module "regional_nat"`

#### ✅ Update Remaining Blocks:

1. **Locals**:
   ```hcl
   locals {
     bucket_name              = var.bucket_name != null ? var.bucket_name : var.project_id
     dataflow_service_account = var.service_account_name != null ? var.service_account_name : "<use-case>-dataflow-sa"
     # use-case specific locals (dataset name, table name, machine type, etc.)
   }
   ```

2. **Service Account (`module.dataflow_sa`)**:
   Change project references from `module.google_cloud_project.project_id` to `var.project_id`:
   ```hcl
   module "dataflow_sa" {
     source     = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v57.0.0"
     project_id = var.project_id
     name       = local.dataflow_service_account
     iam_project_roles = {
       (var.project_id) = [
         "roles/storage.objectAdmin",
         "roles/dataflow.worker",
         "roles/monitoring.metricWriter",
         "roles/pubsub.editor",
         # Add use-case specific roles (e.g. roles/bigquery.dataEditor, roles/bigtable.user, etc.)
       ]
     }
   }
   ```

3. **Optional GCS Bucket (`module.buckets`)**:
   ```hcl
   module "buckets" {
     count         = var.create_bucket ? 1 : 0
     source        = "github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/gcs?ref=v57.0.0"
     project_id    = var.project_id
     name          = local.bucket_name
     location      = var.region
     storage_class = "STANDARD"
     force_destroy = var.destroy_all_resources
   }
   ```

4. **Application Resources (Spanner, BigQuery, Bigtable, Pub/Sub, Vertex AI)**:
   - Replace all `module.google_cloud_project.project_id` with `var.project_id`.
   - Ensure datasets/tables/instances specify `location = var.region`.
   - Ensure lifecycle/deletion protection honors `var.destroy_all_resources` (e.g. `deletion_protection = !var.destroy_all_resources`).

5. **Environment Configuration Generator (`local_file.variables_script`)**:
   ```hcl
   resource "local_file" "variables_script" {
     filename        = "${path.module}/../../pipelines/<use_case>/scripts/01_set_variables.sh"
     file_permission = "0644"
     content         = <<FILE
   # This file is generated by the Terraform code of this Solution Guide.
   # We recommend that you modify this file only through the Terraform deployment.
   export PROJECT=${var.project_id}
   export REGION=${var.region}
   export SUBNETWORK=${var.subnetwork != null ? var.subnetwork : ""}
   export NETWORK=$${SUBNETWORK}
   export TEMP_LOCATION=gs://${local.bucket_name}/tmp
   export SERVICE_ACCOUNT=${module.dataflow_sa.email}

   # Use-case specific exports:
   export TOPIC=...
   export BIGQUERY_DATASET=...
   export MAX_DATAFLOW_WORKERS=...
   export WORKER_TYPE=...
   FILE
   }
   ```

---

### Step 3: Update Pipeline Launch Scripts (`pipelines/<use_case>/scripts/`)

In runner scripts (e.g., `02_run_dataflow.sh`, `02_run_publisher_dataflow.sh`, `03_run_changestream_template.sh`):

#### 1. Dynamic Subnetwork Injection
Support both `$SUBNETWORK` and `$NETWORK` without failing when empty:
```bash
SUBNET_OPT=""
if [ -n "$SUBNETWORK" ]; then
  SUBNET_OPT="--subnetwork=$SUBNETWORK"
elif [ -n "$NETWORK" ]; then
  SUBNET_OPT="--subnetwork=$NETWORK"
fi
```

#### 2. Clean Up Legacy Experiments
Remove `use_network_tags=ssh;dataflow` from `--experiments` because custom firewall tags are no longer created.

**Example for Java / Gradle**:
```bash
./gradlew run -Pargs="
  --runner=DataflowRunner \
  --project=$PROJECT \
  --region=$REGION \
  --tempLocation=$TEMP_LOCATION \
  --serviceAccount=$SERVICE_ACCOUNT \
  $SUBNET_OPT \
  --maxNumWorkers=$MAX_DATAFLOW_WORKERS \
  --experiments=enable_data_sampling \
  --usePublicIps=false \
  ..."
```

**Example for Python / Apache Beam**:
```bash
python main.py \
  --runner=DataflowRunner \
  --project=$PROJECT \
  --region=$REGION \
  --temp_location=$TEMP_LOCATION \
  --service_account_email=$SERVICE_ACCOUNT \
  $SUBNET_OPT \
  --max_num_workers=$MAX_DATAFLOW_WORKERS \
  --no_use_public_ips \
  ...
```

**Example for Dataflow Flex Templates (`gcloud dataflow flex-template run`)**:
```bash
gcloud dataflow flex-template run my-template-job \
    --template-file-gcs-location=gs://... \
    --project=$PROJECT \
    --region=$REGION \
    --temp-location=$TEMP_LOCATION \
    --service-account-email=$SERVICE_ACCOUNT \
    $SUBNET_OPT \
    --disable-public-ips \
    --parameters ...
```

---

### Step 4: Update Documentation

1. **`terraform/<use_case>/README.md`**:
   - Update **Bill of Resources** table to list only application-level resources.
   - Update **Configuration Variables** table.
   - Provide a clean, minimal `terraform.tfvars` example:
     ```hcl
     project_id            = "YOUR_PROJECT_ID"
     region                = "europe-southwest1"
     subnetwork            = "regions/europe-southwest1/subnetworks/dev-default" # Optional
     bucket_name           = "YOUR_BUCKET_NAME"                                 # Optional
     destroy_all_resources = true
     ```
2. **`pipelines/<use_case>/README.md`**: Ensure references to `01_set_variables.sh` or `00_set_environment.sh` are accurate.
3. **`use_cases/<use_case>.md`**: Update introductory text from "deploy a project" to "deploy infrastructure".
4. **`terraform/AGENTS.md` and root `AGENTS.md`**: Update module map tables to remove VPC/Subnet mentions from the use case description.

---

### Step 5: Verification & Quality Assurance Checklist

Run the following commands from the repository root:

```bash
# 1. Terraform Formatting
cd terraform/<use_case>
terraform fmt -check

# 2. Terraform Provider & Module Init (No backend needed)
terraform init -backend=false

# 3. Terraform Syntax Validation
terraform validate

# 4. Pipeline Script Syntax Check
cd ../../pipelines/<use_case>/scripts
bash -n *.sh

# 5. Spotless / Linting (If Java or Python scripts modified)
# For Java:
cd .. && ./gradlew spotlessApply
# For Python:
yapf -i -r --style yapf .
pylint --rcfile ../pylintrc .
```

---

## 3. Matrix of Solution Guides to Refactor

| # | Use Case / Directory | Key Application Resources to Keep | Recommended Default SA Name | Target Pipeline |
| :- | :--- | :--- | :--- | :--- |
| 1 | **`etl_integration/`** *(Done)* | Spanner Instance/DBs/IAM, BigQuery Dataset | `spanner-cdc-dataflow-sa` | `pipelines/etl_integration_java/` |
| 2 | **`cdp/`** | Pub/Sub Topics (`transactions`, `coupon_redemption`), BigQuery Dataset, Artifact Registry | `cdp-dataflow-sa` | `pipelines/cdp/` |
| 3 | **`clickstream_analytics/`** | Cloud Bigtable (Instance & Table), Pub/Sub Topic, BigQuery Dataset & Tables | `clickstream-dataflow-sa` | `pipelines/clickstream_analytics_java/` |
| 4 | **`anomaly_detection/`** | Pub/Sub Topic, BigQuery Dataset, Vertex AI Endpoint | `anomaly-detection-sa` | `pipelines/anomaly_detection/` |
| 5 | **`marketing_intelligence/`** | Pub/Sub Topic, BigQuery Dataset, Vertex AI Model/Endpoint | `marketing-intel-sa` | `pipelines/marketing_intelligence/` |
| 6 | **`iot_analytics/`** | Cloud Bigtable, Pub/Sub Topics, GCS Bucket | `iot-analytics-sa` | `pipelines/iot_analytics/` |
| 7 | **`ml_ai/`** *(Done)* | Pub/Sub Topics (`messages`, `predictions`), Artifact Registry, GCS Bucket | `ml-ai-dataflow-sa` | `pipelines/ml_ai_python/` |
| 8 | **`log_replication_splunk/`** *(Done)* | Pub/Sub Topic, Secret Manager (Splunk HEC token), Optional Splunk Demo VM | `splunk-replication-sa` | `pipelines/log_replication_splunk/` |

---

## 4. Live Testing, Validation & Teardown Best Practices

When validating revamped pipelines on live GCP projects:

1. **Service Account Verification**:
   - Before running `terraform apply`, ensure the target service account name does not collide with existing service accounts in the project (`gcloud iam service-accounts list`).
   - If collision is possible, override `service_account_name = "<custom-name>"` in `terraform.tfvars`.

2. **Subnetwork Verification**:
   - If deploying into a Shared VPC, provide the full subnetwork URI (`https://www.googleapis.com/compute/v1/projects/HOST_PROJECT/regions/REGION/subnetworks/SUBNET_NAME`).
   - Ensure the Dataflow worker service account has `roles/compute.networkUser` on the host project if workers are in a service project.

3. **Safe Teardown Protocol**:
   - **Always stop or cancel active Dataflow streaming jobs first** (`gcloud dataflow jobs cancel <JOB_ID> --region=<REGION>`).
   - Wait until jobs transition to `Cancelled` or `Drained`.
   - Run `terraform destroy -auto-approve` to safely remove Spanner instances, BigQuery datasets, and IAM bindings without locking active worker resources.
