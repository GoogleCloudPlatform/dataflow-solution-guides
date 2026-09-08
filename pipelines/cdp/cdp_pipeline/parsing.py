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
"""Record parsing, payload validation, and timestamp assignment transforms."""

from datetime import datetime, timezone
from typing import Any, Dict, Generator, Tuple, Union

import apache_beam as beam
from apache_beam.metrics import Metrics
from apache_beam.transforms.window import TimestampedValue

from cdp_pipeline.models import CustomerInteractionEvent, EventType

TAG_DEADLETTER = "errors"


class ParseRecordDoFn(beam.DoFn):
  """Safely decodes and validates incoming JSON messages with Dead-Letter side outputs."""

  def __init__(self, record_type: Union[EventType, str]):
    super().__init__()
    if isinstance(record_type, str):
      self.record_type = EventType(record_type)
    else:
      self.record_type = record_type
    self.processed_counter = None
    self.error_counter = None

  def setup(self):
    self.processed_counter = Metrics.counter(
        self.__class__, f"processed_{self.record_type.value}")
    self.error_counter = Metrics.counter(self.__class__,
                                         f"error_{self.record_type.value}")

  def process(
      self,
      element: Union[bytes, str, Dict[str, Any]],
  ) -> Generator[Any, None, None]:
    event, deadletter = CustomerInteractionEvent.from_raw_payload(
        element, self.record_type)
    if deadletter is not None:
      self.error_counter.inc()
      yield beam.pvalue.TaggedOutput(TAG_DEADLETTER, deadletter.to_dict())
      return

    self.processed_counter.inc()
    yield (event.household_key, event)


class AssignEventTimestampDoFn(beam.DoFn):
  """Assigns event timestamp for session windowing based on payload event_timestamp."""

  def process(
      self,
      element: Tuple[str, CustomerInteractionEvent],
      timestamp=beam.DoFn.TimestampParam,
  ) -> Generator[Any, None, None]:
    _, event = element
    event_ts_str = event.event_timestamp
    ts_seconds = None
    if event_ts_str:
      try:
        dt = datetime.fromisoformat(event_ts_str.replace("Z", "+00:00"))
        ts_seconds = dt.timestamp()
      except (ValueError, TypeError):
        ts_seconds = None

    if ts_seconds is None:
      try:
        current_micros = timestamp.micros
        if current_micros > 0:
          ts_seconds = current_micros / 1000000.0
      except (AttributeError, TypeError, ValueError):
        pass

    if ts_seconds is None or ts_seconds <= 0:
      ts_seconds = datetime.now(timezone.utc).timestamp()

    yield TimestampedValue(element, ts_seconds)
