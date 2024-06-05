#  Copyright 2024 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

export PROJECT=<YOUR_PROJECT_ID>
export REGION=us-central1

export DOCKER_REPOSITORY=dataflow-containers
export IMAGE_NAME=dataflow-solutions-ml-ai
export DOCKER_TAG=0.1
export DOCKER_IMAGE=$REGION-docker.pkg.dev/$PROJECT/$DOCKER_REPOSITORY/$IMAGE_NAME

export GCS_GEMMA_PATH=gs://$PROJECT/gemma_2B
export CONTAINER_URI=$DOCKER_IMAGE:$DOCKER_TAG

export SERVICE_ACCOUNT=my-dataflow-sa@$PROJECT.iam.gserviceaccount.com
export SUBNETWORK=regions/$REGION/subnetworks/dataflow-subnet
export MACHINE_TYPE=g2-standard-4