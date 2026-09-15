#!/usr/bin/env python
"""Derive a `landing_ddl.json` BigQuery schema from a target table's `_ddl.json`.

The landing table has no committed schema — it mirrors the table you clone, so
its schema is derived from that table's `_ddl.json` (see
`docs/DEPLOYMENT_PREREQUISITES.md` → "Provisioning the landing table").

`derive_bq_schema()` returns the Beam/REST shape ``{"fields": [...]}``; `bq mk`
and Terraform both want the **bare array**, so this writes ``[...]``.
Partitioning / clustering are NOT part of the schema JSON — they are printed
(and, with ``--print-bq``, folded into a ready-to-run ``bq mk`` command).

Reads a LOCAL `_ddl.json` (the `scripts/extract_ddl.py` output). Pure
sdfb-core — no Beam, no GCP.

Usage:
    python scripts/derive_landing_schema.py DDL_JSON [-o landing_ddl.json]
    python scripts/derive_landing_schema.py DDL_JSON --print-bq project:synthetic_data.landing
"""

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

import argparse
import json
import sys

from sdfb_core.codegen.derive_bq_ddl import derive_bq_schema
from sdfb_core.contracts.schema import TableSchema


def main(argv: list[str] | None = None) -> int:
  p = argparse.ArgumentParser(
      description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  p.add_argument(
      "ddl_json",
      help="Path to the source table's _ddl.json (extract_ddl.py output).")
  p.add_argument(
      "-o",
      "--out",
      default="landing_ddl.json",
      help="Output path for the BQ JSON schema array (default: landing_ddl.json)."
  )
  p.add_argument(
      "--print-bq",
      metavar="TABLE_FQN",
      default=None,
      help="Also print a ready-to-run `bq mk` command for TABLE_FQN "
      "(project:dataset.table), with partitioning/clustering folded in.")
  args = p.parse_args(argv)

  with open(args.ddl_json, encoding="utf-8") as f:
    ts = TableSchema.model_validate(json.load(f))

  # Unwrap {"fields": [...]} → bare array for bq / Terraform.
  fields = derive_bq_schema(ts)["fields"]
  with open(args.out, "w", encoding="utf-8") as f:
    json.dump(fields, f, indent=2)
    f.write("\n")

  print(f"wrote {args.out} ({len(fields)} columns)")
  if ts.partitioning:
    print(f"partitioning: {ts.partitioning.type} {ts.partitioning.field or ''}"
          .rstrip())
  if ts.clustering:
    print(f"clustering: {','.join(ts.clustering.fields)}")

  if args.print_bq:
    cmd = ["bq mk --table", f"    --schema {args.out}"]
    if ts.partitioning and ts.partitioning.field:
      cmd.append(f"    --time_partitioning_type {ts.partitioning.type} "
                 f"--time_partitioning_field {ts.partitioning.field}")
    if ts.clustering:
      cmd.append(f"    --clustering_fields {','.join(ts.clustering.fields)}")
    cmd.append(f"    {args.print_bq}")
    print("\n# Ready-to-run:")
    print(" \\\n".join(cmd))

  return 0


if __name__ == "__main__":
  sys.exit(main())
