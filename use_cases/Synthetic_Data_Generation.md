# Synthetic data generation

Development, testing and analytics prototyping need realistic tabular data,
but production tables usually cannot be copied for privacy, regulatory or
residency reasons. Masking degrades statistical realism, hand-written
fixtures do not scale, and sending real rows to an external LLM API to get
"lookalike" data moves them outside your control. This solution guide
generates fictitious but realistic rows for any BigQuery table from its DDL
and a bounded reference sample. The LLM runs **inside the Dataflow job**, on
GPU workers in your own project, and the output is validated and audited as
it lands.

## Assets included in this repository

- [Terraform code to deploy the infrastructure for synthetic data generation](../terraform/synthetic-llm-dataflow-bigquery/)
- [Sample pipeline in Python generating relational synthetic data with a self-hosted LLM on Dataflow](../pipelines/synthetic-llm-dataflow-bigquery/)

## Architecture

```mermaid
flowchart LR
  bq[(BigQuery<br/>DDL + reference sample)] --> launcher[Flex Template launcher<br/>relationship model · preflight]
  gcs[(GCS<br/>open-weight model)] --> workers
  launcher --> workers[Dataflow GPU workers<br/>vLLM served in-worker]
  workers --> landing[(BigQuery landing tables)]
  workers --> quality[(BigQuery dead-letter + validation_runs)]
```

1. **Plan.** The launcher reads the table's DDL and a relationship model
   (primary keys, foreign keys, identity columns), and orders the connected
   tables parents first.
2. **Learn once, sample in bulk.** Workers profile the reference sample and
   call the LLM a bounded number of times to build value pools for free-text
   columns. Rows are then sampled in vectorized code, so LLM cost does not grow
   with the row count.
3. **Keep relations valid.** Child tables are generated from the keys their
   parents actually landed in the same job, so foreign keys hold by
   construction.
4. **Validate and audit.** Each row passes schema, uniqueness and integrity
   checks. Failures go to a dead-letter table, and every run writes a verdict
   to `validation_runs`.

The sample deployment generates `users → orders → order_items` from the
fictitious `bigquery-public-data.thelook_ecommerce` dataset, into tables with
the same names and schemas in your project. The relationship model and the
tables are described in the
[Terraform README](../terraform/synthetic-llm-dataflow-bigquery/README.md).

## Technical benefits

- **No data egress, no external AI APIs.** Model weights are staged once to
  Cloud Storage and served by vLLM on the workers, with no model hub at
  runtime. Reference rows and prompts stay inside the project.
- **Batch GPU inference on Dataflow.** One image serves the Flex Template
  launcher and NVIDIA L4 or T4 workers; Dataflow provisions the GPUs and drivers
  and releases them when the job ends.
- **Relational integrity at scale.** A whole foreign-key component (chains,
  stars, diamonds) is generated in one job with measured parent-to-child
  fan-out.
- **Quality you can query.** Dead-letter rows keep their full error context;
  per-table verdicts, duplicate and orphan counts are plain BigQuery tables.
- **Portable.** The engines are pure Python behind one interface and the
  pipeline is Apache Beam, so it runs locally with the DirectRunner and a
  fake model client.

## How to deploy (Agentic or Manual)

### Option 1: Deploy with an AI Coding Harness (Recommended)

Open this repository in your AI coding harness and prompt it to inspect your project networking and GPU quotas, stage the open-weight LLM, run the batch Flex Template, and validate the generated relational tables:

> *"Deploy and test the Synthetic Data Generation guide in project `my-gcp-project`, region `europe-southwest1` (or `us-central1`). Check past Dataflow jobs if needed to infer the Shared VPC subnetwork, and check regional `NVIDIA_L4_GPUS` / `NVIDIA_T4_GPUS` quota to select the matching GPU profile. Provision Terraform, build the container, stage the model to GCS, build and run the Dataflow Flex Template, and verify the run with `05_verify_run.sh`."*

### Option 2: Manual CLI Deployment

Follow the step-by-step instructions in the [Terraform module README](../terraform/synthetic-llm-dataflow-bigquery/README.md) and [Pipeline README](../pipelines/synthetic-llm-dataflow-bigquery/README.md):

```bash
# 1. Provision BigQuery datasets, Artifact Registry & Service Accounts
cd terraform/synthetic-llm-dataflow-bigquery
terraform init && terraform apply

# 2. Build container, stage LLM weights, build Flex Template & run batch job
cd ../../pipelines/synthetic-llm-dataflow-bigquery
source scripts/00_set_variables.sh
./scripts/01_build_and_push_container.sh
./scripts/02_stage_models.sh
./scripts/03_build_flex_template.sh
./scripts/04_run_dataflow.sh

# 3. Verify generated tables and quality verdicts
./scripts/05_verify_run.sh <RUN_ID>
```

To tear down the deployment after the batch job completes, run `terraform destroy` in `terraform/synthetic-llm-dataflow-bigquery`.
