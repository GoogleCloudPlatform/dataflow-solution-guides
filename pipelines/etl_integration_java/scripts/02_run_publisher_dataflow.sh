SUBNET_OPT=""
if [ -n "$SUBNETWORK" ]; then
  SUBNET_OPT="--subnetwork=$SUBNETWORK"
elif [ -n "$NETWORK" ]; then
  SUBNET_OPT="--subnetwork=$NETWORK"
fi

./gradlew run -Pargs="
  --pipeline=PUBSUB_TO_SPANNER \
  --streaming \
  --enableStreamingEngine \
  --autoscalingAlgorithm=THROUGHPUT_BASED \
  --runner=DataflowRunner \
  --project=$PROJECT \
  --tempLocation=$TEMP_LOCATION \
  --region=$REGION \
  --serviceAccount=$SERVICE_ACCOUNT \
  $SUBNET_OPT \
  --maxNumWorkers=$MAX_DATAFLOW_WORKERS \
  --experiments=enable_data_sampling \
  --usePublicIps=false \
  --pubsubTopic=$TOPIC \
  --spannerInstance=$SPANNER_INSTANCE \
  --spannerDatabase=$SPANNER_DATABASE \
  --spannerTable=$SPANNER_TABLE"