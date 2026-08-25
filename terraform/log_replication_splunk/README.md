# Log Replication Infrastructure

This directory contains the Terraform code to provision all application-level infrastructure in Google Cloud required to run the **Log Replication & Analytics** solution guide.

These deployment scripts are part of the [Dataflow Log Replication & Analytics Solution Guide](../../use_cases/Log_replication.md).

With this module, you deploy a Cloud Logging sink that routes Google Cloud log events to a Pub/Sub topic (`all-logs`). A streaming Dataflow job using the Google-provided `Cloud_PubSub_to_Splunk` template continuously replicates these logs to Splunk via the Splunk HTTP Event Collector (HEC).

---

## Deployment Modes

This module supports two deployment scenarios:

1. **Production Deployment (`deploy_demo_splunk = false`, default)**:
   Connects the Dataflow pipeline directly to your external, production Splunk Cloud or Splunk Enterprise cluster using `var.splunk_hec_url` and `var.splunk_token`.

2. **GCP Splunk Demo Deployment (`deploy_demo_splunk = true`)**:
   Provisions an in-project Compute Engine VM running the official `splunk/splunk:latest` container with HEC enabled. The Dataflow pipeline connects privately over the internal network (`http://<INTERNAL_IP>:8088`), and you can securely access the Splunk Web UI locally on `http://localhost:8501` via Identity-Aware Proxy (IAP).

> [!NOTE]
> **Splunk Demo Container License**:
> When `deploy_demo_splunk = true`, the demo VM deploys the official `splunk/splunk` container image under Splunk's standard Trial / Free Developer License (up to 500 MB/day indexing volume). Please refer to the [Splunk Software License Agreement](https://www.splunk.com/en_us/legal/splunk-software-license-agreement.html) for details.

---

## Bill of Resources

| Resource | Default Name / ID | Description |
| :--- | :--- | :--- |
| **Pub/Sub Topic** | `all-logs` | Ingests project log messages routed from Cloud Logging. |
| **Pub/Sub Subscription** | `all-logs-sub` | Ingestion subscription consumed by the Dataflow pipeline. |
| **Pub/Sub Topic** | `deadletter-topic` | Receives log events rejected by Splunk due to non-transitory errors. |
| **Pub/Sub Subscription** | `deadletter-sub` | Dead-letter subscription for inspection and alerting. |
| **Cloud Logging Sink** | `pubsub-sink` | Project-level logging sink directing all logs to `all-logs`. |
| **Secret Manager Secret** | `splunk-token` | Securely stores the Splunk HEC authentication token. |
| **Dataflow Service Account** | `splunk-replication-sa` | Dedicated worker service account with least-privilege IAM roles. |
| **GCS Bucket** *(Optional)* | `${var.bucket_name}` | Staging and temporary bucket for Dataflow execution (`create_bucket = true`). |
| **Compute Engine VM** *(Optional)* | `splunk-demo` | Demo Splunk Enterprise instance on Container-Optimized OS (`deploy_demo_splunk = true`). |
| **Firewall Rule** *(Optional)* | `allow-splunk-demo-internal` | Allows ingress TCP on ports 8088 (HEC) and 8000 (Web UI) for demo VM (`deploy_demo_splunk = true`). |

---

## Configuration Variables

