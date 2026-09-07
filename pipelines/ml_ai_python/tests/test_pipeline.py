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
"""Unit tests for Gemma ML streaming inference pipeline transforms and options."""

from types import SimpleNamespace
import unittest

from apache_beam.ml.inference.base import PredictionResult

from ml_ai_pipeline.options import MyPipelineOptions
from ml_ai_pipeline.pipeline import _format_output, _format_prompt


class PipelineTransformsTest(unittest.TestCase):
  """Unit tests for prompt formatting and output formatting transforms."""

  def test_format_prompt_plain_text(self):
    prompt = "Tell me about Apache Beam on Google Cloud Dataflow."
    formatted = _format_prompt(prompt)
    expected = ("<|turn>user\n"
                "Tell me about Apache Beam on Google Cloud Dataflow.<turn|>\n"
                "<|turn>model\n")
    self.assertEqual(formatted, expected)

  def test_format_prompt_already_formatted_turn(self):
    prompt = "<|turn>user\nExisting prompt<turn|>\n<|turn>model\n"
    formatted = _format_prompt(prompt)
    self.assertEqual(formatted, prompt)

  def test_format_prompt_already_formatted_start_of_turn(self):
    prompt = "<start_of_turn>user\nExisting prompt<end_of_turn>"
    formatted = _format_prompt(prompt)
    self.assertEqual(formatted, prompt)

  def test_format_output_string_inference(self):
    result = PredictionResult(example="What is 2+2?", inference="2+2 is 4.")
    formatted = _format_output(result)
    expected = "Input: \nWhat is 2+2?, \n\n\nOutput: \n2+2 is 4."
    self.assertEqual(formatted, expected)

  def test_format_output_with_choices_object(self):
    mock_choices = SimpleNamespace(
        choices=[SimpleNamespace(text="Choice response text ")])
    result = PredictionResult(example="Sample question", inference=mock_choices)
    formatted = _format_output(result)
    expected = (
        "Input: \nSample question, \n\n\nOutput: \nChoice response text")
    self.assertEqual(formatted, expected)


class PipelineOptionsTest(unittest.TestCase):
  """Unit tests for custom pipeline option parsing and defaults."""

  def test_options_defaults(self):
    options = MyPipelineOptions([])
    self.assertEqual(options.model_path, "google/gemma-4-E2B-it")

  def test_options_custom_arguments(self):
    flags = [
        "--messages_subscription=projects/test-p/subscriptions/sub-test",
        "--model_path=custom-local-path",
        "--responses_topic=projects/test-p/topics/top-test",
    ]
    options = MyPipelineOptions(flags)
    self.assertEqual(
        options.messages_subscription,
        "projects/test-p/subscriptions/sub-test",
    )
    self.assertEqual(options.model_path, "custom-local-path")
    self.assertEqual(options.responses_topic, "projects/test-p/topics/top-test")


if __name__ == "__main__":
  unittest.main()
