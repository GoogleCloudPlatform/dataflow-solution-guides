---
name: use-case-deployment
description: >-
  End-to-end orchestration and deployment runner for all solution guides in this repository.
  Use when deploying, running, ingesting test data into, or validating any of the 8 solution guides:
  GenAI & ML (Gemma on GPU), ETL & Integration (Spanner CDC), Customer Data Platform (CDP),
  Anomaly Detection, Marketing Intelligence, Clickstream Analytics, IoT Analytics, or Log Replication.
---

# Use Case Deployment Skill

This skill provides step-by-step execution workflows for deploying, running, and verifying each of the 8 solution guides in this repository.

---

## 1. Solution Guide Matrix

| Guide Name | Terraform Directory | Pipeline Directory | Launch Command / Script | Input Generator / Test Data | Output Validation Target |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **GenAI & ML** | `terraform/ml_ai` | `pipelines/ml_ai_python` | `./scripts/02_run_dataflow.sh` | Pub/Sub `messages` topic | Pub/Sub `predictions-sub` subscription |
| **ETL & Integration** | `terraform/etl_integration` | `pipelines/etl_integration_java` | `./scripts/02_run_publisher_dataflow.sh` & `./scripts/03_run_changestream_template.sh` | Pub/Sub Taxirides feed | Cloud Spanner `events` table & BigQuery `replica` dataset |
| **Customer Data Platform (CDP)** | `terraform/cdp` | `pipelines/cdp` | `./scripts/02_run_dataflow_job.sh` | `python cdp_pipeline/generate_transaction_data.py` | BigQuery `output_dataset.unified-table` |
| **Anomaly Detection** | `terraform/anomaly_detection` | `pipelines/anomaly_detection` | `./scripts/02_run_dataflow.sh` | Pub/Sub `events` topic | BigQuery `anomalies` table |
| **Marketing Intelligence** | `terraform/marketing_intelligence` | `pipelines/marketing_intelligence` | `./scripts/02_run_dataflow.sh` | Pub/Sub user activity stream | BigQuery marketing attribution tables |
| **Clickstream Analytics** | `terraform/clickstream_analytics` | `pipelines/clickstream_analytics_java` | `./scripts/01_launch_pipeline.sh` | Pub/Sub events | Cloud Bigtable & BigQuery analytics table |
| **IoT Analytics** | `terraform/iot_analytics` | `pipelines/iot_analytics` | `./scripts/02_submit_job.sh` | `python scripts/publish_on_pubsub.py` | Cloud Bigtable & GCS output |
| **Log Replication** | `terraform/log_replication_splunk` | `pipelines/log_replication_splunk` | `./scripts/01_launch_ps_to_splunk.sh` | Pub/Sub logging topic | Splunk HTTP Event Collector (HEC) |

---

## 2. End-to-End Execution Workflows

### 1. GenAI & ML (Gemma LLM on GPU)
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

# 3. Validate CDC output in BigQuery
bq query --use_legacy_sql=false 'SELECT COUNT(*) FROM replica.events'
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

---

## 3. Post-Deployment Observability Checklist

1. **Job Status Check**:
   ```bash
   gcloud dataflow jobs list --status=active --region=$REGION
   ```
2. **Worker Log Inspection**:
   ```bash
   gcloud logging read 'resource.type="dataflow_step" severity>=ERROR' --limit=20
   ```
3. **Data Freshness / Backlog**:
   Check the `System Lag` and `Data Freshness` metrics in the Cloud Monitoring / Dataflow UI.
