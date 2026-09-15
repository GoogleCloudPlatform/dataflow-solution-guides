#!/usr/bin/env bash
#  Copyright 2026 The synthetic-llm-dataflow-bigquery Authors
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

# Launch one relational generation job from the Flex Template.
#
# Targeting the leaf table (order_items) expands the launch to its whole
# connected component in config/relationships/gcp_public_fk_example.yaml:
# users are generated first (NUM_ROWS rows), orders from the users' landed
# keys, then order_items from the orders' landed keys, with product_id drawn
# from the products catalog already in the landing dataset (ADR 0036/0037).
# The landing, quality and RAG tables already exist (Terraform), so nothing
# is created at launch. The Terraform launch (launch_job = true) passes the
# same parameters, from terraform/synthetic-llm-dataflow-bigquery/dataflow.tf.
#
# Usage: ./scripts/04_run_dataflow.sh [NUM_ROWS]
# Prints the RUN_ID; pass it to 05_verify_run.sh once the job is done.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -n "${PROJECT:-}" ]] || source "${SCRIPT_DIR}/00_set_variables.sh"

ROWS="${1:-${NUM_ROWS}}"
RUN_ID="thelook-$(date -u +%Y%m%d-%H%M%S)"

SUBNET_OPT=()
if [[ -n "${SUBNETWORK:-}" ]]; then
  SUBNET_OPT=(--subnetwork="${SUBNETWORK}")
fi

# One SDK process per GPU worker: every process would otherwise start its
# own vLLM server on the single GPU (ADR 0034).
EXPERIMENTS="enable_portable_runner,worker_accelerator=${ACCELERATOR},no_use_multiple_sdk_containers"

PARAMS=(
  "reference_table=${PROJECT}.${SOURCE_DATASET}.order_items"
  "landing_table=${PROJECT}.${LANDING_DATASET}.order_items"
  "dlq_table=${PROJECT}.${QUALITY_DATASET}.dlq"
  "validation_runs_table=${PROJECT}.${QUALITY_DATASET}.validation_runs"
  "fk_fanout_stats_table=${PROJECT}.${QUALITY_DATASET}.fk_fanout_stats"
  "rag_chunks_table=${PROJECT}.${RAG_DATASET}.rag_chunks"
  "build_rag_layer=true"
  "freetext_pools_table=${PROJECT}.${RAG_DATASET}.freetext_pools"
  "build_pool_layer=true"
  "source_stats_table=${PROJECT}.${RAG_DATASET}.source_table_stats"
  "relationships_uri=${RELATIONSHIPS_URI}"
  "num_rows=${ROWS}"
  "run_id=${RUN_ID}"
  "engine=b1_rag"
  "client_type=vllm"
  "model_uri=${MODEL_URI}"
  "embedder_uri=${EMBEDDER_URI}"
  "vllm_dtype=${VLLM_DTYPE}"
  "vllm_max_model_len=8192"
  "reference_rows_limit=10000"
  "create_if_not_exists=false"
  "write_disposition=overwrite"
  "env=dev"
)
# `^~^` switches gcloud's list delimiter from ',' to '~' (gcloud topic escaping).
JOINED="$(IFS='~'; echo "${PARAMS[*]}")"

gcloud dataflow flex-template run "sdfb-${RUN_ID}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --template-file-gcs-location="${TEMPLATE_PATH}" \
  --service-account-email="${SERVICE_ACCOUNT}" \
  --temp-location="${TEMP_LOCATION}" \
  --staging-location="${STAGING_LOCATION}" \
  --disable-public-ips \
  ${SUBNET_OPT[@]+"${SUBNET_OPT[@]}"} \
  --worker-machine-type="${MACHINE_TYPE}" \
  --num-workers=1 \
  --max-workers="${MAX_DATAFLOW_WORKERS}" \
  --additional-experiments="${EXPERIMENTS}" \
  --additional-user-labels="solution=synthetic-llm-dataflow-bigquery,run=${RUN_ID}" \
  --parameters="^~^${JOINED}"

echo "RUN_ID=${RUN_ID}"
echo "Follow: https://console.cloud.google.com/dataflow/jobs?project=${PROJECT}"
