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
"""Anomaly detection options; project is provided by GoogleCloudOptions."""
from apache_beam.options.pipeline_options import PipelineOptions


class MyPipelineOptions(PipelineOptions):

  @classmethod
  def _add_argparse_args(cls, parser):
    for name in ('messages_subscription', 'model_endpoint', 'location',
                 'responses_topic', 'error_topic', 'bigtable_instance',
                 'bigquery_table'):
      parser.add_argument('--' + name, required=True)
    parser.add_argument('--bigtable_table', default='customer_profiles')
    parser.add_argument('--bigtable_column_family', default='profile')
