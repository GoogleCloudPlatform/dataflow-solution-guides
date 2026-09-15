"""FK key pools — parent synthetic keys for child-table generation.

Parent-first multi-table (ADR 0021, Option 1): a child table's FK columns
sample from the DISTINCT key values its parent has already LANDED in the
synthetic dataset — driver-side eager read, delivered to workers through
``GenerationContext.fk_key_pools`` exactly like reference rows. This is
the path for a parent that landed in an EARLIER job; a parent generated
in the SAME job delivers its keys as an in-DAG side input instead
(ADR 0030, `pipeline._edge_key_pools`).

The contract's ``fk.ref`` names the SOURCE-world ``dataset.table``; the
landed synthetic parent lives in the landing dataset under the same table
name, so the read targets ``{landing_dataset}.{ref table name}``.

Keys stay JOINT (ADR 0031): one query per edge, one tuple per parent key.
The per-column view below is metadata only — a composite edge drawn
column-by-column lands combinations the parent never held (measured:
81.8% orphans, 2026-08-23).
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

from typing import TYPE_CHECKING

from sdfb_core.observability import log_milestone

if TYPE_CHECKING:  # pragma: no cover - typing only
  from sdfb_core.contracts.relationships import FkEdge

_DEFAULT_LIMIT = 100_000


def parent_landing_fqn(ref: str, landing_dataset: str) -> str:
  """``ds.parent`` + ``project.synthetic_data`` → ``project.synthetic_data.parent``."""
  return f"{landing_dataset}.{ref.rsplit('.', 1)[-1]}"


def load_fk_key_pools(
    fks: tuple[FkEdge, ...],
    landing_dataset: str,
    client=None,
    limit: int = _DEFAULT_LIMIT,
) -> list[dict]:
  """``[{"cols": [child cols], "keys": [(v, …), …]}, …]`` per edge.

    ``client`` is a BigQuery client (injected for tests). NULL-bearing
    parent keys are excluded in SQL: SQL equality never matches NULL, so
    such a key is not referenceable.
    """
  if not fks:
    return []
  if client is None:  # pragma: no cover - GCP-only path
    from google.cloud import bigquery

    client = bigquery.Client()

  payloads: list[dict] = []
  for fk in fks:
    if not fk.enforced:  # documented-only edge (ADR 0032): no pool
      continue
    parent = parent_landing_fqn(fk.ref, landing_dataset)
    cols_sql = ", ".join(f"`{c}`" for c in fk.ref_cols)
    sql = (
        # Identifiers come from a validated contract, not user input.
        f"SELECT DISTINCT {cols_sql} FROM `{parent}` "
        f"WHERE {' AND '.join(f'`{c}` IS NOT NULL' for c in fk.ref_cols)} "
        f"LIMIT {int(limit)}")
    rows = list(client.query(sql).result())
    keys = [tuple(row[c] for c in fk.ref_cols) for row in rows]
    payloads.append({"cols": list(fk.cols), "keys": keys})
    log_milestone(
        "fk_pool_loaded",
        parent=parent,
        child_cols=",".join(fk.cols),
        values=len(keys),
        capped=len(keys) >= int(limit),
    )
  return payloads


def per_column_view(payloads: list[dict]) -> dict[str, tuple]:
  """Per-column projection of joint keys — plan/preflight metadata only."""
  out: dict[str, tuple] = {}
  for payload in payloads:
    keys = payload.get("keys") or ()
    for i, col in enumerate(payload.get("cols") or ()):
      out[col] = tuple(dict.fromkeys(k[i] for k in keys))
  return out


__all__ = ["load_fk_key_pools", "parent_landing_fqn", "per_column_view"]
