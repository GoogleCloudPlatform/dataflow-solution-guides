---
name: terraform-deploy
description: >-
  Manage, validate, plan, apply, and destroy Google Cloud Foundation Fabric Terraform infrastructure across all solution guides in this repository.
  Use when setting up new GCP environments, modifying IAM configurations, managing Spanner/BigQuery/Bigtable resources,
  validating Terraform syntax, or tearing down test infrastructure.
---

# Terraform Deployment Skill

This skill guides the agent through managing, validating, applying, and tearing down Google Cloud infrastructure across all use cases in the `terraform/` directory.

---

## 1. Prerequisites Checklist

Before executing Terraform operations:
1. Verify Google Cloud credentials:
   ```bash
   gcloud auth list
   gcloud config get-value project
   ```
2. Ensure Terraform is installed (`terraform version >= 1.5.0`).
3. Check for service account name collisions in the target project before applying:
   ```bash
   gcloud iam service-accounts list --project=$PROJECT --filter="email:<sa-name>"
   ```

---

## 2. Infrastructure Deployment Procedure

### Step 1: Navigate to the Target Use Case
```bash
cd terraform/<use_case>
```
Available use cases:
- `ml_ai`
- `etl_integration`
- `cdp`
- `anomaly_detection`
- `marketing_intelligence`
- `clickstream_analytics`
- `iot_analytics`
- `log_replication_splunk`

### Step 2: Configure `terraform.tfvars`
Create or inspect `terraform.tfvars`:
```hcl
project_id             = "my-gcp-project-id"
region                 = "us-central1"
subnetwork             = "https://www.googleapis.com/compute/v1/projects/HOST_PROJECT/regions/REGION/subnetworks/SUBNET_NAME" # optional
service_account_name   = "<use-case>-dataflow-sa" # optional override
bucket_name            = "my-gcp-project-id"      # optional
create_bucket          = false                    # Set true if GCS bucket does not exist
destroy_all_resources  = true                     # Enables force_destroy on GCS & Spanner for demo environments
```

### Step 3: Format & Validate
```bash
terraform fmt
terraform init
terraform validate
```

### Step 4: Plan Infrastructure Changes
Review all planned resource additions, modifications, and deletions:
```bash
terraform plan -out=tfplan
```
Verify:
- Dataflow service account has domain-specific name and minimal necessary roles.
- Target datasets and instances use `var.region`.
- No unneeded project/network foundation resources are being created.

### Step 5: Apply Configuration
```bash
terraform apply tfplan
```

### Step 6: Verify Generated Variables Script
Confirm that Terraform successfully rendered the environment file in the corresponding pipeline directory:
```bash
ls -la ../../pipelines/<use_case>/scripts/*_set_variables.sh || ls -la ../../pipelines/<use_case>/scripts/00_set_environment.sh
```

---

## 3. Teardown Procedure

To safely destroy demo resources:
```bash
# 1. First cancel any active Dataflow jobs in the project
gcloud dataflow jobs list --project=$PROJECT --region=$REGION --status=active
gcloud dataflow jobs cancel <JOB_ID> --project=$PROJECT --region=$REGION

# 2. Wait until jobs reach Cancelled or Drained state
# 3. Then run terraform destroy
terraform destroy -auto-approve
```
