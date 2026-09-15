"""SSL / proxy environment configuration for BigQuery clients.

Reads from environment variables only — never hardcoded. Set
`REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` (corporate CA bundle) and
`HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` in the shell or run config
before invoking the extractor.
"""

from __future__ import annotations

import logging
import os

import certifi

logger = logging.getLogger(__name__)


def configure_ssl() -> None:
  """Log which CA bundle is in effect. No mutation — environment-driven."""
  ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get(
      "SSL_CERT_FILE")
  if ca_bundle:
    logger.info("Using custom CA bundle: %s", ca_bundle)
    return
  logger.info("Using certifi default CA bundle: %s", certifi.where())


def log_proxy_config() -> None:
  """Log current proxy configuration (informational only)."""
  http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
  https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
  no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")

  if http_proxy or https_proxy:
    logger.info("Proxy config (from environment):")
    logger.info("  HTTP_PROXY:  %s", http_proxy or "NOT SET")
    logger.info("  HTTPS_PROXY: %s", https_proxy or "NOT SET")
    logger.info("  NO_PROXY:    %s", no_proxy or "NOT SET")
  else:
    logger.info("No proxy configured (HTTP_PROXY / HTTPS_PROXY not set).")
