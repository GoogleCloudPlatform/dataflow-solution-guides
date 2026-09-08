#!/usr/bin/env python3
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
"""CLI launcher script to publish synthetic Customer Data Platform streaming events to Pub/Sub."""

# pylint: disable=invalid-name,wrong-import-position

import os
import sys

# Ensure pipelines/cdp root is on Python path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
CDP_ROOT = os.path.dirname(CURRENT_DIR)
if CDP_ROOT not in sys.path:
  sys.path.insert(0, CDP_ROOT)

from simulator.publisher import main

if __name__ == "__main__":
  main()
