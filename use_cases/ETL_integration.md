# ETL / Integration

Real-time extract-transform-load (ETL) & integration describes systems that are processing & writing
data as soon as it becomes available. This allows for near-instant analysis and decision-making
based on the most up-to-date information. ETL patterns refer to the continuous processing of data,
while integration broadly refers to writing the results of these pipelines to various systems (e.g.
data warehouses, transactional databases, messaging queues). Adopting real-time ETL & integration
architectures are generally regarded as an essential part of modernizing your data systems, and
confer a number of competitive advantages to the company adopting them.

## Documentation

* [One pager: ETL & reverse ETL in real-time with Dataflow (PDF)](./one_pagers/etl_dataflow_onepager.pdf)
* [ETL & reverse ETL Solution Guide and Architecture (PDF)](./guides/etl_dataflow_guide.pdf)

## Assets included in this repository

* [Terraform code to deploy infrastructure for ETL integration](../terraform/etl_integration/)
* [Sample pipelines in Java for ETL / Integration](../pipelines/etl_integration_java/)

## Technical benefits

Dataflow provides enormous advantages as a platform for your real-time ETL and integration use
cases:

* **Resource efficiency**: Increased resource efficiency with horizontal & vertical autoscaling
* **Unified batch & streaming**: Dataflow’s underlying SDK, Apache Beam, allows developers to
  express
  batch & streaming pipelines with the same SDK, with minor modifications required to turn a batch
  pipeline into a streaming one. This simplifies the traditionally accepted practice of maintaining
  two separate systems for batch & stream processing.
* **Limitless scalability**: Dataflow offers two service backends for batch and streaming called
  Shuffle
  and Streaming Engine, respectively. These backends have scaled to process petabytes of data and
  millions of events per second.

## How to deploy (Agentic or Manual)

### Option 1: Deploy with an AI Coding Harness (Recommended)

Open this repository in your AI coding harness and prompt it to discover your project networking (including Shared VPCs), provision Cloud Spanner and BigQuery, launch the CDC pipelines, verify replication, and clean up:

> *"Deploy the ETL & Integration guide in project `my-gcp-project`, region `europe-southwest1`. If this project has no local network and uses a Shared VPC, discover the usable subnet automatically. Provision the Spanner instance, change stream, and BigQuery dataset via Terraform, launch both the publisher pipeline and the Spanner Change Streams to BigQuery job, verify that replicated rows appear in `replica.events_changelog`, and tear down when finished."*

### Option 2: Manual CLI Deployment

Follow the step-by-step instructions in the [Terraform module README](../terraform/etl_integration/README.md) and [Pipeline README](../pipelines/etl_integration_java/README.md):

```bash
# 1. Provision Cloud Spanner (with Change Stream) & BigQuery infrastructure
cd terraform/etl_integration
terraform init && terraform apply

# 2. Launch the Spanner event publisher & Change Stream CDC jobs
cd ../../pipelines/etl_integration_java
source scripts/01_set_variables.sh
./scripts/02_run_publisher_dataflow.sh
./scripts/03_run_changestream_template.sh

# 3. Verify replicated CDC records in Spanner & BigQuery
gcloud spanner databases execute-sql taxis_database --instance=test-spanner-instance --project="$PROJECT" --sql='SELECT COUNT(*) FROM events'
bq query --project_id="$PROJECT" --use_legacy_sql=false 'SELECT COUNT(*) FROM replica.events_changelog'
```

To tear down the deployment, cancel both active Dataflow jobs first, then run `terraform destroy` in `terraform/etl_integration`.
