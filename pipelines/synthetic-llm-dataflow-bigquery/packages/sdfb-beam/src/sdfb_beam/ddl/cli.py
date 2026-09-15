"""CLI entry point for the DDL extraction job.

Invoked from `scripts/extract_ddl.py` or directly:

    python -m sdfb_beam.ddl.cli \\
        --project my-project --dataset my_ds --table my_t \\
        --runner DirectRunner --output_base ./output
"""

from __future__ import annotations

import argparse
import logging
import sys

import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions

from sdfb_beam.ddl.connection import DEFAULT_TIMEOUT, test_bigquery_connection
from sdfb_beam.ddl.env import configure_ssl, log_proxy_config
from sdfb_beam.ddl.pipeline import build_pipeline, get_output_path

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(
      description="BigQuery DDL Metadata Extraction Pipeline")
  parser.add_argument("--project", required=True, help="GCP project ID")
  parser.add_argument("--dataset", required=True, help="BigQuery dataset")
  parser.add_argument("--table", required=True, help="BigQuery table")
  parser.add_argument(
      "--output_base",
      default=None,
      help="Output base path. Local dir for DirectRunner, gs:// for DataflowRunner.",
  )
  parser.add_argument(
      "--runner",
      default="DirectRunner",
      help="Pipeline runner (DirectRunner | DataflowRunner).",
  )
  parser.add_argument(
      "--skip_connection_test",
      action="store_true",
      help="Skip the BigQuery connection pre-check.",
  )
  parser.add_argument(
      "--timeout",
      type=float,
      default=DEFAULT_TIMEOUT,
      help=f"Connection / read timeout in seconds (default: {DEFAULT_TIMEOUT}).",
  )

  args, pipeline_args = parser.parse_known_args(argv)

  # `force=True` is critical: Beam's import side-effects configure the
  # root logger before we get here, which makes a plain `basicConfig`
  # call a silent no-op. Without `force=True` the entire INFO-level
  # banner + preflight + progress stream disappears and the script
  # looks frozen.
  logging.basicConfig(
      level=logging.INFO,
      format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
      force=True,
  )

  logger.info("=" * 60)
  logger.info("BigQuery DDL Metadata Extraction Pipeline")
  logger.info("=" * 60)
  logger.info("  Project:  %s", args.project)
  logger.info("  Dataset:  %s", args.dataset)
  logger.info("  Table:    %s", args.table)
  logger.info("  Runner:   %s", args.runner)
  logger.info("  Timeout:  %ss", args.timeout)
  logger.info("=" * 60)

  log_proxy_config()
  configure_ssl()

  if not args.skip_connection_test:
    if not test_bigquery_connection(args.project, timeout=args.timeout):
      logger.critical("Aborting: cannot connect to BigQuery (see errors).")
      return 1
  else:
    logger.info("Skipping connection pre-check (--skip_connection_test).")

  options = PipelineOptions(
      pipeline_args,
      runner=args.runner,
      project=args.project,
      temp_location=("./temp" if args.runner == "DirectRunner" else
                     f"gs://{args.project}-temp/dataflow/temp"),
      save_main_session=True,
  )

  output_base = args.output_base or (
      "./output" if args.runner == "DirectRunner" else
      f"gs://{args.project}-dataflow/ddl_metadata")
  output_path = get_output_path(options, output_base, args.dataset, args.table)
  logger.info("Resolved output path: %s", output_path)

  try:
    with beam.Pipeline(options=options) as p:
      build_pipeline(
          p,
          project=args.project,
          dataset=args.dataset,
          table=args.table,
          output_path=output_path,
          timeout=args.timeout,
      )
  except KeyboardInterrupt:
    logger.warning("Pipeline interrupted by user (Ctrl+C).")
    return 130
  except Exception:  # pylint: disable=broad-exception-caught
    logger.exception("DDL extraction pipeline failed.")
    return 1

  logger.info("=" * 60)
  logger.info("DDL metadata pipeline completed successfully.")
  logger.info("=" * 60)
  return 0


if __name__ == "__main__":
  sys.exit(main())
