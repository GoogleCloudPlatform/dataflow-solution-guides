# Dataflow Solution Guides

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

Welcome to the Dataflow Solution Guides!

The Dataflow Solution Guides offer full end-to-end deployment for the most
common streaming solutions to run on Dataflow. This repository contains the
following assets for each guide:

* Full Terraform code to spawn all the necessary Google Cloud infrastructure
* Pipelines code in Python, Java and Go (coming soon) for a
  sample pipeline for each use case

## Solution guides

This the list of solution guides available at this moment:

|                             Guide                             |                                              Description                                               |    Development status    |
|:-------------------------------------------------------------:|:------------------------------------------------------------------------------------------------------:|:------------------------:|
| [GenAI & machine learning inference](./use_cases/GenAI_ML.md) |                        Real-time inference with local GenAI models, using a GPU                        | Ready :white_check_mark: |
|      [ETL / Integration](./use_cases/ETL_integration.md)      | Replicate a Spanner database into BigQuery using a change-data-capture streaming pipeline in Dataflow. | Ready :white_check_mark: | 
|         [Customer Data Platform](./use_cases/CDP.md)          |         Real time customer data platform that unifies a customer view from different sources.          |      Beta :factory:      |
|     [Anomaly detection](./use_cases/Anomaly_Detection.md)     |     Detection of anomalies in a stream of data leveraging GenAI, with models deployed in Vertex AI     |      Beta :factory:      |

## Repository structure

* `terraform`: This directory contains the Terraform code for deploying the
  necessary Google Cloud
  infrastructure for each use case.
* `pipelines`: This directory contains the Python, Java, and Go code for the
  sample pipelines.
* `use_cases`: This directory contains the documentation of each use case

## Getting help

* GitHub Issues: Report any issues or ask questions on the GitHub repository.
  * https://github.com/GoogleCloudPlatform/dataflow-solution-guides/issues 
* Stack Overflow: Search for existing solutions or ask questions on Stack
  Overflow using the `google-cloud-dataflow` tag:
  * https://stackoverflow.com/questions/tagged/google-cloud-dataflow
  

## Contributing

Your contributions to this repository are welcome.

* Fork and Pull Request: Fork the repository and submit a pull request with your
  changes.
* Follow the Contribution Guidelines: Please follow the contribution guidelines
  outlined in the
  [CONTRIBUTING.md](CONTRIBUTING.md) file.

## Disclaimer

This is not an officially supported Google product. The code in this repository
is for demonstrative purposes only.
