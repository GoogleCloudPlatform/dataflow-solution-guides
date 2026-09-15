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

# Check a finished run in BigQuery: row counts, primary-key duplicates and
# foreign-key orphans per edge of the thelook model, then the per-table
# verdicts the pipeline wrote to validation_runs.
#
# Usage: ./scripts/05_verify_run.sh RUN_ID
# Exit code 1 when any PK duplicate or FK orphan is found.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -n "${PROJECT:-}" ]] || source "${SCRIPT_DIR}/00_set_variables.sh"

RUN_ID="${1:?usage: 05_verify_run.sh RUN_ID}"
L="\`${PROJECT}.${LANDING_DATASET}"

bq_query() {
  bq query --project_id="${PROJECT}" --location="${BQ_LOCATION}" \
    --use_legacy_sql=false --format=pretty "$@"
}

CHECKS="
SELECT 'users rows' AS check_name, COUNT(*) AS value FROM ${L}.users\`
UNION ALL SELECT 'orders rows', COUNT(*) FROM ${L}.orders\`
UNION ALL SELECT 'order_items rows', COUNT(*) FROM ${L}.order_items\`
UNION ALL SELECT 'users pk duplicates', COUNT(*) - COUNT(DISTINCT id) FROM ${L}.users\`
UNION ALL SELECT 'orders pk duplicates', COUNT(*) - COUNT(DISTINCT order_id) FROM ${L}.orders\`
UNION ALL SELECT 'order_items pk duplicates', COUNT(*) - COUNT(DISTINCT id) FROM ${L}.order_items\`
UNION ALL SELECT 'orders -> users orphans', COUNT(*)
  FROM ${L}.orders\` o LEFT JOIN ${L}.users\` u ON o.user_id = u.id WHERE u.id IS NULL
UNION ALL SELECT 'order_items -> orders orphans', COUNT(*)
  FROM ${L}.order_items\` i LEFT JOIN ${L}.orders\` o
    ON i.order_id = o.order_id AND i.user_id = o.user_id WHERE o.order_id IS NULL
UNION ALL SELECT 'order_items -> products orphans', COUNT(*)
  FROM ${L}.order_items\` i LEFT JOIN ${L}.products\` p ON i.product_id = p.id WHERE p.id IS NULL
"

bq_query "${CHECKS}"

VIOLATIONS="$(bq query --project_id="${PROJECT}" --location="${BQ_LOCATION}" \
  --use_legacy_sql=false --format=csv \
  "SELECT SUM(value) FROM (${CHECKS}) WHERE check_name NOT LIKE '% rows'" | tail -n 1)"

bq_query --parameter="run:STRING:${RUN_ID}%" \
  "SELECT landing_table, status, valid_count, dlq_count, blocker_count, dlq_by_rule
   FROM \`${PROJECT}.${QUALITY_DATASET}.validation_runs\`
   WHERE run_id LIKE @run ORDER BY created_at"

if [[ "${VIOLATIONS}" != "0" ]]; then
  echo "FAILED: ${VIOLATIONS} PK duplicate(s) or FK orphan(s)" >&2
  exit 1
fi
echo "OK: no PK duplicates, no FK orphans"
