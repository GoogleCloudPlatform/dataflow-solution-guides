#!/bin/bash
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PIPELINE_DIR"

SUBNET_OPT=""
if [ -n "$SUBNETWORK" ]; then
  SUBNET_OPT="--subnetwork=$SUBNETWORK"
elif [ -n "$NETWORK" ]; then
  SUBNET_OPT="--subnetwork=$NETWORK"
fi

python3 -m main \
  --streaming \
  --runner=DataflowRunner \
  --project=${PROJECT_ID:-$PROJECT} \
  --project_id=${PROJECT_ID:-$PROJECT} \
  --temp_location=${TEMP_LOCATION:-gs://$PROJECT_ID/tmp} \
  --region=$REGION \
  --save_main_session \
  --service_account_email=$SERVICE_ACCOUNT \
  $SUBNET_OPT \
  --no_use_public_ips \
  --sdk_container_image=$CONTAINER_URI \
  --max_workers=$MAX_DATAFLOW_WORKERS \
  --topic=$TOPIC_ID \
  --dataset=$DATASET \
  --table=$TABLE \
  --bigtable_instance_id=${BIGTABLE_INSTANCE_ID:-$INSTANCE_ID} \
  --bigtable_table_id=$BIGTABLE_TABLE_ID \
  --row_key=$ROW_KEY \
  --enable_streaming_engine