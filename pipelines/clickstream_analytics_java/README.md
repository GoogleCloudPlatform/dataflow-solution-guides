## ClickStream Dataflow Code

This Dataflow pipeline processes clickstream analytics data, using session windowing to group events into user sessions, and then writes the aggregated session data to BigQuery. The pipeline is written in Java and uses Pub/Sub as the input source.

**This pipeline is still under development**.

This pipeline is part of the [Dataflow Clickstream analytics solution guide](../../use_cases/Clickstream_Analytics.md).

## Pipeline Architecture

1. **Pub/Sub Subscription:** The pipeline reads clickstream events from a Pub/Sub subscription.

2. **Dataflow Pipeline:**

- **Event Parsing:** Incoming Pub/Sub messages are parsed into structured clickstream event objects.
- **Bigtable Enrichment (TODO):** Enrich session data with additional information from Bigtable (code implementation pending).
- **Session Windowing (TODO):** Events are grouped into sessions using a session windowing strategy (e.g., 30-minute inactivity gap).
- **BigQuery Write:** Aggregated session data is written to BigQuery tables.
- **Dead-letter Queue:** Failed records are written to a BigQuery dead-letter table for further analysis and error handling.

## TODO

The Bigtable enrichment and the session windowing analytics steps are not implemented at this moment.

## Pipeline Code

- To build the project, run `./gradlew build`

## How to launch the pipelines

All the scripts are located in the `scripts` directory and prepared to be launched from the top
sources directory.

The Terraform code generates a file with all the necessary variables in the location `./scripts/00_set_variables.sh`.

Run the following command to apply that configuration:

```sh
source scripts/00_set_variables.sh
```

Then run the analytics pipeline. This pipeline will take data from the input
topic, and will write it to BigQuery, enriching with metadata available in Bigtable, and applying session analytics.

```sh
./scripts/01_launch_pipeline.sh
```
