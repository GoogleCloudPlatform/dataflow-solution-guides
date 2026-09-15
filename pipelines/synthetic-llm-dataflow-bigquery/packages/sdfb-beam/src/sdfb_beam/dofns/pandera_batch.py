"""Per-batch Pandera validation — line 2 of defense in Mode A.

Converts a micro-batch of record dicts into a pandas DataFrame and runs
the Pandera schema with `lazy=True` (collect-all-failures). Failing
rows are split out via the `invalid` tagged output; passing rows
continue downstream.

REFs:
  - https://pandera.readthedocs.io/en/stable/lazy_validation.html
  - .claude/skills/validation-mode-a.md
"""

from __future__ import annotations

import apache_beam as beam
import pandas as pd
import pandera.errors as pa_err
from apache_beam.metrics import Metrics
from sdfb_core.contracts import TableSchema

from sdfb_beam.codegen import derive_pandera_schema


class PanderaValidateBatchDoFn(beam.DoFn):
  """Validate micro-batches with Pandera; split DLQ via tagged output."""

  def __init__(self, table_schema: TableSchema) -> None:
    super().__init__()
    self.table_schema = table_schema
    self._schema = None  # built in setup()

    self._valid = Metrics.counter("validation", "pandera_valid")
    self._invalid = Metrics.counter("validation", "pandera_invalid")

  def setup(self):
    self._schema = derive_pandera_schema(self.table_schema)

  # pylint: disable-next=arguments-renamed  # Beam passes the element positionally
  def process(self, batch):
    if not batch:
      return

    df = pd.DataFrame(batch)
    try:
      self._schema.validate(df, lazy=True)  # type: ignore[union-attr]
      for record in batch:
        self._valid.inc()
        yield record
      return
    except pa_err.SchemaErrors as e:
      failed_indices = self._extract_failed_indices(e, len(batch))
      for i, record in enumerate(batch):
        if i in failed_indices:
          self._invalid.inc()
          yield beam.pvalue.TaggedOutput(
              "invalid",
              {
                  "raw_record": record,
                  "error_type": "pandera",
                  "error_detail": self._summarize_for_row(e, i),
                  "rule_id": "schema.batch",
                  "stage": "pre_write",
              },
          )
        else:
          self._valid.inc()
          yield record

  @staticmethod
  def _extract_failed_indices(errors: pa_err.SchemaErrors,
                              batch_size: int) -> set[int]:
    """Best-effort: map Pandera failures back to batch row indices.

        Falls back to "whole batch invalid" if indices can't be teased out
        of the failure-cases DataFrame (which happens for schema-level
        failures like missing columns or wrong dtype on the whole series).
        """
    try:
      fc = errors.failure_cases
      if fc.empty:
        return set()
      if "index" not in fc.columns:
        return set(range(batch_size))
      indices = fc["index"].dropna()
      if indices.empty:
        return set(range(batch_size))
      out: set[int] = set()
      for v in indices.tolist():
        try:
          out.add(int(v))
        except (TypeError, ValueError):
          continue
      return out if out else set(range(batch_size))
    except Exception:  # pylint: disable=broad-exception-caught
      return set(range(batch_size))

  @staticmethod
  def _summarize_for_row(errors: pa_err.SchemaErrors, idx: int) -> dict:
    """Compact, JSON-friendly error summary for one row."""
    try:
      fc = errors.failure_cases
      row_fc = fc[fc["index"] == idx] if "index" in fc.columns else fc
      return {
          "failure_count": len(row_fc),
          "first_failures": row_fc.head(5).astype(str).to_dict("records"),
      }
    except Exception:  # pylint: disable=broad-exception-caught
      return {"raw": str(errors)[:500]}
