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
Pipeline of the Marketing Intelligence Dataflow Solution guide.
"""

from apache_beam import Pipeline, PCollection
from apache_beam.ml.inference import RunInference
from apache_beam.io.gcp import pubsub
import json
import apache_beam as beam
from apache_beam.ml.inference.base import PredictionResult
from apache_beam.ml.inference.vertex_ai_inference import VertexAIModelHandlerJSON
from .options import MyPipelineOptions


#  Format the predictions sent by the Vertex AI Endpoint
def _format_output(element: PredictionResult) -> str:
  return f"Input: \n{element.example}, \n\n\nOutput: \n{element.infernece}"


# Format the input and send each input as a dictionary
def _format_input(x: bytes) -> dict:
  instance_dict = json.loads(x.decode("utf-8"))
  return instance_dict


# Read input from Pub/Sub (all input data to be sent in String) and format it
@beam.ptransform_fn
def _extract(p: Pipeline, subscription: str) -> PCollection[str]:
  msgs: PCollection[bytes] = p | "Read subscription" >> beam.io.ReadFromPubSub(
      subscription=subscription)
  return msgs | "Parse and format Input" >> beam.Map(_format_input)


# TODO Add transformation for BigTable Enrichment


# Request predictions from the Vertex AI endpoint by sending the formatted input
@beam.ptransform_fn
def _transform(msgs: PCollection[str], model_endpoint: str, project: str,
               location: str) -> PCollection[str]:
  model_handler = VertexAIModelHandlerJSON(
      endpoint_id=model_endpoint, project=project, location=location)
  preds: PCollection[
      PredictionResult] = msgs | "RunInference-vertexai" >> RunInference(
          model_handler)
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
  messages: PCollection[str] = pipeline | "Read" >> _extract(
      subscription=options.messages_subscription)
  # Transform
  predictions: PCollection[str] = messages | "Transform" >> _transform(
      model_endpoint=options.model_endpoint,
      project=options.project_id,
      location=options.location)
  # Load
  predictions | "Publish Result" >> pubsub.WriteStringsToPubSub(
      topic=options.responses_topic)

  return pipeline
