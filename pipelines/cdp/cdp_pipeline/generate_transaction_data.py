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

import argparse
import asyncio
import json
import os
from google.cloud import pubsub_v1
import pandas as pd


async def publish_coupons_to_pubsub(project_id: str | None = None,
                                    transactions_topic: str | None = None,
                                    coupons_topic: str | None = None,
                                    bucket_name: str | None = None):
  project_id = project_id or os.environ.get("PROJECT", "<project_id>")
  transactions_topic_name = transactions_topic or os.environ.get(
      "TRANSACTIONS_TOPIC", "transactions")
  coupons_topic_name = coupons_topic or os.environ.get(
      "COUPON_REDEMPTION_TOPIC", "coupon_redemption")
  gcs_bucket_env = os.environ.get("GCS_BUCKET", "")
  if not bucket_name and gcs_bucket_env:
    bucket_name = gcs_bucket_env.replace("gs://", "").split("/")[0]

  sample_transactions_id = [
      "27601281299", "27757099033", "28235291311", "27021203242",
      "27101290145", "27853175697"
  ]

  # Local directory fallback
  current_dir = os.path.dirname(os.path.abspath(__file__))
  local_trans_path = os.path.join(
      os.path.dirname(current_dir), "input_data", "transaction_data.csv")
  local_coupons_path = os.path.join(
      os.path.dirname(current_dir), "input_data", "coupon_redempt.csv")

  if bucket_name:
    gcs_prefix = (
        f"gs://{bucket_name}/assets/dataflow-solution-guide-cdp/input_data"
    )
    trans_gcs = f"{gcs_prefix}/transaction_data.csv"
    coupons_gcs = f"{gcs_prefix}/coupon_redempt.csv"
    try:
      transactions_df = pd.read_csv(trans_gcs, dtype=str)
      coupons_df = pd.read_csv(coupons_gcs, dtype=str)
    except Exception:  # pylint: disable=broad-exception-caught
      print(
          f"Falling back to local CSV files from {local_trans_path} and "
          f"{local_coupons_path}")
      transactions_df = pd.read_csv(local_trans_path, dtype=str)
      coupons_df = pd.read_csv(local_coupons_path, dtype=str)
  else:
    transactions_df = pd.read_csv(local_trans_path, dtype=str)
    coupons_df = pd.read_csv(local_coupons_path, dtype=str)

  publisher = pubsub_v1.PublisherClient()
  transactions_topic_path = (
      transactions_topic_name
      if transactions_topic_name.startswith("projects/") else
      publisher.topic_path(project_id, transactions_topic_name))
  coupons_topic_path = (
      coupons_topic_name if coupons_topic_name.startswith("projects/") else
      publisher.topic_path(project_id, coupons_topic_name))

  filtered_trans_df = transactions_df[transactions_df["transaction_id"].isin(
      sample_transactions_id)]
  filtered_coupons_df = coupons_df[coupons_df["transaction_id"].isin(
      sample_transactions_id)]

  if filtered_trans_df.empty:
    filtered_trans_df = transactions_df
  if filtered_coupons_df.empty:
    filtered_coupons_df = coupons_df

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
  parser = argparse.ArgumentParser(
      description="Publish sample transactions and coupons to Pub/Sub.")
  parser.add_argument(
      "--project_id",
      default=os.environ.get("PROJECT"),
      help="GCP Project ID (defaults to $PROJECT)")
  parser.add_argument(
      "--transactions_topic",
      default=os.environ.get("TRANSACTIONS_TOPIC"),
      help="Transactions Pub/Sub topic name or ID (defaults to $TRANSACTIONS_TOPIC)"
  )
  parser.add_argument(
      "--coupons_topic",
      default=os.environ.get("COUPON_REDEMPTION_TOPIC"),
      help="Coupons Pub/Sub topic name or ID (defaults to $COUPON_REDEMPTION_TOPIC)"
  )
  parser.add_argument(
      "--bucket_name",
      default=None,
      help="Optional GCS bucket containing input data")
  args = parser.parse_args()

  asyncio.run(
      publish_coupons_to_pubsub(
          project_id=args.project_id,
          transactions_topic=args.transactions_topic,
          coupons_topic=args.coupons_topic,
          bucket_name=args.bucket_name))
