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
# pylint: disable=invalid-name
"""Generates synthetic customer journey dataset and trains a purchase propensity model.

Exports the trained model artifact to marketing_model.pkl for use in the
Dataflow Beam RunInference pipeline.
"""

import os
import pickle
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, roc_auc_score
from sklearn.model_selection import train_test_split

# Feature columns used for training and inference
FEATURE_NAMES = [
    "session_duration_sec",
    "cart_value",
    "total_lifetime_spend",
    "total_orders",
    "days_since_last_order",
    "category_match",
    "loyalty_tier_score",
]


def generate_synthetic_data(num_samples: int = 10000,
                            random_seed: int = 42) -> pd.DataFrame:
  """Generates a realistic synthetic e-commerce interaction dataset."""
  np.random.seed(random_seed)

  session_duration_sec = np.random.exponential(scale=180, size=num_samples) + 15
  session_duration_sec = np.clip(session_duration_sec, 10, 1800)

  cart_value = np.random.gamma(shape=2.5, scale=25.0, size=num_samples) + 5.0
  cart_value = np.clip(cart_value, 5.0, 600.0)

  total_orders = np.random.poisson(lam=4.0, size=num_samples)
  total_lifetime_spend = total_orders * np.random.uniform(
      20.0, 150.0, size=num_samples)

  days_since_last_order = np.where(
      total_orders == 0,
      999,
      np.random.geometric(p=0.03, size=num_samples),
  )
  days_since_last_order = np.clip(days_since_last_order, 1, 999)

  category_match = np.random.choice([0, 1], p=[0.45, 0.55], size=num_samples)
  loyalty_tier_score = np.random.choice([1, 2, 3, 4],
                                        p=[0.50, 0.30, 0.15, 0.05],
                                        size=num_samples)

  # Latent purchase probability using sigmoid logit
  logit = (-3.0 + 0.003 * session_duration_sec + 0.004 * cart_value +
           0.0008 * total_lifetime_spend + 0.08 * total_orders -
           0.005 * np.minimum(days_since_last_order, 90) +
           0.9 * category_match + 0.35 * loyalty_tier_score +
           np.random.normal(0, 0.4, size=num_samples))

  probability = 1.0 / (1.0 + np.exp(-logit))
  purchased = (probability >= 0.5).astype(int)

  data = pd.DataFrame({
      "session_duration_sec": np.round(session_duration_sec, 1),
      "cart_value": np.round(cart_value, 2),
      "total_lifetime_spend": np.round(total_lifetime_spend, 2),
      "total_orders": total_orders,
      "days_since_last_order": days_since_last_order,
      "category_match": category_match,
      "loyalty_tier_score": loyalty_tier_score,
      "purchased": purchased,
  })
  return data


def train_and_export_model(
    output_model_path: str = "marketing_model.pkl",
    output_data_path: str = "training_data.csv",
) -> RandomForestClassifier:
  """Trains a RandomForestClassifier and exports model artifact."""
  print("Generating synthetic dataset...")
  df = generate_synthetic_data(num_samples=10000, random_seed=42)

  x = df[FEATURE_NAMES]
  y = df["purchased"]

  x_train, x_test, y_train, y_test = train_test_split(
      x, y, test_size=0.2, random_state=42, stratify=y)

  print(f"Training dataset size: {len(x_train)}, Test size: {len(x_test)}")
  print(f"Positive class ratio: {y_train.mean():.2%}")

  model = RandomForestClassifier(
      n_estimators=100,
      max_depth=6,
      min_samples_leaf=5,
      random_state=42,
      n_jobs=-1,
  )
  model.fit(x_train, y_train)

  y_pred = model.predict(x_test)
  y_prob = model.predict_proba(x_test)[:, 1]

  auc = roc_auc_score(y_test, y_prob)
  print("\n--- Model Evaluation ---")
  print(f"ROC-AUC Score: {auc:.4f}")
  print("\nClassification Report:")
  print(classification_report(y_test, y_pred))

  # Save training data
  df.to_csv(output_data_path, index=False)
  print(f"Saved synthetic training dataset to: {output_data_path}")

  # Save model artifact
  with open(output_model_path, "wb") as f:
    pickle.dump(model, f)
  print(f"Saved trained model artifact to: {output_model_path}")

  # Mirror model artifact to pipeline root if run from scripts/
  script_dir = os.path.dirname(os.path.abspath(__file__))
  pipeline_root_model = os.path.join(script_dir, "..", "marketing_model.pkl")
  if os.path.abspath(output_model_path) != os.path.abspath(pipeline_root_model):
    with open(pipeline_root_model, "wb") as f:
      pickle.dump(model, f)
    print(f"Mirrored model artifact to: {pipeline_root_model}")

  return model


if __name__ == "__main__":
  train_and_export_model()
