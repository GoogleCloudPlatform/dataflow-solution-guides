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
"""Beam-layer codegen.

Lives here (not in `sdfb_core.codegen`) because `pandera` + `pandas` belong
to the Beam-layer dep surface — `sdfb_core` stays installable on the
laptop with `pydantic` alone.
"""

from sdfb_beam.codegen.derive_pandera import derive_pandera_schema

__all__ = ["derive_pandera_schema"]
