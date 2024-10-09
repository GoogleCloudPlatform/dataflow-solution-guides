# Market Intelligence inference

Real-time marketing intelligence describes the practice of collecting and analyzing data about your market, customers, and competitors as it happens. This enables you to make informed, agile decisions and respond swiftly to emerging trends, customer behaviors, and competitive moves. The advent of data-driven marketing has transformed the way companies approach their marketing activities, and real-time marketing intelligence requires these companies to accelerate their response times to marketing moments. This reference architecture will describe how you can combine data from your various marketing data sources, common patterns for analyzing them, and integrating them with your data warehouse for faster analysis and databases for faster responses.

## Documentation

- [One pager: Marketing intelligence in real-time with Dataflow (PDF)](./one_pagers/market_intel_dataflow_onepager.pdf)
- [Marketing Intelligence Solution Guide and Architecture (PDF)](./guides/market_intel_dataflow_guide.pdf)

## Assets included in this repository

- [Terraform code to deploy a project for Market Intelligence inference](../terraform/marketing_intelligence/)
- [Sample pipeline in Python for leveraging the Gemma open LLM with Dataflow](../pipelines/marketing_intelligence/)

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
  as well as a strong integration with
  [Tensorflow Extended](https://www.tensorflow.org/tfx)
  Customers should feel comfortable that these pipelines can be ported to
  any other execution engine with Apache Beam support.
