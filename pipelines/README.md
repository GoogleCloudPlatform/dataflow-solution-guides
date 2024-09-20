## Pipelines

This directory contains sample pipelines for the solution guides. These
pipelines demonstrate how
to use Dataflow to process data in streaming for each one of the use cases.

The pipelines are written in Python, Java (coming soon), and Go (coming soon).
Each pipeline
includes a README file that provides a detailed description of the pipeline,
including its purpose,
inputs, outputs, and configuration options.

## Getting Started

To get started with the pipelines, follow these steps:

1. Choose the pipeline that best suits your needs.
2. Read the README file for the pipeline to understand its purpose, inputs,
   outputs,
   and configuration options. MAke sure that you have the necessary
   infrastructure ready, using the
   corresponding deployment scripts in the `terraform` directory.
3. Modify the pipeline code to meet your specific requirements.
4. Run the pipeline using the provided scripts.

## Pipelines

These are the pipelines included in this directory

|        Use case        | Programming language |                          Location                           |
| :--------------------: | :------------------: | :---------------------------------------------------------: |
|       ML & GenAI       |        Python        |               [ml_ai_python](./ml_ai_python)                |
|   ETL & Integration    |         Java         |       [etl_integration_java](./etl_integration_java)        |
| Customer Data Platform |        Python        |                        [cdp](./cdp)                         |
|   Anomaly detection    |        Python        |          [anomaly_detection](./anomaly_detection)           |
| Marketing Intelligence |        Python        |     [marketing_intelligence](./marketing_intelligence/)     |
|    Log replication     |  Dataflow template   |     [log_replication_splunk](./log_replication_splunk/)     |
| Clickstream Analytics  |         Java         | [clickstream_analytics_java](./clickstream_analytics_java/) |
