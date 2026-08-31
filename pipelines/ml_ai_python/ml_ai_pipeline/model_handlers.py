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

from apache_beam.ml.inference.base import ModelHandler, PredictionResult

os.environ.setdefault("VLLM_CONFIGURE_LOGGING", "0")
os.environ.setdefault("VLLM_USE_V1", "0")
os.environ.setdefault("FLASHINFER_ENABLE_JIT", "0")
try:
  import vllm
  from vllm import LLM, SamplingParams
except ModuleNotFoundError:
  vllm = None
  LLM = None
  SamplingParams = None


class GemmaModelHandler(ModelHandler[str, PredictionResult, Any]):
  """A RunInference model handler for Gemma models using vLLM."""

  def __init__(self,
               model_name: str = "google/gemma-4-2b-it",
               max_length: int = 128,
               gpu_memory_utilization: float = 0.85):
    """Implementation of the ModelHandler interface for Gemma using vLLM.

    Args:
      model_name: The Gemma model repo or local path. Default is google/gemma-4-2b-it.
      max_length: The maximum tokens to generate. Default is 128.
      gpu_memory_utilization: The fraction of GPU memory to reserve for vLLM.
    """
    super().__init__()
    self._model_name = model_name
    self._max_length = max_length
    self._gpu_memory_utilization = gpu_memory_utilization
    self._env_vars = {}

  def share_model_across_processes(self) -> bool:
    """Indicates if the model should be loaded once-per-VM rather than

    once-per-worker-process on a VM.
    """
    return True

  def load_model(self) -> Any:
    """Loads and initializes the Gemma model using vLLM."""
    target_path = self._model_name
    baked_candidate = f"/opt/models/{self._model_name}"
    env_preset = os.environ.get("MODEL_PRESET", "")
    env_candidate = f"/opt/models/{env_preset}" if env_preset else None

    def is_valid_model_dir(path: Optional[str]) -> bool:
      if not path or not os.path.isdir(path):
        return False
      return (os.path.isfile(os.path.join(path, "config.json")) or
              os.path.isfile(os.path.join(path, "model.safetensors")) or
              os.path.isfile(os.path.join(path, "model.weights.h5")))

    if is_valid_model_dir("/opt/models/gemma"):
      target_path = "/opt/models/gemma"
    elif is_valid_model_dir(self._model_name):
      target_path = self._model_name
    elif is_valid_model_dir(baked_candidate):
      target_path = baked_candidate
    elif is_valid_model_dir(env_candidate):
      target_path = env_candidate

    print(f"Loading Gemma vLLM model from: {target_path}")
    sampling_params = SamplingParams(
        max_tokens=self._max_length,
        temperature=0.7,
        top_p=0.95,
    )
    llm = LLM(
        model=target_path,
        gpu_memory_utilization=self._gpu_memory_utilization,
        trust_remote_code=True,
        dtype="bfloat16",
        enforce_eager=False,
    )
    return (llm, sampling_params)

  def run_inference(
      self,
      batch: Sequence[str],
      model_obj: Any,
      unused: Optional[dict[str, Any]] = None) -> Iterable[PredictionResult]:
    """Runs inferences on a batch of text strings.

    Args:
      batch: A sequence of prompt strings.
      model_obj: A tuple of (LLM, SamplingParams).
      unused: Optional additional arguments for interface compatibility.

    Returns:
      An Iterable of type PredictionResult.
    """
    _ = unused
    llm, sampling_params = model_obj
    prompts = list(batch)
    outputs = llm.generate(prompts, sampling_params)
    for prompt, output in zip(prompts, outputs):
      generated_text = output.outputs[0].text if output.outputs else ""
      yield PredictionResult(prompt, generated_text, self._model_name)
