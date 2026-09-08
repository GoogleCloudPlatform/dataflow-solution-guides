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
"""Asynchronous Google Cloud Pub/Sub streaming publisher for Customer Data Platform."""

import argparse
import asyncio
import json
import logging
import os
import random
from typing import Optional

from google.cloud import pubsub_v1

from simulator.generator import generate_synthetic_session_events


def get_topic_path(publisher: pubsub_v1.PublisherClient, project: str,
                   topic: str) -> str:
  """Returns a fully-qualified Pub/Sub topic path."""
  if topic.startswith("projects/"):
    return topic
  return publisher.topic_path(project, topic)


async def publish_events_to_pubsub(
    project_id: Optional[str] = None,
    transactions_topic: Optional[str] = None,
    coupons_topic: Optional[str] = None,
    continuous: bool = False,
    interval: float = 1.0,
    count: int = 10,
    inject_errors: bool = False,
):
  """Publishes sessionized transactions and coupon redemptions to Pub/Sub topics."""
  project_id = project_id or os.environ.get("PROJECT", "<project_id>")
  tx_topic_name = transactions_topic or os.environ.get("TRANSACTIONS_TOPIC",
                                                       "cdp-transactions")
  cp_topic_name = coupons_topic or os.environ.get("COUPON_REDEMPTION_TOPIC",
                                                  "cdp-coupon-redemption")

  publisher = pubsub_v1.PublisherClient()
  tx_path = get_topic_path(publisher, project_id, tx_topic_name)
  cp_path = get_topic_path(publisher, project_id, cp_topic_name)

  logging.info("Publishing transactions to: %s", tx_path)
  logging.info("Publishing coupons to: %s", cp_path)

  households = ["1", "13", "42", "99", "125"]
  tx_counter = 27601281000
  published_count = 0

  while True:
    hh = random.choice(households)
    tx_counter += 1
    tx_items, cp_items = generate_synthetic_session_events(hh, tx_counter)

    # Publish transactions
    for tx in tx_items:
      payload = json.dumps(tx).encode("utf-8")
      future = publisher.publish(tx_path, payload)
      logging.info("Published tx [hh=%s, tx=%s]: msg_id=%s", hh,
                   tx["transaction_id"], future.result())

    # Publish coupons
    for cp in cp_items:
      payload = json.dumps(cp).encode("utf-8")
      future = publisher.publish(cp_path, payload)
      logging.info("Published coupon [hh=%s, tx=%s]: msg_id=%s", hh,
                   cp["transaction_id"], future.result())

    # Error injection test (DLQ verification)
    if inject_errors and random.random() < 0.2:
      corrupt_payload = b"NOT_VALID_JSON_{broken: true"
      future = publisher.publish(tx_path, corrupt_payload)
      logging.info("Injected malformed transaction DLQ test payload: msg_id=%s",
                   future.result())

    published_count += len(tx_items) + len(cp_items)
    if not continuous and published_count >= count:
      break

    await asyncio.sleep(interval)


def main():
  """Parses command line arguments and runs the Pub/Sub publishing loop."""
  logging.basicConfig(
      level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
  parser = argparse.ArgumentParser(
      description="Publish Customer Data Platform sessions and events to Pub/Sub."
  )
  parser.add_argument(
      "--project",
      "--project_id",
      dest="project_id",
      default=os.environ.get("PROJECT"),
      help="GCP Project ID (defaults to $PROJECT)",
  )
  parser.add_argument(
      "--transactions_topic",
      default=os.environ.get("TRANSACTIONS_TOPIC"),
      help="Transactions Pub/Sub topic name or path",
  )
  parser.add_argument(
      "--coupons_topic",
      default=os.environ.get("COUPON_REDEMPTION_TOPIC"),
      help="Coupons Pub/Sub topic name or path",
  )
  parser.add_argument(
      "--continuous",
      action="store_true",
      help="Continuously stream synthetic sessions until cancelled",
  )
  parser.add_argument(
      "--interval",
      type=float,
      default=1.0,
      help="Sleep interval in seconds between published customer sessions",
  )
  parser.add_argument(
      "--count",
      type=int,
      default=20,
      help="Total events to publish when not running continuously",
  )
  parser.add_argument(
      "--inject_errors",
      action="store_true",
      help="Inject malformed payloads to verify Dead-Letter Queue (DLQ) processing",
  )
  args = parser.parse_args()

  asyncio.run(
      publish_events_to_pubsub(
          project_id=args.project_id,
          transactions_topic=args.transactions_topic,
          coupons_topic=args.coupons_topic,
          continuous=args.continuous,
          interval=args.interval,
          count=args.count,
          inject_errors=args.inject_errors,
      ))


if __name__ == "__main__":
  main()
