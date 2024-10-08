# IoT Analytics

Organizations employ Internet of Things (IoT) sensors to monitor their production lines in real-time. These sensors gather critical data on various metrics essential for the manufacturing processes and can be utilized for analytical purposes. This data is operational in nature, and currently, it is not utilized for analytical purposes. With data warehouses, companies have leveraged low-granularity operational data for analytical purposes, enabling them to make more informed decisions utilizing large volumes of data. The use case described herein demonstrates how to replicate the same pattern (analytics on large volumes of low-granularity operational data) but with a crucial additional advantage: low latency. The value of data, and consequently, the decisions made based on that data diminish over time. Real-time analytics significantly enhance the value of such decisions.

## Documentation

- [One pager: IoT analytics in real-time with Dataflow (PDF)](./one_pagers/iot_analytics_dataflowonepager.pdf)
- [IoT Analytics Solution Guide & Architecture (PDF)](./guides/iot_analytics_dataflow_guide.pdf)

## Assets included in this repository
- Terraform code to deploy a project for IoT Analytics (WORK IN PROGRESS)
- Sample pipeline in Python for deploying IoT analytics (WORK IN PROGRESS)

## Technical benefits
- **Serverless experience:** Data volume can vary widely from connected devices and IoT appliances, which introduce significant overhead when managing infrastructure. Dataflow obviates that need entirely. Dataflow’s service layer goes beyond auto-provisioning. Features like dynamic work rebalancing, autoscaling, and service backends like Streaming Engine are built to handle your workload at any scale without needing user intervention.
- **Streaming AI & ML:** Dataflow’s suite of ML capabilities enable you to evolve your batch ML systems to streaming ML, enabling a world of real-time features and real-time predictions. Apache Beam and Dataflow include several capabilities that simplify the end-to-end machine learning lifecycle. We make data processing easier for AI easier with ML Transform. Implement RunInference to call predictions with your model of choice, whether it be scikit-learn, PyTorch, VertexAI, or Gemma. Dataflow’s integration with Vertex AI alleviates the need to manage complex computing requirements for your machine learning use cases.
- **Extensible connector framework:** Apache Beam provides more than 60 out of the box connectors that support the majority of your I/O needs, including support for popular messaging platforms like Kafka and Pub/Sub and messaging brokers like JMS and MQTT. If your desired input is not supported, Beam also offers a flexible framework that allows you to build a connector for your own source systems.
- **Open & portable:** For IoT use cases, it is a common requirement to process data in both on-device and multi-cloud enviornments Beam allows you the flexibility to run your business logic in the environment of your choice. Execution engines include the Direct Runner (for local execution), Spark and Flink (for your own self-managed & multi-cloud computing environments), and Dataflow (the preferred execution engine for Google Cloud).
