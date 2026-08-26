#!/usr/bin/env bash
#  Copyright 2025 Google LLC
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

python main.py \
  --runner=DirectRunner \
  --project=${PROJECT:-local-test-project} \
  --temp_location=/tmp/dataflow-temp \
  --project_id=${PROJECT:-local-test-project} \
  --messages_subscription=${INPUT_SUBSCRIPTION:-projects/$PROJECT/subscriptions/dataflow-solutions-guide-market-intelligence-input-sub} \
  --responses_topic=${OUTPUT_TOPIC:-projects/$PROJECT/topics/dataflow-solutions-guide-market-intelligence-output} \
  --firestore_collection=${FIRESTORE_COLLECTION:-customer_profiles} \
  --bq_dataset=${BQ_DATASET:-output_dataset} \
  --bq_table=${BQ_TABLE:-predictions} \
  --model_path=marketing_model.pkl \
  --threshold=0.80
