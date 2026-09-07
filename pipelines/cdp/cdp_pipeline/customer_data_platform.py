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
"""Customer Data Platform analytics and sessionization streaming pipeline."""

import collections
from datetime import datetime, timezone
import json
import logging
import os
from typing import Any, Dict, Generator, Iterable, Optional, Tuple, Union

import apache_beam as beam
from apache_beam import Pipeline, PCollection
from apache_beam.io.gcp.bigquery import BigQueryDisposition, WriteToBigQuery
from apache_beam.metrics import Metrics
from apache_beam.options.pipeline_options import GoogleCloudOptions
from apache_beam.transforms.trigger import AccumulationMode, AfterCount, AfterWatermark
from apache_beam.transforms.window import Sessions, TimestampedValue
from apache_beam.utils.timestamp import Duration

from cdp_pipeline.options import MyPipelineOptions

TAG_DEADLETTER = "errors"
TAG_SESSIONS = "sessions"

DEFAULT_OUTPUT_SCHEMA: Dict[str, Any] = {
    "fields": [
        {
            "name": "session_id",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "transaction_id",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "household_key",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "product_id",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "quantity",
            "type": "INTEGER",
            "mode": "NULLABLE"
        },
        {
            "name": "sales_value",
            "type": "FLOAT",
            "mode": "NULLABLE"
        },
        {
            "name": "store_id",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "retail_disc",
            "type": "FLOAT",
            "mode": "NULLABLE"
        },
        {
            "name": "coupon_discount",
            "type": "FLOAT",
            "mode": "NULLABLE"
        },
        {
            "name": "coupon_match_disc",
            "type": "FLOAT",
            "mode": "NULLABLE"
        },
        {
            "name": "coupon_upc",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "campaign",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "day",
            "type": "INTEGER",
            "mode": "NULLABLE"
        },
        {
            "name": "trans_time",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "week_no",
            "type": "INTEGER",
            "mode": "NULLABLE"
        },
        {
            "name": "event_timestamp",
            "type": "TIMESTAMP",
            "mode": "NULLABLE"
        },
        {
            "name": "processed_timestamp",
            "type": "TIMESTAMP",
            "mode": "REQUIRED"
        },
    ]
}

DEFAULT_SESSIONS_SCHEMA: Dict[str, Any] = {
    "fields": [
        {
            "name": "session_id",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "household_key",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "session_start",
            "type": "TIMESTAMP",
            "mode": "REQUIRED"
        },
        {
            "name": "session_end",
            "type": "TIMESTAMP",
            "mode": "REQUIRED"
        },
        {
            "name": "session_duration_sec",
            "type": "INTEGER",
            "mode": "REQUIRED"
        },
        {
            "name": "total_transactions",
            "type": "INTEGER",
            "mode": "REQUIRED"
        },
        {
            "name": "total_items_purchased",
            "type": "INTEGER",
            "mode": "REQUIRED"
        },
        {
            "name": "total_spend",
            "type": "FLOAT",
            "mode": "REQUIRED"
        },
        {
            "name": "total_discount",
            "type": "FLOAT",
            "mode": "REQUIRED"
        },
        {
            "name": "coupons_redeemed_count",
            "type": "INTEGER",
            "mode": "REQUIRED"
        },
        {
            "name": "distinct_products_count",
            "type": "INTEGER",
            "mode": "REQUIRED"
        },
        {
            "name": "campaigns",
            "type": "STRING",
            "mode": "REPEATED"
        },
        {
            "name": "stores_visited",
            "type": "STRING",
            "mode": "REPEATED"
        },
        {
            "name": "processed_timestamp",
            "type": "TIMESTAMP",
            "mode": "REQUIRED"
        },
    ]
}

DEFAULT_DEADLETTER_SCHEMA: Dict[str, Any] = {
    "fields": [
        {
            "name": "source",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "raw_payload",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "error_message",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "timestamp",
            "type": "TIMESTAMP",
            "mode": "REQUIRED"
        },
    ]
}


