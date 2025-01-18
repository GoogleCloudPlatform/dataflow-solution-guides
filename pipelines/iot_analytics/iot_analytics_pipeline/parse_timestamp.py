#  Copyright 2025 Google LLC
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
Pipeline of the IoT Analytics Dataflow Solution guide.
"""
import typing
import datetime
from apache_beam.transforms.window import TimestampedValue


class VehicleStateEvent(typing.NamedTuple):
  """
  Class to create VehicleState TimestampedValue
  """
  vehicle_id: str
  timestamp: datetime.datetime
  temperature: int
  rpm: int
  vibration: float
  fuel_level: int
  mileage: int

  @staticmethod
  def convert_json_to_vehicleobj(input_json):
    dt_object = datetime.datetime.strptime(input_json["timestamp"],
                                           "%Y-%m-%dT%H:%M:%SZ")
    event = VehicleStateEvent(
        vehicle_id=input_json["vehicle_id"],
        timestamp=dt_object,
        temperature=input_json["temperature"],
        rpm=input_json["rpm"],
        vibration=input_json["vibration"],
        fuel_level=input_json["fuel_level"],
        mileage=input_json["mileage"])
    return TimestampedValue(event, dt_object.timestamp())
