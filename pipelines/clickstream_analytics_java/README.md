# Clickstream Analytics Dataflow Pipeline (Java)

This Apache Beam pipeline processes real-time clickstream event streams on Google Cloud Dataflow. It parses raw JSON events into typed AutoValue data models, enriches each event with article metadata from Cloud Bigtable, calculates user session analytics using dynamic session windowing, and writes the results to dual BigQuery tables with Storage Write API UPSERTs and a unified dead-letter queue (DLQ).

This pipeline is part of the [Dataflow Clickstream Analytics Solution Guide](../../use_cases/Clickstream_Analytics.md).

---

## Pipeline Architecture

```
                                      ┌──> BigQuery: Enriched Raw Events (`wikipedia`)
                                      │    (Storage Write API - Append)
Pub/Sub ──> JsonToEvents ──> BigTableEnrichment
                │                     │
                │ (Parse Errors)      └──> SessionAnalytics ──> BigQuery: User Sessions (`sessions`)
                │                          (Session Windowing)  (Storage Write API - UPSERT on session_id)
                │                                    │
                │                                    │ (Failed Inserts)
                └──────────────────┬─────────────────┘
                                   ▼
                      BigQuery: Deadletter Table (`deadletter`)
```

### Key Stages

1. **Pub/Sub Ingestion**: Reads JSON clickstream events from a dedicated Pub/Sub subscription.
2. **Schema & AutoValue Parsing (`JsonToEvents`)**:
   - Parses raw JSON directly into typed `ClickstreamEvent` records using Google AutoValue, AutoBuilder, and Apache Beam Schemas (`@DefaultSchema(AutoValueSchema.class)`).
   - Validates payload size (max 10MB) and syntax. Malformed or oversized records are immediately routed to a dead-letter failure tag.
3. **Cloud Bigtable Enrichment (`BigTableEnrichment`)**:
   - Uses the modern Google Cloud Bigtable v2 Java Client (`bigtableDataClient.readRows(...)`).
   - Looks up article metadata based on the current page (`curr` attribute) from the Bigtable `wikipedia` table (`cf` column family).
   - Enriches events with `category` and `enriched_data` attributes. Supports a configurable pass-through mode if Bigtable enrichment is disabled or on lookup misses.
4. **Dual BigQuery Sinks**:
   - **Enriched Raw Events Stream (`wikipedia` table)**:
     - Writes full enriched clickstream events to BigQuery using the Storage Write API (`STORAGE_API_AT_LEAST_ONCE`).
   - **User Session Analytics Stream (`sessions` table)**:
     - Uses Apache Beam session windows (`Sessions.withGapDuration(...)`, default 30 minutes, configurable via `--sessionGapDurationMinutes`).
     - Groups events by `user_id` and aggregates session metrics: `session_id`, `duration_seconds`, `event_count`, `first_page`, `last_page`, `unique_pages_count`, and `total_views`.
     - Writes session summaries using BigQuery Storage Write API **UPSERTs** with primary key `session_id` (`<user_id>_<window_start_epoch_millis>`), ensuring that interim and late session updates merge idempotently without duplicates.
5. **Unified Dead-Letter Queue (`DeadletterConverter`)**:
   - Combines parse/validation errors and BigQuery Storage Write API insert failures (`getFailedStorageApiInserts()`).
   - Formats failed records to match `streaming_source_deadletter_table_schema.json` (`timestamp`, `payloadString`, `payloadBytes`, `attributes`, `errorMessage`, `stacktrace`) and writes them to the `deadletter` BigQuery table.

---

## Data Models

All data models are defined in [`ClickstreamObjects.java`](src/main/java/com/google/cloud/dataflow/solutions/clickstream_analytics/data/ClickstreamObjects.java):

- **`ClickstreamEvent`**: Represents individual click events with user ID, timestamp, previous page, current page, link type, view count `n`, category, and enriched metadata.
- **`UserSession`**: Represents aggregated user browsing sessions with unique session ID, start/end timestamps, session duration, event counts, first/last visited pages, unique pages, and total views.
- **`ParsingError`**: Represents malformed or oversized incoming event payloads for dead-letter processing.

---

## Project Structure

