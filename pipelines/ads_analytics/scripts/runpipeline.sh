export JOB_NAME=ads-analytics-pipeline
export PIPELINE_NAME=Ads_Analytics
export BUCKET_NAME=defaultmultiregionus
export BUCKET_PATH=dataflow/templates/adsanalytics
export REGION=us-central1
export SERVICE_ACCOUNT=dataflowrunnerdefault@gilbertos-project-340619.iam.gserviceaccount.com
export NETWORK=customvpc
export SUBNETWORK=regions/us-central1/subnetworks/us-central1

### Required
export CUSTOMER_IDS=5380626448
export QUERY="SELECT campaign.id, campaign.name FROM campaign"
export QPS_PER_WORKER=50
export GOOGLE_ADS_CLIENT_ID="1049690543503-eskj0ing349dalshtmjlluargne4vis8.apps.googleusercontent.com"
export GOOGLE_ADS_CLIENT_SECRET="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"
export GOOGLE_ADS_REFRESH_TOKEN="1//01q4PGrAOSwVPCgYIARAAGAESNwF-L9IrZ4K520Yh3N1reQTCc_mUei1k-IiiV2EI5Po0-e4OJkdYx09hkYWz22VtHuAu7o_oHQ4"
export GOOGLE_ADS_DEVELOPER_TOKEN="vLmwBMcDC-wU8bC06-KTpg"
export OUTPUT_TABLE_SPEC="gilbertos-project-340619:AA.Ads_Results"

### Optional

export LOGIN_CUSTOMER_ID=9136249991
export WRITE_DISPOSITION="WRITE_APPEND"
export CREATE_DISPOSITION="CREATE_IF_NEEDED"

gcloud dataflow flex-template run "$JOB_NAME-`date +%Y%m%d-%H%M%S`" \
--template-file-gcs-location "gs://$BUCKET_NAME/$BUCKET_PATH/$PIPELINE_NAME.json" \
--parameters loginCustomerId=$LOGIN_CUSTOMER_ID \
--parameters customerIds=$CUSTOMER_IDS \
--parameters ^~^query="$QUERY" \
--parameters qpsPerWorker=$QPS_PER_WORKER \
--parameters googleAdsClientId=$GOOGLE_ADS_CLIENT_ID \
--parameters googleAdsClientSecret=$GOOGLE_ADS_CLIENT_SECRET \
--parameters googleAdsRefreshToken=$GOOGLE_ADS_REFRESH_TOKEN \
--parameters googleAdsDeveloperToken=$GOOGLE_ADS_DEVELOPER_TOKEN \
--parameters outputTableSpec=$OUTPUT_TABLE_SPEC \
--region $REGION \
--service-account-email=$SERVICE_ACCOUNT \
--network=$NETWORK \
--subnetwork=$SUBNETWORK
