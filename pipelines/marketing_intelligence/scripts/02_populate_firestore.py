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
"""Populates mock customer profiles into Cloud Firestore in batch.

Seeds the `customer_profiles` collection with historical customer data
used for real-time feature enrichment in the Dataflow pipeline.
"""

import argparse
from datetime import datetime, timezone
import os
import random
from typing import List, Dict, Any

from google.cloud import firestore

CATEGORIES = ["electronics", "clothing", "home", "beauty", "sports"]
LOYALTY_TIERS = ["Bronze", "Silver", "Gold", "Platinum"]
TIER_WEIGHTS = [0.50, 0.30, 0.15, 0.05]


def generate_mock_profiles(count: int = 1000,
                           start_id: int = 1001) -> List[Dict[str, Any]]:
  """Generates a list of synthetic customer profile dictionaries."""
  profiles = []
  random.seed(42)

  now_iso = datetime.now(timezone.utc).isoformat()

  for i in range(count):
    user_id = f"user_{start_id + i}"
    tier = random.choices(LOYALTY_TIERS, weights=TIER_WEIGHTS, k=1)[0]

    if tier == "Platinum":
      orders = random.randint(30, 80)
      spend = round(random.uniform(2000.0, 7500.0), 2)
      days_since = random.randint(1, 20)
    elif tier == "Gold":
      orders = random.randint(12, 35)
      spend = round(random.uniform(600.0, 2500.0), 2)
      days_since = random.randint(2, 45)
    elif tier == "Silver":
      orders = random.randint(4, 15)
      spend = round(random.uniform(150.0, 750.0), 2)
      days_since = random.randint(5, 90)
    else:  # Bronze
      orders = random.randint(1, 5)
      spend = round(random.uniform(20.0, 200.0), 2)
      days_since = random.randint(10, 180)

    category = random.choice(CATEGORIES)

    profile = {
        "user_id": user_id,
        "loyalty_tier": tier,
        "total_lifetime_spend": spend,
        "total_orders": orders,
        "days_since_last_order": days_since,
        "preferred_category": category,
        "email": f"{user_id}@example.com",
        "updated_at": now_iso,
    }
    profiles.append(profile)

  return profiles


def seed_firestore(project: str,
                   collection_name: str = "customer_profiles",
                   count: int = 1000) -> None:
  """Batch inserts mock customer profiles into Cloud Firestore."""
  print(f"Connecting to Firestore in project '{project}'...")
  db = firestore.Client(project=project)
  profiles = generate_mock_profiles(count=count)

  print(
      f"Seeding {len(profiles)} customer profiles into '{collection_name}' collection..."
  )
  batch = db.batch()
  batch_size = 0
  total_written = 0

  for profile in profiles:
    doc_ref = db.collection(collection_name).document(profile["user_id"])
    batch.set(doc_ref, profile)
    batch_size += 1

    # Firestore supports up to 500 writes per batch
    if batch_size == 450:
      batch.commit()
      total_written += batch_size
      print(f"Committed {total_written}/{len(profiles)} documents...")
      batch = db.batch()
      batch_size = 0

  if batch_size > 0:
    batch.commit()
    total_written += batch_size
    print(
        f"Committed final batch. Total: {total_written}/{len(profiles)} documents."
    )

  print(
      f"Successfully populated collection '{collection_name}' in project '{project}'."
  )


def main():
  parser = argparse.ArgumentParser(
      description="Seed mock customer profiles in Firestore.")
  parser.add_argument(
      "--project",
      type=str,
      default=os.environ.get("PROJECT"),
      help="GCP Project ID. Defaults to $PROJECT environment variable.",
  )
  parser.add_argument(
      "--collection",
      type=str,
      default=os.environ.get("FIRESTORE_COLLECTION", "customer_profiles"),
      help="Firestore collection name (default: customer_profiles).",
  )
  parser.add_argument(
      "--count",
      type=int,
      default=1000,
      help="Number of customer profiles to seed (default: 1000).",
  )
  args = parser.parse_args()

  if not args.project:
    parser.error("--project is required (or set PROJECT environment variable).")

  seed_firestore(
      project=args.project,
      collection_name=args.collection,
      count=args.count,
  )


if __name__ == "__main__":
  main()
