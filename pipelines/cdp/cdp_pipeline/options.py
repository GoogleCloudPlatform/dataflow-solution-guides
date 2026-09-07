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
"""Option class for Customer Data Platform pipeline."""

from argparse import ArgumentParser

from apache_beam.options.pipeline_options import PipelineOptions


class MyPipelineOptions(PipelineOptions):
  """Pipeline options for Customer Data Platform streaming sessionization."""

  @classmethod
  def _add_argparse_args(cls, parser: ArgumentParser):
    parser.add_argument(
        "--transactions_topic",
        type=str,
        default=None,
        help="Pub/Sub topic for streaming customer transaction events.",
    )
    parser.add_argument(
        "--transactions_subscription",
        type=str,
        default=None,
        help="Pub/Sub subscription for streaming customer transactions.",
    )
    parser.add_argument(
        "--coupons_redemption_topic",
        type=str,
        default=None,
        help="Pub/Sub topic for streaming coupon redemption events.",
    )
    parser.add_argument(
        "--coupons_redemption_subscription",
        type=str,
        default=None,
        help="Pub/Sub subscription for streaming coupon redemptions.",
    )
    parser.add_argument(
        "--project_id",
        type=str,
        default=None,
        help="Google Cloud Project ID (falls back to --project if omitted).",
    )
    parser.add_argument(
        "--output_dataset",
        type=str,
        default="cdp_dataset",
        help="Destination BigQuery dataset.",
    )
    parser.add_argument(
        "--output_table",
        type=str,
        default="unified_customer_data",
        help="Destination BigQuery table for granular unified transactions.",
    )
    parser.add_argument(
        "--output_sessions_table",
        type=str,
        default="customer_sessions",
        help="Destination BigQuery table for sessionized Customer 360 profiles.",
    )
    parser.add_argument(
        "--deadletter_table",
        type=str,
        default=None,
        help="Optional BigQuery table name for rejected or malformed records.",
    )
    parser.add_argument(
        "--session_gap_seconds",
        type=int,
        default=900,
        help="Session inactivity gap in seconds (default: 900s / 15m).",
    )
    parser.add_argument(
        "--allowed_lateness_seconds",
        type=int,
        default=60,
        help="Allowed lateness in seconds for late-arriving events.",
    )
    parser.add_argument(
        "--use_storage_write_api",
        action="store_true",
        default=True,
        help="Use BigQuery Storage Write API for high-throughput streaming.",
    )
    parser.add_argument(
        "--output_schema_path",
        type=str,
        default=None,
        help="Optional path to custom JSON schema file for unified output table.",
    )
    parser.add_argument(
        "--output_sessions_schema_path",
        type=str,
        default=None,
        help="Optional path to custom JSON schema file for session profiles.",
    )
    parser.add_argument(
        "--deadletter_schema_path",
        type=str,
        default=None,
        help="Optional path to custom JSON schema file for deadletter table.",
    )
