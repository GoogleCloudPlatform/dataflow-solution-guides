## ClickStream Dataflow Code
This Dataflow pipeline processes clickstream analytics data, using session windowing to group events into user sessions, and then writes the aggregated session data to BigQuery. The pipeline is written in Java and uses Pub/Sub as the input source.

## Pipeline Architecture

1. **Pub/Sub Subscription:** The pipeline reads clickstream events from a Pub/Sub subscription.

2. **Dataflow Pipeline:**
   * **Event Parsing:** Incoming Pub/Sub messages are parsed into structured clickstream event objects.
   * **Bigtable Enrichment (TODO):** Enrich session data with additional information from Bigtable (code implementation pending).
   * **Session Windowing:** Events are grouped into sessions using a session windowing strategy (e.g., 30-minute inactivity gap).
   * **BigQuery Write:** Aggregated session data is written to BigQuery tables.
   * **Dead-letter Queue:** Failed records are written to a BigQuery dead-letter table for further analysis and error handling.

## Prerequisites

* **Google Cloud Project:** You need a Google Cloud project with the following APIs enabled:
    * Dataflow
    * Pub/Sub
    * BigQuery
* **Pub/Sub Topic & Subscription:** A Pub/Sub topic and subscription should be set up to receive clickstream events.
* **Bigtable Instance & Table (Optional):**  If you're implementing enrichment, set up a Bigtable instance and table.
* **BigQuery Dataset & Tables:** Create a BigQuery dataset and tables to store the aggregated session data.
* **Gradle:** Ensure you have Gradle installed to build the project.

## TODO
BigTable Enrichment code is not implemented at this moment.

## Pipeline Code
* To build the project, `gradle build`

## Deploy Dataflow job using Gradle
* `gradle clean execute -DmainClass=clickstream_analytics_java.ClickstreamPubSubToBq -Dexec.args="--runner=DataflowRunner --gcpTempLocation=gs://{GCS_BUCKET_PATH} --project_id={PROJECT_ID} --bq_dataset={BQ_DATASET} --bq_table={BQ_TABLE} --pubsub_subscription=projects/{PROJECT_ID}/subscriptions/{PUBSUB_SUB_NAME} --bt_instance={BIGTABLE_INSTANCE} --bt_table={BIGTABLE_TABLE} --bq_deadletter_tbl=BQ_DEADLETTER_TABLE"`

