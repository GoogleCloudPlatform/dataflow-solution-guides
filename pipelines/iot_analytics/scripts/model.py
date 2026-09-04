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
Trains and exports Scikit-Learn predictive maintenance model for IoT Analytics.
"""

import argparse
from datetime import datetime, timedelta, timezone
import os
import pickle
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split


def create_sample_data(num_samples: int) -> pd.DataFrame:
  """Generates synthetic vehicle telemetry and maintenance status records."""
  np.random.seed(42)
  now = datetime.now(timezone.utc)
  data = {
      "vehicle_id": [],
      "max_temperature": [],
      "max_vibration": [],
      "last_service_date": [],
      "days_since_last_service": [],
      "needs_maintenance": [],
  }

  for i in range(num_samples):
    vehicle_id = str(1000 + i)
    max_temperature = np.random.randint(50, 100)
    max_vibration = round(float(np.random.uniform(0.1, 0.8)), 3)
    days_ago = np.random.randint(10, 365)
    last_service_date = now - timedelta(days=days_ago)
    last_service_date_str = last_service_date.strftime("%Y-%m-%d")

    needs_maintenance = int((max_temperature > 75) or (max_vibration > 0.50) or
                            (days_ago > 180))

    data["vehicle_id"].append(vehicle_id)
    data["max_temperature"].append(max_temperature)
    data["max_vibration"].append(max_vibration)
    data["last_service_date"].append(last_service_date_str)
    data["days_since_last_service"].append(float(days_ago))
    data["needs_maintenance"].append(needs_maintenance)

  return pd.DataFrame(data)


def train_and_export_model(output_path: str = "maintenance_model.pkl"):
  """Trains the Scikit-Learn classification model and exports the serialized artifact."""
  df = create_sample_data(500)
  print(f"Generated {len(df)} training samples:")
  print(df.head(n=5).to_markdown())

  x = df[["max_temperature", "max_vibration",
          "days_since_last_service"]].to_numpy(dtype=np.float32)
  y = df["needs_maintenance"].to_numpy(dtype=int)

  x_train, x_test, y_train, y_test = train_test_split(
      x, y, test_size=0.2, random_state=42)

  model = LogisticRegression(random_state=42)
  model.fit(x_train, y_train)

  accuracy = model.score(x_test, y_test)
  print(f"Model test accuracy: {accuracy:.4f}")

  # Ensure destination directory exists
  os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
  with open(output_path, "wb") as f:
    pickle.dump(model, f)
  print(f"Successfully serialized model artifact to: {output_path}")

  # Also write to pipeline root directory if running from scripts/
  script_dir = os.path.dirname(os.path.abspath(__file__))
  pipeline_dir = os.path.dirname(script_dir)
  root_model_path = os.path.join(pipeline_dir, "maintenance_model.pkl")
  if os.path.abspath(output_path) != os.path.abspath(root_model_path):
    with open(root_model_path, "wb") as f:
      pickle.dump(model, f)
    print(f"Also copied model artifact to pipeline root: {root_model_path}")


if __name__ == "__main__":
  parser = argparse.ArgumentParser(
      description="Train IoT Predictive Maintenance Model")
  parser.add_argument(
      "--output_path",
      default="maintenance_model.pkl",
      help="Target file path for the serialized model artifact",
  )
  args = parser.parse_args()
  train_and_export_model(args.output_path)
