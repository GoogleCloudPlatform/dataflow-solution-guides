import random
import datetime
import pandas as pd

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
        "vehicle_id": vehicle_id,
        "last_service_date": (datetime.datetime.now() - datetime.timedelta(days=random.randint(30, 365))).strftime("%Y-%m-%d"),
        "maintenance_type": random.choice(["oil_change", "tire_rotation", "brake_check", "filter_replacement"]),
        "make": "Ford",
        "model": "F-150"
    }

# Generate 10 unique vehicle IDs
vehicle_ids = [str(i) for i in range(1000, 1010)]

# Create vehicle data and maintenance data lists
vehicle_data = [generate_vehicle_data(vehicle_id) for vehicle_id in vehicle_ids]
maintenance_data = [generate_maintenance_data(vehicle_id) for vehicle_id in vehicle_ids]

# Convert lists to Pandas DataFrames
df_vehicle_data = pd.DataFrame(vehicle_data)
df_maintenance_data = pd.DataFrame(maintenance_data)

print(df_vehicle_data.head().to_markdown(index=False, numalign="left", stralign="left"))
df_vehicle_json = df_vehicle_data.to_json(orient='records')
print(df_vehicle_json)
print(df_maintenance_data.head().to_markdown(index=False, numalign="left", stralign="left"))
df_maintenance_json = df_maintenance_data.to_json(orient='records')
print(df_maintenance_json)