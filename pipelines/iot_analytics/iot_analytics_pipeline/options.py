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
Options class for the Marketing Intelligence pipeline.
"""

from argparse import ArgumentParser
from apache_beam.options.pipeline_options import PipelineOptions

class MyPipelineOptions(PipelineOptions):

  @classmethod
  def _add_argparse_args(cls, parser: ArgumentParser):
    parser.add_argument(
        '--subscription',
        dest='subscription',
        help='Provide pub/sub subscription name - "projects/your_project_id/subscriptions/subscription"'
    )
    parser.add_argument(
        '--project_id',
        dest='project',
        help='Your Google Cloud project ID'
    )
    parser.add_argument(
        '--dataset', dest='dataset', help='Enter BigQuery Dataset Id')
    parser.add_argument('--table', dest='table', help='Enter BigQuery Table Id')
    parser.add_argument(
        '--bigtable_instance_id',
        dest='bigtable_instance_id',
        help='Enter BigTable Instance Id')
    parser.add_argument(
        '--bigtable_table_id',
        dest='bigtable_table_id',
        help='Enter BigTable Table Id')
    parser.add_argument(
        '--row_key', dest='row_key', help='Enter BigTable row key')