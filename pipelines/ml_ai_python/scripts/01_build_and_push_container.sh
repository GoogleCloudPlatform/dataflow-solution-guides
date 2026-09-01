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

gcloud builds submit \
  --project=$PROJECT \
  --region=$REGION \
  --default-buckets-behavior=regional-user-owned-bucket \
  --substitutions _TAG=$CONTAINER_URI,_GCS_GEMMA_PATH=${GCS_GEMMA_PATH:-""},_MODEL_PRESET=${MODEL_PRESET:-"google/gemma-4-2b-it"},_KAGGLE_USERNAME=${KAGGLE_USERNAME:-""},_KAGGLE_KEY=${KAGGLE_KEY:-""},_HF_TOKEN=${HF_TOKEN:-""} \
  .