```
pipelines/clickstream_analytics_java/
├── build.gradle                                      # Gradle configuration (Beam 2.76, Java 25, AutoValue)
├── src/main/java/.../clickstream_analytics/
│   ├── ClickstreamPubSubToBq.java                    # Main pipeline DAG orchestrator
│   ├── options/
│   │   └── ClickstreamProcessingOptions.java         # Pipeline options interface
│   ├── data/
│   │   ├── ClickstreamObjects.java                   # AutoValue + Beam Schema data classes
│   │   └── SchemaUtils.java                          # Dead-letter BigQuery JSON schema loader
│   ├── extract/
│   │   └── PubSub.java                               # PubSub streaming read PTransform
│   ├── transform/
│   │   ├── JsonToEvents.java                         # JSON parser & validator PTransform
│   │   ├── BigTableEnrichment.java                   # Cloud Bigtable lookup enrichment PTransform
│   │   ├── SessionAnalytics.java                     # Session windowing & aggregation PTransform
│   │   └── DeadletterConverter.java                  # Dead-letter TableRow converter PTransforms
│   └── load/
│       └── BigQuery.java                             # BigQuery events, sessions, and deadletter sinks
├── src/main/resources/
│   └── streaming_source_deadletter_table_schema.json # DLQ BigQuery table schema
├── src/test/java/.../clickstream_analytics/          # Unit tests (100% pass rate)
│   ├── extract/
│   │   └── PubSubTest.java
│   ├── transform/
│   │   ├── JsonToEventsTest.java
│   │   ├── BigTableEnrichmentTest.java
│   │   ├── SessionAnalyticsTest.java
│   │   └── DeadletterConverterTest.java
│   └── load/
│       └── BigQueryTest.java
└── scripts/
    ├── 01_launch_pipeline.sh                         # Dataflow submission wrapper
    ├── populate_bigtable.py                          # Seeds Cloud Bigtable with Wikipedia metadata
    ├── generate_clickstream_events.py                # Publishes synthetic events & tests DLQ
    └── requirements.txt                              # Python generator dependencies
```

---

## Building and Testing

### Prerequisites
- OpenJDK 25
- Gradle (use the included `./gradlew` wrapper)
- Python 3.10+ (for reference data seeding and event generator scripts)
- Google Cloud SDK (`gcloud` and `bq` CLIs)

### Unit Tests
The unit test suite validates all Beam transforms, error routing, Bigtable client interactions, and session window logic in-memory using Beam's `TestPipeline`:

```bash
./gradlew test --info
```

| Test Class | Scope & Verification |
| :--- | :--- |
| [`JsonToEventsTest`](src/test/java/com/google/cloud/dataflow/solutions/clickstream_analytics/JsonToEventsTest.java) | Validates JSON parsing into typed `ClickstreamEvent` records, ensures oversized payloads (>10MB) and malformed syntax route to the dead-letter failure tag. |
| [`BigTableEnrichmentTest`](src/test/java/com/google/cloud/dataflow/solutions/clickstream_analytics/BigTableEnrichmentTest.java) | Uses Mockito to verify Bigtable row lookups, column cell extraction into `category` and `enriched_data`, and pass-through fallback on cache misses or when enrichment is disabled. |
| [`SessionAnalyticsTest`](src/test/java/com/google/cloud/dataflow/solutions/clickstream_analytics/SessionAnalyticsTest.java) | Validates user session windowing (`Sessions.withGapDuration`), chronological sorting for `first_page` and `last_page`, duration computation, and unique page counting. |
| [`DeadletterConverterTest`](src/test/java/com/google/cloud/dataflow/solutions/clickstream_analytics/DeadletterConverterTest.java) | Tests conversion of `ParsingError` and BigQuery Storage Write API insertion failures into standard dead-letter `TableRow`s matching the DLQ schema. |

### Code Formatting (Spotless)
This repository enforces Google Java Style. Validate and format code using Spotless:

```bash
# Check for style violations
./gradlew spotlessCheck

# Automatically apply Google Java Style formatting
./gradlew spotlessApply
```

### Full Build
Compile classes, run annotation processors (AutoValue / AutoBuilder), and build distribution archives:

```bash
./gradlew build
```

