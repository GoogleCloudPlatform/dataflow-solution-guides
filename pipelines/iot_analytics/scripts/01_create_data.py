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
Pipeline of the Marketing Intelligence Dataflow Solution guide.
"""

import random
import datetime
import pandas as pd
import os

# Get Env variables
current_directory = os.getcwd()
VEHICLE_DATA_PATH = os.environ.get('VEHICLE_DATA_PATH')
MAINTENANCE_DATA_PATH = os.environ.get('MAINTENANCE_DATA_PATH')


# Function to generate random vehicle data
def generate_vehicle_data(vehicle_id):
  return {
      "vehicle_id": vehicle_id,
      "timestamp": datetime.datetime.now().isoformat(timespec="seconds") + "Z",
      "temperature": random.randint(65, 85),
      "rpm": random.randint(1500, 3500),
      "vibration": round(random.uniform(0.1, 0.5), 2),
      "fuel_level": random.randint(50, 90),
      "mileage": random.randint(40000, 60000)
  }


# Function to generate random maintenance data
def generate_maintenance_data(vehicle_id):
  return {
      "vehicle_id":
          vehicle_id,
      "last_service_date": (datetime.datetime.now() - datetime.timedelta(
          days=random.randint(30, 365))).strftime("%Y-%m-%d"),
      "maintenance_type":
          random.choice([
              "oil_change", "tire_rotation", "brake_check", "filter_replacement"
          ]),
      "make":
          "Ford",
      "model":
          "F-150"
  }


# Generate 10 unique vehicle IDs
vehicle_ids = [str(i) for i in range(1000, 1010)]

# Create vehicle data and maintenance data lists
vehicle_data = [generate_vehicle_data(vehicle_id) for vehicle_id in vehicle_ids]
maintenance_data = [
    generate_maintenance_data(vehicle_id) for vehicle_id in vehicle_ids
]

# Convert lists to Pandas DataFrames
df_vehicle_data = pd.DataFrame(vehicle_data)
df_maintenance_data = pd.DataFrame(maintenance_data)

df_vehicle_data.to_json(VEHICLE_DATA_PATH, orient='records', lines=True)
df_maintenance_data.to_json(MAINTENANCE_DATA_PATH, orient='records', lines=True)
