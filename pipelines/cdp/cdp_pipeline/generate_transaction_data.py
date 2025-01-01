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
A data generator for the Customer Data Platform analytics pipeline.
"""

from google.cloud import pubsub_v1
import json
import pandas as pd
import asyncio


async def publish_coupons_to_pubsub():
  bucket_name = "<bucket_name>"
  project_id = "<project_id>"

  # Example: ["27601281299","27757099033","28235291311","27021203242","27101290145","27853175697"]
  transactions_id = [
      "<List of sample transaction IDs to test the pipeline on.>"
  ]
  transactions_topic_name = "transactions"
  # Reference example - "dataflow-solution-guide-cdp/input_data/transaction_data.csv"
  transactions_data = "<path to transactions data in gcs bucket>"

  coupons_topic_name = "coupon_redemption"
  # reference example - "dataflow-solution-guide-cdp/input_data/coupon_redempt.csv"
  coupons_data = "<path to coupon redemption data in gcs bucket>"

  transactions_df = pd.read_csv(
      f"gs://{bucket_name}/{transactions_data}", dtype=str)
  coupons_df = pd.read_csv(f"gs://{bucket_name}/{coupons_data}", dtype=str)
  publisher = pubsub_v1.PublisherClient()

  transactions_topic_path = publisher.topic_path(project_id,
                                                 transactions_topic_name)
  coupons_topic_path = publisher.topic_path(project_id, coupons_topic_name)
  filtered_trans_df = transactions_df[transactions_df["transaction_id"].isin(
      transactions_id)]
  filtered_coupons_df = coupons_df[coupons_df["transaction_id"].isin(
      transactions_id)]
  await asyncio.gather(
      publish_coupons(filtered_coupons_df, publisher, coupons_topic_path),
      publish_transactions(filtered_trans_df, publisher,
                           transactions_topic_path))


async def publish_coupons(filtered_coupons_df, publisher, coupons_topic_path):
  for _, row in filtered_coupons_df.iterrows():
    coupon_message = json.dumps(row.to_dict()).encode("utf-8")
    print(coupon_message)
    future = publisher.publish(coupons_topic_path, coupon_message)
    print(f"Published  coupon message ID: {future.result()}")
    await asyncio.sleep(3)


async def publish_transactions(filtered_trans_df, publisher,
                               transactions_topic_path):
  for _, row in filtered_trans_df.iterrows():
    transaction_message = json.dumps(row.to_dict()).encode("utf-8")
    print(transaction_message)
    future = publisher.publish(transactions_topic_path, transaction_message)
    print(f"Published transaction message ID: {future.result()}")
    await asyncio.sleep(1)


if __name__ == "__main__":
  asyncio.run(publish_coupons_to_pubsub())
