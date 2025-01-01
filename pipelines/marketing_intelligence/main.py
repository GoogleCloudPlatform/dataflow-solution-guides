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
A Product predicter example for the Dataflow Solution Guides.
"""

import time

from apache_beam.options.pipeline_options import PipelineOptions, GoogleCloudOptions

from marketing_intelligence_pipeline.options import MyPipelineOptions
from marketing_intelligence_pipeline.pipeline import create_pipeline


def main(options: MyPipelineOptions):
  pipeline = create_pipeline(options)
  pipeline.run()


if __name__ == "__main__":
  pipeline_options: PipelineOptions = PipelineOptions()
  dataflow_options: GoogleCloudOptions = pipeline_options.view_as(
      GoogleCloudOptions)
  now_epoch_ms = int(time.time() * 1000)
  dataflow_options.job_name = f"marketing-intelligence-pipeline-{now_epoch_ms}"
  custom_options: MyPipelineOptions = pipeline_options.view_as(
      MyPipelineOptions)
  main(custom_options)
