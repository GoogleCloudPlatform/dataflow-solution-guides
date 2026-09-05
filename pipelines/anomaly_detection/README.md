# Anomaly detection pipeline and model workflow

Run the complete [executable walkthrough](../../use_cases/Anomaly_Detection.md)
for provisioning, authentication, training, endpoint deployment, enrichment,
streaming inference, smoke verification and cleanup.

The Python 3.14 Dataflow pipeline reads JSON transactions from Pub/Sub topic
`anomaly-detection-transactions`, loads `profile:average_amount` from Bigtable table
`customer_profiles` (in instance `anomaly-detection`) by customer ID, and sends
batches of `[amount_ratio, distance_from_home, recent_transaction_count]` to a
standard authenticated Vertex AI endpoint through keyed `RunInference`. Successful
predictions retain transaction/customer IDs and timestamp, and are written to
both Pub/Sub (`anomaly-detection-detections`) and BigQuery (`anomaly_detection.detections`).
`-1` denotes an anomaly; `1` denotes normal. Malformed inputs, missing profiles,
exhausted inference failures and permanent BigQuery insertion errors are published
to `anomaly-detection-errors`.

| Component | Purpose |
|---|---|
| `anomaly_detection_pipeline/features.py` | Shared deterministic data and feature contract on Python 3.14 |
| `training/` | Custom Python 3.14 / scikit-learn 1.6+ CPU training container and artifact checks |
| `serving/` | Custom Python 3.14 / FastAPI CPU prediction serving container conforming to Vertex AI HTTP contract |
| `anomaly_detection_pipeline/workflow.py` | Python 3.14 managed training, validation, deployment, verification and ownership-aware cleanup |
| `anomaly_detection_pipeline/lifecycle.py` | Manifest locking, atomic journaling, interrupted-create reconciliation and endpoint IAM |
| `anomaly_detection_pipeline/demo.py` | Profile seeding, bounded publisher and timed cloud smoke check |
| `anomaly_detection_pipeline/pipeline.py` | Enrichment, keyed inference, Pub/Sub/BigQuery outputs and failure routing |
| `anomaly_detection_pipeline/inference.py` | Batched predict-only Vertex client, strict response association and bounded retries |

## Quick execution

Provision [Terraform](../../terraform/anomaly_detection/README.md) first. From this
directory, with Python 3.14 and Docker available:

```bash
source scripts/00_set_variables.sh
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-tooling.txt -r requirements-dev.txt
bash scripts/01_build_and_push_container.sh
bash scripts/01_build_training_container.sh
source .deployment/training_environment.sh
bash scripts/01_build_serving_container.sh
source .deployment/serving_environment.sh
python -m anomaly_detection_pipeline.workflow train
python -m anomaly_detection_pipeline.workflow validate
python -m anomaly_detection_pipeline.workflow deploy
source scripts/03_endpoint_environment.sh
python -m anomaly_detection_pipeline.workflow verify
python -m anomaly_detection_pipeline.workflow seed
bash scripts/02_run_dataflow.sh
# Wait until the submitted Dataflow job is Running.
python -m anomaly_detection_pipeline.workflow smoke --count 20 --timeout 600
```

Both training and serving use custom Python 3.14 containers built with Cloud Build.
The Google prebuilt training image availability ended July 11, 2026, and the prebuilt
prediction image (`sklearn-cpu.1-6`) reaches patch end on October 14, 2026. Custom
containerization on Python 3.14 provides complete control over runtime dependencies
(`scikit-learn>=1.6,<2`, `numpy>=2.0,<3`) and avoids the prebuilt deprecation cycle.
The worker SDK and image remain pinned to matching Beam 2.76.0.

For an existing compatible endpoint, set `MODEL_ENDPOINT` and optional
`MODEL_LOCATION`, skip train/validate/deploy, and have its owner grant endpoint
prediction permission (`roles/anomalyDetectionPredictor`) to `SERVICE_ACCOUNT`.
Run `verify`, seed profiles, build the worker and launch. The workflow never
adopts or cleans up external endpoints.

## Configuration and recovery

Terraform generates `scripts/00_set_variables.sh`; do not edit it. Required
settings include `PROJECT`, `REGION`, `SERVICE_ACCOUNT`, `BUCKET`,
`TRAINING_SERVICE_ACCOUNT`, `TRAINING_CONTAINER_URI`, `SERVING_CONTAINER_URI`,
`ENDPOINT_PREDICTOR_ROLE`, `INPUT_TOPIC`, `INPUT_SUBSCRIPTION`, `OUTPUT_TOPIC`,
`ERROR_TOPIC`, `BIGTABLE_INSTANCE`, `BIGTABLE_TABLE`, `BIGQUERY_DATASET`, and
`BIGQUERY_TABLE`. The launch script supports optional `SUBNETWORK`, falling back
to legacy `NETWORK` as a subnet path, and always disables public worker IPs.

Keep the ignored `.deployment/manifest.json` and reuse it to resume. Commands
journal mutation intent before API calls, reconcile resources by their unique
ownership label and refuse ambiguous duplicate creation. Do not run copies of
the same manifest on different hosts concurrently. Use `--manifest` and
`--endpoint-env` for separately tracked runs; each run must keep its own file
paths. An unknown operation requires inspection and reconciliation as described
in the walkthrough, not deletion of the manifest.

Cancel/drain the Dataflow job and wait for terminal status before running
`python -m anomaly_detection_pipeline.workflow cleanup`. Cleanup removes only
recorded, label-verified Vertex resources and the run's artifact prefix. Then
run `terraform destroy` in the infrastructure directory. Bigtable, endpoint
replicas and Dataflow continue to cost money while idle.

## Verification

```bash
python -m unittest discover -s tests -v
python -m unittest discover -s training/tests -v
python -m unittest discover -s serving/tests -v
yapf --diff --recursive --style yapf anomaly_detection_pipeline tests training serving main.py setup.py
pylint --rcfile ../pylintrc -j 1 anomaly_detection_pipeline tests training serving main.py setup.py
python setup.py sdist
bash scripts/verify_serving_container.sh
```

The tests run DirectRunner with mocked cloud services, exercise JSON/feature
contracts, batched association, failures, recovery and cleanup. The container
script builds both isolated training and serving images, and verifies the model
artifact against the Python 3.14 serving container contract. A cloud smoke run
is separate from these local/container checks and requires a designated test project.
