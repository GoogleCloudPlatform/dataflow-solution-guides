# Log replication & analytics

Google Cloud produces all kinds of logs that are automatically sent to Cloud
Logging. However, in some situations, you may want to use a third party such
as Splunk for log processing and analytics. This solution presents an
architecture to replicate logs from Cloud Logging to a third-party service,
using Dataflow. The solution ensures that all changes done in the upstream
databases are promptly replicated in the destination analytics replica,
with minimal delay (in the order of single digit seconds).

## Documentation

- [One pager: Log replication and analytics in real-time with Dataflow (PDF)](./one_pagers/log_replication_dataflow_onepager.pdf)
- [Log replication and analytics Solution Guide and Architecture (PDF)](./guides/log_replication_dataflow_guide.pdf)

## Assets included in this repository

- [Terraform code to deploy a project for log replication into Splunk](../terraform/log_replication_splunk/)
- [Use Google-provide templates to run a job to replicate to Splunk](../pipelines/log_replication_splunk/)

## Technical benefits

- **Serverless experience**: Data volume can vary widely from logging
  applications or transactional databases. Dataflow obviates that need
  entirely. Dataflow’s service layer goes beyond auto-provisioning. Features
  like dynamic work rebalancing, autoscaling, and service backends like
  Streaming Engine are built to handle your workload at any scale without
  needing user intervention.
- **Easy operations**: Dataflow offers several features that helps
  organizations ensure the uptime of their pipelines. Snapshots preserve the
  state of your pipeline for high availability / disaster recovery scenarios,
  while in-place streaming update can seamlessly migate your pipeline to a
  new version without any data loss or downtime.
- **Google-provided Templates**: Google provides Dataflow templates make
  deployment as easy as filling out a web form. Send logs to Splunk,
  Elasticsearch, or Datadog with our partner-provided templates.
- **Low latency**: Dataflow’s at-least-once delivery mode can help your
  pipeline achieve sub-second processing latencies, essential for your
  mission-critical logging applications.
- **Monitoring tools**: In-line logging, job visualizers, monitoring charts,
  integrated error reporting and smart insights help you optimize the
  performance of your pipeline, and can catch any stuckness or slowness
  issues before they turn into outages.
