"""BigQuery connectivity pre-check — fail fast if the API is unreachable.

The pre-check exists so that proxy / SSL / credentials problems surface
in seconds rather than after a Beam pipeline has built its DAG and
started spending wall-clock time inside DoFn `setup()`.
"""

from __future__ import annotations

import logging
import os
import time

from google.cloud import bigquery

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = float(os.environ.get("BIGQUERY_TIMEOUT", "50"))


def test_bigquery_connection(project: str,
                             timeout: float = DEFAULT_TIMEOUT) -> bool:
  """Run a no-op `SELECT 1` against the given project.

    Returns True on success, False on any failure (with diagnostics
    logged at ERROR level).
    """
  logger.info(
      "Testing BigQuery connection to project %r (timeout=%ss)...",
      project,
      timeout,
  )
  start = time.time()
  try:
    client = bigquery.Client(project=project)
    client.query("SELECT 1", timeout=timeout).result(timeout=timeout)
  except Exception as e:  # pylint: disable=broad-exception-caught
    elapsed = time.time() - start
    logger.error(
        "BigQuery connection FAILED after %.1fs: %s: %s",
        elapsed,
        type(e).__name__,
        e,
    )
    logger.error("Possible causes:")
    logger.error("  1. Proxy not configured / unreachable.")
    logger.error("     HTTP_PROXY=%s", os.environ.get("HTTP_PROXY", "NOT SET"))
    logger.error("  2. SSL: self-signed cert in proxy chain.")
    logger.error(
        "     REQUESTS_CA_BUNDLE=%s",
        os.environ.get("REQUESTS_CA_BUNDLE", "NOT SET"),
    )
    logger.error("  3. No VPN / network connectivity to GCP.")
    logger.error(
        "  4. Invalid creds (run: gcloud auth application-default login).")
    logger.error("  5. Project does not exist / insufficient permissions.")
    return False

  elapsed = time.time() - start
  logger.info("BigQuery connection OK (%.1fs)", elapsed)
  return True
