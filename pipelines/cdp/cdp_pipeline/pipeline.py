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

import logging
from typing import Any, Iterable, Optional, Tuple

import apache_beam as beam
from apache_beam import Pipeline, PCollection
from apache_beam.transforms.trigger import AccumulationMode, AfterCount, AfterWatermark
from apache_beam.transforms.window import Sessions
from apache_beam.utils.timestamp import Duration

from cdp_pipeline.models import EventType
from cdp_pipeline.options import MyPipelineOptions
from cdp_pipeline.parsing import (
    AssignEventTimestampDoFn,
    ParseRecordDoFn,
    TAG_DEADLETTER,
)
from cdp_pipeline.sessionization import (
    ProcessCustomerSessionDoFn,
    TAG_SESSIONS,
)
from cdp_pipeline.sinks import apply_bigquery_sinks


def build_pipeline(
    pipeline: Pipeline,
    pipeline_options: MyPipelineOptions,
    in_memory_transactions: Optional[Iterable[Any]] = None,
    in_memory_coupons: Optional[Iterable[Any]] = None,
) -> Tuple[PCollection, PCollection, PCollection]:
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

  # Note: AccumulationMode.ACCUMULATING ensures late-arriving events (such as
  # delayed coupon redemptions) can still join against prior transactions in the
  # session and recalculate complete session aggregates. Because BigQuery sinks use
  # WRITE_APPEND, late panes append updated records which can be deduplicated
  # downstream in BigQuery views using processed_timestamp.
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

  # 6. Apply Storage Write API Sinks
  apply_bigquery_sinks(
      unified_records=unified_records,
      customer_sessions=customer_sessions,
      all_deadletters=all_deadletters,
      pipeline_options=pipeline_options,
  )

  return unified_records, customer_sessions, all_deadletters


def create_and_run_pipeline(pipeline_options: MyPipelineOptions):
  """Launches the Customer Data Platform streaming pipeline on Dataflow or DirectRunner."""
  logging.info("Starting Customer Data Platform pipeline with options: %s",
               pipeline_options)

  with Pipeline(options=pipeline_options) as p:
    build_pipeline(p, pipeline_options)
