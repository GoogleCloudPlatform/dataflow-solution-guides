echo "Setting environment variables"

export PROJECT=<YOUR PROJECT>
export REGION=<YOUR REGION>
export NETWORK=regions/$REGION/subnetworks/dataflow-net
export TEMP_LOCATION=gs://$PROJECT/tmp
export SERVICE_ACCOUNT=my-dataflow-sa@$PROJECT.iam.gserviceaccount.com

export TOPIC=projects/pubsub-public-data/topics/taxirides-realtime
export SPANNER_INSTANCE=test-spanner-instance
export SPANNER_DATABASE=taxis_database
export SPANNER_TABLE=events

export SPANNER_CHANGE_STREAM="EverythingStream"

