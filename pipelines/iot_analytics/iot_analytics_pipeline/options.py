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
Options class for the IoT Analytics pipeline.
"""

from argparse import ArgumentParser
from apache_beam.options.pipeline_options import PipelineOptions


class MyPipelineOptions(PipelineOptions):
  """
    Options class for the IoT Analytics pipeline.
    """

  @classmethod
  def _add_argparse_args(cls, parser: ArgumentParser):
    parser.add_argument(
        '--topic',
        dest='topic',
        help='Pub/sub topic name :"projects/your_project_id/topics/topic_name"')
    parser.add_argument(
        '--project_id', dest='project', help='Your Google Cloud project ID')
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
        '--subscription',
        dest='subscription',
        default=None,
        help='Pub/sub subscription name :"projects/your_project_id/subscriptions/sub_name"'
    )
    parser.add_argument(
        '--alert_topic',
        dest='alert_topic',
        default=None,
        help='Pub/sub topic name to publish maintenance alerts to')
    parser.add_argument(
        '--model_path',
        dest='model_path',
        default='maintenance_model.pkl',
        help='Path to serialized Scikit-Learn model artifact (local path or GCS URI)'
    )
    parser.add_argument(
        '--window_size_seconds',
        dest='window_size_seconds',
        type=int,
        default=60,
        help='Window size in seconds for vehicle telemetry aggregation')
    parser.add_argument(
        '--row_key',
        dest='row_key',
        default='vehicle_id',
        help='Enter BigTable row key')
