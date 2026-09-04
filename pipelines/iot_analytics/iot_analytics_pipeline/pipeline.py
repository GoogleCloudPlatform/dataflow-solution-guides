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
Pipeline of the IoT Analytics Dataflow Solution guide.
"""
from typing import Any, Dict
import apache_beam as beam
from apache_beam import Pipeline
from apache_beam.io.gcp.bigquery import BigQueryDisposition, WriteToBigQuery
from apache_beam.ml.inference.base import KeyedModelHandler, RunInference
from apache_beam.ml.inference.sklearn_inference import ModelFileType, SklearnModelHandlerNumpy
from apache_beam.transforms.enrichment import Enrichment
from apache_beam.transforms.enrichment_handlers.bigtable import BigTableEnrichmentHandler
from apache_beam.transforms.trigger import AccumulationMode, AfterWatermark
from apache_beam.transforms.window import FixedWindows

from .aggregate_metrics import AggregateMetrics
from .options import MyPipelineOptions
from .parse_timestamp import ParseVehicleEventDoFn
from .trigger_inference import (
    ExtractFeaturesDoFn,
    FormatPredictionDoFn,
    format_alert_payload,
)

BQ_SCHEMA = {
    "fields": [
        {
            "name": "vehicle_id",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "max_temperature",
            "type": "INTEGER",
            "mode": "NULLABLE"
        },
        {
            "name": "max_vibration",
            "type": "FLOAT",
            "mode": "NULLABLE"
        },
        {
            "name": "latest_timestamp",
            "type": "TIMESTAMP",
            "mode": "NULLABLE"
        },
        {
            "name": "last_service_date",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "maintenance_type",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "model",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "needs_maintenance",
            "type": "INTEGER",
            "mode": "REQUIRED"
        },
    ]
}


def custom_join(left: Any, right: Any) -> Dict[str, Any]:
  """Safely merges aggregated telemetry with Bigtable vehicle maintenance metadata."""
  left_dict = left._asdict() if hasattr(left, "_asdict") else dict(left)
  right_dict = right._asdict() if hasattr(right, "_asdict") else dict(right)

  maintenance = right_dict.get("maintenance", {})
  if hasattr(maintenance, "_asdict"):
    maintenance = maintenance._asdict()

  return {
      "vehicle_id": str(left_dict.get("vehicle_id", "")),
      "max_temperature": int(left_dict.get("max_temperature", 0)),
      "max_vibration": float(left_dict.get("max_vibration", 0.0)),
      "latest_timestamp": left_dict.get("max_timestamp"),
      "avg_mileage": int(left_dict.get("avg_mileage", 0)),
      "last_service_date": str(maintenance.get("last_service_date", "")),
      "maintenance_type": str(maintenance.get("maintenance_type", "unknown")),
      "model": str(maintenance.get("model", "unknown")),
  }


def create_pipeline(pipeline_options: MyPipelineOptions) -> Pipeline:
  """Creates the Apache Beam IoT Analytics streaming pipeline.

  Args:
    pipeline_options: The pipeline options with type `MyPipelineOptions`.

  Returns:
    The configured Apache Beam Pipeline object.
  """
  pipeline = beam.Pipeline(options=pipeline_options)

  # 1. Ingest streaming telemetry events from Pub/Sub
  if pipeline_options.subscription:
    raw_events = pipeline | "ReadFromSubscription" >> beam.io.ReadFromPubSub(
        subscription=pipeline_options.subscription)
  elif pipeline_options.topic:
    raw_events = pipeline | "ReadFromTopic" >> beam.io.ReadFromPubSub(
        topic=pipeline_options.topic)
  else:
    raw_events = pipeline | "CreateEmptyFallback" >> beam.Create([])

  # 2. Parse JSON & assign event timestamps with error handling
  events = (
      raw_events
      | "ParseVehicleEvents" >> beam.ParDo(ParseVehicleEventDoFn())
      | "KeyByVehicleId" >> beam.WithKeys(lambda e: e.vehicle_id))

  # 3. Window & Aggregate Metrics per vehicle
  window_seconds = getattr(pipeline_options, "window_size_seconds", 60)
  aggregated = (
      events
      | "Window" >> beam.WindowInto(
          FixedWindows(window_seconds),
          trigger=AfterWatermark(),
          accumulation_mode=AccumulationMode.ACCUMULATING,
      )
      | "AggregateMetrics" >> beam.ParDo(AggregateMetrics()))

  # 4. Enrich with vehicle maintenance metadata from Cloud Bigtable
  if pipeline_options.bigtable_instance_id and pipeline_options.bigtable_table_id:
    bigtable_handler = BigTableEnrichmentHandler(
        project_id=pipeline_options.project,
        instance_id=pipeline_options.bigtable_instance_id,
        table_id=pipeline_options.bigtable_table_id,
        row_key=getattr(pipeline_options, "row_key", "vehicle_id"),
    )
    enriched_data = aggregated | "EnrichWithBigtable" >> Enrichment(
        bigtable_handler, join_fn=custom_join, timeout=10)
  else:
    enriched_data = aggregated | "MockEnrichment" >> beam.Map(
        lambda r: custom_join(r, {}))

  # 5. Extract features & execute Beam Turnkey RunInference (worker-local model)
  features = enriched_data | "ExtractFeatures" >> beam.ParDo(
      ExtractFeaturesDoFn())

  model_path = getattr(pipeline_options, "model_path", "maintenance_model.pkl")
  model_handler = KeyedModelHandler(
      SklearnModelHandlerNumpy(
          model_uri=model_path,
          model_file_type=ModelFileType.PICKLE,
      ))
  scored_elements = features | "RunInference" >> RunInference(model_handler)

  # 6. Format scored predictions
  predictions = scored_elements | "FormatPredictions" >> beam.ParDo(
      FormatPredictionDoFn())

  # 7. Sink 1: Analytical persistence in BigQuery (Storage Write API)
  if pipeline_options.dataset and pipeline_options.table and pipeline_options.project:
    table_spec = f"{pipeline_options.project}:{pipeline_options.dataset}.{pipeline_options.table}"
    predictions | "WriteToBigQuery" >> WriteToBigQuery(
        table=table_spec,
        schema=BQ_SCHEMA,
        create_disposition=BigQueryDisposition.CREATE_IF_NEEDED,
        write_disposition=BigQueryDisposition.WRITE_APPEND,
        method=WriteToBigQuery.Method.STORAGE_WRITE_API,
    )

  # 8. Sink 2: Real-time operational alerts in Pub/Sub
  if pipeline_options.alert_topic:
    alerts = (
        predictions
        | "FilterMaintenanceAlerts" >>
        beam.Filter(lambda r: r.get("needs_maintenance", 0) == 1)
        | "FormatAlertPayload" >> beam.Map(format_alert_payload))
    alerts | "PublishAlerts" >> beam.io.WriteToPubSub(
        topic=pipeline_options.alert_topic)

  return pipeline
