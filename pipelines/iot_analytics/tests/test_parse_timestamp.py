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
"""Unit tests for parse_timestamp and VehicleStateEvent."""

import datetime
import json
import unittest
from apache_beam.transforms.window import TimestampedValue

from iot_analytics_pipeline.parse_timestamp import (
    ParseVehicleEventDoFn,
    VehicleStateEvent,
    parse_timestamp,
)


class TestParseTimestamp(unittest.TestCase):
  """Tests for timestamp parsing and event conversion."""

  def test_parse_timestamp_valid_iso(self):
    ts_str = "2026-09-04T12:30:00Z"
    dt = parse_timestamp(ts_str)
    self.assertEqual(dt.year, 2026)
    self.assertEqual(dt.month, 9)
    self.assertEqual(dt.day, 4)
    self.assertEqual(dt.hour, 12)
    self.assertEqual(dt.minute, 30)

  def test_parse_timestamp_empty_fallback(self):
    dt = parse_timestamp("")
    self.assertIsInstance(dt, datetime.datetime)

  def test_parse_timestamp_invalid_fallback(self):
    dt = parse_timestamp("invalid-date-string")
    self.assertIsInstance(dt, datetime.datetime)

  def test_convert_json_to_vehicleobj(self):
    raw_payload = {
        "vehicle_id": "1001",
        "timestamp": "2026-09-04T10:00:00Z",
        "temperature": 78,
        "rpm": 2400,
        "vibration": 0.35,
        "fuel_level": 82,
        "mileage": 52000,
    }
    ts_val = VehicleStateEvent.convert_json_to_vehicleobj(raw_payload)
    self.assertIsInstance(ts_val, TimestampedValue)
    event = ts_val.value
    self.assertEqual(event.vehicle_id, "1001")
    self.assertEqual(event.temperature, 78)
    self.assertEqual(event.rpm, 2400)
    self.assertEqual(event.vibration, 0.35)
    self.assertEqual(event.fuel_level, 82)
    self.assertEqual(event.mileage, 52000)

  def test_parse_vehicle_event_dofn(self):
    dofn = ParseVehicleEventDoFn()
    raw_payload = {
        "vehicle_id": "1002",
        "timestamp": "2026-09-04T10:05:00Z",
        "temperature": 82,
        "rpm": 2600,
        "vibration": 0.45,
        "fuel_level": 60,
        "mileage": 55000,
    }
    # Test JSON string
    res_str = list(dofn.process(json.dumps(raw_payload)))
    self.assertEqual(len(res_str), 1)
    self.assertEqual(res_str[0].value.vehicle_id, "1002")

    # Test bytes
    res_bytes = list(dofn.process(json.dumps(raw_payload).encode("utf-8")))
    self.assertEqual(len(res_bytes), 1)
    self.assertEqual(res_bytes[0].value.vehicle_id, "1002")

    # Test invalid json handles gracefully without throwing
    res_bad = list(dofn.process("bad-json-payload"))
    self.assertEqual(len(res_bad), 0)


if __name__ == "__main__":
  unittest.main()
