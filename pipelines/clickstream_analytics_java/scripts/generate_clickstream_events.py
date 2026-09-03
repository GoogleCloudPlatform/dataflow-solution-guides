"""Generates and publishes synthetic Wikipedia clickstream events to Pub/Sub."""

import argparse
import datetime
import json
import os
import random
import sys
import time
from typing import Dict, Optional
from google.cloud import pubsub_v1


PAGE_GRAPH = {
    "Main_Page": [
        "Artificial_intelligence", "Earth", "Google_Cloud_Platform", "Physics"
    ],
    "Artificial_intelligence": [
        "Machine_learning", "Deep_learning", "Computer_vision", "Main_Page"
    ],
    "Machine_learning": [
        "Cloud_Dataflow", "BigQuery", "Artificial_intelligence", "Statistics"
    ],
    "Cloud_Dataflow": [
        "Apache_Beam", "BigQuery", "Cloud_Bigtable", "Google_Cloud_Platform"
    ],
    "Apache_Beam": [
        "Cloud_Dataflow", "Flink", "Spark", "Google_Cloud_Platform"
    ],
    "Google_Cloud_Platform": [
        "BigQuery", "Cloud_Bigtable", "Cloud_Dataflow", "Main_Page"
    ],
    "BigQuery": ["Cloud_Bigtable", "SQL", "Google_Cloud_Platform", "Main_Page"],
    "Cloud_Bigtable": ["NoSQL", "BigQuery", "Cloud_Dataflow", "Main_Page"],
    "Earth": ["Physics", "Solar_System", "Main_Page"],
    "Physics": ["Earth", "Quantum_Mechanics", "Main_Page"],
}

ALL_PAGES = list(PAGE_GRAPH.keys())
LINK_TYPES = ["link", "link", "link", "external", "other"]


def generate_event(
    user_id: str,
    prev_page: str,
    curr_page: str,
    timestamp: Optional[str] = None,
) -> Dict[str, object]:
  """Constructs a valid clickstream event payload."""
  if not timestamp:
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
  return {
      "user_id": user_id,
      "timestamp": timestamp,
      "prev": prev_page,
      "curr": curr_page,
      "type": random.choice(LINK_TYPES),
      "n": 1,
  }


def run_generator(
    project_id: str,
    topic_id: str,
    num_events: int,
    num_users: int,
    rate_per_sec: float,
    inject_errors: bool,
) -> None:
  """Streams synthetic clickstream events into Pub/Sub.

  Args:
      project_id: GCP project ID.
      topic_id: Pub/Sub topic ID or fully qualified topic name.
      num_events: Total events to publish (or 0 for continuous generation).
      num_users: Number of simulated concurrent users.
      rate_per_sec: Target publishing rate in messages per second.
      inject_errors: Whether to inject occasional malformed events to test DLQ.
  """
  publisher = pubsub_v1.PublisherClient()
  if "/" in topic_id:
    topic_path = topic_id
  else:
    topic_path = publisher.topic_path(project_id, topic_id)

  print(f"Publishing to topic: {topic_path}")
  print(f"Simulating {num_users} users at ~{rate_per_sec} events/sec...")

  user_states: Dict[str, str] = {
      f"user_{i:04d}": "Main_Page" for i in range(1, num_users + 1)
  }

  delay = 1.0 / rate_per_sec if rate_per_sec > 0 else 0
  sent_count = 0

  try:
    while True:
      user_id = random.choice(list(user_states.keys()))
      curr_page = user_states[user_id]
      next_candidates = PAGE_GRAPH.get(curr_page, ALL_PAGES)
      next_page = random.choice(next_candidates)

      # 3% chance to inject an error event to test Deadletter routing
      if inject_errors and random.random() < 0.03:
        error_type = random.choice(
            ["invalid_json", "bad_types", "corrupted_payload"]
        )
        if error_type == "invalid_json":
          payload_str = f'{{"user_id": "{user_id}", "curr": "{next_page}", "malformed": }}'
        elif error_type == "bad_types":
          payload_str = json.dumps({
              "user_id": user_id,
              "curr": next_page,
              "n": "NOT_AN_INTEGER_ERROR",
          })
        else:
          payload_str = "RAW_UNPARSEABLE_TEXT_CLICKSTREAM_EVENT"
      else:
        event = generate_event(user_id, curr_page, next_page)
        payload_str = json.dumps(event)
        user_states[user_id] = next_page

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
      description="Generate and publish synthetic clickstream events to Pub/Sub."
  )
  parser.add_argument(
      "--project_id",
      default=os.environ.get("PROJECT_ID", ""),
      help="GCP Project ID (defaults to $PROJECT_ID).",
  )
  parser.add_argument(
      "--topic",
      default=os.environ.get("PUBSUB_TOPIC", "wikipedia-clickstream"),
      help="Pub/Sub topic ID or full path (default: wikipedia-clickstream or $PUBSUB_TOPIC).",
  )
  parser.add_argument(
      "--num_events",
      type=int,
      default=500,
      help="Number of events to generate (0 for unlimited continuous stream, default: 500).",
  )
  parser.add_argument(
      "--num_users",
      type=int,
      default=20,
      help="Number of concurrent user sessions to simulate (default: 20).",
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
      help="Occasionally inject malformed/oversized events to verify dead-letter table.",
  )
  return parser.parse_args()


def main() -> None:
  """CLI entry point."""
  args = parse_args()
  if not args.project_id:
    print("Error: --project_id or PROJECT_ID env variable is required.")
    sys.exit(1)

  run_generator(
      project_id=args.project_id,
      topic_id=args.topic,
      num_events=args.num_events,
      num_users=args.num_users,
      rate_per_sec=args.rate,
      inject_errors=args.inject_errors,
  )


if __name__ == "__main__":
  main()
