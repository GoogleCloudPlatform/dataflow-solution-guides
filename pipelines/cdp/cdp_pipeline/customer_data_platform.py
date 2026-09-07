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
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple, Union

import apache_beam as beam
from apache_beam import Pipeline, PCollection
from apache_beam.io.gcp.bigquery import BigQueryDisposition, WriteToBigQuery
from apache_beam.metrics import Metrics
from apache_beam.options.pipeline_options import GoogleCloudOptions
from apache_beam.transforms.trigger import AccumulationMode, AfterCount, AfterWatermark
from apache_beam.transforms.window import Sessions, TimestampedValue
from apache_beam.utils.timestamp import Duration

from cdp_pipeline.models import (
    CouponRedemption,
    CustomerInteractionEvent,
    CustomerSessionProfile,
    EventType,
    TransactionItem,
    UnifiedTransactionRecord,
)
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
      element: Tuple[str, Iterable[CustomerInteractionEvent]],
      window=beam.DoFn.WindowParam,
  ) -> Generator[Any, None, None]:
    household_key, events_iter = element
    events = list(events_iter)
    if not events:
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

    transactions: List[Tuple[str, TransactionItem, Optional[str]]] = []
    coupons_by_tx: Dict[str,
                        List[CouponRedemption]] = collections.defaultdict(list)

    for ev in events:
      if ev.transaction is not None:
        transactions.append(
            (ev.transaction_id, ev.transaction, ev.event_timestamp))
      elif ev.coupon is not None:
        coupons_by_tx[ev.transaction_id].append(ev.coupon)

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
        if c.campaign:
          campaigns.add(c.campaign)

    for tx_id, tx, event_ts in transactions:
      distinct_tx_ids.add(tx_id)
      if tx.product_id:
        distinct_products.add(tx.product_id)
      if tx.store_id:
        stores.add(tx.store_id)

      total_spend += tx.sales_value
      total_items += tx.quantity
      total_discount += (tx.retail_disc + tx.coupon_disc)

      matching_coupons = coupons_by_tx.get(tx_id, [])
      if not matching_coupons:
        coupon_iter: List[Optional[CouponRedemption]] = [None]
      else:
        coupon_iter = matching_coupons
        self.matched_coupons_counter.inc(len(matching_coupons))

      for coup in coupon_iter:
        coupon_upc = coup.coupon_upc if coup else None
        campaign = coup.campaign if coup else None

        unified_record = UnifiedTransactionRecord(
            session_id=session_id,
            transaction_id=tx_id,
            household_key=household_key,
            product_id=tx.product_id,
            quantity=tx.quantity,
            sales_value=tx.sales_value,
            store_id=tx.store_id,
            retail_disc=tx.retail_disc,
            coupon_discount=tx.coupon_disc,
            coupon_match_disc=tx.coupon_match_disc,
            coupon_upc=coupon_upc,
            campaign=campaign,
            day=tx.day,
            trans_time=tx.trans_time,
            week_no=tx.week_no,
            event_timestamp=event_ts or processed_ts,
            processed_timestamp=processed_ts,
        )
        yield unified_record
        self.unified_items_counter.inc()

    session_profile = CustomerSessionProfile(
        session_id=session_id,
        household_key=household_key,
        session_start=session_start_iso,
        session_end=session_end_iso,
        session_duration_sec=session_duration_sec,
        total_transactions=len(distinct_tx_ids),
        total_items_purchased=total_items,
        total_spend=round(total_spend, 2),
        total_discount=round(total_discount, 2),
        coupons_redeemed_count=coupons_redeemed_count,
        distinct_products_count=len(distinct_products),
        campaigns=sorted(list(campaigns)),
        stores_visited=sorted(list(stores)),
        processed_timestamp=processed_ts,
    )
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
      | "Parse Transactions" >> beam.ParDo(
          ParseRecordDoFn(EventType.TRANSACTION)).with_outputs(
              TAG_DEADLETTER, main="valid"))

  parsed_coupon_results = (
      raw_coupons
      | "Parse Coupons" >> beam.ParDo(ParseRecordDoFn(
          EventType.COUPON)).with_outputs(TAG_DEADLETTER, main="valid"))

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
          DEFAULT_DEADLETTER_SCHEMA,
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
