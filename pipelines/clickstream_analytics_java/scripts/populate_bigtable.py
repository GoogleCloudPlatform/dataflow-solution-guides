"""Populates Cloud Bigtable with sample Wikipedia article metadata for clickstream enrichment."""

import argparse
import json
import os
import sys
from typing import Dict, List, Tuple
from google.cloud import bigtable


SAMPLE_ARTICLES: List[Tuple[str, str, Dict[str, object]]] = [
    (
        "Apache_Beam",
        "Technology / Distributed Systems",
        {
            "views_24h": 28400,
            "importance": "high",
            "topics": ["streaming", "batch", "data-engineering"],
        },
    ),
    (
        "Google_Cloud_Platform",
        "Technology / Cloud Computing",
        {
            "views_24h": 94100,
            "importance": "critical",
            "topics": ["cloud", "infrastructure", "ai"],
        },
    ),
    (
        "Artificial_intelligence",
        "Computer Science / AI",
        {
            "views_24h": 352000,
            "importance": "critical",
            "topics": ["deep-learning", "machine-learning", "robotics"],
        },
    ),
    (
        "Machine_learning",
        "Computer Science / AI",
        {
            "views_24h": 182300,
            "importance": "high",
            "topics": ["algorithms", "statistics", "neural-networks"],
        },
    ),
    (
        "Cloud_Dataflow",
        "Technology / Distributed Systems",
        {
            "views_24h": 19450,
            "importance": "high",
            "topics": ["managed-service", "beam", "streaming"],
        },
    ),
    (
        "BigQuery",
        "Technology / Databases",
        {
            "views_24h": 87600,
            "importance": "critical",
            "topics": ["data-warehouse", "analytics", "sql"],
        },
    ),
    (
        "Cloud_Bigtable",
        "Technology / Databases",
        {
            "views_24h": 34100,
            "importance": "high",
            "topics": ["nosql", "low-latency", "key-value"],
        },
    ),
    (
        "Earth",
        "Science / Planetary Science",
        {
            "views_24h": 125000,
            "importance": "high",
            "topics": ["astronomy", "geology", "solar-system"],
        },
    ),
    (
        "Physics",
        "Science / Natural Sciences",
        {
            "views_24h": 110400,
            "importance": "high",
            "topics": ["quantum", "relativity", "mechanics"],
        },
    ),
    (
        "Main_Page",
        "Navigation / Portal",
        {
            "views_24h": 4500000,
            "importance": "critical",
            "topics": ["portal", "home", "search"],
        },
    ),
]


def populate_bigtable(
    project_id: str,
    instance_id: str,
    table_id: str,
    column_family_id: str,
) -> None:
  """Populates the specified Bigtable table with sample article metadata.

  Args:
      project_id: GCP project ID.
      instance_id: Bigtable instance ID.
      table_id: Bigtable table name.
      column_family_id: Column family name to store metadata.
  """
  client = bigtable.Client(project=project_id, admin=True)
  instance = client.instance(instance_id)
  table = instance.table(table_id)

  if not table.exists():
    print(f"Table '{table_id}' does not exist on instance '{instance_id}'.")
    print("Creating table and column family...")
    table.create()
    cf = table.column_family(column_family_id)
    cf.create()
    print(f"Created table '{table_id}' with column family '{column_family_id}'.")

  print(
      f"Populating table '{table_id}' on instance '{instance_id}' with {len(SAMPLE_ARTICLES)} sample rows..."
  )

  rows = []
  for article_name, category, metadata in SAMPLE_ARTICLES:
    row = table.direct_row(article_name)
    row.set_cell(
        column_family_id,
        "category".encode("utf-8"),
        category.encode("utf-8"),
    )
    row.set_cell(
        column_family_id,
        "enriched_data".encode("utf-8"),
        json.dumps(metadata).encode("utf-8"),
    )
    rows.append(row)

  status = table.mutate_rows(rows)
  errors = [s for s in status if s.code != 0]
  if errors:
    print(f"Encountered {len(errors)} errors while mutating rows.")
    for err in errors:
      print(f"  Error: {err.message} (code {err.code})")
    sys.exit(1)

  print(
      f"Successfully populated {len(SAMPLE_ARTICLES)} rows into Bigtable table '{table_id}'."
  )


def parse_args() -> argparse.Namespace:
  """Parses command line arguments."""
  parser = argparse.ArgumentParser(
      description="Populate Cloud Bigtable with Wikipedia article metadata."
  )
  parser.add_argument(
      "--project_id",
      default=os.environ.get("PROJECT_ID", ""),
      help="GCP Project ID (defaults to $PROJECT_ID).",
  )
  parser.add_argument(
      "--instance_id",
      default=os.environ.get("BIGTABLE_INSTANCE", ""),
      help="Bigtable Instance ID (defaults to $BIGTABLE_INSTANCE).",
  )
  parser.add_argument(
      "--table_id",
      default=os.environ.get("BIGTABLE_TABLE", "wikipedia"),
      help="Bigtable Table ID (defaults to 'wikipedia' or $BIGTABLE_TABLE).",
  )
  parser.add_argument(
      "--column_family",
      default="cf",
      help="Column family ID (default: cf).",
  )
  return parser.parse_args()


def main() -> None:
  """CLI entry point."""
  args = parse_args()
  if not args.project_id:
    print("Error: --project_id or PROJECT_ID env variable is required.")
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
