# Marketing Intelligence Pipeline — Modernization & Improvement Plan

## 1. Executive Summary

The **Marketing Intelligence** solution guide is currently in an incomplete state with critical runtime bugs, unimplemented features, and mismatched infrastructure:
1. **Critical Runtime Bug**: A typo in [`pipeline.py`](file:///usr/local/google/home/ihr/projects/dataflow-solution-guides/pipelines/marketing_intelligence/marketing_intelligence_pipeline/pipeline.py#L30) (`element.infernece`) causes a fatal `AttributeError` on every prediction.
2. **Unimplemented Architecture**: Infrastructure is provisioned in Terraform, but the pipeline code does not perform feature enrichment (marked as `# TODO Add transformation for BigTable Enrichment`) or write to BigQuery.
3. **External Dependency Friction**: The current pipeline relies on a remote, unspecified Vertex AI AutoML endpoint (`<vertex_ai_end_point>`) with no dataset, schema, or training code provided.
4. **Resource Inefficiency**: Workers are configured with expensive `nvidia-l4` GPUs to perform remote HTTP/gRPC API calls to Vertex AI, where GPUs sit completely idle.
5. **Terraform Path Typo**: Terraform attempts to generate `00_set_variables.sh` under `pipelines/market_intelligence/` instead of `pipelines/marketing_intelligence/`.

### Strategic Architecture Choices
* **Serverless Enrichment via Cloud Firestore (Native Mode)**: Replace the costly 3-node Bigtable cluster with Cloud Firestore. Firestore provides **$0 idle cost**, native JSON document mapping, visual document inspection in the GCP Console, and sub-millisecond lookups when paired with in-memory worker caching.
* **Hermetic Custom SDK Container**: Pre-bake all ML libraries (`scikit-learn`, `google-cloud-firestore`, `cachetools`, `pandas`, `numpy`), pipeline source code, and the trained model artifact (`marketing_model.pkl`) into a custom Dataflow worker container image. This eliminates worker boot startup delay, avoids runtime network fetches from PyPI, and ensures 100% reproducible worker behavior during autoscaling.
* **Local ML Model on Dataflow Workers**: A pre-trained Scikit-Learn Customer Purchase Propensity / Next-Best-Offer classification model executed on standard CPU workers via Apache Beam's `RunInference`.
* **Multi-Sink Outputs**: Enriched analytics persisted to **BigQuery** via the Storage Write API, alongside high-propensity activation triggers emitted to **Pub/Sub**.
* **Mock Data Suite**: Standalone scripts to generate training data, train the model, batch-seed Firestore customer profiles, and stream mock user actions into Pub/Sub.

---

## 2. Target Architecture & Data Flow

```mermaid
flowchart TD
    subgraph Data Generation & Seeding
        TG["01_train_model.py\n(Synthetic Journey Data)"] -->|"Train & Export"| MODEL[("marketing_model.pkl\n(Random Forest Classifier)")]
        FG["02_populate_firestore.py\n(Historical Profiles)"] -->|"Batch Insert"| FS[("Cloud Firestore (Native Mode)\nCollection: customer_profiles\nDoc ID: user_id")]
        PG["03_publish_events.py\n(Streaming User Actions)"] -->|"Publish JSON"| PS_IN["Pub/Sub Topic\n(Input Events)"]
    end

    subgraph Containerization & Cloud Build
        MODEL -->|"Copy Artifact"| DOCKER["Dockerfile\n(Pre-baked Model + Dependencies)"]
        DOCKER -->|"Cloud Build"| AR[("Artifact Registry\ndataflow-containers/\nmarket-intelligence:0.1")]
    end

    subgraph Dataflow Streaming Pipeline (Custom Container Workers)
        AR -.->|"Worker Boot Image"| WORKERS["Dataflow Workers\n(Hermetic SDK Container)"]
        PS_IN -->|"beam.io.ReadFromPubSub"| EXTRACT["Extract & Parse JSON\n(user_id, item_id, category, duration)"]
        EXTRACT -->|"FirestoreEnrichmentHandler\n(with LRU In-Memory Cache)"| ENRICH["Enrich with Firestore\n(past_spend, loyalty_tier, days_inactive)"]
        FS -.->|"Lookup user_id"| ENRICH
        ENRICH -->|"Feature Vector Mapping"| FEAT["Format Feature Vector"]
        FEAT -->|"RunInference (SklearnModelHandler)"| INF["Local Model Inference\n(Propensity Score & Category)"]
        WORKERS -.->|"Loaded in /workspace/marketing_model.pkl"| INF
    end

    subgraph Storage & Activation Sinks
        INF -->|"beam.io.WriteToBigQuery"| BQ[("BigQuery: output_dataset.predictions\n(Analytics & Looker BI)")]
        INF -->|"Filter: Propensity > 0.8"| ACT["High-Propensity Filter"]
        ACT -->|"pubsub.WriteStringsToPubSub"| PS_OUT["Pub/Sub Topic\n(Instant Coupon / Offer Trigger)"]
    end
```

---

## 3. Data Contracts & Schemas

### A. Streaming Input Event (Pub/Sub)
```json
{
  "event_id": "evt_884920",
  "user_id": "user_1042",
  "timestamp": "2026-08-26T08:30:00Z",
  "event_type": "view_item",
  "item_id": "prod_elec_441",
  "item_category": "electronics",
  "session_duration_sec": 240,
  "cart_value": 49.99
}
```

### B. Firestore Document Schema (`customer_profiles` collection)
* **Document ID**: `user_id` (e.g., `user_1042`)
* **Document Payload**:
```json
{
  "user_id": "user_1042",
  "loyalty_tier": "Gold",
  "total_lifetime_spend": 1250.75,
  "total_orders": 14,
  "days_since_last_order": 12,
  "preferred_category": "electronics",
  "email": "user_1042@example.com",
  "updated_at": "2026-08-20T10:00:00Z"
}
```

### C. BigQuery Output Schema (`output_dataset.predictions`)
```sql
CREATE TABLE `output_dataset.predictions` (
  event_id STRING NOT NULL,
  user_id STRING NOT NULL,
  event_timestamp TIMESTAMP,
  event_type STRING,
  item_id STRING,
  item_category STRING,
  session_duration_sec INT64,
  cart_value FLOAT64,
  loyalty_tier STRING,
  total_lifetime_spend FLOAT64,
  total_orders INT64,
  days_since_last_order INT64,
  preferred_category STRING,
  propensity_score FLOAT64,
  predicted_purchase INT64,
  recommended_offer STRING,
  processed_timestamp TIMESTAMP
);
```

---

## 4. Custom Container Architecture

The custom container ensures the pipeline is **100% self-contained**, eliminating all external runtime package downloads and guaranteeing fast, predictable worker startup during autoscaling.

### A. Dockerfile Specifications ([`Dockerfile`](file:///usr/local/google/home/ihr/projects/dataflow-solution-guides/pipelines/marketing_intelligence/Dockerfile))
* **Base Image**: `python:3.11-slim` (streamlined CPU base, avoiding heavy GPU/CUDA overhead).
* **Pre-Baked Model**: Bundles `marketing_model.pkl` directly into `/workspace/marketing_model.pkl`.
* **Pre-Installed Dependencies**: Installs all required packages at build time (`apache-beam[gcp]==2.75.0`, `google-cloud-firestore`, `scikit-learn`, `pandas`, `numpy`, `cachetools`).
* **Beam Boot Loader**: Copies the official Apache Beam SDK boot launcher from `apache/beam_python3.11_sdk:2.75.0` to `/opt/apache/beam/boot`.
* **Entrypoint**: Sets `ENTRYPOINT ["/opt/apache/beam/boot"]`.

```dockerfile
FROM python:3.11-slim
WORKDIR /workspace

RUN apt-get update -y && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.txt
COPY setup.py setup.py
COPY MANIFEST.in MANIFEST.in
COPY main.py main.py
COPY marketing_model.pkl marketing_model.pkl
COPY marketing_intelligence_pipeline marketing_intelligence_pipeline

RUN pip install --upgrade --no-cache-dir pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -e .

# Copy Apache Beam boot entrypoint binaries
COPY --from=apache/beam_python3.11_sdk:2.75.0 /opt/apache/beam /opt/apache/beam

ENTRYPOINT ["/opt/apache/beam/boot"]
```

### B. Build and Registry Integration
1. **Artifact Registry**: Terraform provisions `module.registry_docker` at `dataflow-containers`.
2. **Cloud Build**: [`cloudbuild.yaml`](file:///usr/local/google/home/ihr/projects/dataflow-solution-guides/pipelines/marketing_intelligence/cloudbuild.yaml) executes `docker build -t ${_TAG} .` and tags the image.
3. **Execution Script**: [`scripts/01_build_and_push_container.sh`](file:///usr/local/google/home/ihr/projects/dataflow-solution-guides/pipelines/marketing_intelligence/scripts/01_build_and_push_container.sh) triggers the build in the target GCP region.
4. **Dataflow Submission**: [`scripts/02_run_dataflow.sh`](file:///usr/local/google/home/ihr/projects/dataflow-solution-guides/pipelines/marketing_intelligence/scripts/02_run_dataflow.sh) passes `--sdk_container_image=$CONTAINER_URI`.

---

## 5. Phase-by-Phase Implementation Plan

### Phase 1: Infrastructure & IAM Alignment (`terraform/marketing_intelligence/`)
- [ ] **Fix Script Path**: In [`main.tf:L193`](file:///usr/local/google/home/ihr/projects/dataflow-solution-guides/terraform/marketing_intelligence/main.tf#L193), correct the target file path:
  `../../pipelines/marketing_intelligence/scripts/00_set_variables.sh` (change `market_intelligence` to `marketing_intelligence`).
- [ ] **Provision Firestore (Native Mode)**: Replace the Bigtable module `enrichment_table` in `main.tf` with a serverless Firestore database:
  ```hcl
  resource "google_firestore_database" "database" {
    project     = module.google_cloud_project.project_id
    name        = "(default)"
    location_id = var.region
    type        = "FIRESTORE_NATIVE"
  }
  ```
- [ ] **Grant IAM Roles**: Add `roles/datastore.user` and `roles/bigquery.dataEditor` to `module.dataflow_sa` in [`main.tf:L127-L134`](file:///usr/local/google/home/ihr/projects/dataflow-solution-guides/terraform/marketing_intelligence/main.tf#L127-L134).
- [ ] **Optimize Machine Type**: Update `locals.machine_type` from `g2-standard-4` (GPU) to cost-effective `e2-standard-4` or `n2-standard-4` (standard CPU workers).
- [ ] **Export Variables**: Ensure Firestore collection name (`customer_profiles`), BigQuery dataset (`output_dataset`), and table (`predictions`) are exported in `00_set_variables.sh`.

---

### Phase 2: Local Model & Mock Data Pipeline (`pipelines/marketing_intelligence/scripts/`)
- [ ] **Model Training Script (`scripts/01_train_model.py`)**:
  - Generate synthetic tabular dataset modeling realistic e-commerce purchase behaviors (features: `session_duration_sec`, `cart_value`, `total_lifetime_spend`, `total_orders`, `days_since_last_order`, `category_match`, `loyalty_tier`).
  - Target: `purchased` (0 or 1).
  - Train an ensemble classifier (`RandomForestClassifier` or `GradientBoostingClassifier`).
  - Serialize the trained model to `marketing_model.pkl` and save `training_data.csv` for inspection.
  - Output evaluation metrics (ROC-AUC > 0.85, Precision, Recall).
- [ ] **Firestore Population Script (`scripts/02_populate_firestore.py`)**:
  - Use `google-cloud-firestore` SDK with batch writes (`db.batch()`) to seed 1,000+ mock customer profiles into the `customer_profiles` collection.
- [ ] **Event Publisher Script (`scripts/03_publish_events.py`)**:
  - Continuously stream realistic mock interaction JSON messages into the Pub/Sub input topic with configurable rate (events/sec).

---

### Phase 3: Pipeline Core Logic Refactoring (`marketing_intelligence_pipeline/`)
- [ ] **Options Configuration ([`options.py`](file:///usr/local/google/home/ihr/projects/dataflow-solution-guides/pipelines/marketing_intelligence/marketing_intelligence_pipeline/options.py))**:
  - Add CLI arguments:
    - `--firestore_collection` (default: `customer_profiles`)
    - `--model_path` (default: `marketing_model.pkl` or GCS URI)
    - `--bq_dataset` (default: `output_dataset`)
    - `--bq_table` (default: `predictions`)
- [ ] **Firestore Enrichment Handler ([`pipeline.py`](file:///usr/local/google/home/ihr/projects/dataflow-solution-guides/pipelines/marketing_intelligence/marketing_intelligence_pipeline/pipeline.py))**:
  - Implement a `FirestoreEnrichmentHandler(EnrichmentSourceHandler)` with:
    - Thread-safe client initialization via `__enter__` / `__exit__`.
    - Integrated in-memory LRU cache (`cachetools.TTLCache`) for sub-millisecond lookups on active users.
  - Implement `custom_join(event, firestore_doc)` to merge streaming event data with historical user profile attributes (with graceful defaults for cold-start users).
- [ ] **Feature Transformation & RunInference**:
  - Implement `extract_features(enriched_dict)` to map enriched attributes into numeric vectors.
  - Implement inference using Apache Beam's native `RunInference(SklearnModelHandlerNumpy(model_uri=...))`.
  - Calculate `propensity_score`, `predicted_purchase`, and assign rule-based `recommended_offer` (e.g. "15% VIP Discount", "Free Shipping", "Catalog Browse").
- [ ] **Dual Sinks (BigQuery + Pub/Sub)**:
  - **BigQuery**: Write all scored and enriched records using `beam.io.gcp.bigquery.WriteToBigQuery` with `Method.STORAGE_WRITE_API` and `CREATE_IF_NEEDED`.
  - **Pub/Sub (Real-Time Activation)**: Filter records where `propensity_score >= 0.80` and publish actionable coupon payloads to the output Pub/Sub topic.

---

### Phase 4: Containerization, Build & Run Scripts
- [ ] **Update [`requirements.txt`](file:///usr/local/google/home/ihr/projects/dataflow-solution-guides/pipelines/marketing_intelligence/requirements.txt)**:
  - Pin matching versions:
    ```text
    apache-beam[gcp]==2.75.0
    google-cloud-firestore>=2.14.0
    scikit-learn>=1.3.0
    pandas>=2.0.0
    numpy>=1.24.0
    cachetools>=5.3.0
    ```
- [ ] **Update [`Dockerfile`](file:///usr/local/google/home/ihr/projects/dataflow-solution-guides/pipelines/marketing_intelligence/Dockerfile)**:
  - Streamline to CPU base (`python:3.11-slim`).
  - Bundle `marketing_model.pkl` and package dependencies.
  - Ensure strict version parity with Beam SDK `2.75.0`.
- [ ] **Clean Up [`cloudbuild.yaml`](file:///usr/local/google/home/ihr/projects/dataflow-solution-guides/pipelines/marketing_intelligence/cloudbuild.yaml)**:
  - Remove leftover Gemma comments and unnecessary substitutions.
- [ ] **Update [`scripts/02_run_dataflow.sh`](file:///usr/local/google/home/ihr/projects/dataflow-solution-guides/pipelines/marketing_intelligence/scripts/02_run_dataflow.sh)**:
  - Wire all new arguments (`--firestore_collection`, `--bq_dataset`, `--model_path`, etc.).
  - Pass `--sdk_container_image=$CONTAINER_URI`.
  - Remove unused GPU worker accelerator options.
- [ ] **Add Local Runner Script (`scripts/02_run_local.sh`)**:
  - Enable running locally with `DirectRunner` for rapid development and testing without GCP cloud deployment.

---

### Phase 5: Testing, Validation & Documentation
- [ ] **Unit Tests (`tests/`)**:
  - `test_firestore_enrichment.py`: Validate `custom_join` and handler caching behavior with complete and missing customer documents.
  - `test_feature_extraction.py`: Validate numerical feature vector format matching model expectations.
  - `test_pipeline.py`: Run local test pipeline with `TestPipeline` / `DirectRunner`.
- [ ] **Documentation Update**:
  - **[`README.md`](file:///usr/local/google/home/ihr/projects/dataflow-solution-guides/pipelines/marketing_intelligence/README.md)**: Replace obsolete Vertex AI AutoML instructions with end-to-end setup guide (Model Training -> Container Build -> Firestore Seeding -> Running Dataflow -> Ingesting Events -> Querying BigQuery).
  - **[`use_cases/Marketing_Intelligence.md`](file:///usr/local/google/home/ihr/projects/dataflow-solution-guides/use_cases/Marketing_Intelligence.md)**: Remove obsolete Gemma references and update architectural diagrams.

---

## 6. Verification & Acceptance Criteria

| Stage | Verification Action | Success Criteria |
| :--- | :--- | :--- |
| **Model & Mock Data** | Run `python scripts/01_train_model.py` | `marketing_model.pkl` and `training_data.csv` generated (ROC-AUC > 0.85) |
| **Container Build** | Run `./scripts/01_build_and_push_container.sh` | Container image built and pushed to Artifact Registry |
| **Firestore Seeding** | Run `python scripts/02_populate_firestore.py` | 1,000+ customer documents visible in Firestore Console |
| **Local Pipeline Test** | Run `./scripts/02_run_local.sh` (DirectRunner) | Processes test events without errors, logs enriched predictions |
| **Dataflow Submission** | Run `./scripts/02_run_dataflow.sh` | Job launches in GCP Dataflow console with `JOB_STATE_RUNNING` |
| **Live Ingestion** | Run `python scripts/03_publish_events.py` | Streaming input watermark advances; elements processed in Dataflow UI |
| **BigQuery Verification**| `SELECT COUNT(*) FROM output_dataset.predictions` | Rows stream continuously with valid feature values and propensity scores |
| **Activation Verification**| Pull messages from output Pub/Sub subscription | High-propensity discount trigger messages received |

---

## 7. Security & Best Practices Compliance

* **Private IP Only**: Workers launch with `--no_use_public_ip` and communicate via VPC subnetwork with Private Google Access.
* **IAM Least Privilege**: Uses custom Service Account with `roles/dataflow.worker`, `roles/datastore.user`, `roles/bigquery.dataEditor`, `roles/pubsub.editor`, and `roles/storage.objectAdmin`.
* **Zero Standing Infrastructure Cost**: Serverless Firestore and BigQuery incur $0 when no pipelines or queries are active.
* **Version Parity**: Python Beam SDK in `requirements.txt` and base container in `Dockerfile` strictly pinned to `2.75.0`.
* **Streaming Engine**: Uses Dataflow Streaming Engine and Storage Write API for optimal BigQuery write throughput.
