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
"""Generates and publishes synthetic gameplay events to Pub/Sub."""

import argparse
import datetime
import json
import os
import random
import sys
import time
from typing import Dict, Optional
from google.cloud import pubsub_v1

# Event types the pipeline reacts to. 'level_failed' and 'level_complete' drive
# most of the recommendation rules, so they are over-represented on purpose.
EVENT_TYPES = [
    "level_start",
    "level_complete",
    "level_failed",
    "level_failed",
    "purchase",
    "item_used",
]

# Players seeded by populate_bigtable.py, plus one that is deliberately absent
# from the feature store to exercise the feature-store miss path.
PLAYER_IDS = [
    "player_0001",
    "player_0002",
    "player_0003",
    "player_0004",
    "player_0005",
    "player_9999",
]


def generate_event(
    player_id: str,
    session_id: str,
    level: int,
    timestamp: Optional[str] = None,
) -> Dict[str, object]:
  """Constructs a valid gameplay event payload."""
  if not timestamp:
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
  return {
      "player_id": player_id,
      "session_id": session_id,
      "event_type": random.choice(EVENT_TYPES),
      "level": level,
      "score": random.randint(0, 25000),
      "event_timestamp": timestamp,
  }


def generate_error_payload(player_id: str) -> str:
  """Builds a payload the pipeline must route to the dead-letter topic."""
  error_type = random.choice(
      ["invalid_json", "bad_types", "no_player_id", "bad_timestamp", "raw_text"]
  )
  if error_type == "invalid_json":
    return f'{{"player_id": "{player_id}", "level": }}'
  if error_type == "bad_types":
    return json.dumps({"player_id": player_id, "level": "NOT_AN_INTEGER"})
  if error_type == "no_player_id":
    return json.dumps({"session_id": "orphan", "event_type": "level_start"})
  if error_type == "bad_timestamp":
    return json.dumps({"player_id": player_id, "event_timestamp": "last tuesday"})
  return "RAW_UNPARSEABLE_GAMEPLAY_EVENT"


def run_generator(
    project_id: str,
    topic_id: str,
    num_events: int,
    rate_per_sec: float,
    inject_errors: bool,
) -> None:
  """Streams synthetic gameplay events into Pub/Sub.

  Args:
      project_id: GCP project ID.
      topic_id: Pub/Sub topic ID or fully qualified topic name.
      num_events: Total events to publish (0 for a continuous stream).
      rate_per_sec: Target publishing rate in messages per second.
      inject_errors: Whether to inject malformed events to test the dead-letter
        topic.
  """
  publisher = pubsub_v1.PublisherClient()
  if "/" in topic_id:
    topic_path = topic_id
  else:
    topic_path = publisher.topic_path(project_id, topic_id)

  print(f"Publishing to topic: {topic_path}")
  print(f"Simulating {len(PLAYER_IDS)} players at ~{rate_per_sec} events/sec...")

  sessions: Dict[str, str] = {
      player_id: f"session_{random.randint(1000, 9999)}" for player_id in PLAYER_IDS
  }
  levels: Dict[str, int] = {player_id: 1 for player_id in PLAYER_IDS}

  delay = 1.0 / rate_per_sec if rate_per_sec > 0 else 0
  sent_count = 0

  try:
    while True:
      player_id = random.choice(PLAYER_IDS)

      if inject_errors and random.random() < 0.05:
        payload_str = generate_error_payload(player_id)
      else:
        event = generate_event(player_id, sessions[player_id], levels[player_id])
        payload_str = json.dumps(event)
        if event["event_type"] == "level_complete":
          levels[player_id] += 1

      publisher.publish(topic_path, payload_str.encode("utf-8"))
      sent_count += 1

      if sent_count % 100 == 0:
        print(f"[{datetime.datetime.now()}] Published {sent_count} events...")

      if num_events > 0 and sent_count >= num_events:
        print(f"Reached target of {num_events} events. Done.")
        break

      if delay > 0:
        time.sleep(delay)

  except KeyboardInterrupt:
    print(f"\nStopped by user. Total events sent: {sent_count}")


def parse_args() -> argparse.Namespace:
  """Parses command line arguments."""
  parser = argparse.ArgumentParser(
      description="Generate and publish synthetic gameplay events to Pub/Sub."
  )
  parser.add_argument(
      "--project_id",
      default=os.environ.get("PROJECT", ""),
      help="GCP Project ID (defaults to $PROJECT).",
  )
  parser.add_argument(
      "--topic",
      default=os.environ.get("INPUT_TOPIC", "gaming-events"),
      help="Pub/Sub topic ID or full path (defaults to $INPUT_TOPIC).",
  )
  parser.add_argument(
      "--num_events",
      type=int,
      default=500,
      help="Number of events to generate (0 for a continuous stream, default: 500).",
  )
  parser.add_argument(
      "--rate",
      type=float,
      default=20.0,
      help="Publishing rate in events per second (default: 20.0).",
  )
  parser.add_argument(
      "--inject_errors",
      action="store_true",
      help="Inject malformed events to verify the dead-letter topic.",
  )
  return parser.parse_args()


def main() -> None:
  """CLI entry point."""
  args = parse_args()
  if not args.project_id:
    print("Error: --project_id or PROJECT env variable is required.")
    sys.exit(1)

  run_generator(
      project_id=args.project_id,
      topic_id=args.topic,
      num_events=args.num_events,
      rate_per_sec=args.rate,
      inject_errors=args.inject_errors,
  )


if __name__ == "__main__":
  main()
