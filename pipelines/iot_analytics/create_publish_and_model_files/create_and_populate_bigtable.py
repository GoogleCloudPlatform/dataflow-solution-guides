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

# Create Bigtable Data (Weather data) and Load Records
PROJECT_ID = 'learnings-421714'
INSTANCE_ID = 'iot-analytics'
TABLE_ID = 'maintenance_data'

# Create a bigtable and populate the weather data table
from google.cloud import bigtable
from google.cloud.bigtable import column_family
from google.cloud.bigtable import row
from google.cloud.bigtable import Client
from datetime import datetime

# Create a Bigtable client
client = Client(project=PROJECT_ID, admin=True)
instance = client.instance(INSTANCE_ID)

# Create a column family.
column_family_id = 'maintenance'
max_versions_rule = column_family.MaxVersionsGCRule(2)
column_families = {column_family_id: max_versions_rule}

# Create a table.
table = instance.table(TABLE_ID)

# You need admin access to use `.exists()`. If you don't have the admin access, then
# comment out the if-else block.
if not table.exists():
  table.create(column_families=column_families)
else:
  print("Table %s already exists in %s:%s" %
        (TABLE_ID, PROJECT_ID, INSTANCE_ID))

# Define column names for the table.
vehicle_id = 'vehicle_id'
last_service_date = 'last_service_date'
maintenance_type = 'maintenance_type'
make = 'make'
model = 'model'

# Sample weather data
maintenance_data = [{
    "vehicle_id": "1000",
    "last_service_date": "2023-12-22",
    "maintenance_type": "filter_replacement",
    "make": "Ford",
    "model": "F-150"
}, {
    "vehicle_id": "1001",
    "last_service_date": "2024-05-31",
    "maintenance_type": "tire_rotation",
    "make": "Ford",
    "model": "F-150"
}, {
    "vehicle_id": "1002",
    "last_service_date": "2024-04-06",
    "maintenance_type": "tire_rotation",
    "make": "Ford",
    "model": "F-150"
}, {
    "vehicle_id": "1003",
    "last_service_date": "2023-11-26",
    "maintenance_type": "filter_replacement",
    "make": "Ford",
    "model": "F-150"
}, {
    "vehicle_id": "1004",
    "last_service_date": "2023-10-18",
    "maintenance_type": "filter_replacement",
    "make": "Ford",
    "model": "F-150"
}, {
    "vehicle_id": "1005",
    "last_service_date": "2024-07-14",
    "maintenance_type": "tire_rotation",
    "make": "Ford",
    "model": "F-150"
}, {
    "vehicle_id": "1006",
    "last_service_date": "2024-01-25",
    "maintenance_type": "filter_replacement",
    "make": "Ford",
    "model": "F-150"
}, {
    "vehicle_id": "1007",
    "last_service_date": "2024-04-07",
    "maintenance_type": "tire_rotation",
    "make": "Ford",
    "model": "F-150"
}, {
    "vehicle_id": "1008",
    "last_service_date": "2023-10-05",
    "maintenance_type": "brake_check",
    "make": "Ford",
    "model": "F-150"
}, {
    "vehicle_id": "1009",
    "last_service_date": "2023-11-30",
    "maintenance_type": "oil_change",
    "make": "Ford",
    "model": "F-150"
}]

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
  print('Inserted row for key: %s' % record[vehicle_id])

print("Bigtable populated with sample weather information.")
