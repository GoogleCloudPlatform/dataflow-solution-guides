# Dataflow Solution Guides — Agent Guidelines

Welcome to the **Dataflow Solution Guides** repository. This repository hosts reference architectures, Cloud Foundation Fabric Terraform infrastructure, and production-ready Apache Beam pipelines deployed on **Google Cloud Dataflow**.

This guide outlines architectural patterns, coding conventions, development workflows, security guardrails, and deployment instructions for AI coding agents (such as Antigravity, Claude Code, Cursor, Codex, Copilot, etc.).

---

## 1. Repository Architecture

The codebase is organized into three interconnected tiers:

```
dataflow-solution-guides/
├── use_cases/                # Solution architecture documentation, one-pagers, and guides
│   ├── GenAI_ML.md           # Real-time inference with local GenAI models (Gemma 4 on GPU)
│   ├── ETL_integration.md    # Change Data Capture (CDC) from Cloud Spanner to BigQuery
│   ├── CDP.md                # Real-time Customer Data Platform (multi-topic streaming joins)
│   ├── Anomaly_Detection.md  # Real-time anomaly detection with Vertex AI
│   ├── Marketing_Intelligence.md # Real-time marketing intelligence with Firestore & Scikit-Learn RunInference
│   ├── Clickstream_Analytics.md  # Real-time clickstream analytics with Bigtable enrichment
│   ├── IoT_Analytics.md      # Real-time IoT analytics with Bigtable & Vertex AI
│   └── Log_replication.md    # Real-time log replication into Splunk
│
├── terraform/                # Infrastructure-as-Code using Google Cloud Foundation Fabric
│   ├── ml_ai/                # Pub/Sub topics, Artifact Registry, GCS bucket, Service Account
│   ├── etl_integration/      # Spanner instance/database/change stream, BigQuery, Service Account
│   ├── cdp/                  # Pub/Sub topics, BigQuery dataset/tables, VPC
│   ├── anomaly_detection/    # Pub/Sub, BigQuery, Vertex AI endpoints, VPC
│   ├── marketing_intelligence/ # Pub/Sub topics, Firestore, BigQuery dataset, Artifact Registry, Service Account
│   ├── clickstream_analytics/  # Bigtable instance, Pub/Sub, BigQuery, Service Account
│   ├── iot_analytics/        # Bigtable, Pub/Sub, GCS, VPC
│   └── log_replication_splunk/ # Pub/Sub, Secret Manager, Service Account, Optional Splunk VM
│
├── pipelines/                # Apache Beam streaming pipeline implementations
│   ├── ml_ai_python/         # Python: Beam RunInference with Gemma 4 using vLLM on NVIDIA L4 GPU
│   ├── etl_integration_java/ # Java: Spanner change stream CDC publisher & template
│   ├── cdp/                  # Python: Multi-stream customer data unification to BigQuery
│   ├── anomaly_detection/    # Python: Vertex AI prediction pipeline
│   ├── marketing_intelligence/ # Python: Firestore enrichment & Scikit-Learn RunInference
│   ├── clickstream_analytics_java/ # Java: Bigtable lookup enrichment + BigQuery deadletter
│   ├── iot_analytics/        # Python: IoT sensor aggregation + Bigtable/Vertex AI
│   ├── log_replication_splunk/ # Dataflow Flex Template: Pub/Sub to Splunk HEC
│   └── pylintrc              # Google Python Style Guide Pylint configuration
│
└── .agents/                  # Workspace Agent Customizations
    ├── skills.json           # Agent skill registration manifest
    └── skills/               # Specialized on-demand operational skills
```

### The Deployment Link Between Terraform and Pipelines
Every Terraform module in `terraform/<use_case>/` contains a `resource "local_file" "variables_script"` that dynamically generates an environment configuration file (e.g. `01_set_variables.sh` or `00_set_environment.sh`) directly inside the corresponding `pipelines/<use_case>/scripts/` directory.

---

## 2. Technology Stack & Prerequisites

| Technology | Role / Version | Key Tooling |
| :--- | :--- | :--- |
| **Dataflow / Beam** | Stream processing runtime | Apache Beam Python SDK 2.50+, Apache Beam Java SDK 2.50+ |
| **Python** | Pipeline development | Python 3.13 / 3.14, `yapf`, `pylint`, `pipenv` / `venv` |
| **Java** | Pipeline development | OpenJDK 25, Gradle Wrapper (`./gradlew`), Spotless |
| **Terraform** | Infrastructure as Code | Terraform >= 1.5, Google Cloud Foundation Fabric v56.2.0 |
| **Containers & CI** | Worker environment & CI | Docker, Google Cloud Build (`cloudbuild.yaml`), GitHub Actions |
| **Google Cloud** | Managed platform | Dataflow, Pub/Sub, Cloud Storage, BigQuery, Spanner, Bigtable, Vertex AI |

---

## 3. Development Workflows & Quality Standards

### Python Pipelines (`pipelines/<use_case>`)
1. **Code Formatting**:
   Format all Python files using Google style with `yapf`:
   ```bash
   yapf -i -r --style yapf .
   ```
2. **Linting & Style Checks**:
   Check code against the root `pipelines/pylintrc` configuration:
   ```bash
   pylint --rcfile ../pylintrc .
   ```
