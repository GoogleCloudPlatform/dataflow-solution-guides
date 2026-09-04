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
"""
Publishes simulated IoT vehicle telemetry events to Cloud Pub/Sub.
"""

import argparse
from datetime import datetime, timezone
import json
import os
import time
from google.cloud import pubsub_v1


def get_topic_path(publisher: pubsub_v1.PublisherClient, project: str,
                   topic: str) -> str:
  """Returns a fully-qualified Pub/Sub topic path."""
  if topic.startswith("projects/"):
    return topic
  return publisher.topic_path(project, topic)


def publish_messages(project: str,
                     topic: str,
                     data_path: str,
                     continuous: bool = False,
                     interval: float = 1.0,
                     count: int = 0):
  """Publishes JSON messages from a file to a Pub/Sub topic."""
  publisher = pubsub_v1.PublisherClient()
  topic_path = get_topic_path(publisher, project, topic)
  print(f"Publishing messages to topic: {topic_path}")

  # Read source data
  lines = []
  try:
    with open(data_path, "r", encoding="utf-8") as f:
      for line in f:
        line_str = line.strip()
        if line_str:
          lines.append(json.loads(line_str))
  except FileNotFoundError:
    print(f"Data file not found: {data_path}. Running generator first...")
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    gen_script = os.path.join(current_script_dir, "create_data.py")
    os.system(f"python3 {gen_script}")
    with open(data_path, "r", encoding="utf-8") as f:
      for line in f:
        line_str = line.strip()
        if line_str:
          lines.append(json.loads(line_str))

  total_published = 0
  iteration = 0

  while True:
    iteration += 1
    futures = []
    for item in lines:
      event = dict(item)
      # In continuous simulation, stamp with current time
      if continuous:
        event["timestamp"] = datetime.now(
            timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

      message_data = json.dumps(event).encode("utf-8")
      future = publisher.publish(topic_path, message_data)
      futures.append(future)
      total_published += 1

      if count > 0 and total_published >= count:
        break

    for f in futures:
      f.result(timeout=30)

    print(
        f"Iteration {iteration}: Published batch of {len(futures)} events. Total: {total_published}"
    )

    if not continuous or (count > 0 and total_published >= count):
      break

    time.sleep(interval)

  print(f"Finished publishing. Total events published: {total_published}")


if __name__ == "__main__":
  script_dir = os.path.dirname(os.path.abspath(__file__))
  default_data_path = os.path.join(script_dir, "vehicle_data.jsonl")

  parser = argparse.ArgumentParser(
      description="Publish vehicle telemetry events to Pub/Sub")
  parser.add_argument(
      "--project", default=os.environ.get("PROJECT_ID"), help="GCP Project ID")
  parser.add_argument(
      "--topic",
      default=os.environ.get("PUBSUB_TOPIC_ID") or os.environ.get("TOPIC_ID"),
      help="Pub/Sub topic name or full URI")
  parser.add_argument(
      "--data_path",
      default=os.environ.get("VEHICLE_DATA_PATH") or default_data_path,
      help="Path to vehicle data JSONL file")
  parser.add_argument(
      "--continuous", action="store_true", help="Stream events continuously")
  parser.add_argument(
      "--interval",
      type=float,
      default=1.0,
      help="Interval in seconds between batches in continuous mode")
  parser.add_argument(
      "--count",
      type=int,
      default=0,
      help="Max total events to publish (0 = all / infinite in continuous)")

  args = parser.parse_args()

  if not args.project or not args.topic:
    raise ValueError(
        "Both --project and --topic (or PROJECT_ID and PUBSUB_TOPIC_ID env vars) are required."
    )

  publish_messages(
      project=args.project,
      topic=args.topic,
      data_path=args.data_path,
      continuous=args.continuous,
      interval=args.interval,
      count=args.count,
  )
