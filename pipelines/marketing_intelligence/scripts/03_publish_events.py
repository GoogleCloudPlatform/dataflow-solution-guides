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
# pylint: disable=invalid-name
"""Publishes simulated user interaction events to Pub/Sub for pipeline ingestion.

Streams JSON events to the Dataflow input topic with configurable rate.
"""

import argparse
from datetime import datetime, timezone
import json
import os
import random
import time
from typing import Dict, Any

from google.cloud import pubsub_v1

CATEGORIES = ["electronics", "clothing", "home", "beauty", "sports"]
EVENT_TYPES = ["view_item", "add_to_cart", "view_cart", "checkout_start"]
EVENT_WEIGHTS = [0.60, 0.25, 0.10, 0.05]


def generate_single_event(event_counter: int) -> Dict[str, Any]:
  """Generates a realistic single e-commerce event."""
  # 90% known seeded users, 10% cold-start / new visitors
  if random.random() < 0.90:
    user_id = f"user_{random.randint(1001, 2000)}"
  else:
    user_id = f"user_{random.randint(9000, 9999)}"

  event_type = random.choices(EVENT_TYPES, weights=EVENT_WEIGHTS, k=1)[0]
  category = random.choice(CATEGORIES)
  item_id = f"prod_{category[:4]}_{random.randint(100, 999)}"

  # Duration and cart value vary by event type
  if event_type == "checkout_start":
    session_sec = random.randint(180, 1200)
    cart_val = round(random.uniform(50.0, 450.0), 2)
  elif event_type == "add_to_cart":
    session_sec = random.randint(60, 600)
    cart_val = round(random.uniform(20.0, 250.0), 2)
  else:
    session_sec = random.randint(10, 300)
    cart_val = round(random.uniform(0.0, 80.0), 2)

  return {
      "event_id": f"evt_{int(time.time())}_{event_counter}",
      "user_id": user_id,
      "timestamp": datetime.now(timezone.utc).isoformat(),
      "event_type": event_type,
      "item_id": item_id,
      "item_category": category,
      "session_duration_sec": session_sec,
      "cart_value": cart_val,
  }


def publish_events(project: str,
                   topic_name: str,
                   count: int = 100,
                   rate_per_sec: float = 5.0) -> None:
  """Publishes mock events to Pub/Sub topic."""
  publisher = pubsub_v1.PublisherClient()
  topic_path = publisher.topic_path(project, topic_name)

  print(f"Publishing to Pub/Sub topic: {topic_path}")
  count_str = "unlimited (continuous)" if count <= 0 else str(count)
  print(f"Target count: {count_str}, Rate: {rate_per_sec} events/sec")

  sleep_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0
  counter = 0

  try:
    while count <= 0 or counter < count:
      counter += 1
      event = generate_single_event(counter)
      data = json.dumps(event).encode("utf-8")
      future = publisher.publish(topic_path, data)
      _ = future.result()

      if counter % 25 == 0:
        latest_id = event["event_id"]
        latest_user = event["user_id"]
        latest_type = event["event_type"]
        print(f"Published {counter} events (Latest: {latest_id}, "
              f"User: {latest_user}, Type: {latest_type})")

      if sleep_interval > 0:
        time.sleep(sleep_interval)

  except KeyboardInterrupt:
    print(f"\nPublishing interrupted by user after {counter} events.")

  print(f"Finished publishing. Total events sent: {counter}")


def main():
  parser = argparse.ArgumentParser(
      description="Publish simulated e-commerce events to Pub/Sub.")
  parser.add_argument(
      "--project",
      type=str,
      default=os.environ.get("PROJECT"),
      help="GCP Project ID. Defaults to $PROJECT environment variable.",
  )
  parser.add_argument(
      "--topic",
      type=str,
      default=os.environ.get(
          "INPUT_TOPIC_NAME",
          "dataflow-solutions-guide-market-intelligence-input",
      ),
      help="Pub/Sub topic name.",
  )
  parser.add_argument(
      "--count",
      type=int,
      default=100,
      help="Number of events to publish (use 0 or -1 for continuous). Default: 100.",
  )
  parser.add_argument(
      "--rate",
      type=float,
      default=5.0,
      help="Events per second. Default: 5.0.",
  )
  args = parser.parse_args()

  if not args.project:
    parser.error("--project is required (or set PROJECT environment variable).")

  publish_events(
      project=args.project,
      topic_name=args.topic,
      count=args.count,
      rate_per_sec=args.rate,
  )


if __name__ == "__main__":
  main()