3. **Packaging**:
   Validate package builds via source distribution:
   ```bash
   python setup.py sdist
   ```
4. **Local Execution**:
   Test pipeline transforms locally with `DirectRunner` before submitting to Dataflow:
   ```bash
   python main.py --runner=DirectRunner [options...]
   ```

### Java Pipelines (`pipelines/<use_case>_java`)
1. **Build & Test**:
   Execute the Gradle wrapper build:
   ```bash
   ./gradlew build
   ```
2. **Code Formatting**:
   Apply Google Java Style via Spotless:
   ```bash
   ./gradlew spotlessApply
   ```
3. **Local Execution**:
   Run with `DirectRunner`:
   ```bash
   ./gradlew run -Pargs="--runner=DirectRunner [options...]"
   ```

### Terraform Infrastructure (`terraform/<use_case>`)
1. **Formatting**:
   ```bash
   terraform fmt
   ```
2. **Initialization & Validation**:
   ```bash
   terraform init
   terraform validate
   ```
3. **Planning & Application**:
   ```bash
   terraform plan -out=tfplan
   terraform apply tfplan
   ```

---

## 4. Security & Networking Guardrails

When authoring or modifying code in this repository, strictly adhere to the following security rules:

1. **Private IPs Only for Dataflow Workers**:
   - **Never** enable public IPs for Dataflow workers.
   - In Python options: `--no_use_public_ip`
   - In Java options: `--usePublicIps=false`
   - In Flex Templates / gcloud commands: `--disable-public-ips`
2. **VPC & Subnetwork Configuration**:
   - Subnetworks must have `enable_private_access = true` (Private Google Access).
   - If workers need internet access (e.g. downloading external dependencies), configure Cloud NAT (`module.regional_nat`).
3. **Firewall Rules**:
   - Dataflow worker-to-worker communication requires TCP ingress and egress on ports `12345` and `12346` tagged with `dataflow`.
4. **Identity & Access Management (IAM)**:
   - Always run Dataflow jobs with a dedicated custom service account (`--service_account_email` / `--serviceAccount`).
   - Grant least-privilege roles (e.g., `roles/dataflow.worker`, `roles/storage.objectAdmin`, `roles/pubsub.editor`, `roles/bigquery.dataEditor`, `roles/spanner.databaseUser`).
   - Never use the default Compute Engine service account.
5. **Beam SDK & Custom Container Version Parity**:
   - The Apache Beam SDK version pinned in `requirements.txt` (`apache-beam[gcp]==<version>`) and the base/boot image tag in `Dockerfile` (`apache/beam_python3.13_sdk:<version>` or `apache/beam_python3.14_sdk:<version>`) **must strictly match**.
   - Do not upgrade container tags unless the matching stable SDK package is published to PyPI and `requirements.txt` is updated in the same change.

---

## 5. End-to-End Deployment Lifecycle

When assisting a user with deploying a solution guide, follow this structured 7-step process:

1. **Infrastructure Provisioning**:
   - Navigate to `terraform/<use_case>/`.
   - Ensure `terraform.tfvars` defines `project_id`, `region`, and `billing_account`.
   - Run `terraform init` and `terraform apply`.
2. **Environment Variable Loading**:
   - Navigate to `pipelines/<use_case>/`.
   - Source the generated variables file:
     ```bash
     source scripts/01_set_variables.sh   # (or 00_set_variables.sh / 00_set_environment.sh)
     ```
3. **Container Build (for Custom Container Pipelines)**:
   - If the pipeline requires a custom SDK container (e.g., GPU/ML models):
     ```bash
     ./scripts/01_build_and_push_container.sh
     ```
4. **Pipeline Submission**:
   - Launch the streaming pipeline to Google Cloud Dataflow:
     ```bash
     ./scripts/02_run_dataflow.sh         # (or ./scripts/01_launch_pipeline.sh)
     ```
5. **Data Ingestion & Simulation**:
   - Run the data generator or publisher script to produce streaming events (e.g. `python cdp_pipeline/generate_transaction_data.py` or publishing to Pub/Sub).
6. **Verification & Observability**:
   - Inspect Dataflow Job status via GCP Console or `gcloud dataflow jobs list`.
   - Query target destinations (BigQuery tables, Cloud Spanner database, Cloud Bigtable rows, Pub/Sub output subscriptions).
7. **Resource Cleanup**:
   - Cancel or drain active Dataflow jobs.
   - Run `terraform destroy` in `terraform/<use_case>/`.

---

## 6. Antigravity Custom Skills

The repository includes specialized workspace skills located in `.agents/skills/`:

- **`dataflow-pipeline-dev`**: Runbooks and procedures for developing, linting, packaging, and locally testing Beam pipelines.
- **`terraform-deploy`**: Procedures for provisioning, validating, planning, and managing Cloud Foundation Fabric Terraform modules.
- **`use-case-deployment`**: Matrix and step-by-step guides for end-to-end execution of any of the 8 solution guides.
- **`dataflow-troubleshooting`**: Diagnostic playbooks for resolving common Dataflow worker, IAM, quota, networking, and serialization errors.
- **`pr-review`**: Procedures for reviewing Pull Requests, monitoring CI builds, verifying security guardrails and code style policies, approving, merging, or providing corrective feedback.

