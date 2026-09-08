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
"""Backward-compatible entry point forwarding to simulator.publisher."""

import os
import sys
import warnings

# Ensure cdp root directory is on Python path if executed directly as a script
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_CURRENT_DIR)
if _PARENT_DIR not in sys.path:
  sys.path.insert(0, _PARENT_DIR)

# pylint: disable=wrong-import-position
from simulator.generator import generate_synthetic_session_events
from simulator.publisher import get_topic_path, publish_events_to_pubsub

warnings.warn(
    "cdp_pipeline.generate_transaction_data has moved to "
    "simulator.publisher and scripts/03_publish_events.py.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "generate_synthetic_session_events",
    "get_topic_path",
    "publish_events_to_pubsub",
]

if __name__ == "__main__":
  from simulator.publisher import main  # pylint: disable=reimported
  main()
