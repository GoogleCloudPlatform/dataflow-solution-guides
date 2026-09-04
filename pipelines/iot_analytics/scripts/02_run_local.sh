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

# Runs the pipeline locally with DirectRunner for development and testing

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PIPELINE_DIR"

python3 -m main \
  --runner=DirectRunner \
  --project_id=${PROJECT_ID:-local-test-project} \
  --temp_location=/tmp/dataflow-temp \
  --topic=${TOPIC_ID:-projects/local-test-project/topics/maintenance-data} \
  --alert_topic=${ALERT_TOPIC_ID:-projects/local-test-project/topics/maintenance-alerts} \
  --model_path=${MODEL_FILE_PATH:-maintenance_model.pkl} \
  --dataset=${DATASET:-iot} \
  --table=${TABLE:-maintenance_analytics} \
  --bigtable_instance_id=${BIGTABLE_INSTANCE_ID:-iot-analytics} \
  --bigtable_table_id=${BIGTABLE_TABLE_ID:-maintenance_data} \
  --row_key=vehicle_id \
  --window_size_seconds=10
