"""Load relationship models from a directory, a file, or GCS (ADR 0032).

`config/relationships/` ships inside the flex-template image, so the
default needs no upload and no `bq update`. Pointing
``--relationships_uri`` at a `gs://` path overrides the packaged models
for that launch — a relationship change is then a file upload, not an
image rebuild and not a production metadata edit.

Parsing and every graph question live in
:mod:`sdfb_core.contracts.relationships` (pure Python, no Beam); this
module only turns a URI into ``(source, text)`` pairs.
"""

from __future__ import annotations

import logging

from apache_beam.io.filesystems import FileSystems
from sdfb_core.contracts.relationships import (
    RelationshipError,
    RelationshipRegistry,
)
from sdfb_core.observability import log_milestone

_SUFFIXES = (".yaml", ".yml")

# The packaged location, shipped inside the flex-template image. Absence
# is only legitimate HERE: anywhere else, somebody pointed on purpose.
DEFAULT_RELATIONSHIPS_URI = "config/relationships"


def _patterns(uri: str) -> list[str]:
  """A file URI matches itself; anything else is treated as a folder.

    One level, no recursion: Beam's ``*`` does not cross ``/``, so
    ``gs://bucket/models`` finds ``gs://bucket/models/retail.yaml`` and
    NOT ``gs://bucket/models/legacy/retail.yaml``.
    """
  if uri.endswith(_SUFFIXES):
    return [uri]
  base = uri.rstrip("/")
  return [f"{base}/*{suffix}" for suffix in _SUFFIXES]


def is_sample_model(path: str) -> bool:
  """True for a documentation sample (`example_*.yaml`, `*.example.yaml`).

    Public because the two places that walk a relationships directory —
    this loader's directory scan and `scripts/relationships/card.py`'s
    local-path scan — must agree on exactly which files are samples.
    """
  name = path.rsplit("/", 1)[-1]
  return name.startswith("example_") or ".example." in name


def load_relationship_registry(uri: str) -> RelationshipRegistry:
  """Every model file under ``uri``, validated into one registry.

    Three outcomes, and the difference matters more than the code:

    * ``uri=""`` — relationships deliberately off; empty registry.
    * the PACKAGED default holds no models — legitimate ("nothing
      declared anywhere"); every table generates alone.
    * anything else empty, unreadable or unparseable — a LOUD stop.
      Someone typed that path; silently degrading to "no relationships"
      would generate every table alone and lose the whole model, which
      looks like a successful run.
    """
  if not uri:
    return RelationshipRegistry()
  patterns = _patterns(uri)
  paths: list[str] = []
  for pattern in patterns:
    try:
      for match in FileSystems.match([pattern]):
        paths.extend(metadata.path for metadata in match.metadata_list)
    except Exception as exc:
      # A missing bucket, a denied read, a bad scheme. Never
      # swallowed: this is the first thing a gs:// override gets
      # wrong, and it must not read as "no relationships".
      raise RelationshipError(
          f"{uri}: could not be listed ({type(exc).__name__}: "
          f"{exc}). Checked {patterns}. Fix the URI, or grant the "
          f"launcher's service account storage.objects.list/get on "
          f"the bucket.") from exc
  # Documentation samples (`example_*.yaml`, `*.example.yaml`) live next
  # to real models in the packaged directory and use the same anonymised
  # aliases; a directory scan skips them (launch …-5531344118137403488
  # died on "A_TABLE declared in 2 models"). A URI that names a sample
  # file directly still loads it.
  direct = uri.rstrip("/").endswith((".yaml", ".yml"))
  skipped = ([] if direct else
             [p for p in sorted(set(paths)) if is_sample_model(p)])
  if skipped:
    log_milestone(
        "relationships_example_skipped",
        uri=uri,
        files=",".join(p.rsplit("/", 1)[-1] for p in skipped),
        note="documentation samples are never loaded from a directory "
        "scan; name the file directly to load one",
    )
  sources: list[tuple[str, str]] = []
  for path in sorted(set(paths) - set(skipped)):
    try:
      with FileSystems.open(path) as handle:
        sources.append((path, handle.read().decode("utf-8")))
    except Exception as exc:
      raise RelationshipError(
          f"{path}: could not be read ({type(exc).__name__}: {exc}) — "
          f"refusing to launch with a partially-loaded relational "
          f"model") from exc
  if not sources:
    if uri.rstrip("/") != DEFAULT_RELATIONSHIPS_URI.rstrip("/"):
      raise RelationshipError(
          f"{uri}: no model files there. Checked {patterns}. A "
          f"relationships URI you pass explicitly must hold at "
          f"least one .yaml/.yml model — refusing to generate every "
          f"table in isolation as if none were declared. (Pass "
          f"--relationships_uri='' to turn relationships off on "
          f"purpose; note that the match is ONE level deep and the "
          f"files must end in .yaml or .yml.)")
    log_milestone(
        "relationships_absent",
        uri=uri,
        note="no model files in the packaged default location — "
        "every table generates in isolation with PK/identity from "
        "the CLI flags",
    )
    return RelationshipRegistry()
  registry = RelationshipRegistry.from_sources(sources)
  log_milestone(
      "relationships_loaded",
      uri=uri,
      files=len(sources),
      models=",".join(m.model for m in registry.models),
      tables=sum(len(m.tables) for m in registry.models),
      sha=registry.sha12(),
      level=logging.INFO,
  )
  return registry


__all__ = [
    "DEFAULT_RELATIONSHIPS_URI",
    "is_sample_model",
    "load_relationship_registry",
]
