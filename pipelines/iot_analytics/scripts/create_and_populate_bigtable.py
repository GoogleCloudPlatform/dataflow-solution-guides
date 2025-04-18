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

# Create a bigtable and populate the weather data table
from google.cloud.bigtable import column_family
from google.cloud.bigtable import row
from google.cloud.bigtable import Client
from datetime import datetime
import os
import json

# Create Bigtable Data (Weather data) and Load Records
current_directory = os.getcwd()
PROJECT_ID = os.environ.get("PROJECT_ID")
INSTANCE_ID = os.environ.get("BIGTABLE_INSTANCE_ID")
TABLE_ID = os.environ.get("BIGTABLE_TABLE_ID")
MAINTENANCE_DATA_PATH = os.environ.get("MAINTENANCE_DATA_PATH")

# Create a Bigtable client
client = Client(project=PROJECT_ID, admin=True)
instance = client.instance(INSTANCE_ID)

# Create a column family.
column_family_id = "maintenance"
max_versions_rule = column_family.MaxVersionsGCRule(2)
column_families = {column_family_id: max_versions_rule}

# Create a table.
table = instance.table(TABLE_ID)

# You need admin access to use `.exists()`. If you don't have the admin access, then
# comment out the if-else block.
if not table.exists():
  table.create(column_families=column_families)
else:
  print(f"Table {TABLE_ID} already exists in {PROJECT_ID}:{INSTANCE_ID}")

# Define column names for the table.
vehicle_id = "vehicle_id"
last_service_date = "last_service_date"
maintenance_type = "maintenance_type"
make = "make"
model = "model"

# Sample weather data
maintenance_data = []
try:
  with open(MAINTENANCE_DATA_PATH, "r", encoding="utf-8") as f:
    for line in f:
      try:
        data = json.loads(line)
        maintenance_data.append(data)
      except json.JSONDecodeError as e:
        print(f"Error decoding JSON from line: {line.strip()}")
        print(f"Error message: {e}")
        # Handle the error (e.g., log it, skip the line, or raise an exception)

except FileNotFoundError:
  print(f"File not found: {MAINTENANCE_DATA_PATH}")

# Populate Bigtable
for record in maintenance_data:
  row_key = str(record[vehicle_id]).encode()
  row = table.direct_row(row_key)
  row.set_cell(
      column_family_id,
      vehicle_id.encode(),
      str(record[vehicle_id]),
      timestamp=datetime.utcnow())
  row.set_cell(
      column_family_id,
      last_service_date.encode(),
      str(record[last_service_date]),
      timestamp=datetime.utcnow())
  row.set_cell(
      column_family_id,
      maintenance_type.encode(),
      str(record[maintenance_type]),
      timestamp=datetime.utcnow())
  row.set_cell(
      column_family_id,
      make.encode(),
      str(record[make]),
      timestamp=datetime.utcnow())
  row.set_cell(
      column_family_id,
      model.encode(),
      str(record[model]),
      timestamp=datetime.utcnow())
  row.commit()
  print(f"Inserted row for key: {record[vehicle_id]}")

print("Bigtable populated with sample weather information.")
