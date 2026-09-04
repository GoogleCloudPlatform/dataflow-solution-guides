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
AggregateMetrics DoFn for IoT Analytics Dataflow Solution guide.
"""
from typing import Tuple
import apache_beam as beam
from apache_beam import coders
from apache_beam.transforms.timeutil import TimeDomain
from apache_beam.transforms.userstate import CombiningValueStateSpec, TimerSpec, on_timer
from apache_beam.utils.timestamp import Timestamp
from .parse_timestamp import VehicleStateEvent


class AggregateMetrics(beam.DoFn):
  """Stateful aggregation of vehicle telemetry metrics over a sliding or fixed window."""

  # State specifications (no unbounded bag state)
  MAX_TIMESTAMP = CombiningValueStateSpec(
      "max_timestamp_seen", coders.IterableCoder(coders.TimestampCoder()),
      lambda elements: max(elements, default=Timestamp(0)))
  MAX_TEMPERATURE = CombiningValueStateSpec(
      "max_temperature", coders.IterableCoder(coders.VarIntCoder()),
      lambda elements: max(elements, default=0))
  MAX_VIBRATION = CombiningValueStateSpec(
      "max_vibration", coders.IterableCoder(coders.FloatCoder()),
      lambda elements: max(elements, default=0.0))
  SUM_MILEAGE = CombiningValueStateSpec(
      "sum_mileage", coders.IterableCoder(coders.VarIntCoder()), sum)
  COUNT_MILEAGE = CombiningValueStateSpec(
      "count_mileage", coders.IterableCoder(coders.VarIntCoder()), sum)

  # Timer for window expiration
  WINDOW_TIMER = TimerSpec("window_timer", TimeDomain.WATERMARK)

  def process(self,
              element: Tuple[str, VehicleStateEvent],
              timestamp=beam.DoFn.TimestampParam,
              window=beam.DoFn.WindowParam,
              max_timestamp_seen=beam.DoFn.StateParam(MAX_TIMESTAMP),
              max_temperature=beam.DoFn.StateParam(MAX_TEMPERATURE),
              max_vibration=beam.DoFn.StateParam(MAX_VIBRATION),
              sum_mileage=beam.DoFn.StateParam(SUM_MILEAGE),
              count_mileage=beam.DoFn.StateParam(COUNT_MILEAGE),
              window_timer=beam.DoFn.TimerParam(WINDOW_TIMER)):
    # Update state with current event's values
    max_timestamp_seen.add(timestamp)
    max_temperature.add(element[1].temperature)
    max_vibration.add(element[1].vibration)
    sum_mileage.add(element[1].mileage)
    count_mileage.add(1)

    # Set timer for the end of the window
    window_timer.set(window.end)

  @on_timer(WINDOW_TIMER)
  def window_expiry_callback(
      self,
      vehicle_id=beam.DoFn.KeyParam,
      max_timestamp_seen=beam.DoFn.StateParam(MAX_TIMESTAMP),
      max_temperature=beam.DoFn.StateParam(MAX_TEMPERATURE),
      max_vibration=beam.DoFn.StateParam(MAX_VIBRATION),
      sum_mileage=beam.DoFn.StateParam(SUM_MILEAGE),
      count_mileage=beam.DoFn.StateParam(COUNT_MILEAGE)):
    cnt = count_mileage.read()
    avg_mileage = (sum_mileage.read() / cnt) if cnt > 0 else 0.0

    # Output Row object with aggregated values
    output_row = beam.Row(
        vehicle_id=str(vehicle_id),
        max_timestamp=max_timestamp_seen.read(),
        max_temperature=int(max_temperature.read()),
        max_vibration=float(max_vibration.read()),
        avg_mileage=int(avg_mileage))

    yield output_row

    # Clean up state
    max_timestamp_seen.clear()
    max_temperature.clear()
    max_vibration.clear()
    sum_mileage.clear()
    count_mileage.clear()
