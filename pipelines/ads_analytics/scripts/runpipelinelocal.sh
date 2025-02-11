export BUCKET_NAME=defaultmultiregionus
export BUCKET_PATH=dataflow/templates/adsanalytics

### Required
export CUSTOMER_IDS=5380626448
export QUERY="SELECT campaign.id, campaign.name FROM campaign"
export QPS_PER_WORKER=50
export GOOGLE_ADS_CLIENT_ID=1049690543503-eskj0ing349dalshtmjlluargne4vis8.apps.googleusercontent.com
export GOOGLE_ADS_CLIENT_SECRET=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
export GOOGLE_ADS_REFRESH_TOKEN=1//01q4PGrAOSwVPCgYIARAAGAESNwF-L9IrZ4K520Yh3N1reQTCc_mUei1k-IiiV2EI5Po0-e4OJkdYx09hkYWz22VtHuAu7o_oHQ4
export GOOGLE_ADS_DEVELOPER_TOKEN=vLmwBMcDC-wU8bC06-KTpg
export OUTPUT_TABLE_SPEC=gilbertos-project-340619:AA.Ads_Results

### Optional
export LOGIN_CUSTOMER_ID=9136249991
export WRITE_DISPOSITION=WRITE_APPEND
export CREATE_DISPOSITION=CREATE_IF_NEEDED

mvn compile exec:java -Dexec.mainClass=com.google.dataflow.samples.solutionguides.pipelines.AdsAnalyticsPipeline \
-Dexec.args="--loginCustomerId=$LOGIN_CUSTOMER_ID \
--customerIds=$CUSTOMER_IDS \
--query='$QUERY' \
--qpsPerWorker=$QPS_PER_WORKER \
--googleAdsClientId=$GOOGLE_ADS_CLIENT_ID \
--googleAdsClientSecret=$GOOGLE_ADS_CLIENT_SECRET \
--googleAdsRefreshToken=$GOOGLE_ADS_REFRESH_TOKEN \
--googleAdsDeveloperToken=$GOOGLE_ADS_DEVELOPER_TOKEN \
--outputTableSpec=$OUTPUT_TABLE_SPEC \
--tempLocation=gs://$BUCKET_NAME/$BUCKET_PATH/" -e -f ads_analytics/pom.xml
