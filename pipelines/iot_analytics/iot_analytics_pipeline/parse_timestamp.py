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
"""
Vehicle state event models and parsing transforms for IoT Analytics.
"""
import datetime
import json
import logging
from typing import Any, Dict, Iterator, NamedTuple, Union
import apache_beam as beam
from apache_beam.metrics import Metrics
from apache_beam.transforms.window import TimestampedValue


def parse_timestamp(timestamp_str: str) -> datetime.datetime:
  """Parses RFC3339 or ISO8601 timestamp string safely."""
  if not timestamp_str:
    return datetime.datetime.now(datetime.timezone.utc)
  try:
    # Handle 'Z' suffix for Python fromisoformat
    normalized = timestamp_str.replace("Z", "+00:00")
    dt = datetime.datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
      dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt
  except (ValueError, TypeError):
    logging.warning("Could not parse timestamp '%s', using UTC now",
                    timestamp_str)
    return datetime.datetime.now(datetime.timezone.utc)


class VehicleStateEvent(NamedTuple):
  """Container for vehicle telemetry event."""
  vehicle_id: str
  timestamp: datetime.datetime
  temperature: int
  rpm: int
  vibration: float
  fuel_level: int
  mileage: int

  @staticmethod
  def convert_json_to_vehicleobj(
      input_json: Dict[str, Any]) -> TimestampedValue:
    """Converts a parsed JSON dictionary into a TimestampedValue of VehicleStateEvent."""
    dt_object = parse_timestamp(input_json.get("timestamp", ""))
    event = VehicleStateEvent(
        vehicle_id=str(input_json.get("vehicle_id", "")),
        timestamp=dt_object,
        temperature=int(input_json.get("temperature", 0)),
        rpm=int(input_json.get("rpm", 0)),
        vibration=float(input_json.get("vibration", 0.0)),
        fuel_level=int(input_json.get("fuel_level", 0)),
        mileage=int(input_json.get("mileage", 0)))
    return TimestampedValue(event, dt_object.timestamp())


class ParseVehicleEventDoFn(beam.DoFn):
  """Parses raw Pub/Sub bytes or JSON string into VehicleStateEvent with metrics."""

  def __init__(self):
    super().__init__()
    self.processed_counter = Metrics.counter(self.__class__, "events_parsed")
    self.parse_error_counter = Metrics.counter(self.__class__,
                                               "events_parse_errors")

  def process(
      self, element: Union[bytes, str,
                           Dict[str, Any]]) -> Iterator[TimestampedValue]:
    try:
      if isinstance(element, bytes):
        payload = json.loads(element.decode("utf-8"))
      elif isinstance(element, str):
        payload = json.loads(element)
      elif isinstance(element, dict):
        payload = element
      else:
        raise ValueError(f"Unsupported element type: {type(element)}")

      self.processed_counter.inc()
      yield VehicleStateEvent.convert_json_to_vehicleobj(payload)
    except Exception as e:  # pylint: disable=broad-exception-caught
      self.parse_error_counter.inc()
      logging.error("Failed to parse event element '%s': %s", element, e)
