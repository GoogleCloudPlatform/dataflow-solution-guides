# Dataflow Solution Guides

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

Welcome to the Dataflow Solution Guides!

The Dataflow Solution Guides offer full end-to-end deployment for the most
common streaming solutions to run
on [Dataflow](https://cloud.google.com/dataflow/).

This repository contains the following assets for each guide:

- Full Terraform code to spawn all the necessary Google Cloud infrastructure
- Pipelines code in Python, Java and Go (coming soon) for a
  sample pipeline for each use case

## Solution guides

This the list of solution guides available at this moment:

|                              Guide                              |                                              Description                                               |    Development status     |
| :-------------------------------------------------------------: | :----------------------------------------------------------------------------------------------------: | :-----------------------: |
|  [GenAI & Machine Learning Inference](./use_cases/GenAI_ML.md)  |                        Real-time inference with local GenAI models, using a GPU                        | Ready :white_check_mark:  |
|       [ETL / Integration](./use_cases/ETL_integration.md)       | Real-time change data capture from a Spanner database to BigQuery                                      | Ready :white_check_mark:  |
|  [Log Replication & Analytics](./use_cases/Log_replication.md)  |                                Real-time log replication into Splunk                                   |      Beta :factory:       |
| [Marketing Intelligence](./use_cases/Marketing_Intelligence.md) |               Real-time marketing intelligence, using an AutoML model deployed in Vertex               |      Beta :factory:       |
|  [Clickstream Analytics](./use_cases/Clickstream_Analytics.md)  |               Real-time clickstream analytics with Bigtable enrichment / data hydration                | Work in progress :hammer: |
| [IoT Analytics](./use_cases/IoT_Analytics.md)                   |  Real-time Internet of Things (IoT) analytics with Bigtable enrichment & models deployed in Vertex AI  | Work in progress :hammer: |
|      [Anomaly Detection](./use_cases/Anomaly_Detection.md)      |Real-time detection of anomalies in a stream of data leveraging GenAI with models deployed in Vertex AI |      Beta :factory:       |
|          [Customer Data Platform](./use_cases/CDP.md)           |         Real-time customer data platform that unifies a customer view from different sources.          |      Beta :factory:       |
|      [Gaming Analytics](./use_cases/gaming_analytics.md)        |          Real-time analyis of gaming data to enhance live gameplay & offer targeting                   |      Beta :factory:       |



## Repository structure

- `terraform`: This directory contains the Terraform code for deploying the
  necessary Google Cloud
  infrastructure for each use case.
- `pipelines`: This directory contains the Python, Java, and Go code for the
  sample pipelines.
- `use_cases`: This directory contains the documentation of each use case

## Getting help

- GitHub Issues: Report any issues or ask questions on the GitHub repository.
  - https://github.com/GoogleCloudPlatform/dataflow-solution-guides/issues
- Stack Overflow: Search for existing solutions or ask questions on Stack
  Overflow using the `google-cloud-dataflow` tag:
  - https://stackoverflow.com/questions/tagged/google-cloud-dataflow

## Contributing

Your contributions to this repository are welcome.

- Fork and Pull Request: Fork the repository and submit a pull request with your
  changes.
- Follow the Contribution Guidelines: Please follow the contribution guidelines
  outlined in the
  [CONTRIBUTING.md](CONTRIBUTING.md) file.

## Disclaimer

This is not an officially supported Google product. The code in this repository
is for demonstrative purposes only.
