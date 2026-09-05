---
name: use-case-deployment
description: >-
  End-to-end orchestration and deployment runner for all solution guides in this repository.
  Use when deploying, running, ingesting test data into, or validating any of the 8 solution guides:
  GenAI & ML (Gemma 4 with vLLM on GPU), ETL & Integration (Spanner CDC), Customer Data Platform (CDP),
  Anomaly Detection, Marketing Intelligence, Clickstream Analytics, IoT Analytics, or Log Replication.
---

# Use Case Deployment Skill

This skill provides step-by-step execution workflows for deploying, running, verifying, and safely tearing down each of the 8 solution guides in this repository.

---

## 1. Solution Guide Matrix

| Guide Name | Terraform Directory | Pipeline Directory | Launch Command / Script | Input Generator / Test Data | Output Validation Target |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **GenAI & ML** | `terraform/ml_ai` | `pipelines/ml_ai_python` | `./scripts/02_run_dataflow.sh` | Pub/Sub `messages` topic | Pub/Sub `predictions-sub` subscription |
| **ETL & Integration** | `terraform/etl_integration` | `pipelines/etl_integration_java` | `./scripts/02_run_publisher_dataflow.sh` & `./scripts/03_run_changestream_template.sh` | Pub/Sub Taxirides feed | Cloud Spanner `events` table & BigQuery `replica.events_changelog` |
| **Customer Data Platform (CDP)** | `terraform/cdp` | `pipelines/cdp` | `./scripts/02_run_dataflow_job.sh` | `python cdp_pipeline/generate_transaction_data.py` | BigQuery `output_dataset.unified-table` |
| **Anomaly Detection** | `terraform/anomaly_detection` | `pipelines/anomaly_detection` | `./scripts/02_run_dataflow.sh` | Pub/Sub `anomaly-detection-transactions` topic | Pub/Sub `anomaly-detection-detections`, BigQuery `anomaly_detection.detections`, errors `anomaly-detection-errors` |
| **Marketing Intelligence** | `terraform/marketing_intelligence` | `pipelines/marketing_intelligence` | `./scripts/02_run_dataflow.sh` | Pub/Sub user activity stream | BigQuery marketing attribution tables |
| **Clickstream Analytics** | `terraform/clickstream_analytics` | `pipelines/clickstream_analytics_java` | `./scripts/01_launch_pipeline.sh` | Pub/Sub events | Cloud Bigtable & BigQuery analytics table |
| **IoT Analytics** | `terraform/iot_analytics` | `pipelines/iot_analytics` | `./scripts/02_submit_job.sh` | `python scripts/publish_on_pubsub.py` | BigQuery `iot.maintenance_analytics` & Pub/Sub `maintenance-alerts` |
| **Log Replication** | `terraform/log_replication_splunk` | `pipelines/log_replication_splunk` | `./scripts/01_launch_ps_to_splunk.sh` | Pub/Sub logging topic | Splunk HTTP Event Collector (HEC) |

---

## 2. Pre-Flight Checks & Best Practices

Before deploying infrastructure or submitting Dataflow jobs:

1. **Service Account Collision Check**:
   Check if the default service account name already exists in the project:
   ```bash
   gcloud iam service-accounts list --project=$PROJECT --filter="email:<sa-name>"
   ```
   If a collision exists, set a custom `service_account_name = "<custom-name>"` in `terraform.tfvars`.

2. **Shared VPC / Subnetwork Verification**:
   If running workers in a Shared VPC, verify that the full subnetwork URI is configured:
   `https://www.googleapis.com/compute/v1/projects/HOST_PROJECT/regions/REGION/subnetworks/SUBNET_NAME`

3. **GCS Staging Bucket**:
   Ensure the staging bucket (`gs://BUCKET_NAME/tmp`) exists in the same region as the Dataflow workers or set `create_bucket = true` in `terraform.tfvars`.

4. **Java Pipeline Toolchains**:
   For Java pipelines (`etl_integration_java`, `clickstream_analytics_java`), ensure `build.gradle` specifies `JavaLanguageVersion.of(25)`.

---

## 3. End-to-End Execution Workflows

### 1. GenAI & ML (Gemma 4 with vLLM on GPU)
```bash
# 1. Terraform
cd terraform/ml_ai
terraform init && terraform apply -auto-approve

# 2. Upload Gemma Model to GCS
gcloud storage cp -r LOCAL_GEMMA_PATH gs://$PROJECT/gemma_2B

# 3. Build Container & Launch Pipeline
cd ../../pipelines/ml_ai_python
source scripts/00_set_environment.sh
./scripts/01_build_and_push_container.sh
./scripts/02_run_dataflow.sh

# 4. Ingest & Validate
gcloud pubsub topics publish messages --message='{"prompt": "What is Google Cloud Dataflow?"}'
gcloud pubsub subscriptions pull predictions-sub --auto-ack --limit=1
```

### 2. ETL & Spanner Change Data Capture (Java)
```bash
# 1. Terraform
cd terraform/etl_integration
terraform init && terraform apply -auto-approve

# 2. Launch Spanner Ingestion & Change Stream Flex Template
cd ../../pipelines/etl_integration_java
source scripts/01_set_variables.sh
./scripts/02_run_publisher_dataflow.sh
./scripts/03_run_changestream_template.sh

# 3. Validate CDC output in Spanner & BigQuery
gcloud spanner databases execute-sql taxis_database --instance=test-spanner-instance --sql='SELECT COUNT(*) FROM events'
bq query --use_legacy_sql=false 'SELECT COUNT(*) FROM replica.events_changelog'
```

