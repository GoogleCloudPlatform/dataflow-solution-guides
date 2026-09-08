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

# Runs the Customer Data Platform pipeline locally with DirectRunner for testing and development

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$SCRIPT_DIR/00_set_environment.sh" ]; then
  # shellcheck source=/dev/null
  source "$SCRIPT_DIR/00_set_environment.sh"
fi

cd "$PIPELINE_DIR"

python3 -m main \
  --runner=DirectRunner \
  --project="${PROJECT:-local-test-project}" \
  --temp_location=/tmp/dataflow-temp \
  --transactions_topic="${TRANSACTIONS_TOPIC:-projects/local-test-project/topics/cdp-transactions}" \
  --coupons_redemption_topic="${COUPON_REDEMPTION_TOPIC:-projects/local-test-project/topics/cdp-coupon-redemption}" \
  --output_dataset="${BQ_DATASET:-cdp_dataset}" \
  --output_table="${BQ_UNIFIED_TABLE:-unified_customer_data}" \
  --output_sessions_table="${BQ_SESSIONS_TABLE:-customer_sessions}" \
  --session_gap_seconds=10 \
  --allowed_lateness_seconds=5
