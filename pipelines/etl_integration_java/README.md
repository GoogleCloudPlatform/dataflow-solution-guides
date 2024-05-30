# ETL & integration sample pipeline (Java)

This sample pipeline demonstrates how to use Dataflow to create replicas of transactional databases, using change
streams, to create and maintain constantly updated replicas of the database. This pipeline is written in Java.

This pipeline is part of the [Dataflow ETL & integration solution guide](../../use_cases/ETL_integration.md).

## Architecture

There are two pipelines in this repository. The first pipeline reads from a Pub/Sub topic of public data, and writes
to a Spanner database. This pipeline's purpose is to keep Spanner with constant updates. The data is written in an
`events` table.

The second pipeline reads from a change stream from Spanner, and replicates the `events` table in BigQuery. The table
in BigQuery receives updates continuously and has the same data as the Spanner table, with a minimal latency. 

So the generic architecture for both looks like this:

![Architecture](./imgs/etl_integration.png)

## Spanner instance size and configuration

For the Spanner database, we are 

## How to launch the pipeline

All the scripts are located in the `scripts` directory and prepared to be launched from the top
sources directory.

In the script `scripts/00_set_environment.sh`, define the value of the project id and the region variable:

```
export PROJECT=<YOUR PROJECT ID>
export REGION=<YOUR CLOUD REGION>
```

Leave the rest of variables untouched, although you can override them if you prefer.

After you edit the script, load those variables into the environment

```sh
source scripts/00_set_environment.sh
```

And then run the script that builds and publishes the custom Dataflow container. This container will
contain the Gemma model, and all the required dependencies.

```sh
./scripts/01_build_and_push_container.sh
```

This will create a Cloud Build job that can take a few minutes to complete. Once it completes, you
can trigger the pipeline with the following:

```sh
./scripts/02_run_dataflow.sh
```

## Input data

To send data into the pipeline, you need to publish messages in the `messages` topic. Those
messages are passed "as is" to Gemma, so you may want to add some prompting to the question.

## Output data

The predictions are published into the topic `predictions`, and can be observed using the
subscription `predictions-sub`.