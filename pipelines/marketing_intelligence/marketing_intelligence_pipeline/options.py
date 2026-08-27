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
"""
Options class for the Marketing Intelligence pipeline.
"""

from argparse import ArgumentParser

from apache_beam.options.pipeline_options import PipelineOptions


class MyPipelineOptions(PipelineOptions):
  """Custom pipeline options for Marketing Intelligence streaming inference."""

  @classmethod
  def _add_argparse_args(cls, parser: ArgumentParser):
    parser.add_argument(
        "--messages_subscription",
        type=str,
        default=None,
        help="Pub/Sub subscription to read input streaming events from.",
    )
    parser.add_argument(
        "--input_topic",
        type=str,
        default=None,
        help="Pub/Sub topic to read input streaming events from.",
    )
    parser.add_argument(
        "--responses_topic",
        type=str,
        default=None,
        help="Pub/Sub topic to publish high-propensity activation offers to.",
    )
    parser.add_argument(
        "--firestore_project",
        type=str,
        default=None,
        help="GCP Project ID for Firestore (defaults to project_id).",
    )
    parser.add_argument(
        "--firestore_collection",
        type=str,
        default="customer_profiles",
        help="Firestore collection name for customer profiles.",
    )
    parser.add_argument(
        "--model_path",
        type=str,
        default="marketing_model.pkl",
        help="Path or URI to the serialized Scikit-Learn model artifact.",
    )
    parser.add_argument(
        "--bq_dataset",
        type=str,
        default="output_dataset",
        help="BigQuery dataset for storing predictions.",
    )
    parser.add_argument(
        "--bq_table",
        type=str,
        default="predictions",
        help="BigQuery table name for storing predictions.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.80,
        help="Propensity threshold for triggering instant marketing activation.",
    )
    parser.add_argument(
        "--project_id",
        type=str,
        default=None,
        help="GCP Project ID.",
    )
    parser.add_argument(
        "--location",
        type=str,
        default=None,
        help="GCP Region/Location.",
    )
