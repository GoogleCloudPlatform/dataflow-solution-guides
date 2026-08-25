---
name: dataflow-troubleshooting
description: >-
  Diagnose, debug, and resolve common Apache Beam and Google Cloud Dataflow errors in this repository.
  Use when Dataflow jobs fail to start, workers crash or fail health checks, IAM permission errors occur,
  Cloud Build or Docker container images fail, GPU quotas are exceeded, or streaming pipelines experience lag or out-of-memory errors.
---

# Dataflow Troubleshooting & Diagnostics Runbook

This skill provides diagnostic workflows and solutions for common failures encountered when building, provisioning, deploying, and running Dataflow pipelines in this repository.

---

## 1. Worker Startup & Networking Failures

### Symptom: `Workflow failed: All workers have timed out...` or `Worker failed to check in`
* **Root Cause 1: Missing Private Google Access**
  * Dataflow workers running with `--no_use_public_ip` require Private Google Access to reach Google APIs (Dataflow service, Cloud Storage, Pub/Sub).
  * **Fix**: Ensure the Terraform subnet has `enable_private_access = true`.
* **Root Cause 2: Missing Worker-to-Worker Firewall Rules**
  * Workers exchange shuffle data and health checks on TCP ports `12345` and `12346`.
  * **Fix**: Verify firewall rules in `module.firewall_rules` allow ingress and egress on `12345, 12346` for instances with tag `dataflow`.
* **Root Cause 3: Subnet CIDR Exhaustion**
  * The subnet CIDR range is too small to accommodate the requested `--max_num_workers`.
  * **Fix**: Use at least a `/20` or `/16` CIDR block for the subnetwork.

---

## 2. IAM & Authentication Errors

### Symptom: `403 Forbidden` or `Permission denied on Cloud Storage / PubSub / BigQuery / Spanner`
* **Root Cause: Service Account Permissions**
  * The Dataflow worker runs under the service account specified by `--service_account_email`.
  * **Required Roles**:
    - Dataflow Worker: `roles/dataflow.worker`
    - Cloud Storage (staging & temp files): `roles/storage.admin` or `roles/storage.objectAdmin`
    - Cloud Monitoring: `roles/monitoring.metricWriter`
    - Pub/Sub Ingestion/Output: `roles/pubsub.editor` or `roles/pubsub.subscriber` / `roles/pubsub.publisher`
    - BigQuery Output: `roles/bigquery.dataEditor` + `roles/bigquery.jobUser`
    - Spanner Ingestion/Change Stream: `roles/spanner.databaseUser`
  * **Fix**: Check `module.dataflow_sa` in the use case's `terraform/<use_case>/main.tf`.

---

## 3. GPU & Worker Accelerator Issues

### Symptom: `Quota 'GPUS_ALL_REGIONS' exceeded` or `NVIDIA driver failed to install`
* **Root Cause 1: GPU Quota**
  * NVIDIA L4 (`nvidia-l4`) or T4 GPUs require available compute quota in the target region (e.g. `us-central1`).
  * **Fix**: Check regional GPU quota:
    ```bash
    gcloud compute regions describe $REGION --format="flatten(quotas)" | grep -i gpu
    ```
* **Root Cause 2: Driver Option Mismatch**
  * **Fix**: Ensure the pipeline options include:
    `--dataflow_service_options="worker_accelerator=type:nvidia-l4;count:1;install-nvidia-driver:5xx"`

---

## 4. Python Serialization & DoFn Failures

### Symptom: `TypeError: cannot pickle '...' object` or `AttributeError` during execution
* **Root Cause: Instantiating unpickleable objects in DoFn `__init__`**
  * Database connections, ML model instances, and network clients cannot be pickled across worker processes.
  * **Fix**: Initialize clients and models inside `setup()` or `start_bundle()`, NOT `__init__()`:
    ```python
    class InferenceDoFn(beam.DoFn):
        def __init__(self, model_path):
            self.model_path = model_path
            self.model = None  # Do NOT load model here

        def setup(self):
            # Load model once per worker process
            self.model = load_model(self.model_path)

        def process(self, element):
            yield self.model.predict(element)
    ```

---

## 5. Cloud Build & Custom Container Errors

### Symptom: `Step #0: Failed to fetch base image` or `Substitutions error`
* **Root Cause: Missing Cloud Build Substitutions or Regional Bucket Configuration**
  * **Fix**: Ensure `_TAG` substitution is passed:
    ```bash
    gcloud builds submit \
      --region=$REGION \
      --default-buckets-behavior=regional-user-owned-bucket \
      --substitutions _TAG=$CONTAINER_URI \
      .
    ```

---

## 6. Custom Container & Beam SDK Version Mismatches

### Symptom: Worker crashes on startup with `SDK harness failed to connect`, `Incompatible SDK version`, or serialization / unpickling errors
* **Root Cause: Mismatch between pipeline submission environment and worker image**
  * The pipeline graph was generated using an `apache-beam` version in the launching environment (e.g. `requirements.txt` = `2.75.0`), but the worker custom container used a different version in `Dockerfile` (e.g. `COPY --from=apache/beam_python3.11_sdk:2.76.0 /opt/apache/beam /opt/apache/beam`).
  * **Fix**:
    1. Check the Beam version in `requirements.txt` (`grep apache-beam requirements.txt`).
    2. Check the container base and boot image tag in `Dockerfile` (`grep apache/beam Dockerfile`).
    3. Ensure both specify the exact same version (e.g., both `2.75.0`).
    4. Rebuild the custom container via Cloud Build and resubmit the Dataflow job.
