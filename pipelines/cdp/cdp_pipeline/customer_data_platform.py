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

import logging
import apache_beam as beam
from apache_beam import Pipeline, PCollection
from apache_beam.io.gcp.bigquery import WriteToBigQuery
import json
from apache_beam.transforms.window import FixedWindows
from apache_beam.transforms.trigger import AfterWatermark, AfterProcessingTime, AccumulationMode
from cdp_pipeline.options import MyPipelineOptions

# FIXME: This will not work in Dataflow, the schema should be passed as an option to the PTrasnform
# Get the schema of bigquery output table
with open("./schema/unified_table.json", encoding="utf-8") as schema_file:
  output_schema = json.load(schema_file)


def left_join(key_value_pair):
  _, values = key_value_pair
  trans_values, coupon_redempt_values = values
  if not coupon_redempt_values:
    coupon_redempt_values = [None]  # Fill missing values with None
  for trans_value in trans_values:
    if trans_value is not None:
      for coupon_redempt_value in coupon_redempt_values:
        coupon_redempt_value: dict
        unified_data = {
            "transaction_id":
                trans_value["transaction_id"],
            "household_key":
                trans_value["household_key"],
            "coupon_upc":  # FIXME: Is this a dictionary?
                coupon_redempt_value["coupon_upc"]
                if coupon_redempt_value is not None else None,
            "product_id":
                trans_value["product_id"],
            "coupon_discount":
                trans_value["coupon_disc"],
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
                 output_dataset: str, output_table: str, unified_schema: str):
  unified_pcoll | "Write to bigquery" >> \
  WriteToBigQuery(
          project=project_id,
          dataset=output_dataset,
          table=output_table,
          schema=unified_schema,
          create_disposition=beam.io.BigQueryDisposition.CREATE_IF_NEEDED,
          write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND
      )


def create_and_run_pipeline(pipeline_options: MyPipelineOptions):
  logging.info(pipeline_options)

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
