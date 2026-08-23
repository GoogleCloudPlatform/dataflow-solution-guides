SUBNET_OPT=""
if [ -n "$SUBNETWORK" ]; then
  SUBNET_OPT="--subnetwork=$SUBNETWORK"
elif [ -n "$NETWORK" ]; then
  SUBNET_OPT="--subnetwork=$NETWORK"
fi

gcloud dataflow flex-template run spanner-change-streams \
    --template-file-gcs-location=gs://dataflow-templates-$REGION/latest/flex/Spanner_Change_Streams_to_BigQuery \
    --project=$PROJECT \
    --region $REGION \
    --temp-location=$TEMP_LOCATION \
    --service-account-email=$SERVICE_ACCOUNT \
    $SUBNET_OPT \
    --max-workers=$MAX_DATAFLOW_WORKERS \
    --worker-machine-type=$WORKER_TYPE \
    --disable-public-ips \
    --parameters \
spannerInstanceId=$SPANNER_INSTANCE,\
spannerDatabase=$SPANNER_DATABASE,\
spannerMetadataInstanceId=$SPANNER_INSTANCE,\
spannerMetadataDatabase=$SPANNER_METADATA_DB,\
spannerChangeStreamName=$SPANNER_CHANGE_STREAM,\
bigQueryDataset=$BIGQUERY_DATASET