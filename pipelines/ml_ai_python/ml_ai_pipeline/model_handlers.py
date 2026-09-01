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
"""Model handlers leveraging Apache Beam built-in vllm_inference."""

import os
from typing import Optional

from apache_beam.ml.inference.vllm_inference import VLLMCompletionsModelHandler


def get_model_path(model_name: Optional[str] = None) -> str:
  """Resolves the model path to baked local weights or model preset."""
  target_path = model_name or os.environ.get("MODEL_PRESET", "google/gemma-4-E2B-it")
  baked_candidate = f"/opt/models/{target_path}"
  env_preset = os.environ.get("MODEL_PRESET", "")
  env_candidate = f"/opt/models/{env_preset}" if env_preset else None

  def is_valid_model_dir(path: Optional[str]) -> bool:
    if not path or not os.path.isdir(path):
      return False
    return (os.path.isfile(os.path.join(path, "config.json")) or
            os.path.isfile(os.path.join(path, "model.safetensors")) or
            os.path.isfile(os.path.join(path, "model.weights.h5")))

  if is_valid_model_dir("/opt/models/gemma"):
    return "/opt/models/gemma"
  if is_valid_model_dir(target_path):
    return target_path
  if is_valid_model_dir(baked_candidate):
    return baked_candidate
  if is_valid_model_dir(env_candidate):
    return env_candidate
  return target_path


def create_vllm_model_handler(
    model_name: Optional[str] = None,
    gpu_memory_utilization: float = 0.85,
) -> VLLMCompletionsModelHandler:
  """Creates a VLLMCompletionsModelHandler using Beam's built-in vllm_inference."""
  resolved_path = get_model_path(model_name)
  vllm_server_kwargs = {
      "gpu-memory-utilization": str(gpu_memory_utilization),
      "trust-remote-code": None,
      "dtype": "bfloat16",
      "max-model-len": "8192",
  }
  return VLLMCompletionsModelHandler(
      model_name=resolved_path,
      vllm_server_kwargs=vllm_server_kwargs,
  )


# Alias for backward compatibility
GemmaModelHandler = create_vllm_model_handler
