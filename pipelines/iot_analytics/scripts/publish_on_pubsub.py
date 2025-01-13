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

# Replace with your project ID and Pub/Sub topic ID
current_directory = os.getcwd()
project_id = os.environ.get("PROJECT_ID")
vehicle_data_path = os.environ.get("VEHICLE_DATA_PATH")
topic_id = os.environ.get("PUBSUB_TOPIC_ID")


def publish_messages(project_id, topic_id, vehicle_data_path):
  """
    Publishes JSON messages from a file to a Pub/Sub topic.

    Args:
        project_id: The ID of the Google Cloud project.
        topic_id: The ID of the Pub/Sub topic.
        vehicle_data_path: The path to the JSON data file.
    """

  publisher = pubsub_v1.PublisherClient()
  topic_path = publisher.topic_path(project_id, topic_id)

  with open(vehicle_data_path, "r") as f:
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
  publish_messages(project_id, topic_id, vehicle_data_path)
