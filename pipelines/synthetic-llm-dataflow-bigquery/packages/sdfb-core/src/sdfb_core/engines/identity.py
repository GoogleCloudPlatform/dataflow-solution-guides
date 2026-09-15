"""Identity-column synthesis — never emit reference values for row identifiers.

The 2026-07 E2E report found identity columns copied verbatim from the source
table (copy_ratio = 1.0 — a re-identification leak). Identity columns are
therefore synthesized per row from ``(run_id, batch_id, row_index, column)``,
bypassing reference pools, empirical sampling, and the LLM path entirely.
Deterministic per run_id so reruns reproduce.
"""

from __future__ import annotations

import hashlib
import uuid

# Width of a UUIDv4 string (32 hex digits + 4 hyphens). A `max_length`
# narrower than this can't hold a full UUID, so `synthesize_identity_value`
# falls back to a hex-digest slice instead.
_UUID_STRING_LENGTH = 36


def _digest(run_id: str, batch_id: int, row_index: int, column: str) -> bytes:
  key = f"{run_id}\x1f{batch_id}\x1f{row_index}\x1f{column}".encode()
  return hashlib.blake2b(key, digest_size=16).digest()


def synthesize_identity_value(
    bq_type: str,
    run_id: str,
    batch_id: int,
    row_index: int,
    column: str,
    max_length: int | None = None,
) -> str | int:
  raw = _digest(run_id, batch_id, row_index, column)
  if bq_type in {"INTEGER", "INT64"}:
    return int.from_bytes(raw[:8], "big") >> 1
  if max_length is not None and max_length < _UUID_STRING_LENGTH:
    # The 36-char UUIDv4 string would overflow a narrower STRING column
    # (e.g. VARCHAR(10)-style max_length constraints from the BQ JSON
    # schema). Still deterministic and unique for realistic lengths —
    # `raw` is 16 bytes (32 hex chars) of blake2b digest.
    return raw.hex()[:max_length]
  # UUIDv4-shaped so downstream format checks (regex.format) keep passing.
  return str(uuid.UUID(bytes=raw, version=4))


def apply_identity_columns(
    record: dict,
    *,
    identity_columns: list[str],
    column_types: dict[str, str],
    run_id: str,
    batch_id: int,
    row_index: int,
    column_max_lengths: dict[str, int | None] | None = None,
) -> dict:
  max_lengths = column_max_lengths or {}
  for column in identity_columns:
    if column in record:
      record[column] = synthesize_identity_value(
          column_types.get(column, "STRING"),
          run_id,
          batch_id,
          row_index,
          column,
          max_length=max_lengths.get(column),
      )
  return record