def load_output_schema(
    schema_path: Optional[str] = None,
    default_filename: str = "unified_table.json",
    fallback_schema: Optional[Dict[str, Any]] = None,
) -> Union[Dict[str, Any], str]:
  """Loads a BigQuery schema from a custom path, packaged file, or fallback dict."""
  if fallback_schema is None:
    fallback_schema = DEFAULT_OUTPUT_SCHEMA

  if schema_path:
    with open(schema_path, encoding="utf-8") as schema_file:
      return json.load(schema_file)

  # Check package schema directory
  default_schema_file = os.path.join(
      os.path.dirname(os.path.dirname(__file__)), "schema", default_filename)
  if os.path.exists(default_schema_file):
    with open(default_schema_file, encoding="utf-8") as schema_file:
      return json.load(schema_file)

  return fallback_schema


class ParseRecordDoFn(beam.DoFn):
  """Safely decodes and validates incoming JSON messages with Dead-Letter side outputs."""

  def __init__(self, record_type: str):
    super().__init__()
    self.record_type = record_type
    self.processed_counter = None
    self.error_counter = None

  def setup(self):
    self.processed_counter = Metrics.counter(self.__class__,
                                             f"processed_{self.record_type}")
    self.error_counter = Metrics.counter(self.__class__,
                                         f"error_{self.record_type}")

  def process(self, element: Union[bytes, str, Dict[str, Any]]):
    try:
      if isinstance(element, bytes):
        payload_str = element.decode("utf-8")
        data = json.loads(payload_str)
      elif isinstance(element, str):
        payload_str = element
        data = json.loads(payload_str)
      elif isinstance(element, dict):
        payload_str = json.dumps(element)
        data = dict(element)
      else:
        raise ValueError(f"Unsupported record type: {type(element)}")
    except Exception as exc:  # pylint: disable=broad-exception-caught
      self.error_counter.inc()
      yield beam.pvalue.TaggedOutput(
          TAG_DEADLETTER,
          {
              "source": self.record_type,
              "raw_payload": str(element)[:2000],
              "error_message": f"Malformed payload: {exc}",
              "timestamp": datetime.now(timezone.utc).isoformat(),
          },
      )
      return

    household_key = str(data.get("household_key", "")).strip()
    transaction_id = str(data.get("transaction_id", "")).strip()

    if not household_key or not transaction_id:
      self.error_counter.inc()
      yield beam.pvalue.TaggedOutput(
          TAG_DEADLETTER,
          {
              "source":
                  self.record_type,
              "raw_payload":
                  payload_str[:2000],
              "error_message":
                  "Missing required household_key or transaction_id",
              "timestamp":
                  datetime.now(timezone.utc).isoformat(),
          },
      )
      return

    data["_record_type"] = self.record_type
    self.processed_counter.inc()
    yield (household_key, data)


class AssignEventTimestampDoFn(beam.DoFn):
  """Assigns event timestamp for session windowing based on payload event_timestamp."""

  def process(
      self,
      element: Tuple[str, Dict[str, Any]],
      timestamp=beam.DoFn.TimestampParam,
  ) -> Generator[Any, None, None]:
    _, data = element
    event_ts_str = data.get("event_timestamp")
    ts_seconds = None
    if event_ts_str:
      try:
        if isinstance(event_ts_str, str):
          dt = datetime.fromisoformat(event_ts_str.replace("Z", "+00:00"))
          ts_seconds = dt.timestamp()
        elif isinstance(event_ts_str, (int, float)):
          ts_seconds = float(event_ts_str)
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


