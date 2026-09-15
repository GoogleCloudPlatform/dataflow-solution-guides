"""Helpers to load fixture files from `packages/sdfb-tests/fixtures/`.

Fixture layout:
    fixtures/
        ddl/{name}_ddl.json          — parsed by `load_ddl(name)`
        reference/{name}_reference.json — parsed by `load_reference(name)`
"""

from __future__ import annotations

import json
from pathlib import Path

from sdfb_core.contracts import TableSchema

# This file lives at `packages/sdfb-tests/src/sdfb_tests/fixtures.py`.
# fixtures dir is at `packages/sdfb-tests/fixtures/`.
FIXTURES_ROOT: Path = Path(__file__).resolve().parent.parent.parent / "fixtures"


def load_ddl(name: str) -> TableSchema:
  """Load `fixtures/ddl/{name}_ddl.json` and parse it into a `TableSchema`."""
  path = FIXTURES_ROOT / "ddl" / f"{name}_ddl.json"
  return TableSchema.model_validate(json.loads(path.read_text()))


def load_reference(name: str) -> list[dict]:
  """Load `fixtures/reference/{name}_reference.json` as a list of dicts."""
  path = FIXTURES_ROOT / "reference" / f"{name}_reference.json"
  return json.loads(path.read_text())
