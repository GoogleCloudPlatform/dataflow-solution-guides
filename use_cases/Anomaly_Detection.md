# Real-Time Anomaly Detection

Real-time anomaly detection refers to stream processing workloads that identify abnormal events in-flight and
potentially respond with a relevant measure. Incoming events are analyzed and/or compared against a reference/benchmark
that validates whether a record is irregular or not. Anomaly detection architectures can enhance the security
posture of a company’s infrastructure or mitigate against the threat of malicious actors in a value chain.
Companies are increasingly adding proprietary machine learning models to augment their anomaly detection capabilities.
Low latency is normally a requirement for these kinds of workloads given the inherent nature of these adverse events.

## Documentation

- [One pager: Real-time Anomaly Detection with Dataflow (PDF)](./one_pagers/anomaly_detection_dataflow_onepager.pdf)
- [Real-time Anomaly Detection Solution Guide and Architecture (PDF)](./guides/anomaly_detection_dataflow_guide.pdf)

## Assets included in this repository

- [Terraform code to deploy a project for GenAI & ML inference](../terraform/anomaly_detection/)
- [Sample pipeline in Python for leveraging the Gemma open LLM with Dataflow](../pipelines/anomaly_detection/)

## Technical benefits

Dataflow is the best platform for building real-time
applications. Several unique capabilities make Dataflow the leading choice:

- **Integrated ML**:Combine your intelligence with your streaming pipeline using Datafow ML.
  RunInference helps you seamlessly call models hosted on Vertex AI from your Dataflow
  pipelines without the overhead of maintaining ML infrastrucure. Dataflow ML comes with
  the combined benefit of decoupling your prediction loop from your main application, thus
  eliminating the risk that pipeline stuckness can bring down your application.
- **Low latency**: Dataflow’s at-least-once delivery mode can help your pipeline achieve sub-second
  processing latencies, crucial to responding to threats as quickly as possible.

- **Integrated alerting**: Dataflow’s suite of observability tools enhances your ability to identify
  and respond to anomalous events. Create an alert from a Dataflow monitoring dashboard in a matter of a few clicks.

  - **Advanced stream processing**: Apache Beam’s state and timer APIs enables data engineers to manipulate and
    analyze state in-flight. These primitives allow you maximum flexibility to express the business logic
    that your application requires.

  - **Scalable infrastructure**: Pipeline scale up and down to meet the resourcing requirements of your pipeline.
    Powered by battle-tested backends in Shuffle & Streaming Engine, Dataflow is fit to support virtually pipelines
    of any size, with minimal tuning needed.
