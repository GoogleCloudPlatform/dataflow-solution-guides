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
"""sdfb-evaluation stands alone: no import of the generator packages (ADR 0041).

Design: docs/DESIGN.md §11 Evaluation (sdfb-evaluation)
(ADR 0041).
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).parents[2] / "src" / "sdfb_evaluation"
_FORBIDDEN = ("sdfb_core", "sdfb_beam", "sdfb_tests")


def _imported_roots(path: Path) -> set[str]:
  tree = ast.parse(path.read_text(), filename=str(path))
  roots: set[str] = set()
  for node in ast.walk(tree):
    if isinstance(node, ast.Import):
      roots.update(alias.name.split(".")[0] for alias in node.names)
    elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
      roots.add(node.module.split(".")[0])
  return roots


def test_package_source_exists():
  assert (_SRC / "__init__.py").is_file()


def test_no_generator_package_imports():
  offenders = {
      str(p.relative_to(_SRC)): sorted(_imported_roots(p) & set(_FORBIDDEN))
      for p in _SRC.rglob("*.py")
  }
  assert not {k: v for k, v in offenders.items() if v}
