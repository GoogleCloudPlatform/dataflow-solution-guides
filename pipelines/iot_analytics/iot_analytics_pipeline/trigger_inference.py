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
Feature extraction, prediction formatting, and alert transforms for IoT Analytics.
"""
import datetime
import json
from typing import Any, Dict, Iterator, Tuple
import apache_beam as beam
from apache_beam.ml.inference.base import PredictionResult
from apache_beam.utils.timestamp import Timestamp
import numpy as np


class ExtractFeaturesDoFn(beam.DoFn):
  """Extracts numeric feature vectors from enriched vehicle telemetry events."""

  def process(
      self, element: Dict[str,
                          Any]) -> Iterator[Tuple[Dict[str, Any], np.ndarray]]:
    max_temperature = float(element.get("max_temperature", 0.0))
    max_vibration = float(element.get("max_vibration", 0.0))

    last_service_date_str = str(element.get("last_service_date", ""))
    days_since_last_service = 180.0
    if last_service_date_str:
      try:
        service_dt = datetime.datetime.strptime(
            last_service_date_str.split("T")[0],
            "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        days_since_last_service = max(0.0, float((now_dt - service_dt).days))
      except (ValueError, TypeError):
        days_since_last_service = 180.0

    feature_vector = np.array(
        [max_temperature, max_vibration, days_since_last_service],
        dtype=np.float32,
    )
    yield (element, feature_vector)


class FormatPredictionDoFn(beam.DoFn):
  """Formats scored inferences into output records matching BigQuery schema."""

  def process(
      self, element: Tuple[Dict[str, Any],
                           PredictionResult]) -> Iterator[Dict[str, Any]]:
    enriched, prediction_result = element
    inference = prediction_result.inference

    # Support both scalar and 1D array predictions from Sklearn
    if hasattr(inference,
               "__iter__") and not isinstance(inference, (str, bytes)):
      pred_val = int(inference[0])
    else:
      pred_val = int(inference)

    ts_val = enriched.get("latest_timestamp")
    if isinstance(ts_val, Timestamp):
      latest_timestamp = ts_val
    elif isinstance(ts_val, (int, float)):
      latest_timestamp = Timestamp(ts_val)
    elif isinstance(ts_val, str) and ts_val:
      try:
        latest_timestamp = Timestamp.from_rfc3339(ts_val.replace("Z", "+00:00"))
      except (ValueError, TypeError):
        latest_timestamp = Timestamp.now()
    else:
      latest_timestamp = Timestamp.now()

    record = {
        "vehicle_id": str(enriched.get("vehicle_id", "")),
        "max_temperature": int(enriched.get("max_temperature", 0)),
        "max_vibration": float(enriched.get("max_vibration", 0.0)),
        "latest_timestamp": latest_timestamp,
        "last_service_date": str(enriched.get("last_service_date", "")),
        "maintenance_type": str(enriched.get("maintenance_type", "unknown")),
        "model": str(enriched.get("model", "unknown")),
        "needs_maintenance": pred_val,
    }
    yield record


def format_alert_payload(record: Dict[str, Any]) -> bytes:
  """Formats high-priority maintenance records into Pub/Sub alert JSON payloads."""
  payload = {
      "vehicle_id":
          record["vehicle_id"],
      "alert_type":
          "PREDICTIVE_MAINTENANCE_REQUIRED",
      "severity":
          "HIGH",
      "max_temperature":
          record["max_temperature"],
      "max_vibration":
          record["max_vibration"],
      "last_service_date":
          record["last_service_date"],
      "vehicle_model":
          record["model"],
      "alert_timestamp":
          datetime.datetime.now(datetime.timezone.utc).isoformat(),
  }
  return json.dumps(payload).encode("utf-8")
