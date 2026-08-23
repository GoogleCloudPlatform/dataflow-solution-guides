---
name: terraform-deploy
description: >-
  Manage, validate, plan, apply, and destroy Google Cloud Foundation Fabric Terraform infrastructure across all solution guides in this repository.
  Use when setting up new GCP environments, modifying VPC or IAM configurations, managing Spanner/BigQuery/Bigtable resources,
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
3. Ensure the billing account ID and organization ID (or folder ID) are known if creating new projects.

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
project_create         = false   # Set to true if TF should create the project
billing_account        = "XXXXXX-XXXXXX-XXXXXX"
destroy_all_resources  = true    # Enables force_destroy on GCS & Spanner for demo environments
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
- Subnetwork has `enable_private_access = true`.
- Dataflow service account has minimal necessary roles.
- Firewall rules include TCP ports `12345` and `12346` for `dataflow` target tag.

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
# First drain or cancel any active Dataflow jobs in the project
gcloud dataflow jobs list --status=active --region=<REGION>
# Then run terraform destroy
terraform destroy -auto-approve
```
