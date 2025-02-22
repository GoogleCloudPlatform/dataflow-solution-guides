export PROJECT=gilbertos-project-340619
export BUCKET_NAME=defaultmultiregionus
export BUCKET_PATH=dataflow/templates/adsanalytics
export PIPELINE_NAME=Ads_Analytics
export REPOSITORY_REGION=us-central1
export REPOSITORY=defaultdockeruscentral1
export IMAGE_NAME=ads_analytics_pipeline

gcloud dataflow flex-template build gs://$BUCKET_NAME/$BUCKET_PATH/$PIPELINE_NAME.json --image-gcr-path "$REPOSITORY_REGION-docker.pkg.dev/$PROJECT/$REPOSITORY/$IMAGE_NAME:latest" --sdk-language "JAVA" --flex-template-base-image JAVA21 --metadata-file "ads_analytics/metadata.json" --jar "ads_analytics/target/ads_analytics-1.0-SNAPSHOT.jar" --env FLEX_TEMPLATE_JAVA_MAIN_CLASS="com.google.dataflow.samples.solutionguides.pipelines.AdsAnalyticsPipeline"
