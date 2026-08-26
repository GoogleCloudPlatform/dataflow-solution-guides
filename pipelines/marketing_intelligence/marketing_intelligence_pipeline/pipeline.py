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
Pipeline of the Marketing Intelligence Dataflow Solution guide.
"""

from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, Iterator, Optional, Tuple

import apache_beam as beam
from apache_beam import Pipeline
from apache_beam.io.gcp import pubsub
from apache_beam.io.gcp.bigquery import BigQueryDisposition, WriteToBigQuery
from apache_beam.ml.inference.base import KeyedModelHandler, PredictionResult
from apache_beam.ml.inference.sklearn_inference import ModelFileType, SklearnModelHandlerNumpy
import cachetools
import numpy as np

try:
  from google.cloud import firestore
except ImportError:
  firestore = None

from .options import MyPipelineOptions

# BigQuery output table schema
BIGQUERY_TABLE_SCHEMA = {
    "fields": [
        {
            "name": "event_id",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "user_id",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "event_timestamp",
            "type": "TIMESTAMP",
            "mode": "NULLABLE"
        },
        {
            "name": "event_type",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "item_id",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "item_category",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "session_duration_sec",
            "type": "INTEGER",
            "mode": "NULLABLE"
        },
        {
            "name": "cart_value",
            "type": "FLOAT",
            "mode": "NULLABLE"
        },
        {
            "name": "loyalty_tier",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "total_lifetime_spend",
            "type": "FLOAT",
            "mode": "NULLABLE"
        },
        {
            "name": "total_orders",
            "type": "INTEGER",
            "mode": "NULLABLE"
        },
        {
            "name": "days_since_last_order",
            "type": "INTEGER",
            "mode": "NULLABLE"
        },
        {
            "name": "preferred_category",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "propensity_score",
            "type": "FLOAT",
            "mode": "REQUIRED"
        },
        {
            "name": "predicted_purchase",
            "type": "INTEGER",
            "mode": "REQUIRED"
        },
        {
            "name": "recommended_offer",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "processed_timestamp",
            "type": "TIMESTAMP",
            "mode": "REQUIRED"
        },
    ]
}

TIER_MAP = {
    "Bronze": 1.0,
    "Silver": 2.0,
    "Gold": 3.0,
    "Platinum": 4.0,
}


def _format_input(x: bytes) -> Dict[str, Any]:
  """Parses JSON bytes into a Python dictionary."""
  return json.loads(x.decode("utf-8"))


class FirestoreEnrichmentDoFn(beam.DoFn):
  """Enriches streaming events with customer profiles from Cloud Firestore."""

  def __init__(
      self,
      project: Optional[str] = None,
      collection: str = "customer_profiles",
      cache_maxsize: int = 10000,
      cache_ttl: int = 300,
  ):
    super().__init__()
    self.project = project
    self.collection = collection
    self.cache_maxsize = cache_maxsize
    self.cache_ttl = cache_ttl
    self.client = None
    self.cache = None

  def setup(self):
    self.cache = cachetools.TTLCache(
        maxsize=self.cache_maxsize, ttl=self.cache_ttl)
    if firestore is not None and self.project:
      try:
        self.client = firestore.Client(project=self.project)
      except Exception as e:  # pylint: disable=broad-exception-caught
        logging.warning(
            "Could not initialize Firestore client for project %s: %s",
            self.project,
            e,
        )
        self.client = None

  def process(self, element: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    user_id = str(element.get("user_id", ""))
    profile = None

    if self.cache is not None and user_id in self.cache:
      profile = self.cache[user_id]
    elif self.client is not None and user_id:
      try:
        doc_ref = self.client.collection(self.collection).document(user_id)
        doc = doc_ref.get()
        if doc.exists:
          profile = doc.to_dict()
        else:
          profile = {}
      except Exception as e:  # pylint: disable=broad-exception-caught
        logging.error("Firestore lookup failed for user %s: %s", user_id, e)
        profile = {}
      if self.cache is not None:
        self.cache[user_id] = profile
    else:
      profile = {}

    enriched = dict(element)
    enriched["loyalty_tier"] = profile.get("loyalty_tier", "Bronze")
    enriched["total_lifetime_spend"] = float(
        profile.get("total_lifetime_spend", 0.0))
    enriched["total_orders"] = int(profile.get("total_orders", 0))
    enriched["days_since_last_order"] = int(
        profile.get("days_since_last_order", 999))
    enriched["preferred_category"] = profile.get("preferred_category",
                                                 "unknown")

    yield enriched


class ExtractFeaturesDoFn(beam.DoFn):
  """Extracts numeric feature vectors from enriched customer event dictionaries."""

  def process(
      self, element: Dict[str,
                          Any]) -> Iterator[Tuple[Dict[str, Any], np.ndarray]]:
    session_duration_sec = float(element.get("session_duration_sec", 0.0))
    cart_value = float(element.get("cart_value", 0.0))
    total_lifetime_spend = float(element.get("total_lifetime_spend", 0.0))
    total_orders = float(element.get("total_orders", 0.0))
    days_since_last_order = float(element.get("days_since_last_order", 999.0))

    item_category = str(element.get("item_category", "")).lower()
    preferred_category = str(element.get("preferred_category", "")).lower()
    category_match = 1.0 if (item_category == preferred_category and
                             preferred_category != "unknown") else 0.0

    loyalty_tier = element.get("loyalty_tier", "Bronze")
    loyalty_tier_score = TIER_MAP.get(loyalty_tier, 1.0)

    feature_vector = np.array(
        [
            session_duration_sec,
            cart_value,
            total_lifetime_spend,
            total_orders,
            days_since_last_order,
            category_match,
            loyalty_tier_score,
        ],
        dtype=np.float32,
    )
    yield (element, feature_vector)


class SklearnModelHandlerNumpyProb(SklearnModelHandlerNumpy):
  """Custom ModelHandler that outputs both classification label and prediction probability."""

  def __init__(self, model_uri: str):
    super().__init__(model_uri=model_uri, model_file_type=ModelFileType.PICKLE)

  def run_inference(self, batch, model, inference_args=None):
    stacked = np.stack(batch)
    if hasattr(model, "predict_proba"):
      probabilities = model.predict_proba(stacked)[:, 1]
    else:
      probabilities = np.zeros(len(stacked), dtype=np.float32)

    predictions = model.predict(stacked)
    return [
        PredictionResult(
            example=x,
            inference={
                "predicted_purchase": int(p),
                "propensity_score": float(prob),
            },
        ) for x, p, prob in zip(batch, predictions, probabilities)
    ]


class FormatPredictionDoFn(beam.DoFn):
  """Formats scored inferences into output records and assigns recommendation offers."""

  def process(
      self, element: Tuple[Dict[str, Any],
                           PredictionResult]) -> Iterator[Dict[str, Any]]:
    enriched, prediction_result = element
    inference = prediction_result.inference

    propensity_score = float(inference.get("propensity_score", 0.0))
    predicted_purchase = int(inference.get("predicted_purchase", 0))

    if propensity_score >= 0.85:
      recommended_offer = "VIP 20% Instant Discount"
    elif propensity_score >= 0.65:
      recommended_offer = "10% Category Boost Coupon"
    elif propensity_score >= 0.40:
      recommended_offer = "Free Expedited Shipping"
    else:
      recommended_offer = "Standard Catalog Recommendation"

    now_iso = datetime.now(timezone.utc).isoformat()

    record = {
        "event_id":
            str(enriched.get("event_id", "")),
        "user_id":
            str(enriched.get("user_id", "")),
        "event_timestamp":
            enriched.get("timestamp"),
        "event_type":
            str(enriched.get("event_type", "")),
        "item_id":
            str(enriched.get("item_id", "")),
        "item_category":
            str(enriched.get("item_category", "")),
        "session_duration_sec":
            int(enriched.get("session_duration_sec", 0)),
        "cart_value":
            float(enriched.get("cart_value", 0.0)),
        "loyalty_tier":
            str(enriched.get("loyalty_tier", "Bronze")),
        "total_lifetime_spend":
            float(enriched.get("total_lifetime_spend", 0.0)),
        "total_orders":
            int(enriched.get("total_orders", 0)),
        "days_since_last_order":
            int(enriched.get("days_since_last_order", 999)),
        "preferred_category":
            str(enriched.get("preferred_category", "")),
        "propensity_score":
            round(propensity_score, 4),
        "predicted_purchase":
            predicted_purchase,
        "recommended_offer":
            recommended_offer,
        "processed_timestamp":
            now_iso,
    }
    yield record


def _format_activation_payload(record: Dict[str, Any]) -> str:
  """Formats high-propensity records into activation JSON payloads."""
  payload = {
      "event_id": record["event_id"],
      "user_id": record["user_id"],
      "propensity_score": record["propensity_score"],
      "recommended_offer": record["recommended_offer"],
      "item_id": record["item_id"],
      "item_category": record["item_category"],
      "cart_value": record["cart_value"],
      "activated_at": datetime.now(timezone.utc).isoformat(),
  }
  return json.dumps(payload)


def create_pipeline(options: MyPipelineOptions) -> Pipeline:
  """Creates the Marketing Intelligence streaming inference pipeline.

  Args:
    options: The pipeline options with type `MyPipelineOptions`.

  Returns:
    The configured Apache Beam Pipeline object.
  """
  pipeline = beam.Pipeline(options=options)

  # 1. Read input events
  if options.messages_subscription:
    raw_events = pipeline | "Read Subscription" >> beam.io.ReadFromPubSub(
        subscription=options.messages_subscription)
  elif options.input_topic:
    raw_events = pipeline | "Read Topic" >> beam.io.ReadFromPubSub(
        topic=options.input_topic)
  else:
    # Direct / Test fallback
    raw_events = pipeline | "Create Empty" >> beam.Create([])

  parsed_events = raw_events | "Parse JSON" >> beam.Map(_format_input)

  # 2. Enrich with Cloud Firestore customer profiles
  firestore_project = options.firestore_project or options.project_id
  enriched_events = (
      parsed_events
      | "Firestore Enrichment" >> beam.ParDo(
          FirestoreEnrichmentDoFn(
              project=firestore_project,
              collection=options.firestore_collection,
          )))

  # 3. Extract feature vectors
  keyed_features = enriched_events | "Extract Features" >> beam.ParDo(
      ExtractFeaturesDoFn())

  # 4. RunInference using local Scikit-Learn model
  model_handler = KeyedModelHandler(
      SklearnModelHandlerNumpyProb(model_uri=options.model_path))
  scored_elements = keyed_features | "RunInference" >> beam.ml.inference.base.RunInference(
      model_handler)

  # 5. Format output records
  formatted_records = scored_elements | "Format Predictions" >> beam.ParDo(
      FormatPredictionDoFn())

  # 6. Sink 1: BigQuery Storage Write API
  if options.bq_dataset and options.project_id:
    table_spec = f"{options.project_id}:{options.bq_dataset}.{options.bq_table}"
    formatted_records | "Write to BigQuery" >> WriteToBigQuery(
        table=table_spec,
        schema=BIGQUERY_TABLE_SCHEMA,
        create_disposition=BigQueryDisposition.CREATE_IF_NEEDED,
        write_disposition=BigQueryDisposition.WRITE_APPEND,
        method=WriteToBigQuery.Method.STORAGE_WRITE_API,
    )

  # 7. Sink 2: Pub/Sub High-Propensity Activation
  if options.responses_topic:
    high_propensity = (
        formatted_records
        | "Filter High Propensity" >> beam.Filter(
            lambda r: r.get("propensity_score", 0.0) >= options.threshold)
        | "Format Activation Payload" >> beam.Map(_format_activation_payload))
    high_propensity | "Publish Activation" >> pubsub.WriteStringsToPubSub(
        topic=options.responses_topic)

  return pipeline
