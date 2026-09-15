"""Beam pipeline wrapper around the DDL extractor.

For the single-table common case the pipeline is overkill, but it
gives us `FileSystems.create()` for transparent local / `gs://` output
paths and keeps the entire extraction flow consistent with the rest of
the Beam-first project stance.
"""

from __future__ import annotations

import json
import logging
import os

import apache_beam as beam
from apache_beam.io.filesystems import FileSystems
from apache_beam.options.pipeline_options import PipelineOptions, StandardOptions

from sdfb_beam.ddl.extractor import extract_ddl_metadata

logger = logging.getLogger(__name__)


class ExtractDDLMetadataDoFn(beam.DoFn):
  """Calls `extract_ddl_metadata()` for each `{project, dataset, table}`."""

  def __init__(self, timeout: float) -> None:
    super().__init__()
    self.timeout = timeout

  # pylint: disable-next=arguments-renamed  # Beam passes the element positionally
  def process(self, table_ref: dict):
    yield extract_ddl_metadata(
        project=table_ref["project"],
        dataset=table_ref["dataset"],
        table=table_ref["table"],
        timeout=self.timeout,
    )


class WriteDDLToJSON(beam.DoFn):
  """Write a single DDL metadata dict to `output_path` (local or gs://)."""

  def __init__(self, output_path: str) -> None:
    super().__init__()
    self.output_path = output_path

  def process(self, element: dict):
    content = json.dumps(element, indent=2).encode("utf-8")
    with FileSystems.create(self.output_path) as f:
      f.write(content)
    logger.info(
        "Wrote %d bytes of DDL metadata to %s",
        len(content),
        self.output_path,
    )
    yield {"path": self.output_path, "bytes": len(content)}


def get_output_path(
    options: PipelineOptions,
    base_path: str,
    dataset: str,
    table: str,
) -> str:
  """Resolve the JSON output path for `{dataset}.{table}`.

    Layout: `{base_path}/{dataset}/ddl_metadata_{dataset}_{table}.json`.
    For `DirectRunner` the parent directory is created on disk.
    """
  runner = options.view_as(StandardOptions).runner
  filename = f"ddl_metadata_{dataset}_{table}.json"

  if runner == "DirectRunner":
    out_dir = os.path.join(base_path, dataset)
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, filename)

  if not base_path.startswith("gs://"):
    logger.warning(
        "Output base %s does not start with gs:// — Dataflow runners "
        "expect a GCS path.",
        base_path,
    )
  return f"{base_path}/{dataset}/{filename}"


def build_pipeline(
    p: beam.Pipeline,
    *,
    project: str,
    dataset: str,
    table: str,
    output_path: str,
    timeout: float,
):
  """Wire the DDL extraction DAG onto an existing Beam Pipeline."""
  return (p
          | "CreateTableRef" >> beam.Create([{
              "project": project,
              "dataset": dataset,
              "table": table
          }])
          | "ExtractDDL" >> beam.ParDo(ExtractDDLMetadataDoFn(timeout=timeout))
          | "WriteDDL" >> beam.ParDo(WriteDDLToJSON(output_path)))