### 3. Customer Data Platform (CDP)
```bash
# 1. Terraform
cd terraform/cdp
terraform init && terraform apply -auto-approve

# 2. Build Container & Launch Dataflow
cd ../../pipelines/cdp
source scripts/00_set_variables.sh
./scripts/01_cloudbuild_and_push_container.sh
./scripts/02_run_dataflow_job.sh

# 3. Generate Streaming Transactions
python3 ./cdp_pipeline/generate_transaction_data.py

# 4. Validate Unified BigQuery Table
bq query --use_legacy_sql=false 'SELECT * FROM output_dataset.`unified-table` LIMIT 10'
```

### 4. Clickstream Analytics with Bigtable (Java)
```bash
# 1. Terraform
cd terraform/clickstream_analytics
terraform init && terraform apply -auto-approve

# 2. Launch Java Pipeline
cd ../../pipelines/clickstream_analytics_java
source scripts/00_set_variables.sh
./scripts/01_launch_pipeline.sh
```

### 5. Anomaly Detection with Vertex AI

Follow [the executable guide](../../../use_cases/Anomaly_Detection.md) from the
repository root. Use an existing project/network and an existing bucket by default.
Python 3.14 is required across all components: Dataflow pipeline, local tooling,
custom training container, and custom prediction serving container.

After provisioning Terraform, source `scripts/00_set_variables.sh` in
`pipelines/anomaly_detection`. Build worker, training, and serving images through the
provided Cloud Build scripts (`scripts/01_build_and_push_container.sh`,
`scripts/01_build_training_container.sh`, `scripts/01_build_serving_container.sh`),
then source their environment files (`.deployment/training_environment.sh`,
`.deployment/serving_environment.sh`). Run `python -m
anomaly_detection_pipeline.workflow` stages `train`, `validate` and `deploy`;
source the separate `scripts/03_endpoint_environment.sh`, then `verify`, `seed`,
launch `scripts/02_run_dataflow.sh` and run `smoke --count 20 --timeout 600`.
The guide contains the exact commands, identities, quotas, and non-Terraform cleanup procedures.

Keep the ignored deployment manifest to resume partial runs and clean up only
owned resources. Ambiguous creates must be reconciled before retrying. Compatible
external `MODEL_ENDPOINT` values are supported with endpoint-level worker
prediction access (`roles/anomalyDetectionPredictor`); never adopt or delete external
endpoints. Workers use private IPs and CPU `n1-standard-2`. Bigtable customer profiles
(`customer_profiles`) and BigQuery archival (`anomaly_detection.detections`) are active
parts of the graph. Stop Dataflow, run workflow cleanup, execute any required manual
resource deletions, then destroy Terraform. Report local/container tests separately
from live-cloud smoke results.

### 6. Marketing Intelligence
```bash
# 1. Terraform
cd terraform/marketing_intelligence
terraform init && terraform apply -auto-approve

# 2. Launch Pipeline
cd ../../pipelines/marketing_intelligence
source scripts/00_set_environment.sh
./scripts/02_run_dataflow.sh
```

### 7. IoT Analytics
```bash
# 1. Terraform
cd terraform/iot_analytics
terraform init && terraform apply -auto-approve

# 2. Build Container & Seed Metadata
cd ../../pipelines/iot_analytics
source scripts/00_set_environment.sh
./scripts/01_cloud_build_and_push.sh
python scripts/create_and_populate_bigtable.py

# 3. Launch Pipeline & Simulator
./scripts/02_submit_job.sh
python scripts/publish_on_pubsub.py
```

### 8. Log Replication into Splunk
```bash
# 1. Terraform
cd terraform/log_replication_splunk
terraform init && terraform apply -auto-approve

# 2. Launch Dataflow Pipeline
cd ../../pipelines/log_replication_splunk
source scripts/00_set_variables.sh
./scripts/01_launch_ps_to_splunk.sh

# 3. Optional: Access Demo Splunk Web UI (Port 8501 via IAP Tunnel)
# If deploy_demo_splunk = true:
gcloud compute start-iap-tunnel $SPLUNK_DEMO_INSTANCE 8000 --local-host-port=localhost:8501 --zone=$ZONE --project=$PROJECT

# 4. Ingest Test Log Event & Verify
gcloud pubsub topics publish splunk-logs --message='{"message": "Test replication log", "severity": "INFO"}'
```

---

## 4. Post-Deployment Observability Checklist

1. **Job Status Check**:
   ```bash
   gcloud dataflow jobs list --project=$PROJECT --region=$REGION --status=active --limit=10 --format="table(id, name, type, state)"
   ```
2. **Worker Log Inspection**:
   ```bash
   gcloud logging read 'resource.labels.job_id="<JOB_ID>" severity>=ERROR' --project=$PROJECT --limit=20
   ```
3. **Data Freshness / Backlog**:
   Check the `System Lag` and `Data Freshness` metrics in the Cloud Monitoring / Dataflow UI.

---

## 5. Safe Teardown & Resource Cleanup

When cleaning up or concluding test deployments, always follow this order:

1. **Stop / Cancel Active Dataflow Jobs**:
   ```bash
   gcloud dataflow jobs cancel <JOB_ID_1> <JOB_ID_2> --project=$PROJECT --region=$REGION
   ```
2. **Wait for Job Cancellation**:
   Ensure jobs have reached `Cancelled` or `Drained` state before destroying infrastructure:
   ```bash
   gcloud dataflow jobs list --project=$PROJECT --region=$REGION --status=active
   ```
3. **Run Terraform Destroy**:
   ```bash
   cd terraform/<use_case>
   terraform destroy -auto-approve
   ```
4. **Verify Clean Teardown**:
   ```bash
   terraform state list
   ```
