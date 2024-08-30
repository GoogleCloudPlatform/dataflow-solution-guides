gcloud dataflow jobs run logs-to-splunk \
  --gcs-location gs://dataflow-templates-$REGION/latest/Cloud_PubSub_to_Splunk \
  --region $REGION \
  --project $PROJECT \
  --service-account-email $SERVICE_ACCOUNT \
  --staging-location $TEMP_LOCATION \
  --subnetwork $NETWORK \
  --enable-streaming-engine \
  --disable-public-ips \
  --max-workers=$MAX_DATAFLOW_WORKERS \
  --parameters \
inputSubscription=$INPUT_SUBSCRIPTION,\
url=$SPLUNK_HEC_URL,\
disableCertificateValidation=false,\
includePubsubMessage=false,\
tokenSecretId=$TOKEN_SECRET_ID,\
tokenSource=SECRET_MANAGER,\
enableBatchLogs=true,\
enableGzipHttpCompression=true,\
outputDeadletterTopic=$DEADLETTER_TOPIC
