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
"""Custom model handlers to be used with RunInference."""

import os
from typing import Any, Iterable, Optional, Sequence

# Set JAX as the Keras backend before importing Keras/KerasHub
os.environ.setdefault("KERAS_BACKEND", "jax")

from apache_beam.ml.inference.base import ModelHandler, PredictionResult
import keras_hub


class GemmaModelHandler(ModelHandler[str, PredictionResult, Any]):
  """A RunInference model handler for Gemma models using Keras 3 with JAX backend."""

  def __init__(self,
               model_name: str = "gemma4_instruct_2b",
               max_length: int = 128):
    """Implementation of the ModelHandler interface for Gemma using text as input.

    Example Usage::

      pcoll | RunInference(GemmaModelHandler())

    Args:
      model_name: The Gemma model preset or path. Default is gemma4_instruct_2b.
      max_length: The maximum sequence length to generate. Default is 128.
    """
    super().__init__()
    self._model_name = model_name
    self._max_length = max_length
    self._env_vars = {}

  def share_model_across_processes(self) -> bool:
    """Indicates if the model should be loaded once-per-VM rather than

    once-per-worker-process on a VM. Because Gemma is a large language model,
    this will always return True to optimize GPU memory usage.
    """
    return True

  def load_model(self) -> Any:
    """Loads and initializes the Gemma model using KerasHub with JAX backend."""
    # Check if the model is baked into local container path /opt/models/...
    target_path = self._model_name
    baked_candidate = f"/opt/models/{self._model_name}"
    env_preset = os.environ.get("MODEL_PRESET", "")
    env_candidate = f"/opt/models/{env_preset}" if env_preset else None

    if os.path.isdir(self._model_name):
      target_path = self._model_name
    elif os.path.isdir(baked_candidate):
      target_path = baked_candidate
    elif env_candidate and os.path.isdir(env_candidate):
      target_path = env_candidate
    elif os.path.isdir("/opt/models/gemma"):
      target_path = "/opt/models/gemma"

    print(f"Loading Gemma model from: {target_path}")
    model_name_lower = self._model_name.lower()
    if hasattr(keras_hub.models, "Gemma4CausalLM") and "gemma4" in model_name_lower:
      return keras_hub.models.Gemma4CausalLM.from_preset(target_path)
    if hasattr(keras_hub.models, "Gemma3CausalLM") and "gemma3" in model_name_lower:
      return keras_hub.models.Gemma3CausalLM.from_preset(target_path)
    return keras_hub.models.GemmaCausalLM.from_preset(target_path)

  def run_inference(
      self,
      batch: Sequence[str],
      model: Any,
      unused: Optional[dict[str, Any]] = None) -> Iterable[PredictionResult]:
    """Runs inferences on a batch of text strings.

    Args:
      batch: A sequence of examples as text strings.
      model: The Gemma model being used.
      unused: Optional additional arguments for interface compatibility.

    Returns:
      An Iterable of type PredictionResult.
    """
    _ = unused  # for interface compatibility with Model Handler
    for one_text in batch:
      result = model.generate(one_text, max_length=self._max_length)
      yield PredictionResult(one_text, result, self._model_name)
