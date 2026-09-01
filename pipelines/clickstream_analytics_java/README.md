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
│   ├── ClickstreamPubSubToBq.java                    # Main pipeline DAG & options interface
│   ├── BigTableEnrichment.java                       # Cloud Bigtable lookup DoFn
│   ├── SessionAnalytics.java                         # Session windowing & aggregation transform
│   ├── JsonToEvents.java                             # JSON parser & validator transform
│   ├── DeadletterConverter.java                      # Dead-letter TableRow converter
│   ├── Metrics.java                                  # Pipeline counters & metrics
│   └── data/
│       └── ClickstreamObjects.java                   # AutoValue + Beam Schema data classes
├── src/main/resources/
│   └── streaming_source_deadletter_table_schema.json # DLQ BigQuery table schema
├── src/test/java/.../clickstream_analytics/          # Unit tests (100% pass rate)
│   ├── JsonToEventsTest.java
│   ├── BigTableEnrichmentTest.java
│   ├── SessionAnalyticsTest.java
│   └── DeadletterConverterTest.java
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

### Build & Run Unit Tests
```bash
./gradlew test --info
```

### Code Formatting (Spotless)
Verify and apply Google Java Style:
```bash
./gradlew spotlessCheck
./gradlew spotlessApply
```

### Full Build
```bash
./gradlew build
```

---

## Deployment & Running

### Step 1: Provision Infrastructure with Terraform
Navigate to `terraform/clickstream_analytics/` and apply the configuration:
```bash
cd ../../terraform/clickstream_analytics
terraform init
terraform apply
```
This provisions Pub/Sub topics/subscriptions, Bigtable instance and `wikipedia` table, BigQuery dataset with `wikipedia`, `sessions`, and `deadletter` tables, IAM roles, and automatically generates `00_set_variables.sh`.

### Step 2: Set Environment Variables
Return to the pipeline directory and source the environment variables generated by Terraform:
```bash
cd ../../pipelines/clickstream_analytics_java
source scripts/00_set_variables.sh
```

### Step 3: Populate Cloud Bigtable with Reference Data
Install script dependencies and seed the Bigtable table with sample article metadata:
```bash
pip install -r scripts/requirements.txt
python scripts/populate_bigtable.py
```

### Step 4: Launch the Pipeline on Dataflow
Execute the launch script:
```bash
./scripts/01_launch_pipeline.sh
```
The script runs Dataflow with worker isolation (`--usePublicIps=false`), dedicated service accounts, and configurable session gap duration (`--sessionGapDurationMinutes=30`).

### Step 5: Ingest Test Clickstream Events
Stream synthetic Wikipedia clickstream events into Pub/Sub:
```bash
python scripts/generate_clickstream_events.py --num_events=1000 --rate=50 --inject_errors
```

### Step 6: Verify in BigQuery
Inspect data in BigQuery:
```sql
-- Query enriched events
SELECT curr, category, enriched_data, count(*) as views
FROM `<project_id>.<dataset>.wikipedia`
GROUP BY curr, category, enriched_data
ORDER BY views DESC;

-- Query user sessions (updated via Storage Write API UPSERT)
SELECT session_id, user_id, duration_seconds, event_count, first_page, last_page
FROM `<project_id>.<dataset>.sessions`
ORDER BY duration_seconds DESC;

-- Query dead-letter errors
SELECT timestamp, errorMessage, payloadString
FROM `<project_id>.<dataset>.deadletter`;
```
