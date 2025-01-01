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
Custom model handlers to be used with RunInference.
"""

from typing import Sequence, Optional, Any, Iterable

import keras_nlp
from apache_beam.ml.inference.base import ModelHandler, PredictionResult
from keras_nlp.src.models import GemmaCausalLM


class GemmaModelHandler(ModelHandler[str, PredictionResult, GemmaCausalLM]):
  """
  A RunInference model handler for the Gemma model.
  """

  def __init__(self, model_name: str = "gemma_2B"):
    """ Implementation of the ModelHandler interface for Gemma using text as input.

    Example Usage::

      pcoll | RunInference(GemmaModelHandler())

    Args:
      model_name: The Gemma model name. Default is gemma_2B.
    """
    super().__init__()
    self._model_name = model_name
    self._env_vars = {}

  def share_model_across_processes(self) -> bool:
    """ Indicates if the model should be loaded once-per-VM rather than
    once-per-worker-process on a VM. Because Gemma is a large language model,
    this will always return True to avoid OOM errors.
    """
    return True

  def load_model(self) -> GemmaCausalLM:
    """Loads and initializes a model for processing."""
    return keras_nlp.models.GemmaCausalLM.from_preset(self._model_name)

  def run_inference(
      self,
      batch: Sequence[str],
      model: GemmaCausalLM,
      unused: Optional[dict[str, Any]] = None) -> Iterable[PredictionResult]:
    """Runs inferences on a batch of text strings.

    Args:
      batch: A sequence of examples as text strings.
      model: The Gemma model being used.

    Returns:
      An Iterable of type PredictionResult.
    """
    _ = unused  # for interface compatibility with Model Handler
    # Loop each text string, and use a tuple to store the inference results.
    for one_text in batch:
      result = model.generate(one_text, max_length=64)
      yield PredictionResult(one_text, result, self._model_name)
