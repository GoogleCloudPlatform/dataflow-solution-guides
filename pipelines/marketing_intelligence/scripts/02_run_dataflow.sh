#  Copyright 2026 Google LLC
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

python main.py \
  --runner=DataflowRunner \
  --project=$PROJECT \
  --temp_location=${TEMP_LOCATION:-gs://$PROJECT/tmp} \
  --region=$REGION \
  --save_main_session \
  --machine_type=$MACHINE_TYPE \
  --num_workers=1 \
  --disk_size_gb=$DISK_SIZE_GB \
  --max_num_workers=$MAX_DATAFLOW_WORKERS \
  --no_use_public_ips \
  --service_account_email=$SERVICE_ACCOUNT \
  $SUBNET_OPT \
  --sdk_container_image=$CONTAINER_URI \
  --messages_subscription=${INPUT_SUBSCRIPTION:-projects/$PROJECT/subscriptions/dataflow-solutions-guide-market-intelligence-input-sub} \
  --responses_topic=${OUTPUT_TOPIC:-projects/$PROJECT/topics/dataflow-solutions-guide-market-intelligence-output} \
  --project_id=$PROJECT \
  --firestore_collection=${FIRESTORE_COLLECTION:-customer_profiles} \
  --bq_dataset=${BQ_DATASET:-output_dataset} \
  --bq_table=${BQ_TABLE:-predictions} \
  --model_path=/workspace/marketing_model.pkl \
  --threshold=0.80

