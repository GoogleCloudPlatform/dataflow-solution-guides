"""DDL metadata extraction for a single BigQuery table.

The pure `extract_ddl_metadata()` function returns a dict whose shape
matches `TableSchema.model_validate()`'s expected input — no further
transformation needed downstream.

Differences from the original `bigquery_ddl_metadata.py`:
  - Emits the canonical BQ JSON schema key `"type"` (not the
    `field_type` attribute name used by the BigQuery client library).
    `TableSchema` accepts both via `AliasChoices`, but canonical output
    matches `bq load --schema_from_json` directly.
  - Recurses into `RECORD` / `STRUCT` fields. The original silently
    dropped nested schemas.
  - The `bigquery.Client` is an optional argument so the function can
    be unit-tested with a mock without standing up live credentials.

REFs:
  - https://docs.cloud.google.com/bigquery/docs/schemas#creating_a_JSON_schema_file
  - https://docs.cloud.google.com/bigquery/docs/primary-foreign-keys
"""

from __future__ import annotations

import logging
import time
from typing import Any

from google.api_core.retry import Retry
from google.cloud import bigquery
from sdfb_core.contracts import TableSchema

from sdfb_beam.ddl.connection import DEFAULT_TIMEOUT

logger = logging.getLogger(__name__)

# FQN format: project.dataset.table
_FQN_PARTS = 3


def extract_ddl_metadata(
    project: str,
    dataset: str,
    table: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    client: bigquery.Client | None = None,
) -> dict[str, Any]:
  """Extract DDL metadata for a single BigQuery table.

    Returns a dict directly consumable by
    `TableSchema.model_validate(result)`.
    """
  full_table_id = f"{project}.{dataset}.{table}"
  logger.info("Extracting DDL for %s (timeout=%ss)", full_table_id, timeout)
  start = time.time()

  if client is None:
    client = bigquery.Client(project=project)

  retry = Retry(deadline=timeout, maximum=3)
  try:
    bq_table = client.get_table(full_table_id, retry=retry, timeout=timeout)
  except Exception:
    logger.exception("Failed to get table %s", full_table_id)
    raise

  logger.info(
      "Got table metadata in %.1fs — %d fields",
      time.time() - start,
      len(bq_table.schema),
  )

  result: dict[str, Any] = {
      "table_info":
          _build_table_info(bq_table, full_table_id),
      "schema": [_field_to_dict(f) for f in bq_table.schema],
      "primary_keys":
          _get_primary_keys(bq_table),
      "partitioning":
          _get_partitioning(bq_table),
      "clustering":
          _get_clustering(bq_table),
      "storage_info":
          _get_storage_info(client, project, dataset, table, bq_table, timeout),
  }
  logger.info("DDL extraction complete in %.1fs", time.time() - start)
  return result


def extract_table_schema(
    table_fqn: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    client: bigquery.Client | None = None,
) -> TableSchema:
  """Live-extract a validated ``TableSchema`` for ``project.dataset.table``
    (WS4 §6b).

    The launch-time twin of the offline extract → stage → ``--ddl_uri``
    flow: same extractor, no GCS artifact. Runs at graph-construction time
    on the driver — the same lifecycle stage as the live reference read.
    """
  parts = table_fqn.split(".")
  if len(parts) != _FQN_PARTS or not all(parts):
    raise ValueError(
        f"table_fqn must be 'project.dataset.table', got {table_fqn!r}")
  project, dataset, table = parts
  metadata = extract_ddl_metadata(
      project, dataset, table, timeout=timeout, client=client)
  return TableSchema.model_validate(metadata)


# ---------------------------------------------------------------------------
# Field-level conversion.
# ---------------------------------------------------------------------------


def _field_to_dict(field: bigquery.SchemaField) -> dict[str, Any]:
  """Convert a BQ SchemaField to the canonical dict.

    Recurses for RECORD / STRUCT fields. Emits `"type"` (canonical),
    not `"field_type"`.
    """
  out: dict[str, Any] = {
      "name": field.name,
      "type": field.field_type,
      "mode": field.mode,
      "description": field.description or "",
  }
  nested = getattr(field, "fields", None)
  if field.field_type in {"RECORD", "STRUCT"} and nested:
    out["fields"] = [_field_to_dict(sub) for sub in nested]

  max_length = getattr(field, "max_length", None)
  if max_length is not None:
    out["max_length"] = int(max_length)

  precision = getattr(field, "precision", None)
  if precision is not None:
    out["precision"] = int(precision)

  scale = getattr(field, "scale", None)
  if scale is not None:
    out["scale"] = int(scale)

  return out


# ---------------------------------------------------------------------------
# Table-level metadata.
# ---------------------------------------------------------------------------


