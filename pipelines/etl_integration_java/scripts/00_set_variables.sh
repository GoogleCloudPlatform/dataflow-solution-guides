# This file is generated by the Terraform code of this Solution Guide.
# We recommend that you modify this file only through the Terraform deployment.
export PROJECT=ihr-dataflow-ml-ai
export REGION=us-central1
export NETWORK=regions/us-central1/subnetworks/dataflow-subnet
export TEMP_LOCATION=gs://$PROJECT/tmp
export SERVICE_ACCOUNT=my-dataflow-sa@ihr-dataflow-ml-ai.iam.gserviceaccount.com

export DOCKER_REPOSITORY=dataflow-containers
export IMAGE_NAME=dataflow-solutions-ml-ai
export DOCKER_TAG=0.1
export DOCKER_IMAGE=$REGION-docker.pkg.dev/$PROJECT/$DOCKER_REPOSITORY/$IMAGE_NAME

export GCS_GEMMA_PATH=gs://$PROJECT/gemma_2B
export CONTAINER_URI=$DOCKER_IMAGE:$DOCKER_TAG

export MAX_DATAFLOW_WORKERS=1