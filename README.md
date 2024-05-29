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

* [GenAI & machine learning inference](./use_cases/GenAI_ML.md). This guide
  demonstrates how to use Dataflow to perform real-time inference with GenAI
  models.
* [ETL / Integration](./use_cases/ETL_integration.md). This guide shows how 
  to replicate a database into BigQuery using a change-data-capture 
  streaming pipeline in Dataflow.

## Repository structure

* `terraform`: This directory contains the Terraform code for deploying the
  necessary Google Cloud
  infrastructure for each use case.
* `pipelines`: This directory contains the Python, Java, and Go code for the
  sample pipelines.
* `use_cases`: This directory contains the documentation of each use case

## Getting help

* GitHub Issues: Report any issues or ask questions on the GitHub repository.
* Stack Overflow: Search for existing solutions or ask questions on Stack
  Overflow using the
  `google-cloud-dataflow` tag.

## Contributing

* Fork and Pull Request: Fork the repository and submit a pull request with your
  changes.
* Follow the Contribution Guidelines: Please follow the contribution guidelines
  outlined in the
  [CONTRIBUTING.md](CONTRIBUTING.md) file.

## Disclaimer

This is not an officially supported Google product. The code in this repository
is for demonstrative purposes only.