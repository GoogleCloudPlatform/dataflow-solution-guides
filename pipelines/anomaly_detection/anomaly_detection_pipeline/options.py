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
Options class for the Anomaly Detection pipeline.
"""

from argparse import ArgumentParser

from apache_beam.options.pipeline_options import PipelineOptions


class MyPipelineOptions(PipelineOptions):

  @classmethod
  def _add_argparse_args(cls, parser: ArgumentParser):
    parser.add_argument("--messages_subscription", type=str)
    parser.add_argument("--model_endpoint", type=str)
    parser.add_argument("--project", type=str)
    parser.add_argument("--location", type=str)
    parser.add_argument("--responses_topic", type=str)
