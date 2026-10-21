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
"""Standalone statistical evaluation of synthetic BigQuery tables.

Measures fidelity, privacy, and utility of a landed synthetic table against
its live source — a post-write sibling to the generation pipeline, never a
dependency of it (ADR 0041). This package imports none of `sdfb_core`,
`sdfb_beam`, or `sdfb_tests`; it has its own `pyproject.toml`, `uv.lock`, and
Python pin, and installs and runs with only the generator packages absent.

Design: docs/designs/2026-07-07-evaluation-framework-design.md

Design: docs/DESIGN.md §11 Evaluation (sdfb-evaluation)
(ADR 0041).
"""

from __future__ import annotations

from sdfb_evaluation.version import EVALUATOR_VERSION

__version__ = EVALUATOR_VERSION

__all__ = ["__version__"]
