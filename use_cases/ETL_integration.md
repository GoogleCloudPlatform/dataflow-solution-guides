# ETL / Integration

Real-time extract-transform-load (ETL) & integration describes systems that are processing & writing
data as soon as it becomes available. This allows for near-instant analysis and decision-making
based on the most up-to-date information. ETL patterns refer to the continuous processing of data,
while integration broadly refers to writing the results of these pipelines to various systems (e.g.
data warehouses, transactional databases, messaging queues). Adopting real-time ETL & integration
architectures are generally regarded as an essential part of modernizing your data systems, and
confer a number of competitive advantages to the company adopting them.

For the full version of this solution guide, please refer to:
* https://solutions.cloud.google.com/app/solutions/dataflow-real-time-etl-integration

## Documentation

* [One pager: ETL & reverse ETL in real-time with Dataflow (PDF)](./one_pagers/etl_dataflow_onepager.pdf)
* [ETL & reverse ETL Solution Guide and Architecture (PDF)](./guides/etl_dataflow_guide.pdf)

## Assets included in this repository

* [Terraform code to deploy a project for ETL integration](../terraform/etl_integration/)
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
  and Streaming Engine, respectively. These backends have scaled
