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
Customer Data Platform analytics pipeline for the Dataflow Solution Guides.
"""

import json
import logging
import os
from typing import Any, Generator, Iterable, Optional, Union

import apache_beam as beam
from apache_beam import Pipeline, PCollection
from apache_beam.io.gcp.bigquery import WriteToBigQuery
from apache_beam.transforms.trigger import AccumulationMode, AfterProcessingTime, AfterWatermark
from apache_beam.transforms.window import FixedWindows
from cdp_pipeline.options import MyPipelineOptions

DEFAULT_OUTPUT_SCHEMA: dict[str, Any] = {
    "fields": [
        {
            "name": "transaction_id",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "household_key",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "coupon_upc",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "product_id",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "coupon_discount",
            "type": "STRING",
            "mode": "NULLABLE"
        },
    ]
}


def load_output_schema(
    schema_path: Optional[str] = None) -> Union[dict[str, Any], str]:
  """Loads the BigQuery output table schema from a specified path or default package location."""
  if schema_path:
    with open(schema_path, encoding="utf-8") as schema_file:
      return json.load(schema_file)

  # Check relative to the schema directory inside the cdp pipeline package
  default_schema_file = os.path.join(
      os.path.dirname(os.path.dirname(__file__)), "schema",
      "unified_table.json")
  if os.path.exists(default_schema_file):
    with open(default_schema_file, encoding="utf-8") as schema_file:
      return json.load(schema_file)

  return DEFAULT_OUTPUT_SCHEMA


def left_join(
    key_value_pair: tuple[Any, tuple[Iterable[dict[str, Any]],
                                     Iterable[Optional[dict[str, Any]]]]]
) -> Generator[dict[str, Any], None, None]:
  """Performs a left join between transaction and coupon redemption records."""
  _, values = key_value_pair
  trans_values, coupon_redempt_values = values
  coupon_list = list(coupon_redempt_values)
  if not coupon_list:
    coupon_list = [None]  # Fill missing values with None
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
def _read_pub_sub_topic(p: Pipeline, topic: str) -> PCollection[str]:
  msgs: PCollection[bytes] = (
      p
      | "Read subscription" >> beam.io.ReadFromPubSub(topic=topic)
      | "Decode Transactions" >>
      beam.Map(lambda msg: json.loads(msg.decode("utf-8")))
      | "Add Transaction Key" >> beam.Map(lambda transaction: ((transaction[
          "transaction_id"], transaction["household_key"]), transaction))
      | "Window Transactions" >> beam.WindowInto(
          FixedWindows(60),
          trigger=AfterWatermark(early=AfterProcessingTime(10)),
          accumulation_mode=AccumulationMode.DISCARDING))

  return msgs


@beam.ptransform_fn
def _unify_data(pcolls: tuple[PCollection, PCollection]) -> PCollection[str]:
  transactions_pcoll, coupons_redempt_pcoll = pcolls
  unified_data = ((transactions_pcoll, coupons_redempt_pcoll)
                  | "Combine Transactions and Coupons" >> beam.CoGroupByKey()
                  | beam.FlatMap(left_join))
  return unified_data


@beam.ptransform_fn
def _write_to_bq(unified_pcoll: PCollection, project_id: str,
                 output_dataset: str, output_table: str,
                 unified_schema: Union[dict[str, Any], str]):
  unified_pcoll | "Write to bigquery" >> \
  WriteToBigQuery(
          project=project_id,
          dataset=output_dataset,
          table=output_table,
          schema=unified_schema,
          create_disposition=beam.io.BigQueryDisposition.CREATE_IF_NEEDED,
          write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND
      )


def create_and_run_pipeline(pipeline_options: MyPipelineOptions,
                            output_schema: Optional[Union[dict[str, Any],
                                                          str]] = None):
  logging.info(pipeline_options)

  if output_schema is None:
    schema_path = getattr(pipeline_options, "output_schema_path", None)
    output_schema = load_output_schema(schema_path)

  with Pipeline(options=pipeline_options) as p:

    # Read transcation pub-sub topic
    transactions_pcoll = p | "Read transactions topic" >> _read_pub_sub_topic(
        topic=pipeline_options.transactions_topic)
    # Read coupon_redemption pub-sub topic
    coupons_redempt_pcoll = p | "Read coupon redemption topic" >> _read_pub_sub_topic(
        topic=pipeline_options.coupons_redemption_topic)

    # call _unify_data to unify the data from two streaming sources
    unified_data: PCollection = (transactions_pcoll, coupons_redempt_pcoll
                                ) | "Transform" >> _unify_data()

    # Write it to bigquery. Provide schema of the output table as parameter output_schema
    unified_data | "Write to bigquery" >> _write_to_bq(
        pipeline_options.project_id, pipeline_options.output_dataset,
        pipeline_options.output_table, output_schema)