### Local Execution (`DirectRunner`)
You can run the pipeline locally with Apache Beam's `DirectRunner` against live GCP resources (or local emulators):

```bash
./gradlew run -Pargs="--runner=DirectRunner \
  --subscription=projects/<PROJECT_ID>/subscriptions/<SUBSCRIPTION_NAME> \
  --bqProjectId=<PROJECT_ID> \
  --bqDataset=<DATASET_NAME> \
  --bqTable=wikipedia \
  --bqSessionsTable=sessions \
  --outputDeadletterTable=deadletter \
  --btInstance=<BIGTABLE_INSTANCE_ID> \
  --btTable=wikipedia \
  --sessionGapDurationMinutes=2 \
  --enableBigtableEnrichment=true"
```

---

## End-to-End Cloud Deployment & Testing Runbook

Follow this step-by-step procedure to deploy the infrastructure, run the Dataflow pipeline on Google Cloud, simulate realistic traffic with error injection, and verify outputs across all three BigQuery tables.

### Step 1: Provision Infrastructure with Terraform
Navigate to `terraform/clickstream_analytics/`:

```bash
cd terraform/clickstream_analytics
terraform init
terraform apply
```

This provisions:
- **Cloud Bigtable**: Instance `clickstream-analytics` with table `wikipedia` (column family `cf`).
- **Pub/Sub**: Topic `dataflow-clickstream-input` and subscription `dataflow-clickstream-input-sub`.
- **BigQuery**: Dataset `clickstream_analytics` containing:
  - `wikipedia`: Partitioned/clustered table for raw enriched clickstream events.
  - `sessions`: Table with primary key constraint `session_id` required for Storage Write API UPSERTs.
  - `deadletter`: Table matching `streaming_source_deadletter_table_schema.json`.
- **IAM & Networking**: Custom worker service account `clickstream-dataflow-sa` with least-privilege roles.
- **Environment Script**: Generates `pipelines/clickstream_analytics_java/scripts/00_set_variables.sh`.

> [!NOTE]
> If deploying into a **Shared VPC**, set `shared_vpc_project_id` and `subnetwork` in `terraform.tfvars` or pass them via `-var`. Ensure the worker service account is granted `roles/compute.networkUser` on the host project subnet.

### Step 2: Source Environment Variables
Navigate to the pipeline directory and source the variables generated by Terraform:

```bash
cd ../../pipelines/clickstream_analytics_java
source scripts/00_set_variables.sh
```

Verify the active variables:
```bash
echo "Project:   $PROJECT_ID"
echo "Region:    $REGION"
echo "Subnet:    $SUBNETWORK"
echo "Bigtable:  $BT_INSTANCE / $BT_TABLE"
echo "BigQuery:  $BQ_DATASET (Raw: $BQ_TABLE, Sessions: $BQ_SESSIONS_TABLE, DLQ: $BQ_DEADLETTER_TABLE)"
```

### Step 3: Populate Cloud Bigtable with Reference Data
Create a Python virtual environment, install dependencies, and seed the Bigtable table with sample Wikipedia article metadata:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r scripts/requirements.txt

python scripts/populate_bigtable.py
```

Expected output:
```
Connected to Bigtable instance clickstream-analytics, table wikipedia
Populating sample Wikipedia article metadata...
  - Written: Cloud_Dataflow (Technology / Distributed stream processing service...)
  - Written: Cloud_Bigtable (Technology / Managed NoSQL database...)
  ...
Successfully populated 10 articles into Bigtable!
```

### Step 4: Launch the Pipeline on Dataflow
Submit the streaming pipeline to Dataflow using the launch script:

```bash
./scripts/01_launch_pipeline.sh
```

The launch script configures:
- Streaming Engine enabled (`--enableStreamingEngine`, `--streaming`)
- Private IPs only (`--usePublicIps=false`)
- Dedicated worker service account (`--serviceAccount=$SERVICE_ACCOUNT`)
- Worker machine type (`--workerMachineType=$WORKER_TYPE` if defined)
- Shared VPC subnetwork (if defined)
- Storage Write API UPSERTs on the `sessions` table
- Session inactivity gap duration (`--sessionGapDurationMinutes=30`)

Retrieve and monitor the job status:
```bash
# List active jobs
gcloud dataflow jobs list --status=active --region=$REGION

