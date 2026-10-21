# GenAI & machine learning inference

Machine learning (ML) and artificial intelligence (AI) empower businesses to respond to evolving
market conditions and tailor their offerings to users and customers. However, decision cycles
involving AI and ML can span days or even weeks, particularly when dealing with larger models
(model retraining, large inference batch pipelines, etc). This solution guide introduces an
architecture designed for real-time predictions, guaranteeing low latency outcomes with both custom
and third-party models. Leveraging the capabilities of graphics processing units (GPUs),
the proposed architecture effectively reduces prediction times to seconds.

## Documentation

- [One pager: GenAI & ML inference in real-time with Dataflow (PDF)](./one_pagers/genai_ml_dataflow_onepager.pdf)
- [Gen AI & ML inference Solution Guide and Architecture (PDF)](./guides/genai_ml_dataflow_guide.pdf)

For the full documentation of this solution guide, please refer to:

- https://solutions.cloud.google.com/app/solutions/data-flow-real-time-ml-and-genai

## Assets included in this repository

- [Terraform code to deploy infrastructure for GenAI & ML inference](../terraform/ml_ai/)
- [Sample pipeline in Python for leveraging Gemma 4 with vLLM on Dataflow](../pipelines/ml_ai_python/)

## Technical benefits

Dataflow is the best platform for building real-time ML & generative AI
applications. Several unique capabilities make Dataflow the leading choice:

- **Developer ease of use with turnkey transforms:** Author complex ML
  pipelines using utility transforms that can reduce lines code by orders of magnitude
  - [MLTransform](https://cloud.google.com/dataflow/docs/machine-learning/ml-preprocess-data)
    helps you prepare your data for training machine learning models without
    writing complex code or managing underlying libraries. ML Transforms can
    generate embeddings that can push data into vector databases to run
    inference.
  - [RunInference](https://beam.apache.org/documentation/ml/about-ml/#use-runinference)
    lets you efficiently use ML models in your pipelines, and contains a
    number of different optimizations underneath the hood that make this an
    essential part of any streaming AI pipelines
- **Advanced stream processing**: Customers can implement advanced streaming
  architectures using the open-source
  [Apache Beam SDK](https://beam.apache.org/get-started/), which provides a rich
  set capabilities of including state & timer APIs, transformations, side
  inputs, enrichment, and a broad list of I/O connectors.
- **Notebooks integration**: Develop your streaming AI pipeline in a
  notebook environment, which allows for interactive development and
  sampling unbounded data sources.
- **Cost efficiency**: Run pipelines without wasting precious resources &
  cost overruns.
  - [GPU support](https://cloud.google.com/dataflow/docs/gpu/gpu-support)
    Accelerate your processing with GPUs, which can return results faster
    for your most computationally demanding pipelines
  - [Right-fitting](https://cloud.google.com/dataflow/docs/guides/right-fitting)
    Deploy pipelines on heterogeneous worker pools. Rightfitting allows you
    to allocate additional resources to individual stages in your pipeline,
    which prevents wasteful utilization for stages that don’t require the
    same compute.
- **Open-source compatibility**: Dataflow has support for
  [running inference with Gemma](https://cloud.google.com/dataflow/docs/machine-learning/gemma)
  using high-throughput inference engines such as **vLLM** and **PyTorch**.
  Customers can leverage high-performance GPU serving with Gemma 4 (`google/gemma-4-E2B-it`)
  on GPU-accelerated workers (e.g. NVIDIA L4 GPUs) running Python 3.14. Model weights are baked directly into custom container images, eliminating runtime downloads. Customers should feel comfortable that these pipelines can be ported to
  any other execution engine with Apache Beam support.

## How to deploy (Agentic or Manual)

### Option 1: Deploy with an AI Coding Harness (Recommended)

Open this repository in your AI coding harness and prompt it to handle environment discovery (including Shared VPCs and GPU quotas), infrastructure provisioning, container building, live testing, and cleanup:

> *"Deploy the GenAI & ML Inference guide in project `my-gcp-project` and region `us-central1` (check if the project uses a Shared VPC and verify NVIDIA L4 GPU quota). Build the Gemma 4 vLLM worker container, launch the streaming Dataflow pipeline with private IPs, publish a test prompt to Pub/Sub, verify the response on `predictions-sub`, and tear down the resources when done."*

### Option 2: Manual CLI Deployment

Follow the step-by-step instructions in the [Terraform module README](../terraform/ml_ai/README.md) and [Pipeline README](../pipelines/ml_ai_python/README.md):

```bash
# 1. Provision Terraform infrastructure
cd terraform/ml_ai
terraform init && terraform apply

# 2. Build custom GPU container & launch Dataflow pipeline
cd ../../pipelines/ml_ai_python
source scripts/00_set_environment.sh
./scripts/01_build_and_push_container.sh
./scripts/02_run_dataflow.sh

# 3. Publish a test prompt & verify prediction output
gcloud pubsub topics publish messages --project="$PROJECT" --message='{"prompt": "What is Google Cloud Dataflow?"}'
gcloud pubsub subscriptions pull predictions-sub --project="$PROJECT" --auto-ack --limit=1
```

To tear down the deployment, cancel the active Dataflow job first, then run `terraform destroy` in `terraform/ml_ai`.
