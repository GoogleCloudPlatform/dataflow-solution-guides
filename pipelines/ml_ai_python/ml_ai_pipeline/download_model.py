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
import shutil
import sys


def download_and_save_model(model_name: str, output_dir: str) -> None:
  """Downloads the model preset and saves it locally for offline container usage."""
  print(f"Downloading model preset '{model_name}' to '{output_dir}'...")
  os.makedirs(output_dir, exist_ok=True)
  hf_token = os.environ.get("HF_TOKEN") or None

  try:
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=model_name,
        local_dir=output_dir,
        token=hf_token,
    )
    print(f"Successfully baked HuggingFace model weights into {output_dir}")
    return
  except Exception as e:
    print(f"HuggingFace snapshot_download failed: {e}")

  try:
    import kagglehub

    download_path = kagglehub.model_download(model_name)
    if os.path.exists(download_path):
      for item in os.listdir(download_path):
        s = os.path.join(download_path, item)
        d = os.path.join(output_dir, item)
        if os.path.isdir(s):
          shutil.copytree(s, d, dirs_exist_ok=True)
        else:
          shutil.copy2(s, d)
      print(f"Successfully baked Kaggle model weights into {output_dir}")
      return
  except Exception as e:
    print(f"Kagglehub model_download failed: {e}")

  raise RuntimeError(
      f"Failed to download model '{model_name}' from HuggingFace and Kaggle.")


def main():
  preset = os.environ.get("MODEL_PRESET", "google/gemma-4-2b-it")
  target_dir = sys.argv[1] if len(sys.argv) > 1 else f"/opt/models/{preset}"
  download_and_save_model(preset, target_dir)


if __name__ == "__main__":
  main()
