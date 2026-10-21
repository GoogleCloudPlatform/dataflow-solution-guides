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
"""The root side of the two-sided golden parity test (D2) for
`packages/sdfb-evaluation`'s relationship-model and reference-sample
mirrors.

`packages/sdfb-evaluation/` cannot import `sdfb_core`/`sdfb_beam` (it must
stand alone), so it carries small hand-written mirrors of
`sdfb_core.contracts.relationships` and the reference-sample helpers in
`sdfb_beam.io`. `scripts/evaluation/make_parity_goldens.py`, run in the
root environment, writes
`packages/sdfb-evaluation/tests/fixtures/parity/goldens.json` from the
ORIGINALS. This test recomputes the same values from the originals again
(a regression pin on `RelationshipRegistry`, `is_sample_model`,
`compute_reference_digest`, `load_reference_rows`'s query text and the
row-doc prefix `MAX_ROW_DOC_ROWS`) and asserts equality with that file.
The mirror side of the pair is
`packages/sdfb-evaluation/tests/unit/test_parity_goldens.py`, which
recomputes them from the mirrors — this module deliberately never
imports `sdfb_evaluation`, so the two sides cannot cheat by sharing code.

Skipped when the goldens file has not been generated (the standalone
eval project is not part of every checkout's workflow). The Dataflow
Solution Guides copy ships both sides (`dsg/manifest.yaml`): the goldens
file is one of the evaluator's test fixtures.
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sdfb_beam.io.bq_sources import load_reference_rows
from sdfb_beam.io.digest import compute_reference_digest
from sdfb_beam.io.relationships import is_sample_model
from sdfb_core.contracts.relationships import RelationshipRegistry
from sdfb_core.rag.chunking import MAX_ROW_DOC_ROWS

_GOLDENS = (
    Path(__file__).parents[5] / "packages" / "sdfb-evaluation" / "tests" /
    "fixtures" / "parity" / "goldens.json")

pytestmark = pytest.mark.skipif(
    not _GOLDENS.is_file(),
    reason=(f"{_GOLDENS} not generated — run "
            "scripts/evaluation/make_parity_goldens.py"))


def _load_goldens() -> dict[str, Any]:
  return json.loads(_GOLDENS.read_text(encoding="utf-8"))


def _decode_value(  # noqa: PLR0911 — type dispatch, clearer flat than nested
    value: Any,) -> Any:
  """The inverse of the goldens script's `_encode_value`: a type-tagged
  JSON value back to the native Python object `compute_reference_digest`
  expects. Written independently of the eval side's own decoder — both
  must agree with the golden `digest`, not with each other."""
  if isinstance(value, dict):
    if "$decimal" in value:
      return Decimal(value["$decimal"])
    if "$datetime" in value:
      return datetime.fromisoformat(value["$datetime"])
    if "$date" in value:
      return date.fromisoformat(value["$date"])
    if "$bytes" in value:
      return base64.b64decode(value["$bytes"])
    return {k: _decode_value(v) for k, v in value.items()}
  if isinstance(value, list):
    return [_decode_value(v) for v in value]
  return value


def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
  return {k: _decode_value(v) for k, v in row.items()}


# ---------------------------------------------------------------------------
# Relationship models: sha12, component, generation_order, enforced_edges
# ---------------------------------------------------------------------------


def test_model_goldens_match_the_registry():
  goldens = _load_goldens()
  for name, expected in goldens["models"].items():
    sources = [tuple(pair) for pair in expected["sources"]]
    registry = RelationshipRegistry.from_sources(sources)
    assert registry.sha12() == expected["sha12"], name
    for table, expected_component in expected["component"].items():
      assert list(registry.component(table)) == expected_component, (name,
                                                                     table)
    for table, expected_order in expected["generation_order"].items():
      actual_order = list(registry.generation_order(registry.component(table)))
      assert actual_order == expected_order, (name, table)
    for table, expected_edges in expected["enforced_edges"].items():
      actual_edges = [[
          list(e.cols), e.ref,
          list(e.ref_cols), e.enforced, e.drives
      ] for e in registry.enforced_edges(table)]
      assert actual_edges == expected_edges, (name, table)
    expected_widenings = [[rec[0], rec[1], rec[2], [tuple(p)
                                                    for p in rec[3]]]
                          for rec in expected["derived_widenings"]]
    actual_widenings = [[rec["table"], rec["ref"], rec["via"], rec["added"]]
                        for rec in registry.derived_widenings()]
    assert actual_widenings == expected_widenings, name


# ---------------------------------------------------------------------------
# parse_relationship_model / RelationshipRegistry.from_sources strictness
# ---------------------------------------------------------------------------


def test_parse_verdicts_match_the_registry():
  goldens = _load_goldens()["parse_verdicts"]
  for entry in goldens:
    sources = [tuple(pair) for pair in entry["sources"]]
    if entry["accept"]:
      registry = RelationshipRegistry.from_sources(sources)
      assert registry.sha12() == entry["sha12"], entry["case"]
    else:
      try:
        RelationshipRegistry.from_sources(sources)
      except Exception as exc:  # pylint: disable=broad-except
        assert type(exc).__name__ == entry["error_class"], entry["case"]
      else:
        case = entry["case"]
        raise AssertionError(f"{case}: expected a rejection")


# ---------------------------------------------------------------------------
# is_sample_model
# ---------------------------------------------------------------------------


def test_is_sample_model_matches_the_golden():
  goldens = _load_goldens()
  for path, expected in goldens["is_sample_model"].items():
    assert is_sample_model(path) is expected, path


# ---------------------------------------------------------------------------
# compute_reference_digest
# ---------------------------------------------------------------------------


def test_reference_digest_matches_the_golden():
  goldens = _load_goldens()["reference_digest"]
  rows = [_decode_row(row) for row in goldens["rows"]]
  assert compute_reference_digest(rows) == goldens["digest"]


# ---------------------------------------------------------------------------
# load_reference_rows' query text
# ---------------------------------------------------------------------------


def test_reference_sql_matches_the_golden():
  goldens = _load_goldens()["reference_sql"]
  client = MagicMock()
  client.query.return_value.result.return_value = []
  load_reference_rows(
      table=goldens["table"], limit=goldens["limit"], client=client)
  sql = client.query.call_args[0][0]
  assert sql == goldens["sql"]


# ---------------------------------------------------------------------------
# The prompt-exposed prefix: MAX_ROW_DOC_ROWS
# ---------------------------------------------------------------------------


def test_exposure_rows_match_the_golden():
  assert _load_goldens()["exposure_rows"] == MAX_ROW_DOC_ROWS
