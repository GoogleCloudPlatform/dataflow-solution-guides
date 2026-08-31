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
"""Pre-downloads and bakes the Gemma model weights into the container image."""

import os
import sys

# Set JAX backend
os.environ.setdefault("KERAS_BACKEND", "jax")

import keras_hub


def download_and_save_model(model_name: str, output_dir: str) -> None:
  """Downloads the model preset and saves it locally for offline container usage."""
  print(f"Downloading model preset '{model_name}' to '{output_dir}'...")
  model_name_lower = model_name.lower()
  if hasattr(keras_hub.models, "Gemma4CausalLM") and "gemma4" in model_name_lower:
    model = keras_hub.models.Gemma4CausalLM.from_preset(model_name)
  elif hasattr(keras_hub.models, "Gemma3CausalLM") and "gemma3" in model_name_lower:
    model = keras_hub.models.Gemma3CausalLM.from_preset(model_name)
  else:
    model = keras_hub.models.GemmaCausalLM.from_preset(model_name)

  os.makedirs(output_dir, exist_ok=True)
  model.save_to_preset(output_dir)
  print(f"Successfully baked model weights into {output_dir}")


def main():
  preset = os.environ.get("MODEL_PRESET", "gemma4_instruct_2b")
  target_dir = sys.argv[1] if len(sys.argv) > 1 else f"/opt/models/{preset}"
  download_and_save_model(preset, target_dir)


if __name__ == "__main__":
  main()
