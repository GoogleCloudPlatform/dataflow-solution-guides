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
"""`build_pipeline` hands the landing schema's column order to
`EnforceUniqueness` so the exact barrier shuffles rows as value tuples
(ADR 0034) — graph-construction only, never executed.

Design: docs/DESIGN.md §6 Throughput
(ADR 0034).
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=unbalanced-tuple-unpacking

from __future__ import annotations

import apache_beam as beam
from sdfb_beam.dofns.uniqueness import EnforceUniqueness
from sdfb_beam.pipeline import PipelineConfig, build_pipeline
from sdfb_core.contracts import TableSchema

_SCHEMA = TableSchema.model_validate({
    "table_info": {
        "table_id": "demo.t"
    },
    "schema": [
        {
            "name": "b",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "a",
            "type": "INT64",
            "mode": "NULLABLE"
        },
    ],
})


def _uniqueness_transforms(node) -> list[EnforceUniqueness]:
  found: list[EnforceUniqueness] = []
  for part in getattr(node, "parts", []):
    if isinstance(part.transform, EnforceUniqueness):
      found.append(part.transform)
    found.extend(_uniqueness_transforms(part))
  return found


def test_build_pipeline_passes_schema_columns_to_the_uniqueness_barrier():
  cfg = PipelineConfig(
      table_schema=_SCHEMA,
      engine_name="fake",
      model_client=None,
      num_rows=4,
      batch_size=4,
      run_id="r1",
      pk_columns=("b",),
  )
  p = beam.Pipeline()
  build_pipeline(
      p,
      reference_rows=[{
          "b": "x",
          "a": 1
      }],
      config=cfg,
      landing_sink=beam.Map(lambda x: x),
      dlq_sink=beam.Map(lambda x: x),
  )
  (uniq,) = _uniqueness_transforms(p.transforms_stack[0])
  assert uniq.columns == ["b", "a"]  # schema order, not sorted
  assert uniq.pk_columns == ["b"]
