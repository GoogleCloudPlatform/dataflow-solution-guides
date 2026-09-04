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
"""Unit tests for AggregateMetrics stateful DoFn."""

import datetime
import unittest
import apache_beam as beam
from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.util import assert_that
from apache_beam.transforms.window import FixedWindows

from iot_analytics_pipeline.aggregate_metrics import AggregateMetrics
from iot_analytics_pipeline.parse_timestamp import VehicleStateEvent


def _check_aggregated_row(actual):
  assert len(actual) == 1, f"Expected 1 aggregated row, got {len(actual)}"
  row = actual[0]
  assert row.vehicle_id == "v_101", f"Unexpected vehicle_id: {row.vehicle_id}"
  assert row.max_temperature == 88, f"Unexpected max_temp: {row.max_temperature}"
  assert abs(row.max_vibration -
             0.45) < 1e-4, f"Unexpected max_vib: {row.max_vibration}"
  assert row.avg_mileage == 50000, f"Unexpected avg_mileage: {row.avg_mileage}"


class TestAggregateMetrics(unittest.TestCase):
  """Tests for metric aggregation over windows."""

  def test_aggregate_metrics_pipeline(self):
    now = datetime.datetime.now(datetime.timezone.utc)
    e1 = VehicleStateEvent(
        vehicle_id="v_101",
        timestamp=now,
        temperature=72,
        rpm=2200,
        vibration=0.20,
        fuel_level=80,
        mileage=40000,
    )
    e2 = VehicleStateEvent(
        vehicle_id="v_101",
        timestamp=now,
        temperature=88,
        rpm=2800,
        vibration=0.45,
        fuel_level=75,
        mileage=60000,
    )

    with TestPipeline() as p:
      aggregated = (
          p
          | "CreateEvents" >> beam.Create([("v_101", e1), ("v_101", e2)])
          | "WindowInto" >> beam.WindowInto(FixedWindows(60))
          | "Aggregate" >> beam.ParDo(AggregateMetrics()))
      assert_that(aggregated, _check_aggregated_row)


if __name__ == "__main__":
  unittest.main()
