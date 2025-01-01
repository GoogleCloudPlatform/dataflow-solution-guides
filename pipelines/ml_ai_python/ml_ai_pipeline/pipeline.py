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
A machine learning streaming inference pipeline for the Dataflow Solution Guides.
"""

from apache_beam import Pipeline, PCollection
from apache_beam.ml.inference import RunInference
from apache_beam.io.gcp import pubsub

import apache_beam as beam
from apache_beam.ml.inference.base import PredictionResult

from .model_handlers import GemmaModelHandler
from .options import MyPipelineOptions


def _format_output(element: PredictionResult) -> str:
  return f"Input: \n{element.example}, \n\n\nOutput: \n{element.inference}"


@beam.ptransform_fn
def _extract(p: Pipeline, subscription: str) -> PCollection[str]:
  msgs: PCollection[bytes] = p | "Read subscription" >> beam.io.ReadFromPubSub(
      subscription=subscription)
  return msgs | "Parse" >> beam.Map(lambda x: x.decode("utf-8"))


@beam.ptransform_fn
def _transform(msgs: PCollection[str], model_path: str) -> PCollection[str]:
  preds: PCollection[
      PredictionResult] = msgs | "RunInference-Gemma" >> RunInference(
          GemmaModelHandler(model_path))
  return preds | "Format Output" >> beam.Map(_format_output)


def create_pipeline(options: MyPipelineOptions) -> Pipeline:
  """ Create the pipeline object.

  Args:
    options: The pipeline options, with type `MyPipelineOptions`.

  Returns:
    The pipeline object.
    """
  pipeline = beam.Pipeline(options=options)
  # Extract
  msgs: PCollection[str] = pipeline | "Read" >> _extract(
      subscription=options.messages_subscription)
  # Transform
  responses: PCollection[str] = msgs | "Transform" >> _transform(
      model_path=options.model_path)
  # Load
  responses | "Publish Result" >> pubsub.WriteStringsToPubSub(
      topic=options.responses_topic)

  return pipeline