def _build_table_info(bq_table: bigquery.Table,
                      full_table_id: str) -> dict[str, Any]:
  return {
      "table_id": full_table_id,
      "created": bq_table.created.isoformat() if bq_table.created else None,
      "last_modified":
          (bq_table.modified.isoformat() if bq_table.modified else None),
      "table_expiry":
          (bq_table.expires.isoformat() if bq_table.expires else "NEVER"),
      "data_location": bq_table.location,
      "description": bq_table.description or "",
      "labels": dict(bq_table.labels) if bq_table.labels else {},
      "table_type": bq_table.table_type,
      "encryption_configuration":
          (bq_table.encryption_configuration.kms_key_name
           if bq_table.encryption_configuration else None),
      "default_collation": getattr(bq_table, "default_collation_name", None),
  }


def _get_primary_keys(bq_table: bigquery.Table) -> list[str] | None:
  """Extract primary keys declared in BigQuery itself.

    The PK of RECORD (the relational model) lives in
    `config/relationships/` since ADR 0032 and is applied by the
    launcher; what this reads is whatever BigQuery itself knows —
    `table.table_constraints` (set via `ALTER TABLE … ADD PRIMARY KEY`),
    then the legacy `PRIMARY KEY: col1, col2` description line. Useful
    context in `_ddl.json`, never the source of truth.

    REF: https://docs.cloud.google.com/bigquery/docs/primary-foreign-keys
    """
  constraints = getattr(bq_table, "table_constraints", None)
  if constraints is not None:
    pk = getattr(constraints, "primary_key", None)
    if pk is not None:
      cols = getattr(pk, "columns", None)
      if cols:
        return list(cols)

  description = bq_table.description or ""
  if "PRIMARY KEY:" in description:
    return [
        pk.strip()
        for pk in description.split("PRIMARY KEY:")[1].split("\n")[0].split(",")
    ]
  return None


def _get_partitioning(bq_table: bigquery.Table) -> dict[str, Any] | None:
  if bq_table.time_partitioning:
    tp = bq_table.time_partitioning
    return {
        "type": tp.type_ or "DAY",
        "field": tp.field,
        "expiration_days":
            (tp.expiration_ms / 86_400_000 if tp.expiration_ms else None),
        "require_partition_filter": bq_table.require_partition_filter or False,
    }
  if bq_table.range_partitioning:
    rp = bq_table.range_partitioning
    return {
        "type": "RANGE",
        "field": rp.field,
        "range": {
            "start": rp.range_.start,
            "end": rp.range_.end,
            "interval": rp.range_.interval,
        },
    }
  return None


def _get_clustering(bq_table: bigquery.Table) -> dict[str, Any] | None:
  if bq_table.clustering_fields:
    return {"fields": list(bq_table.clustering_fields)}
  return None


# ---------------------------------------------------------------------------
# Storage info — informational, best-effort. Not consumed by TableSchema.
# ---------------------------------------------------------------------------

_BYTES_PER_UNIT = 1024.0


def _human_bytes(num_bytes: int | None) -> str | None:
  if num_bytes is None:
    return None
  val: float = float(num_bytes)
  for unit in ("B", "KB", "MB", "GB", "TB"):
    if abs(val) < _BYTES_PER_UNIT:
      return f"{val:.2f} {unit}"
    val /= _BYTES_PER_UNIT
  return f"{val:.2f} PB"


def _get_storage_info(
    client: bigquery.Client,
    project: str,
    dataset: str,
    table: str,
    bq_table: bigquery.Table,
    timeout: float,
) -> dict[str, Any]:
  """Fetch detailed storage info from table properties + INFORMATION_SCHEMA.

    Best-effort: missing fields surface as None, partial failures don't
    abort the extraction.
    """
  props = getattr(bq_table, "_properties", {}) or {}

  def _int_prop(key: str) -> int | None:
    v = props.get(key)
    return int(v) if v is not None else None

  storage_info: dict[str, Any] = {
      "num_rows":
          bq_table.num_rows,
      "num_partitions":
          None,
      "total_logical_bytes":
          _int_prop("numBytes"),
      "total_logical_bytes_human":
          _human_bytes(_int_prop("numBytes")),
      "active_logical_bytes":
          _int_prop("numActiveLogicalBytes"),
      "active_logical_bytes_human":
          _human_bytes(_int_prop("numActiveLogicalBytes")),
      "long_term_logical_bytes":
          _int_prop("numLongTermLogicalBytes") or _int_prop("numLongTermBytes"),
      "total_physical_bytes":
          _int_prop("numTotalPhysicalBytes") or _int_prop("numPhysicalBytes"),
      "time_travel_physical_bytes":
          _int_prop("numTimeTravelPhysicalBytes"),
  }

  # Partition count from INFORMATION_SCHEMA.PARTITIONS — optional.
  try:
    partition_query = f"""
            SELECT COUNT(*) AS num_partitions
            FROM `{project}.{dataset}.INFORMATION_SCHEMA.PARTITIONS`
            WHERE table_name = '{table}'
        """
    rows = list(
        client.query(partition_query, timeout=timeout).result(timeout=timeout))
    if rows:
      storage_info["num_partitions"] = rows[0].num_partitions
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.warning("Could not get partition count: %s: %s", type(e).__name__, e)

  return storage_info