class ProcessCustomerSessionDoFn(beam.DoFn):
  """Aggregates customer interactions into unified records and Customer 360 profiles."""

  def __init__(self):
    super().__init__()
    self.sessions_counter = None
    self.unified_items_counter = None
    self.matched_coupons_counter = None

  def setup(self):
    self.sessions_counter = Metrics.counter(self.__class__,
                                            "completed_sessions")
    self.unified_items_counter = Metrics.counter(self.__class__,
                                                 "unified_items_emitted")
    self.matched_coupons_counter = Metrics.counter(self.__class__,
                                                   "matched_coupons")

  def process(
      self,
      element: Tuple[str, Iterable[Dict[str, Any]]],
      window=beam.DoFn.WindowParam,
  ):
    household_key, items_iter = element
    items = list(items_iter)
    if not items:
      return

    try:
      session_start_iso = window.start.to_utc_datetime().isoformat()
    except (OverflowError, ValueError):
      session_start_iso = datetime.now(timezone.utc).isoformat()

    try:
      session_end_iso = window.end.to_utc_datetime().isoformat()
    except (OverflowError, ValueError):
      session_end_iso = datetime.now(timezone.utc).isoformat()

    try:
      session_duration_sec = max(
          0, int((window.end.micros - window.start.micros) / 1000000))
    except (OverflowError, ValueError):
      session_duration_sec = 0

    try:
      start_sec = int(window.start.micros / 1000000)
    except (OverflowError, ValueError):
      start_sec = int(datetime.now(timezone.utc).timestamp())
    session_id = f"sess_{household_key}_{start_sec}"
    processed_ts = datetime.now(timezone.utc).isoformat()

    transactions: list[Dict[str, Any]] = []
    coupons_by_tx: Dict[str, list[Dict[str,
                                       Any]]] = collections.defaultdict(list)

    for item in items:
      rec_type = item.get("_record_type")
      if rec_type == "coupon":
        tx_id = str(item.get("transaction_id", ""))
        coupons_by_tx[tx_id].append(item)
      elif rec_type == "transaction":
        transactions.append(item)
      elif "coupon_upc" in item and "product_id" not in item:
        tx_id = str(item.get("transaction_id", ""))
        coupons_by_tx[tx_id].append(item)
      else:
        transactions.append(item)

    distinct_tx_ids = set()
    distinct_products = set()
    campaigns = set()
    stores = set()
    total_spend = 0.0
    total_items = 0
    total_discount = 0.0
    coupons_redeemed_count = 0

    for coupon_list in coupons_by_tx.values():
      coupons_redeemed_count += len(coupon_list)
      for c in coupon_list:
        camp = c.get("campaign")
        if camp:
          campaigns.add(str(camp))

    for tx in transactions:
      tx_id = str(tx.get("transaction_id", ""))
      distinct_tx_ids.add(tx_id)
      prod_id = str(tx.get("product_id", "")).strip()
      if prod_id:
        distinct_products.add(prod_id)
      store_val = tx.get("store_id")
      if store_val:
        stores.add(str(store_val))

      sales = float(tx.get("sales_value", 0.0) or 0.0)
      total_spend += sales
      qty = int(float(tx.get("quantity", 1) or 1))
      total_items += qty
      ret_disc = float(tx.get("retail_disc", 0.0) or 0.0)
      coup_disc = float(
          tx.get("coupon_disc", tx.get("coupon_discount", 0.0)) or 0.0)
      total_discount += (ret_disc + coup_disc)

      matching_coupons = coupons_by_tx.get(tx_id, [])
      if not matching_coupons:
        coupon_iter = [None]
      else:
        coupon_iter = matching_coupons
        self.matched_coupons_counter.inc(len(matching_coupons))

      for coup in coupon_iter:
        coupon_upc = None
        campaign = None
        if isinstance(coup, dict):
          coupon_upc = str(coup.get("coupon_upc", "")) or None
          campaign = str(coup.get("campaign", "")) or None

        day_val = tx.get("day")
        week_val = tx.get("week_no")
        unified_record = {
            "session_id": session_id,
            "transaction_id": tx_id,
            "household_key": household_key,
            "product_id": prod_id or None,
            "quantity": qty,
            "sales_value": sales,
            "store_id": str(store_val) if store_val else None,
            "retail_disc": ret_disc,
            "coupon_discount": coup_disc,
            "coupon_match_disc": float(tx.get("coupon_match_disc", 0.0) or 0.0),
            "coupon_upc": coupon_upc,
            "campaign": campaign,
            "day": int(day_val) if day_val is not None else None,
            "trans_time": str(tx.get("trans_time", "")) or None,
            "week_no": int(week_val) if week_val is not None else None,
            "event_timestamp": tx.get("event_timestamp") or processed_ts,
            "processed_timestamp": processed_ts,
        }
        yield unified_record
        self.unified_items_counter.inc()

    session_profile = {
        "session_id": session_id,
        "household_key": household_key,
        "session_start": session_start_iso,
        "session_end": session_end_iso,
        "session_duration_sec": session_duration_sec,
        "total_transactions": len(distinct_tx_ids),
        "total_items_purchased": total_items,
        "total_spend": round(total_spend, 2),
        "total_discount": round(total_discount, 2),
        "coupons_redeemed_count": coupons_redeemed_count,
        "distinct_products_count": len(distinct_products),
        "campaigns": sorted(list(campaigns)),
        "stores_visited": sorted(list(stores)),
        "processed_timestamp": processed_ts,
    }
    yield beam.pvalue.TaggedOutput(TAG_SESSIONS, session_profile)
    self.sessions_counter.inc()


