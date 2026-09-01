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
"""Setup file for the Gemma ML streaming inference pipeline."""

from setuptools import find_packages, setup

with open("requirements.txt", encoding="utf-8") as f:
  requirements = [
      line.strip() for line in f if line.strip() and not line.startswith("#")
  ]

setup(
    name="dataflow-solution-ml-ai",
    version="0.2.0",
    description="A Gemma ML/AI inference pipeline for Dataflow Solution Guides.",
    packages=find_packages(),
    install_requires=requirements,
)
