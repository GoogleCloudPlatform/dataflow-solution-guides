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

# Score what a 04_run_dataflow.sh run landed: launch the evaluator as a
# second, CPU-only Dataflow job from the SAME Flex Template and image
# (03_build_flex_template.sh); sdfb_job=evaluation selects it.
#
# The evaluation is named like the generation: the table 04 targets
# (order_items) and config/relationships/gcp_public_fk_example.yaml, so it
# covers users, orders and order_items. Each landed table is read whole
# (scope=manual) and compared with the table of the same name in the source
# dataset. The generation job's id gives the launch's window: the evaluator
# reads that Dataflow job (nothing else about it, never its log) and this
# launch's validation_runs rows, whose reference digest lets it rebuild the
# 10000-row reference sample 04 passes, for the privacy metrics.
#
# Results land in the dataset's evaluation_* tables, with the views
# evaluation_latest and evaluation_latest_per_job (Terraform creates them).
# How to read them: packages/sdfb-evaluation/README.md.
#
# Usage: ./scripts/06_run_evaluation.sh RUN_ID [GENERATION_JOB_ID]
# RUN_ID is what 04 printed. Without GENERATION_JOB_ID the generation job is
# found by its name, sdfb-RUN_ID; it must be done. EVALUATION_MACHINE_TYPE
# (default e2-standard-8) and EVALUATION_MAX_WORKERS (default 2) size the
# CPU workers.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -n "${PROJECT:-}" ]] || source "${SCRIPT_DIR}/00_set_variables.sh"

RUN_ID="${1:?usage: 06_run_evaluation.sh RUN_ID [GENERATION_JOB_ID]}"
JOB_ID="${2:-}"
EVALUATION_MACHINE_TYPE="${EVALUATION_MACHINE_TYPE:-e2-standard-8}"
EVALUATION_MAX_WORKERS="${EVALUATION_MAX_WORKERS:-2}"

if [[ -z "${JOB_ID}" ]]; then
  JOB_ID="$(gcloud dataflow jobs list --project="${PROJECT}" --region="${REGION}" \
    --status=all --filter="name=sdfb-${RUN_ID}" --format="value(id)" --limit=1)"
  if [[ -z "${JOB_ID}" ]]; then
    echo "no Dataflow job named sdfb-${RUN_ID} in ${REGION}; pass its id as the second argument" >&2
    exit 1
  fi
fi

# The landed tables are evaluated, so the generation must be over.
STATE="$(gcloud dataflow jobs describe "${JOB_ID}" --project="${PROJECT}" \
  --region="${REGION}" --format="value(currentState)")"
if [[ "${STATE}" != "JOB_STATE_DONE" ]]; then
  echo "generation job ${JOB_ID} is ${STATE:-unknown}: evaluate it once it is JOB_STATE_DONE" >&2
  exit 1
fi

SUBNET_OPT=()
if [[ -n "${SUBNETWORK:-}" ]]; then
  SUBNET_OPT=(--subnetwork="${SUBNETWORK}")
fi

# Parameter names are the template's (docker/flex_template_metadata.json);
# the launcher supplies runner, project and region, and the image names its
# own worker image.
PARAMS=(
  "sdfb_job=evaluation"
  "generation_job_id=${JOB_ID}"
  "seed_table=order_items"
  "relationships_uri=${RELATIONSHIPS_URI}"
  "landing_dataset=${PROJECT}.${LANDING_DATASET}"
  "reference_dataset=${PROJECT}.${SOURCE_DATASET}"
  "scope=manual"
  "reference_rows_limit=10000"
  "validation_runs_table=${PROJECT}.${QUALITY_DATASET}.validation_runs"
  "output_dataset=${QUALITY_DATASET}"
  "trigger=cli"
  "disk_size_gb=200"
)
# `^~^` switches gcloud's list delimiter from ',' to '~' (gcloud topic escaping).
JOINED="$(IFS='~'; echo "${PARAMS[*]}")"

# disk_size_gb: the workers unpack the same multi-GB image as the GPU workers.
# The launcher VM pulls that image and plans the evaluation inside the
# 12 minutes Dataflow gives a Flex Template launch, hence a larger one.
gcloud dataflow flex-template run "sdfb-eval-${RUN_ID}-$(date -u +%H%M%S)" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --template-file-gcs-location="${TEMPLATE_PATH}" \
  --service-account-email="${SERVICE_ACCOUNT}" \
  --temp-location="${TEMP_LOCATION}" \
  --staging-location="${STAGING_LOCATION}" \
  --disable-public-ips \
  ${SUBNET_OPT[@]+"${SUBNET_OPT[@]}"} \
  --launcher-machine-type=e2-standard-8 \
  --worker-machine-type="${EVALUATION_MACHINE_TYPE}" \
  --max-workers="${EVALUATION_MAX_WORKERS}" \
  --additional-experiments=use_runner_v2 \
  --additional-user-labels="solution=synthetic-llm-dataflow-bigquery,run=${RUN_ID},job=evaluation" \
  --parameters="^~^${JOINED}"

echo "Evaluating generation job ${JOB_ID} (RUN_ID=${RUN_ID})"
echo "Results, once the evaluation job is done: ${PROJECT}.${QUALITY_DATASET}.evaluation_latest_per_job, generation_job_id = '${JOB_ID}'"
echo "Follow: https://console.cloud.google.com/dataflow/jobs?project=${PROJECT}"
