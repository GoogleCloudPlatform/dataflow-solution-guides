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

import apache_beam as beam
from apache_beam import PCollection
from apache_beam.io.gcp.bigquery import BigQueryDisposition, WriteToBigQuery
from apache_beam.options.pipeline_options import GoogleCloudOptions

from cdp_pipeline.options import MyPipelineOptions
from cdp_pipeline.schemas import load_output_schema


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
   | "Unified to Dict" >> beam.Map(lambda r: r.to_dict())
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
   | "Sessions to Dict" >> beam.Map(lambda r: r.to_dict())
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
     | "Write Deadletter to BigQuery" >> WriteToBigQuery(
         table=dlq_table_spec,
         schema=deadletter_schema,
         create_disposition=BigQueryDisposition.CREATE_IF_NEEDED,
         write_disposition=BigQueryDisposition.WRITE_APPEND,
         method=write_method,
         with_auto_sharding=True if use_storage_api else False,
         triggering_frequency=5 if use_storage_api else None,
     ))
