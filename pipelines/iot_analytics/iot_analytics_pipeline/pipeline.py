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
import apache_beam as beam
from apache_beam import Pipeline
from .options import MyPipelineOptions
import json
import pickle
from .aggregate_metrics import AggregateMetrics
from .parse_timestamp import VehicleStateEvent
from .trigger_inference import RunInference
from apache_beam.transforms.window import FixedWindows
from apache_beam.transforms.trigger import AccumulationMode, AfterWatermark
from typing import Any, Dict, Tuple

from apache_beam.transforms.enrichment import Enrichment
from apache_beam.transforms.enrichment_handlers.bigtable import BigTableEnrichmentHandler


def custom_join(left: Dict[str, Any], right: Dict[str, Any]):
  enriched = {}
  enriched["vehicle_id"] = left["vehicle_id"]
  enriched["max_temperature"] = left["max_temperature"]
  enriched["max_vibration"] = left["max_vibration"]
  enriched["latest_timestamp"] = left["max_timestamp"]
  enriched["avg_mileage"] = left["avg_mileage"]
  enriched["last_service_date"] = right["maintenance"]["last_service_date"]
  enriched["maintenance_type"] = right["maintenance"]["maintenance_type"]
  enriched["model"] = right["maintenance"]["model"]
  return enriched


with open("maintenance_model.pkl", "rb") as model_file:
  sklearn_model_handler = pickle.load(model_file)


def create_pipeline(pipeline_options: MyPipelineOptions) -> Pipeline:
  """ Create the pipeline object.

    Args:
    options: The pipeline options, with type `MyPipelineOptions`.

    Returns:
    The pipeline object.
    """
  # Define your pipeline options
  bigtable_handler = BigTableEnrichmentHandler(
      project_id=pipeline_options.project,
      instance_id=pipeline_options.bigtable_instance_id,
      table_id=pipeline_options.bigtable_table_id,
      row_key=pipeline_options.row_key)
  bq_schema = "vehicle_id:STRING, \
    max_temperature:INTEGER, \
    max_vibration:FLOAT, \
    latest_timestamp:TIMESTAMP, \
    last_service_date:STRING, \
    maintenance_type:STRING, \
    model:STRING, \
    needs_maintenance:INTEGER"

  pipeline = beam.Pipeline(options=pipeline_options)
  enriched_data = pipeline \
  | "ReadFromPubSub" >> beam.io.ReadFromPubSub(topic=pipeline_options.topic) \
  | "Read JSON" >> beam.Map(json.loads) \
  | "Parse&EventTimestamp" >> beam.Map(
      VehicleStateEvent.convert_json_to_vehicleobj).with_output_types(
          VehicleStateEvent) \
  | "AddKeys" >>  beam.WithKeys(lambda event: event.vehicle_id).with_output_types(
      Tuple[str, VehicleStateEvent]) \
  | "Window" >> beam.WindowInto(
      FixedWindows(60),
      trigger=AfterWatermark(),
      accumulation_mode=AccumulationMode.ACCUMULATING) \
  | "AggregateMetrics" >> beam.ParDo(AggregateMetrics()).with_output_types(
      VehicleStateEvent).with_input_types(Tuple[str, VehicleStateEvent]) \
  | "EnrichWithBigtable" >> Enrichment(
      bigtable_handler, join_fn=custom_join, timeout=10)
  predictions = enriched_data | "RunInference" >> beam.ParDo(
      RunInference(model=sklearn_model_handler))
  predictions | "WriteToBigQuery" >> beam.io.gcp.bigquery.WriteToBigQuery(
      method=beam.io.WriteToBigQuery.Method.STORAGE_WRITE_API,
      project=pipeline_options.project,
      dataset=pipeline_options.dataset,
      table=pipeline_options.table,
      schema=bq_schema,
      create_disposition=beam.io.BigQueryDisposition.CREATE_IF_NEEDED,
      write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND)
  return pipeline
