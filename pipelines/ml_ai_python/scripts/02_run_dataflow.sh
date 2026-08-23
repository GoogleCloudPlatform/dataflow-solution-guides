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

SUBNET_OPT=""
if [ -n "$SUBNETWORK" ]; then
  SUBNET_OPT="--subnetwork=$SUBNETWORK"
elif [ -n "$NETWORK" ]; then
  SUBNET_OPT="--subnetwork=$NETWORK"
fi

MACHINE_TYPE="n1-highmem-8"
ACCELERATOR_OPT="worker_accelerator=type:nvidia-tesla-t4;count:1;install-nvidia-driver:5xx"

python main.py \
  --runner=DataflowRunner \
  --project=$PROJECT \
  --temp_location=$TEMP_LOCATION \
  --region=$REGION \
  --save_main_session \
  --machine_type=$MACHINE_TYPE \
  --num_workers=1 \
  --disk_size_gb=$DISK_SIZE_GB \
  --max_num_workers=$MAX_DATAFLOW_WORKERS \
  --number_of_worker_harness_threads=1 \
  --no_use_public_ips \
  --service_account_email=$SERVICE_ACCOUNT \
  $SUBNET_OPT \
  --sdk_container_image=$CONTAINER_URI \
  --dataflow_service_options="$ACCELERATOR_OPT" \
  --messages_subscription=projects/$PROJECT/subscriptions/messages-sub \
  --responses_topic=projects/$PROJECT/topics/predictions \
  --model_path="gemma_2B"

