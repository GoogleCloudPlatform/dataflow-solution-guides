## Getting Started

## Pipeline Code
* To build the project, `gradle build`

gradle clean execute -DmainClass=clickstream_analytics_java.ClickstreamPubSubToBq -Dexec.args="--runner=DataflowRunner --gcpTempLocation=gs://df-sol-guide-gcs/tmp-loc --project_id=dev-experiment-project --bq_dataset=df_sol_guide --bq_table=sample_tbl --pubsub_subscription=projects/dev-experiment-project/subscriptions/df-sol-pubsub-sub --bt_instance=df-sol-bt-instance --bt_table=sdf-sol-tbl --bq_deadletter_tbl=deadletter_tbl"
