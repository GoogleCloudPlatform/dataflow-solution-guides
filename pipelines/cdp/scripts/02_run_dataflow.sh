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

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "$SCRIPT_DIR/00_set_environment.sh" ]; then
  # shellcheck source=/dev/null
  source "$SCRIPT_DIR/00_set_environment.sh"
fi

: "${PROJECT:?PROJECT must be set or source 00_set_environment.sh}"
: "${REGION:?REGION must be set or source 00_set_environment.sh}"
: "${SERVICE_ACCOUNT:?SERVICE_ACCOUNT must be set or source 00_set_environment.sh}"
: "${CONTAINER_URI:?CONTAINER_URI must be set or source 00_set_environment.sh}"

SUBNET_OPT=""
if [ -n "$SUBNETWORK" ]; then
  SUBNET_OPT="--subnetwork=$SUBNETWORK"
elif [ -n "$NETWORK" ]; then
  SUBNET_OPT="--subnetwork=$NETWORK"
fi

INPUT_ARGS=()
if [ -n "$TRANSACTIONS_SUBSCRIPTION" ]; then
  INPUT_ARGS+=(--transactions_subscription="$TRANSACTIONS_SUBSCRIPTION")
elif [ -n "$TRANSACTIONS_TOPIC" ]; then
  INPUT_ARGS+=(--transactions_topic="$TRANSACTIONS_TOPIC")
fi

if [ -n "$COUPON_REDEMPTION_SUBSCRIPTION" ]; then
  INPUT_ARGS+=(--coupons_redemption_subscription="$COUPON_REDEMPTION_SUBSCRIPTION")
elif [ -n "$COUPON_REDEMPTION_TOPIC" ]; then
  INPUT_ARGS+=(--coupons_redemption_topic="$COUPON_REDEMPTION_TOPIC")
fi

DLQ_ARGS=()
if [ -n "$BQ_DEADLETTER_TABLE" ]; then
  DLQ_ARGS+=(--deadletter_table="$BQ_DEADLETTER_TABLE")
fi

echo "Submitting Customer Data Platform Dataflow pipeline..."
python3 -m main \
  --streaming \
  --runner=DataflowRunner \
  --project="$PROJECT" \
  --temp_location="${TEMP_LOCATION:-gs://$PROJECT/tmp}" \
  --region="$REGION" \
  --save_main_session \
  --service_account_email="$SERVICE_ACCOUNT" \
  $SUBNET_OPT \
  --no_use_public_ips \
  --sdk_container_image="$CONTAINER_URI" \
  --max_num_workers="$MAX_DATAFLOW_WORKERS" \
  --disk_size_gb="$DISK_SIZE_GB" \
  --machine_type="$MACHINE_TYPE" \
  "${INPUT_ARGS[@]}" \
  --output_dataset="$BQ_DATASET" \
  --output_table="$BQ_UNIFIED_TABLE" \
  --output_sessions_table="${BQ_SESSIONS_TABLE:-customer_sessions}" \
  "${DLQ_ARGS[@]}" \
  --session_gap_seconds="${SESSION_GAP_SECONDS:-900}" \
  --allowed_lateness_seconds="${ALLOWED_LATENESS_SECONDS:-60}" \
  --use_storage_write_api \
  --enable_streaming_engine
