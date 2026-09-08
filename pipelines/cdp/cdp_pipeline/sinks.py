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
"""BigQuery sinks using the Storage Write API with auto-sharding."""

from datetime import datetime
import time
from typing import Any, Dict, Optional

import apache_beam as beam
from apache_beam import PCollection
from apache_beam.io.gcp.bigquery import BigQueryDisposition, WriteToBigQuery
from apache_beam.options.pipeline_options import GoogleCloudOptions
from apache_beam.utils.timestamp import Timestamp

from cdp_pipeline.options import MyPipelineOptions
from cdp_pipeline.schemas import load_output_schema


def _to_beam_timestamp(val: Any) -> Optional[Timestamp]:
  """Converts string, numeric, or datetime timestamp into Beam Timestamp.

  Required for BigQuery Storage Write API compatibility.
  """
  if val is None:
    return None
  if isinstance(val, Timestamp):
    return val
  if isinstance(val, (int, float)):
    return Timestamp.of(float(val))
  if isinstance(val, datetime):
    return Timestamp.of(val.timestamp())
  if isinstance(val, str):
    try:
      dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
      return Timestamp.of(dt.timestamp())
    except (ValueError, TypeError):
      return Timestamp.of(time.time())
  return Timestamp.of(time.time())


def _format_unified_dict(record: Any, use_storage_api: bool) -> Dict[str, Any]:
  row = record.to_dict() if hasattr(record, "to_dict") else dict(record)
  if use_storage_api:
    if "event_timestamp" in row and row["event_timestamp"]:
      row["event_timestamp"] = _to_beam_timestamp(row["event_timestamp"])
    if "processed_timestamp" in row and row["processed_timestamp"]:
      row["processed_timestamp"] = _to_beam_timestamp(
          row["processed_timestamp"])
  return row


def _format_session_dict(record: Any, use_storage_api: bool) -> Dict[str, Any]:
  row = record.to_dict() if hasattr(record, "to_dict") else dict(record)
  if use_storage_api:
    if "session_start" in row and row["session_start"]:
      row["session_start"] = _to_beam_timestamp(row["session_start"])
    if "session_end" in row and row["session_end"]:
      row["session_end"] = _to_beam_timestamp(row["session_end"])
    if "processed_timestamp" in row and row["processed_timestamp"]:
      row["processed_timestamp"] = _to_beam_timestamp(
          row["processed_timestamp"])
  return row


def _format_deadletter_dict(record: Any,
                            use_storage_api: bool) -> Dict[str, Any]:
  row = record.to_dict() if hasattr(record, "to_dict") else dict(record)
  if use_storage_api:
    if "timestamp" in row and row["timestamp"]:
      row["timestamp"] = _to_beam_timestamp(row["timestamp"])
  return row


def apply_bigquery_sinks(
    unified_records: PCollection,
    customer_sessions: PCollection,
    all_deadletters: PCollection,
    pipeline_options: MyPipelineOptions,
) -> None:
  """Configures BigQuery Storage Write API sinks for unified, session, and DLQ records."""
  project_id = pipeline_options.view_as(GoogleCloudOptions).project
  dataset = getattr(pipeline_options, "output_dataset", "cdp_dataset")
  unified_table = getattr(pipeline_options, "output_table",
                          "unified_customer_data")
  sessions_table = getattr(pipeline_options, "output_sessions_table",
                           "customer_sessions")
  deadletter_table = getattr(pipeline_options, "deadletter_table", None)
  use_storage_api = getattr(pipeline_options, "use_storage_write_api", True)

  write_method = (
      WriteToBigQuery.Method.STORAGE_WRITE_API
      if use_storage_api else WriteToBigQuery.Method.STREAMING_INSERTS)

  if not (project_id and dataset):
    return

  unified_schema = load_output_schema(
      getattr(pipeline_options, "output_schema_path", None),
      "unified_table.json",
  )
  sessions_schema = load_output_schema(
      getattr(pipeline_options, "output_sessions_schema_path", None),
      "customer_sessions.json",
  )

  unified_table_spec = f"{project_id}:{dataset}.{unified_table}"
  (unified_records
   | "Format Unified Rows" >> beam.Map(_format_unified_dict, use_storage_api)
   | "Write Unified to BigQuery" >> WriteToBigQuery(
       table=unified_table_spec,
       schema=unified_schema,
       create_disposition=BigQueryDisposition.CREATE_IF_NEEDED,
       write_disposition=BigQueryDisposition.WRITE_APPEND,
       method=write_method,
       with_auto_sharding=True if use_storage_api else False,
       triggering_frequency=5 if use_storage_api else None,
   ))

  sessions_table_spec = f"{project_id}:{dataset}.{sessions_table}"
  (customer_sessions
   | "Format Sessions Rows" >> beam.Map(_format_session_dict, use_storage_api)
   | "Write Sessions to BigQuery" >> WriteToBigQuery(
       table=sessions_table_spec,
       schema=sessions_schema,
       create_disposition=BigQueryDisposition.CREATE_IF_NEEDED,
       write_disposition=BigQueryDisposition.WRITE_APPEND,
       method=write_method,
       with_auto_sharding=True if use_storage_api else False,
       triggering_frequency=5 if use_storage_api else None,
   ))

  if deadletter_table:
    deadletter_schema = load_output_schema(
        getattr(pipeline_options, "deadletter_schema_path", None),
        "deadletter_table.json",
    )
    dlq_table_spec = f"{project_id}:{dataset}.{deadletter_table}"
    (all_deadletters
     | "Format Deadletter Rows" >> beam.Map(_format_deadletter_dict,
                                            use_storage_api)
     | "Write Deadletter to BigQuery" >> WriteToBigQuery(
         table=dlq_table_spec,
         schema=deadletter_schema,
         create_disposition=BigQueryDisposition.CREATE_IF_NEEDED,
         write_disposition=BigQueryDisposition.WRITE_APPEND,
         method=write_method,
         with_auto_sharding=True if use_storage_api else False,
         triggering_frequency=5 if use_storage_api else None,
     ))
