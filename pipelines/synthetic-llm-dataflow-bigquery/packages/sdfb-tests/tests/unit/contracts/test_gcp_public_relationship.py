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
"""The public demo model shipped for the Dataflow Solution Guides launch.

`config/relationships/gcp_public_fk_example.yaml` is what the
DSG `04_run_dataflow.sh` passes as `--relationships_uri` (ADR 0040). These
tests pin what that launch plans — waves and edge roles — and that the
model stays out of a default directory scan.

Design: docs/DESIGN.md §9 ADR reference map
(ADR 0040).
"""

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

from pathlib import Path

from sdfb_beam.io.relationships import load_relationship_registry

_MODEL = (
    Path(__file__).parents[5] / "config" / "relationships" /
    "gcp_public_fk_example.yaml")


def _roles(registry, table):
  return {
      f"({','.join(e.cols)})->{e.ref}": role
      for e, role in registry.edge_roles(table).items()
  }


def test_the_model_loads_from_its_file():
  registry = load_relationship_registry(str(_MODEL))
  assert [m.model for m in registry.models] == ["gcp_public_thelook"]


def test_a_leaf_launch_generates_the_chain_parents_first():
  registry = load_relationship_registry(str(_MODEL))
  component = registry.component("order_items")
  assert set(component) == {"users", "orders", "order_items"}
  assert registry.generation_waves(component) == (("users",), ("orders",),
                                                  ("order_items",))


def test_every_edge_gets_the_role_the_launch_scripts_rely_on():
  registry = load_relationship_registry(str(_MODEL))
  assert _roles(registry, "orders") == {"(user_id)->users": "driving"}
  assert _roles(registry, "order_items") == {
      "(order_id,user_id)->orders": "driving",
      "(user_id)->users": "implied",
      "(product_id)->synthetic_data.products": "external",
  }


def test_every_primary_key_is_synthesized_as_identity():
  registry = load_relationship_registry(str(_MODEL))
  for table in ("users", "orders", "order_items"):
    relations = registry.relations(table)
    assert relations.identity == relations.pk


def test_a_directory_scan_skips_the_example_model(tmp_path):
  (tmp_path / _MODEL.name).write_text(
      _MODEL.read_text(encoding="utf-8"), encoding="utf-8")
  (tmp_path / "real.yaml").write_text(
      "model: real\ntables:\n  A_TABLE:\n    pk: [A_COL_001]\n",
      encoding="utf-8")
  registry = load_relationship_registry(str(tmp_path))
  assert [m.model for m in registry.models] == ["real"]