| Variable | Type | Default | Description |
| :--- | :---: | :---: | :--- |
| `project_id` | `string` | *(Required)* | Project ID of the existing GCP project. |
| `region` | `string` | *(Required)* | GCP region for application resources and Dataflow jobs. |
| `zone` | `string` | `null` | Optional GCP zone for demo resources (defaults to `{region}-a`). |
| `subnetwork` | `string` | `null` | Optional subnetwork URL or path for Dataflow workers and demo VM. |
| `bucket_name` | `string` | `null` | Optional GCS bucket name for Dataflow temp/staging files (defaults to `project_id`). |
| `service_account_name` | `string` | `"splunk-replication-sa"` | Name of the dedicated Dataflow worker service account. |
| `create_bucket` | `bool` | `false` | Set to `true` to provision a new GCS bucket for staging files. |
| `destroy_all_resources` | `bool` | `true` | When `true`, enables force destroy on bucket and demo resources for dev environments. |
| `deploy_demo_splunk` | `bool` | `false` | Set to `true` to deploy a demo Splunk Enterprise instance in the project. |
| `splunk_hec_url` | `string` | `"http://some-endpoint:8088"` | Splunk HEC endpoint URL (used when `deploy_demo_splunk = false`). |
| `splunk_token` | `string` | `"WRITE_YOUR_TOKEN_HERE"` | Splunk HEC token stored in Secret Manager (used when `deploy_demo_splunk = false`). |
| `splunk_admin_password` | `string` | `"SplunkDemoPass123!"` | Admin password for Splunk demo Web UI (used when `deploy_demo_splunk = true`). |

---

## How to Deploy

### Step 1: Configure `terraform.tfvars`

#### Option A: GCP Demo Splunk Deployment
```hcl
project_id            = "YOUR_PROJECT_ID"
region                = "europe-southwest1"
subnetwork            = "regions/europe-southwest1/subnetworks/dev-default" # Optional
deploy_demo_splunk    = true
splunk_admin_password = "YourSecurePassword123!"
destroy_all_resources = true
```

#### Option B: Production Splunk Cloud Deployment
```hcl
project_id            = "YOUR_PROJECT_ID"
region                = "europe-southwest1"
subnetwork            = "regions/europe-southwest1/subnetworks/dev-default" # Optional
deploy_demo_splunk    = false
splunk_hec_url        = "https://http-inputs-my-stack.splunkcloud.com:8088"
splunk_token          = "YOUR_SPLUNK_HEC_TOKEN"
destroy_all_resources = false
```

### Step 2: Initialize & Apply Terraform

```bash
terraform init
terraform apply
```

### Step 3: Run the Pipeline

Terraform generates `scripts/01_set_variables.sh` inside `pipelines/log_replication_splunk/`. Proceed to the [pipeline guide](../../pipelines/log_replication_splunk/README.md):

```bash
cd ../../pipelines/log_replication_splunk
source scripts/01_set_variables.sh
./scripts/01_launch_ps_to_splunk.sh
```

---

## Accessing the Demo Splunk Web UI (Port 8501)

When using `deploy_demo_splunk = true`, the demo VM does not require a public IP. Use Google Cloud Identity-Aware Proxy (IAP) to tunnel the Splunk Web UI to your local workstation:

```bash
# Method 1: SSH Port Forwarding over IAP (Recommended)
gcloud compute ssh splunk-demo \
    --zone=YOUR_ZONE \
    --project=YOUR_PROJECT_ID \
    -- -N -L 8501:localhost:8000

# Method 2: Direct IAP TCP Forwarding
gcloud compute start-iap-tunnel splunk-demo 8000 \
    --local-host-port=localhost:8501 \
    --zone=YOUR_ZONE \
    --project=YOUR_PROJECT_ID
```

Navigate to `http://localhost:8501` in your browser and sign in with username `admin` and your configured password.

---

## Resource Teardown

To safely tear down resources without locking active workers:

1. **Stop active Dataflow streaming jobs**:
   ```bash
   gcloud dataflow jobs cancel <JOB_ID> --region=YOUR_REGION --project=YOUR_PROJECT_ID
   ```
2. **Wait for jobs to reach `Cancelled` state**.
3. **Run Terraform Destroy**:
   ```bash
   terraform destroy
   ```

> [!NOTE]
> **Expected Cloud Logging Notification During Teardown**:
> During `terraform destroy`, project owners may receive an automated email notification from Google Cloud Logging stating *"Error in Cloud Logging sink configuration"* with error code `topic_not_found`. This is a normal transient notification caused when the destination Pub/Sub topic (`all-logs`) is deleted in parallel with the log sink (`pubsub-sink`) while background shutdown logs are emitted. It requires no action.