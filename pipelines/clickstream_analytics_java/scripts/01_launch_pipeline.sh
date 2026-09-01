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

SUBNET_OPT=""
if [ -n "$SUBNETWORK" ]; then
  SUBNET_OPT="--subnetwork=$SUBNETWORK"
elif [ -n "$NETWORK" ]; then
  SUBNET_OPT="--subnetwork=$NETWORK"
fi

./gradlew run -Pargs="
  --runner=DataflowRunner \
  --project=$PROJECT \
  --region=$REGION \
  --tempLocation=$TEMP_LOCATION \
  --serviceAccount=$SERVICE_ACCOUNT \
  $SUBNET_OPT \
  --usePublicIps=false \
  --maxNumWorkers=$MAX_DATAFLOW_WORKERS \
  --bqProjectId=$PROJECT \
  --bqDataset=$BQ_DATASET \
  --bqTable=$BQ_TABLE \
  --pubsubSubscription=$SUBSCRIPTION \
  --btInstance=$BIGTABLE_INSTANCE \
  --btTable=$BIGTABLE_TABLE \
  --outputDeadletterTable=$BQ_DEADLETTER_TABLE \
  --btLookupKey=$BT_LOOKUP_KEY"