# Describe job state
gcloud dataflow jobs describe <JOB_ID> --region=$REGION --format="value(currentState)"
```

Wait until the job reaches `JOB_STATE_RUNNING` and workers have provisioned.

### Step 5: Ingest Test Events & Inject Errors
Generate synthetic user browsing sessions based on a Wikipedia page transition graph. Use the `--inject_errors` flag to test Deadletter routing:

```bash
python scripts/generate_clickstream_events.py \
  --project_id=$PROJECT_ID \
  --topic_id=$PUBSUB_TOPIC \
  --num_events=300 \
  --rate=10 \
  --inject_errors
```

This simulates:
- 15 distinct user browsing sessions traversing linked articles.
- Injected error payloads (unparseable JSON syntax, invalid field types, corrupted text) to verify dead-letter isolation without dropping valid traffic.

### Step 6: Verify Outputs in BigQuery

Execute the following queries using `bq query` or the BigQuery Console:

#### A. Verify Enriched Raw Events (`wikipedia` table)
Verify that events were ingested, parsed, and enriched with Bigtable metadata:

```sql
SELECT
  count(*) AS total_raw_events,
  countif(enriched_data IS NOT NULL) AS enriched_with_bigtable_count,
  countif(category IS NOT NULL) AS categorized_count
FROM `<PROJECT_ID>.<DATASET>.wikipedia`;
```

Inspect the most popular articles and their enriched categories:
```sql
SELECT
  curr AS article_page,
  category,
  count(*) AS view_count
FROM `<PROJECT_ID>.<DATASET>.wikipedia`
GROUP BY curr, category
ORDER BY view_count DESC
LIMIT 10;
```

#### B. Verify Dead-Letter Queue Routing (`deadletter` table)
Verify that the injected corrupted events were captured by the DLQ with diagnostics:

```sql
SELECT
  timestamp,
  substr(errorMessage, 1, 40) AS error_type,
  substr(payloadString, 1, 60) AS raw_payload_snippet
FROM `<PROJECT_ID>.<DATASET>.deadletter`
ORDER BY timestamp DESC
LIMIT 10;
```

Expected result: Captures `JsonParseError` entries along with the original payload strings and stack traces.

### Step 7: Flush & Verify Session Aggregations (`sessions` table)
In streaming pipelines, session windows trigger when a session closes after an inactivity gap (e.g., 30 minutes of no new events from that user). To immediately close all active session windows and verify aggregations:

1. **Drain the Dataflow job**:
   ```bash
   gcloud dataflow jobs drain <JOB_ID> --region=$REGION
   ```
   Draining stops ingesting new messages from Pub/Sub, advances the pipeline watermark to infinity, triggers all open session windows, and shuts down workers cleanly.

2. **Wait for drain completion**:
   ```bash
   gcloud dataflow jobs describe <JOB_ID> --region=$REGION --format="value(currentState)"
   # Wait until it outputs: JOB_STATE_DRAINED
   ```

3. **Query User Sessions**:
   ```sql
   SELECT
     session_id,
     user_id,
     duration_seconds,
     event_count,
     first_page,
     last_page,
     unique_pages_count,
     total_views
   FROM `<PROJECT_ID>.<DATASET>.sessions`
   ORDER BY event_count DESC
   LIMIT 10;
   ```

   **Verification Points**:
   - `session_id`: Unique identifier formatted as `<user_id>_<start_time_epoch_ms>`.
   - `duration_seconds`: Time elapsed between user's first and last action in the session.
   - `first_page` & `last_page`: Correctly reflects entry and exit navigation order.
   - `unique_pages_count` & `total_views`: Accurately aggregated event statistics.
   - **Idempotency**: Late or interim updates to existing sessions merge via BigQuery Storage Write API UPSERTs on `session_id` without creating duplicate session rows.

---

## Clean Up Resources

Once verification is complete, clean up all provisioned GCP resources:

```bash
# 1. Ensure Dataflow jobs are drained or cancelled
gcloud dataflow jobs list --status=active --region=$REGION

# 2. Destroy Terraform resources
cd ../../terraform/clickstream_analytics
terraform destroy
```
