# Market Intelligence sample pipeline (Python)

This sample pipeline demonstrates how to use Dataflow to process data, and calculate predictions
using Vertex AI Endpoint, by training a model using AutoML and deploying it on Vertex AI Endpoint.
This pipeline is written in Python.

This pipeline is part of the [Dataflow Gen AI & ML solution guide](../../use_cases/Market_Intelligence.md).

## Architecture

The generic architecture for an inference pipeline is shown below:

![Architecture](../imgs/market_intel.png)

In this directory, you will find a specific implementation of the above architecture, with the
following stages:

1. **Data ingestion:** Reads data from a Pub/Sub topic.
For more information about Pub/Sub [ Cloud Pub/Sub Overview]( https://cloud.google.com/pubsub/docs/overview).
2. **Data preprocessing:** While this sample pipeline doesn't perform any transformations, you can easily add a preprocessing step using the
   [the Enrichment transform](https://cloud.google.com/dataflow/docs/guides/enrichment) for feature engineering before invoking the model.

3. **Inference:** Uses the RunInference transform with VertexAIModelHandlerJSON, which in turn sends online prediction request to an Auto-ML generated model. The pipeline uses a GPU with the Dataflow worker, to speed up the inference. For more information about Vertex AI [Vertex AI Overview](https://cloud.google.com/vertex-ai/docs/overview).

4. **Predictions:** The predictions are sent to another Pub/Sub topic as output.

## Auto-ML model

The model can be deployed on a Vertex AI endpoint to serve various purposes.
To demonstrate the process, we followed these steps to create, train, and deploy the model:

1. Create a [Vertex-AI Dataset](https://cloud.google.com/vertex-ai/docs/tutorials/tabular-bq-prediction/create-dataset) using an existing Bigquery table.
2. Train a [Text-Classification Model](https://cloud.google.com/vertex-ai/docs/beginner/beginners-guide#train_model) using AutoML.
3. Once the model is ready, it can be [deployed and tested](https://cloud.google.com/vertex-ai/docs/tutorials/image-classification-automl/deploy-predict#deploy_your_model_to_an_endpoint) on Vertex-AI endpoint.
4. Take a note of the end-point ID.

## Getting Started

## Choosing Your Cloud Region

It's important to choose your cloud region carefully, as not all Google Cloud services and features are available in every region.

Tip: The default settings in this directory have been tested and work well in the us-central1 region. If you choose a different region, you may need to adjust some settings.

## Choosing Your Machine Type:

The cloudbuild.yaml file currently uses the E2_HIGHCPU_8 machine type. If this type isn't available in your chosen region, you'll need to edit the file and select a different machine type that is supported.
[Machine types](https://cloud.google.com/compute/docs/machine-types)


## Choosing Your Dataflow Worker Machine Type

The `scripts/00_set_environment.sh` file also specifies the machine type for your Dataflow workers. We recommend `g2-standard-4` for optimal GPU inference performance.

If `g2-standard-4` isn't available in your region, you can list suitable machine types using this command (replace <ZONE A>, <ZONE B>, etc. with your desired zones):

```sh
gcloud compute machine-types list --zones=<ZONE A>,<ZONE B>,.
```

See more info about selecting the right type of machine Type in the [Machine Types](https://cloud.google.com/compute/docs/machine-types)


## Next Steps

## Launch the pipeline

All scripts are in the scripts directory and designed to run from the project's root directory.

In the script `scripts/00_set_environment.sh`, define the value of the project id and the region variable:

```
export PROJECT=<YOUR PROJECT ID>
export REGION=<YOUR CLOUD REGION>
```

You can leave the remaining variables at their default values, but feel free to override them if you have specific preferences or requirements.

Once you've finished editing the script, make sure to source it again to load the updated variables into your current environment by running the following command.

```sh
source scripts/00_set_environment.sh
```

Next, run the script to build and publish the custom Dataflow container. This container will include the necessary dependencies for the worker.

```sh
./scripts/01_build_and_push_container.sh
```

This will create a Cloud Build job that can take a few minutes to complete. Once it completes, you
can trigger the pipeline with the following:

```sh
./scripts/02_run_dataflow.sh
```

## Input data

To feed data into the pipeline, publish messages to the Pub/Sub topic `dataflow-solutions-guide-market-intelligence-input` These messages are sent directly to the endpoint without modification.

## Output data

Predictions are published to the Pub/Sub topic `dataflow-solutions-guide-market-intelligence-output topic and can be viewed using the Pub/Sub console.dataflow-solutions-guide-market-intelligence-output-sub subscription.
