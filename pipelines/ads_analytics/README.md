# Google Ads Analytics Pipeline
This sample pipeline demonstrates how to use Dataflow to read data from Google Ads API using a user provided query and write the results into a BigQuery table. This pipeline is written in Java.

This pipeline is part of the [Dataflow Real-time Ads Analytics](https://github.com/GoogleCloudPlatform/dataflow-solution-guides) solution guide.

## Prerequisites

Before launching this pipeline, make sure you fulfill each of the prerequisites listed in the [Google Ads API tutorial](https://developers.google.com/google-ads/api/docs/get-started/introduction#prerequisites).

## Disclaimer

The rate limiting service used by the published template does not guarantee adherence to [Google Ads Quota Limits](https://developers.google.com/google-ads/api/docs/best-practices/quotas) and should only be used for one-off jobs or recurring jobs with a scheduling interval of once a day.
For customers with existing Google Ads workloads, the published template may cause them to exceed their quota if they're already pushing the limit and repeated abuse could lead to fines and invalidation of their API tokens.

## Template Parameters

You can find the list of parameters needed to run this pipeline in the provided [metadata.json](metadata.json) file.

### Required Parameters

* **customerIds** : A list of Google Ads account IDs to use to execute the query. (e.g. 12345, 67890).
* **query** : The query to use to get data. See [Google Ads Query Language](https://developers.google.com/google-ads/api/docs/query/overview). (e.g. select campaign.id, campaign.name from campaign). You can use [Google Ads Query Validator](https://developers.google.com/google-ads/api/fields/v16/query_validator) to test the query before launching the job.
* **qpsPerWorker** : The rate of query requests per second (QPS) to submit to Google Ads. Divide the desired per pipeline QPS by the maximum number of workers. Avoid exceeding per-account or developer token limits. See [Rate Limits](https://developers.google.com/google-ads/api/docs/best-practices/rate-limits).
* **googleAdsClientId** : The OAuth 2.0 client ID that identifies the application. See [Create a client ID and client secret](https://developers.google.com/google-ads/api/docs/oauth/cloud-project#create_a_client_id_and_client_secret).
* **googleAdsClientSecret** : The OAuth 2.0 client secret that corresponds to the specified client ID. See [Create a client ID and client secret](https://developers.google.com/google-ads/api/docs/oauth/cloud-project#create_a_client_id_and_client_secret).
* **googleAdsRefreshToken** : The OAuth 2.0 refresh token to use to connect to Google Ads API. See [Generate a refresh token](https://developers.google.com/google-ads/api/docs/get-started/make-first-call#fetch_a_refresh_token).
* **googleAdsDeveloperToken** : The Google Ads developer token to use to connect to Google Ads API. See [Obtain a developer token](https://developers.google.com/google-ads/api/docs/get-started/dev-token).
* **outputTableSpec** : The BigQuery output table to write the output to. (i.e. <PROJECT_ID>:<DATASET_NAME>.<TABLE_NAME>).

### Optional Parameters

* **loginCustomerId** : A Google Ads manager account ID to use to access the account IDs. (e.g. 12345).
* **writeDisposition** : The BigQuery [WriteDisposition](https://beam.apache.org/releases/javadoc/current/org/apache/beam/sdk/io/gcp/bigquery/BigQueryIO.Write.WriteDisposition.html) value. Defaults to WRITE_APPEND.
* **createDisposition** : The BigQuery [CreateDisposition](https://beam.apache.org/releases/javadoc/current/org/apache/beam/sdk/io/gcp/bigquery/BigQueryIO.Write.CreateDisposition.html) value. Defaults to CREATE_IF_NEEDED.

## Getting Started

### Requirements

* Java 21
* Maven
* [gcloud CLI](https://cloud.google.com/sdk/docs/install), and execution of the following commands:
  * ```gcloud auth login```
  * ```gcloud auth application-default login```

### Building the Template 

This template is a Flex Template, meaning that the pipeline code will be containerized and the container will be executed on Dataflow. Please check [Use Flex Templates](https://cloud.google.com/dataflow/docs/guides/templates/using-flex-templates) and [Configure Flex Templates](https://cloud.google.com/dataflow/docs/guides/templates/configuring-flex-templates) for more information.

#### Packaging the Template

To build the jar with all the dependencies, execute the following command:

```mvn clean package```

#### Staging the Template

After building the jar, stage the container in Artifact Registry and the pipeline's metadata in Google Cloud Storage 

``` 
export PROJECT=<my-project>
export BUCKET_NAME=<my-bucket>
export BUCKET_PATH=<my-bucket-path>
export PIPELINE_NAME=<my-pipeline_name>
export REPOSITORY_REGION=us-central1
export REPOSITORY=<my-repository>
export IMAGE_NAME=<my-image-name>

gcloud dataflow flex-template build gs://$BUCKET_NAME/$BUCKET_PATH/$PIPELINE_NAME.json --image-gcr-path "$REPOSITORY_REGION-docker.pkg.dev/$PROJECT/$REPOSITORY/$IMAGE_NAME:latest" --sdk-language "JAVA" --flex-template-base-image JAVA21 --metadata-file "ads_analytics/metadata.json" --jar "ads_analytics/target/ads_analytics-1.0-SNAPSHOT.jar" --env FLEX_TEMPLATE_JAVA_MAIN_CLASS="com.google.dataflow.samples.solutionguides.pipelines.AdsAnalyticsPipeline"
```

The command should build and save the template to Google Cloud, and then print the complete location on Cloud Storage:

```
Successfully saved container spec in flex template file.
Template File GCS Location: gs://<my-bucket>/<my-bucket-path>/<my-pipeline-name>.json
```

### Running the Template

#### Using Local Runner

To run the template using Beam Direct Runner, the following command can be used:

```
export BUCKET_NAME=<my-bucket-name>
export BUCKET_PATH=<my-bucket-path>

### Required
export CUSTOMER_IDS=<customerIDs>
export QUERY="SELECT campaign.id, campaign.name FROM campaign"
export QPS_PER_WORKER=<qpsPerWorker>
export GOOGLE_ADS_CLIENT_ID=<googleAdsClientId>
export GOOGLE_ADS_CLIENT_SECRET=<googleAdsClientSecret>
export GOOGLE_ADS_REFRESH_TOKEN=<googleAdsRefreshToken>
export GOOGLE_ADS_DEVELOPER_TOKEN=<googleAdsDeveloperToken>
export OUTPUT_TABLE_SPEC=<outputTableSpec>

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
```

The command should run directly in your computer, execute the provided query in Google Ads API per each Google Ads account and store the results in the provided BigQuery output table.

#### Using Dataflow

It is recommended to create a [user-managed worker service account](https://cloud.google.com/dataflow/docs/concepts/security-and-permissions#user-managed) with the [appropriate permissions](https://cloud.google.com/dataflow/docs/guides/templates/configuring-flex-templates#permissions_to_build_a_flex_template) when running the following command:

```
export JOB_NAME=<my-job-name>
export PIPELINE_NAME=<my-pipeline-name>
export BUCKET_NAME=<my-bucket-name>
export BUCKET_PATH=<my-bucket-path>
export REGION=us-central1
export SERVICE_ACCOUNT=<my-service-account>
export NETWORK=<my-network>
export SUBNETWORK=<my-subnetwork>

### Required
export CUSTOMER_IDS=<customerIDs>
export QUERY="SELECT campaign.id, campaign.name FROM campaign"
export QPS_PER_WORKER=<qpsPerWorker>
export GOOGLE_ADS_CLIENT_ID=<googleAdsClientId>
export GOOGLE_ADS_CLIENT_SECRET=<googleAdsClientSecret>
export GOOGLE_ADS_REFRESH_TOKEN=<googleAdsRefreshToken>
export GOOGLE_ADS_DEVELOPER_TOKEN=<googleAdsDeveloperToken>
export OUTPUT_TABLE_SPEC=<outputTableSpec>

### Optional
export LOGIN_CUSTOMER_ID=<loginCustomerId>
export WRITE_DISPOSITION=WRITE_APPEND
export CREATE_DISPOSITION=CREATE_IF_NEEDED

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
```

The command should run the pipeline in Dataflow, execute the provided query in Google Ads API per each Google Ads account and store the results in the provided BigQuery output table.