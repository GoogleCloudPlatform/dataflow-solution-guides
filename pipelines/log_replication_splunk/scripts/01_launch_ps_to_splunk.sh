SUBNET_OPT=""
if [ -n "$SUBNETWORK" ]; then
  SUBNET_OPT="--subnetwork=$SUBNETWORK"
elif [ -n "$NETWORK" ]; then
  SUBNET_OPT="--subnetwork=$NETWORK"
fi

gcloud dataflow jobs run splunk-log-replication \
  --gcs-location gs://dataflow-templates-$REGION/latest/Cloud_PubSub_to_Splunk \
  --region $REGION \
  --project $PROJECT \
  --service-account-email $SERVICE_ACCOUNT \
  --staging-location $TEMP_LOCATION \
  $SUBNET_OPT \
  --enable-streaming-engine \
  --disable-public-ips \
  --max-workers=$MAX_DATAFLOW_WORKERS \
  --parameters \
inputSubscription=$INPUT_SUBSCRIPTION,\
url=$SPLUNK_HEC_URL,\
disableCertificateValidation=true,\
includePubsubMessage=false,\
tokenSecretId=$TOKEN_SECRET_ID,\
tokenSource=SECRET_MANAGER,\
enableBatchLogs=true,\
enableGzipHttpCompression=true,\
outputDeadletterTopic=$DEADLETTER_TOPIC

