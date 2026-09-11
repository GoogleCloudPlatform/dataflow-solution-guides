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
"""Populates the Cloud Bigtable feature store with sample player features."""

import argparse
import os
import sys
from typing import Dict, List, Tuple
from google.cloud import bigtable

# Player id, then the feature qualifiers stored in the 'features' column family.
# 'churn_risk' and 'level_failures' drive the recommendation rules of the
# pipeline, so the sample rows deliberately cover every branch.
SAMPLE_PLAYERS: List[Tuple[str, Dict[str, str]]] = [
    (
        "player_0001",
        {
            "churn_risk": "0.85",
            "level_failures": "2",
            "skill_rating": "1200",
            "spend_tier": "free",
            "favorite_mode": "campaign",
        },
    ),
    (
        "player_0002",
        {
            "churn_risk": "0.10",
            "level_failures": "5",
            "skill_rating": "2400",
            "spend_tier": "whale",
            "favorite_mode": "ranked",
        },
    ),
    (
        "player_0003",
        {
            "churn_risk": "0.35",
            "level_failures": "0",
            "skill_rating": "800",
            "spend_tier": "paying",
            "favorite_mode": "coop",
        },
    ),
    (
        "player_0004",
        {
            "churn_risk": "0.05",
            "level_failures": "1",
            "skill_rating": "300",
            "spend_tier": "free",
            "favorite_mode": "casual",
        },
    ),
    (
        "player_0005",
        {
            "churn_risk": "0.72",
            "level_failures": "4",
            "skill_rating": "1500",
            "spend_tier": "paying",
            "favorite_mode": "ranked",
        },
    ),
]


def populate_bigtable(
    project_id: str,
    instance_id: str,
    table_id: str,
    column_family_id: str,
) -> None:
  """Writes the sample player features into Bigtable.

  Args:
      project_id: GCP project ID.
      instance_id: Bigtable instance ID.
      table_id: Bigtable table name.
      column_family_id: Column family holding the features.
  """
  client = bigtable.Client(project=project_id, admin=True)
  instance = client.instance(instance_id)
  table = instance.table(table_id)

  if not table.exists():
    print(f"Table '{table_id}' does not exist on instance '{instance_id}'.")
    print("Creating table and column family...")
    table.create()
    table.column_family(column_family_id).create()
    print(f"Created table '{table_id}' with column family '{column_family_id}'.")

  print(
      f"Populating table '{table_id}' on instance '{instance_id}' "
      f"with {len(SAMPLE_PLAYERS)} player rows..."
  )

  rows = []
  for player_id, features in SAMPLE_PLAYERS:
    row = table.direct_row(player_id)
    for qualifier, value in features.items():
      row.set_cell(
          column_family_id,
          qualifier.encode("utf-8"),
          value.encode("utf-8"),
      )
    rows.append(row)

  status = table.mutate_rows(rows)
  errors = [s for s in status if s.code != 0]
  if errors:
    print(f"Encountered {len(errors)} errors while mutating rows.")
    for err in errors:
      print(f"  Error: {err.message} (code {err.code})")
    sys.exit(1)

  print(f"Successfully populated {len(SAMPLE_PLAYERS)} players into '{table_id}'.")


def parse_args() -> argparse.Namespace:
  """Parses command line arguments."""
  parser = argparse.ArgumentParser(
      description="Populate the Cloud Bigtable player feature store."
  )
  parser.add_argument(
      "--project_id",
      default=os.environ.get("PROJECT", ""),
      help="GCP Project ID (defaults to $PROJECT).",
  )
  parser.add_argument(
      "--instance_id",
      default=os.environ.get("BIGTABLE_INSTANCE", "gaming-analytics"),
      help="Bigtable instance ID (defaults to $BIGTABLE_INSTANCE).",
  )
  parser.add_argument(
      "--table_id",
      default=os.environ.get("BIGTABLE_TABLE", "player_features"),
      help="Bigtable table ID (defaults to $BIGTABLE_TABLE).",
  )
  parser.add_argument(
      "--column_family",
      default=os.environ.get("BIGTABLE_COLUMN_FAMILY", "features"),
      help="Column family ID (defaults to $BIGTABLE_COLUMN_FAMILY).",
  )
  return parser.parse_args()


def main() -> None:
  """CLI entry point."""
  args = parse_args()
  if not args.project_id:
    print("Error: --project_id or PROJECT env variable is required.")
    sys.exit(1)
  if not args.instance_id:
    print("Error: --instance_id or BIGTABLE_INSTANCE env variable is required.")
    sys.exit(1)

  populate_bigtable(
      project_id=args.project_id,
      instance_id=args.instance_id,
      table_id=args.table_id,
      column_family_id=args.column_family,
  )


if __name__ == "__main__":
  main()
