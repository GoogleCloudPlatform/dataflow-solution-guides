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
"""Options class for the streaming inference pipeline."""

from argparse import ArgumentParser

from apache_beam.options.pipeline_options import PipelineOptions


class MyPipelineOptions(PipelineOptions):
  """Pipeline options for the Gemma ML inference streaming pipeline."""

  @classmethod
  def _add_argparse_args(cls, parser: ArgumentParser):
    parser.add_argument(
        "--messages_subscription",
        type=str,
        help="Pub/Sub subscription to ingest prompt messages from.",
    )
    parser.add_argument(
        "--model_path",
        type=str,
        default="google/gemma-4-E2B-it",
        help="Model preset identifier (e.g. google/gemma-4-E2B-it) or local path.",
    )
    parser.add_argument(
        "--responses_topic",
        type=str,
        help="Pub/Sub topic to publish generated predictions to.",
    )