def left_join(
    key_value_pair: Tuple[Any, Tuple[Iterable[Dict[str, Any]],
                                     Iterable[Optional[Dict[str, Any]]]]]
) -> Generator[Dict[str, Any], None, None]:
  """Legacy helper performing a left join between transaction and coupon redemption records."""
  _, values = key_value_pair
  trans_values, coupon_redempt_values = values
  coupon_list = list(coupon_redempt_values)
  if not coupon_list:
    coupon_list = [None]
  for trans_value in trans_values:
    if trans_value is not None:
      for coupon_redempt_value in coupon_list:
        coupon_upc = None
        if isinstance(coupon_redempt_value, dict):
          raw_upc = coupon_redempt_value.get("coupon_upc")
          if raw_upc is not None:
            coupon_upc = str(raw_upc)
        unified_data = {
            "transaction_id":
                str(trans_value["transaction_id"]),
            "household_key":
                str(trans_value["household_key"]),
            "coupon_upc":
                coupon_upc,
            "product_id":
                str(trans_value["product_id"]),
            "coupon_discount":
                str(
                    trans_value.get("coupon_disc",
                                    trans_value.get("coupon_discount", "0"))),
        }
        yield unified_data


@beam.ptransform_fn
def _unify_data(
    pcolls: Tuple[PCollection, PCollection]) -> PCollection[Dict[str, Any]]:
  """Legacy transform combining transactions and coupons via CoGroupByKey."""
  transactions_pcoll, coupons_redempt_pcoll = pcolls
  unified_data = ((transactions_pcoll, coupons_redempt_pcoll)
                  | "Combine Transactions and Coupons" >> beam.CoGroupByKey()
                  | beam.FlatMap(left_join))
  return unified_data


