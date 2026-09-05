#!/usr/bin/env bash
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

set -euo pipefail
: "${MODEL_ENDPOINT:?Set MODEL_ENDPOINT to an existing Vertex AI endpoint ID with a deployed model}"
: "${PROJECT:?Source scripts/00_set_variables.sh first}"
: "${REGION:?REGION is required}"
: "${TEMP_LOCATION:?TEMP_LOCATION is required}"
: "${SERVICE_ACCOUNT:?SERVICE_ACCOUNT is required}"
: "${CONTAINER_URI:?CONTAINER_URI is required}"
: "${INPUT_SUBSCRIPTION:?INPUT_SUBSCRIPTION is required}"
: "${OUTPUT_TOPIC:?OUTPUT_TOPIC is required}"
subnet_args=()
subnet="${SUBNETWORK:-${NETWORK:-}}"
if [[ -n "$subnet" ]]; then
  subnet_args+=(--subnetwork="$subnet")
fi
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python3.14 main.py \
  --runner=DataflowRunner \
  --project="$PROJECT" \
  --region="$REGION" \
  --temp_location="$TEMP_LOCATION" \
  --service_account_email="$SERVICE_ACCOUNT" \
  --sdk_container_image="$CONTAINER_URI" \
  --sdk_location=container \
  --streaming \
  --save_main_session \
  --setup_file=./setup.py \
  --no_use_public_ips \
  --max_num_workers="${MAX_DATAFLOW_WORKERS:?MAX_DATAFLOW_WORKERS is required}" \
  --disk_size_gb="${DISK_SIZE_GB:?DISK_SIZE_GB is required}" \
  --machine_type="${MACHINE_TYPE:?MACHINE_TYPE is required}" \
  --messages_subscription="$INPUT_SUBSCRIPTION" \
  --responses_topic="$OUTPUT_TOPIC" \
  --error_topic="${ERROR_TOPIC:?ERROR_TOPIC is required}" \
  --bigtable_instance="${BIGTABLE_INSTANCE:?BIGTABLE_INSTANCE is required}" \
  --bigtable_table="${BIGTABLE_TABLE:-customer_profiles}" \
  --bigtable_column_family="${BIGTABLE_COLUMN_FAMILY:-profile}" \
  --bigquery_table="${BIGQUERY_TABLE:?BIGQUERY_TABLE is required}" \
  --model_endpoint="$MODEL_ENDPOINT" \
  --location="${MODEL_LOCATION:-$REGION}" \
  "${subnet_args[@]}"
