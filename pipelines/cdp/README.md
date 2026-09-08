# Customer Data Platform Streaming Pipeline (Python)

This production-grade streaming pipeline demonstrates how to use Google Cloud Dataflow and Apache Beam to implement an end-to-end Customer Data Platform (CDP). It ingests multi-stream customer interactions from Cloud Pub/Sub (`cdp-transactions` and `cdp-coupon-redemption`), reconstructs individual customer shopping sessions using dynamic **`Sessions(gap_size)` windowing**, unifies transaction baskets with redeemed coupons, aggregates **Customer 360 session profiles**, routes malformed inputs to a **Dead-Letter Queue (DLQ)**, and streams output records to **BigQuery** using the high-performance **Storage Write API**.

This pipeline is part of the [Dataflow Customer Data Platform solution guide](../../use_cases/CDP.md).

## Architecture

The real-time sessionization architecture operates as follows:

1. **Multi-Stream Ingestion**: Reads streaming events from Pub/Sub subscriptions (`cdp-transactions-sub` and `cdp-coupon-redemption-sub`) with automatic topic fallback.
2. **Safe Deserialization & Dead-Letter Routing**: `ParseRecordDoFn` validates JSON payloads and verifies mandatory keys (`household_key`, `transaction_id`). Invalid payloads or schema violations are tagged as `errors` and routed to the dead-letter sink.
3. **Event-Time Timestamping & Sessionization**: Records are timestamped based on `event_timestamp` and windowed into dynamic session windows via `Sessions(gap_size)` (default 15 minutes). Watermark-based late data triggers and accumulating modes ensure late events are incorporated into sessions safely.
4. **Customer 360 Session Aggregation**: `ProcessCustomerSessionDoFn` groups interactions by `household_key`, emitting:
   - **Granular Unified Items** (main output): Each purchased item unified with its session ID, price, quantity, store, and applied coupon/discount.
   - **Customer 360 Session Profiles** (tagged output `sessions`): Rollup of total session spend, item count, discounts, distinct products, stores visited, and marketing campaigns engaged.
5. **Storage Write API Dual Sinks**: High-throughput direct ingestion into BigQuery tables with deadletter error routing.

## BigQuery Data Schemas

The pipeline outputs into three BigQuery tables defined in `schema/`:

- **`cdp_dataset.unified_customer_data`** (`schema/unified_table.json`): Granular item-level purchases enriched with session ID, store, retail discount, coupon UPC, and campaign.
- **`cdp_dataset.customer_sessions`** (`schema/customer_sessions.json`): Aggregated Customer 360 profile per shopping session (session duration, total spend, total items, coupon count, campaigns, visited stores).
- **`cdp_dataset.cdp_deadletter`** (`schema/deadletter_table.json`): Error records, including original payload, error reason, source topic, and timestamp.

## How to Launch the Pipeline

All launch scripts are located in the `scripts/` directory.

### 1. Load Environment Variables
The environment configuration file `scripts/00_set_environment.sh` is generated automatically when deploying the Terraform infrastructure in `terraform/cdp/`:

```bash
source scripts/00_set_environment.sh
```

### 2. Run Locally with DirectRunner (Optional for Development)
To test pipeline transforms locally with DirectRunner:

```bash
./scripts/02_run_local.sh
```

### 3. Build and Publish Custom Container
Build and push the custom Dataflow worker container to Artifact Registry using Cloud Build:

```bash
./scripts/01_build_and_push_container.sh
```

### 4. Launch Dataflow Streaming Pipeline
Submit the streaming pipeline job to Google Cloud Dataflow:

```bash
./scripts/02_run_dataflow.sh
```

## Automated Tests & Code Quality

Execute unit, DoFn, and end-to-end pipeline transform tests with `pytest`:

```bash
pytest tests/ -v
```

Run code formatting and PyLint checks against Google Python style:

```bash
yapf -i -r --style yapf cdp_pipeline simulator scripts tests main.py
pylint --rcfile ../pylintrc cdp_pipeline simulator scripts/03_publish_events.py tests main.py
```

## Input Data Simulation

To publish streaming transactions and session journeys into Pub/Sub:

```bash
# Continuous streaming mode (1 session journey per second)
python3 ./scripts/03_publish_events.py --continuous --interval=1.0

# Batch burst mode (100 sessions)
python3 ./scripts/03_publish_events.py --count=100

# Continuous mode with injected error payloads to test the DLQ
python3 ./scripts/03_publish_events.py --continuous --inject_errors
```

## Output Data Verification

Verify output records via `bq`:

```bash
# Inspect unified basket items
bq query --use_legacy_sql=false \
  "SELECT session_id, household_key, transaction_id, product_id, sales_value, coupon_upc \
   FROM \`${PROJECT}.cdp_dataset.unified_customer_data\` LIMIT 10"

# Inspect Customer 360 session rollups
bq query --use_legacy_sql=false \
  "SELECT session_id, household_key, total_spend, total_items_purchased, coupons_redeemed_count \
   FROM \`${PROJECT}.cdp_dataset.customer_sessions\` LIMIT 10"

# Inspect Dead-Letter Queue
bq query --use_legacy_sql=false \
  "SELECT source, error_message, timestamp \
   FROM \`${PROJECT}.cdp_dataset.cdp_deadletter\` LIMIT 10"
```