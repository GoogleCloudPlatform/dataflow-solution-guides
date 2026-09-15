"""Local-filesystem sinks for DirectRunner development.

Production BigQuery sinks (`FILE_LOADS` method, partitioned + clustered
DLQ table) live in `sdfb_beam/io/bq_sinks.py` (M1 §11). The DAG accepts
any `PTransform` for landing / dlq, so the production wiring is a
one-line swap at the call site.
"""

from __future__ import annotations

import json

import apache_beam as beam


class WriteToJsonLines(beam.PTransform):
  """Serialize a PCollection of dicts to JSONL on local disk / GCS text.

    Wraps Beam's `WriteToText` with `json.dumps(default=str)` so that
    `datetime`, `Decimal`, and other non-JSON-native types in record
    dicts survive serialization without per-call adapter code.

    For deterministic outputs in tests, leave `num_shards=1` so all
    records land in a single `*-00000-of-00001.jsonl` file (when
    `num_shards>1`) or `<prefix>.jsonl` (when `num_shards=1`).
    """

  def __init__(self, path_prefix: str, num_shards: int = 1) -> None:
    super().__init__()
    self.path_prefix = path_prefix
    self.num_shards = num_shards

  # pylint: disable-next=arguments-renamed  # Beam passes the PCollection positionally
  def expand(self, pcoll):  # type: ignore[override]
    return (pcoll
            | "ToJSON" >>
            beam.Map(lambda r: json.dumps(r, default=str, sort_keys=True))
            | "WriteText" >> beam.io.WriteToText(
                self.path_prefix,
                file_name_suffix=".jsonl",
                num_shards=self.num_shards,
                shard_name_template=("-SSSSS-of-NNNNN"
                                     if self.num_shards != 1 else ""),
            ))
