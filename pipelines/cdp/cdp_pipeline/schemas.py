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
"""BigQuery table schema loader for Customer Data Platform."""

import json
import os
from typing import Any, Dict, Optional, Union


def load_output_schema(
    schema_path: Optional[str] = None,
    default_filename: str = "unified_table.json",
) -> Union[Dict[str, Any], str]:
  """Loads a BigQuery schema from a custom path or packaged schema JSON file."""
  if schema_path:
    with open(schema_path, encoding="utf-8") as schema_file:
      return json.load(schema_file)

  # Check package schema directory
  default_schema_file = os.path.join(
      os.path.dirname(os.path.dirname(__file__)), "schema", default_filename)
  if os.path.exists(default_schema_file):
    with open(default_schema_file, encoding="utf-8") as schema_file:
      return json.load(schema_file)

  raise FileNotFoundError(
      f"Schema file '{default_filename}' not found at {default_schema_file}.")