def build_pipeline(
    pipeline: Pipeline,
    pipeline_options: MyPipelineOptions,
    in_memory_transactions: Optional[Iterable[Any]] = None,
    in_memory_coupons: Optional[Iterable[Any]] = None,
):
  """Builds the streaming sessionization pipeline graph on the given Pipeline object."""
  # 1. Read transactions stream
  if in_memory_transactions is not None:
    raw_transactions = pipeline | "Create Transactions" >> beam.Create(
        in_memory_transactions)
  elif pipeline_options.transactions_subscription:
    raw_transactions = pipeline | "Read Transactions Sub" >> beam.io.ReadFromPubSub(
        subscription=pipeline_options.transactions_subscription)
  elif pipeline_options.transactions_topic:
    raw_transactions = pipeline | "Read Transactions Topic" >> beam.io.ReadFromPubSub(
        topic=pipeline_options.transactions_topic)
  else:
    raw_transactions = pipeline | "Empty Transactions" >> beam.Create([])

  # 2. Read coupons stream
  if in_memory_coupons is not None:
    raw_coupons = pipeline | "Create Coupons" >> beam.Create(in_memory_coupons)
  elif pipeline_options.coupons_redemption_subscription:
    raw_coupons = pipeline | "Read Coupons Sub" >> beam.io.ReadFromPubSub(
        subscription=pipeline_options.coupons_redemption_subscription)
  elif pipeline_options.coupons_redemption_topic:
    raw_coupons = pipeline | "Read Coupons Topic" >> beam.io.ReadFromPubSub(
        topic=pipeline_options.coupons_redemption_topic)
  else:
    raw_coupons = pipeline | "Empty Coupons" >> beam.Create([])

  # 3. Parse and extract customer key with dead-letter side outputs
  parsed_tx_results = (
      raw_transactions
      | "Parse Transactions" >> beam.ParDo(ParseRecordDoFn(
          "transaction")).with_outputs(TAG_DEADLETTER, main="valid"))

  parsed_coupon_results = (
      raw_coupons
      | "Parse Coupons" >> beam.ParDo(ParseRecordDoFn("coupon")).with_outputs(
          TAG_DEADLETTER, main="valid"))

  valid_transactions = parsed_tx_results.valid
  valid_coupons = parsed_coupon_results.valid

  all_deadletters = (
      (parsed_tx_results[TAG_DEADLETTER], parsed_coupon_results[TAG_DEADLETTER])
      | "Merge Deadletter Errors" >> beam.Flatten())

  # 4. Merge valid streams and apply dynamic Session Windows
  gap_sec = getattr(pipeline_options, "session_gap_seconds", 900) or 900
  allowed_lateness_sec = getattr(pipeline_options, "allowed_lateness_seconds",
                                 60) or 60

  trigger = (
      AfterWatermark(
          late=AfterCount(1)) if allowed_lateness_sec > 0 else AfterWatermark())

  all_events = ((valid_transactions, valid_coupons)
                | "Merge Customer Streams" >> beam.Flatten()
                | "Assign Timestamps" >> beam.ParDo(AssignEventTimestampDoFn())
                | "Customer Session Window" >> beam.WindowInto(
                    Sessions(gap_sec),
                    allowed_lateness=Duration(seconds=allowed_lateness_sec),
                    trigger=trigger,
                    accumulation_mode=AccumulationMode.ACCUMULATING,
                )
                | "Group Customer Events" >> beam.GroupByKey())

  # 5. Process Sessions to produce granular unified records and Customer 360 profiles
  session_results = (
      all_events
      | "Process Customer Sessions" >> beam.ParDo(
          ProcessCustomerSessionDoFn()).with_outputs(
              TAG_SESSIONS, main="unified_records"))

  unified_records = session_results.unified_records
  customer_sessions = session_results[TAG_SESSIONS]

  # 6. Sinks
  project_id = (
      getattr(pipeline_options, "project_id", None) or
      pipeline_options.view_as(GoogleCloudOptions).project)
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

  if project_id and dataset:
    unified_schema = load_output_schema(
        getattr(pipeline_options, "output_schema_path", None),
        "unified_table.json",
        DEFAULT_OUTPUT_SCHEMA,
    )
    sessions_schema = load_output_schema(
        getattr(pipeline_options, "output_sessions_schema_path", None),
        "customer_sessions.json",
        DEFAULT_SESSIONS_SCHEMA,
    )

    unified_table_spec = f"{project_id}:{dataset}.{unified_table}"
    unified_records | "Write Unified to BigQuery" >> WriteToBigQuery(
        table=unified_table_spec,
        schema=unified_schema,
        create_disposition=BigQueryDisposition.CREATE_IF_NEEDED,
        write_disposition=BigQueryDisposition.WRITE_APPEND,
        method=write_method,
    )

    sessions_table_spec = f"{project_id}:{dataset}.{sessions_table}"
    customer_sessions | "Write Sessions to BigQuery" >> WriteToBigQuery(
        table=sessions_table_spec,
        schema=sessions_schema,
        create_disposition=BigQueryDisposition.CREATE_IF_NEEDED,
        write_disposition=BigQueryDisposition.WRITE_APPEND,
        method=write_method,
    )

    if deadletter_table:
      deadletter_schema = load_output_schema(
          getattr(pipeline_options, "deadletter_schema_path", None),
          "deadletter_table.json",
          DEFAULT_DEADLETTER_SCHEMA,
      )
      dlq_table_spec = f"{project_id}:{dataset}.{deadletter_table}"
      all_deadletters | "Write Deadletter to BigQuery" >> WriteToBigQuery(
          table=dlq_table_spec,
          schema=deadletter_schema,
          create_disposition=BigQueryDisposition.CREATE_IF_NEEDED,
          write_disposition=BigQueryDisposition.WRITE_APPEND,
          method=write_method,
      )

  return unified_records, customer_sessions, all_deadletters


def create_and_run_pipeline(
    pipeline_options: MyPipelineOptions,
    output_schema: Optional[Union[Dict[str, Any], str]] = None,
):
  """Launches the Customer Data Platform streaming pipeline on Dataflow or DirectRunner."""
  del output_schema  # Handled via options or schema loader
  logging.info("Starting Customer Data Platform pipeline with options: %s",
               pipeline_options)

  with Pipeline(options=pipeline_options) as p:
    build_pipeline(p, pipeline_options)
