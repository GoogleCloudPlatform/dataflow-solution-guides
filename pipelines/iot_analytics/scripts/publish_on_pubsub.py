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
Pipeline of the IoT Analytics Dataflow Solution guide.
"""

import json
from google.cloud import pubsub_v1
import os


def publish_messages(project, topic, data_path):
  """
    Publishes JSON messages from a file to a Pub/Sub topic.

    Args:
        project: The ID of the Google Cloud project.
        topic: The ID of the Pub/Sub topic.
        data_path: The path to the JSON data file.
    """

  publisher = pubsub_v1.PublisherClient()
  topic_path = publisher.topic_path(project, topic)

  with open(data_path, "r", encoding="utf-8") as f:
    for line in f:
      try:
        # Parse each line as a JSON object
        json_data = json.loads(line)

        # Publish the JSON data as a message
        message_data = json.dumps(json_data).encode("utf-8")
        future = publisher.publish(topic_path, message_data)
        print(f"Published message ID: {future.result()}")

      except json.JSONDecodeError as e:
        print(f"Error decoding JSON: {e}")


if __name__ == "__main__":
  current_directory = os.getcwd()
  publish_messages(
      os.environ.get("PROJECT_ID"), os.environ.get("PUBSUB_TOPIC_ID"),
      os.environ.get("VEHICLE_DATA_PATH"))
