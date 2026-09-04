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
Creates and populates the Cloud Bigtable vehicle maintenance metadata table.
"""

from datetime import datetime, timezone
import json
import os
from google.cloud.bigtable import Client, column_family

# Configuration and environment variables
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MAINTENANCE_PATH = os.path.join(SCRIPT_DIR, "maintenance_data.jsonl")

PROJECT_ID = os.environ.get("PROJECT_ID")
INSTANCE_ID = os.environ.get("BIGTABLE_INSTANCE_ID", "iot-analytics")
TABLE_ID = os.environ.get("BIGTABLE_TABLE_ID", "maintenance_data")
MAINTENANCE_DATA_PATH = os.environ.get(
    "MAINTENANCE_DATA_PATH") or DEFAULT_MAINTENANCE_PATH

if not PROJECT_ID:
  raise ValueError("PROJECT_ID environment variable is required.")

# Create Bigtable client
client = Client(project=PROJECT_ID, admin=True)
instance = client.instance(INSTANCE_ID)

# Column family configuration
column_family_id = "maintenance"
max_versions_rule = column_family.MaxVersionsGCRule(2)
column_families = {column_family_id: max_versions_rule}

table = instance.table(TABLE_ID)
if not table.exists():
  table.create(column_families=column_families)
  print(f"Created Bigtable table '{TABLE_ID}' in instance '{INSTANCE_ID}'.")
else:
  print(f"Table '{TABLE_ID}' already exists in {PROJECT_ID}:{INSTANCE_ID}.")

# Load maintenance records
maintenance_data = []
try:
  with open(MAINTENANCE_DATA_PATH, "r", encoding="utf-8") as f:
    for line in f:
      line_str = line.strip()
      if not line_str:
        continue
      try:
        maintenance_data.append(json.loads(line_str))
      except json.JSONDecodeError as e:
        print(f"Error decoding JSON: {e}")
except FileNotFoundError:
  print(f"File not found: {MAINTENANCE_DATA_PATH}")

# Populate Bigtable using mutate_rows for high throughput
rows_to_mutate = []
now_utc = datetime.now(timezone.utc)

for record in maintenance_data:
  vehicle_id_val = str(record.get("vehicle_id", ""))
  row_key = vehicle_id_val.encode("utf-8")
  row = table.direct_row(row_key)
  row.set_cell(
      column_family_id, b"vehicle_id", vehicle_id_val, timestamp=now_utc)
  row.set_cell(
      column_family_id,
      b"last_service_date",
      str(record.get("last_service_date", "")),
      timestamp=now_utc)
  row.set_cell(
      column_family_id,
      b"maintenance_type",
      str(record.get("maintenance_type", "")),
      timestamp=now_utc)
  row.set_cell(
      column_family_id, b"make", str(record.get("make", "")), timestamp=now_utc)
  row.set_cell(
      column_family_id,
      b"model",
      str(record.get("model", "")),
      timestamp=now_utc)
  rows_to_mutate.append(row)

if rows_to_mutate:
  statuses = table.mutate_rows(rows_to_mutate)
  for i, status in enumerate(statuses):
    if status.code != 0:
      print(f"Error writing row {i}: {status.message}")
  print(
      f"Successfully populated Bigtable with {len(rows_to_mutate)} vehicle maintenance records."
  )
else:
  print("No maintenance records found to populate.")
