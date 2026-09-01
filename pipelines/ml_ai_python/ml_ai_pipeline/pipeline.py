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
"""A machine learning streaming inference pipeline for Gemma on Dataflow."""

import apache_beam as beam
from apache_beam import PCollection, Pipeline
from apache_beam.io.gcp import pubsub
from apache_beam.ml.inference import RunInference
from apache_beam.ml.inference.base import PredictionResult

from .model_handlers import create_vllm_model_handler
from .options import MyPipelineOptions


def _format_output(element: PredictionResult) -> str:
  output_text = ""
  if hasattr(element.inference, "choices") and element.inference.choices:
    output_text = element.inference.choices[0].text
  elif isinstance(element.inference, str):
    output_text = element.inference
  else:
    output_text = str(element.inference)
  return f"Input: \n{element.example}, \n\n\nOutput: \n{output_text.strip()}"


def _format_prompt(prompt: str) -> str:
  if "<|turn>" in prompt or "<start_of_turn>" in prompt:
    return prompt
  return f"<|turn>user\n{prompt}<turn|>\n<|turn>model\n"


@beam.ptransform_fn
def _extract(p: Pipeline, subscription: str) -> PCollection[str]:
  msgs: PCollection[bytes] = p | "Read subscription" >> beam.io.ReadFromPubSub(
      subscription=subscription)
  return msgs | "Parse" >> beam.Map(lambda x: x.decode("utf-8"))


@beam.ptransform_fn
def _transform(msgs: PCollection[str], model_path: str) -> PCollection[str]:
  formatted_msgs = msgs | "Format Prompt" >> beam.Map(_format_prompt)
  preds: PCollection[PredictionResult] = (
      formatted_msgs
      | "RunInference-vLLM" >> RunInference(
          create_vllm_model_handler(model_name=model_path),
          inference_args={"max_tokens": 128, "temperature": 0.0},
      )
  )
  return preds | "Format Output" >> beam.Map(_format_output)


def create_pipeline(options: MyPipelineOptions) -> Pipeline:
  """Create the pipeline object.

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
