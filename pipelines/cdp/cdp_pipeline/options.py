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
"""
Option class for Customer Data Platform pipeline.
"""

from argparse import ArgumentParser

from apache_beam.options.pipeline_options import PipelineOptions


class MyPipelineOptions(PipelineOptions):

  @classmethod
  def _add_argparse_args(cls, parser: ArgumentParser):
    parser.add_argument("--transactions_topic", type=str)
    parser.add_argument("--coupons_redemption_topic", type=str)
    parser.add_argument("--project_id", type=str)
    parser.add_argument("--location", type=str)
    parser.add_argument("--output_dataset", type=str)
    parser.add_argument("--output_table", type=str)
