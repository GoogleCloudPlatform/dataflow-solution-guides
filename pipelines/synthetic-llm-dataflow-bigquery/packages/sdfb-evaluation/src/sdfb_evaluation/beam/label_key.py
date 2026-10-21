#  Copyright 2026 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""The hashed-label key, made on a worker so it never enters the job graph
(Ruling R68).

    p | LabelKey(uri) ──► PCollection[bytes], one element
          │  Create([uri or ""]) → Map(resolve_label_key)   (on a worker)
          ▼
    consumers (the dense and census emitters) read it as
    beam.pvalue.AsSingleton(key); their pure functions take the bytes

Only the URI is in the graph. A key passed to a transform's constructor
would be pickled into its ParDo payload, where anyone who can read the
job (dataflow.jobs.get, the staging bucket) could unpickle it.

    uri                     mode        key
    ──────────────────────  ──────────  ─────────────────────────────────
    "" / None               ephemeral   os.urandom(32), made per run
    gs://bucket/object      operator    the object's bytes, verbatim
    /local/path             operator    the file's bytes (DirectRunner)
    projects/P/secrets/S/   operator    the Secret Manager version's
      versions/V                        payload

A ParDo that reads the key as a side input sees one materialised value,
so the dense and census passes label alike within a run.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Callable
from typing import Any

import apache_beam as beam

__all__ = [
    "EPHEMERAL_KEY_BYTES",
    "LabelKey",
    "is_label_key_uri",
    "label_key_mode",
    "read_label_key",
    "resolve_label_key",
]

EPHEMERAL_KEY_BYTES = 32
_SECRET_RE = re.compile(r"projects/[^/]+/secrets/[^/]+/versions/[^/]+")
_SECRET_API = "https://secretmanager.googleapis.com/v1/{name}:access"
_HTTP_OK = 200

Reader = Callable[[str], bytes]


def label_key_mode(uri: str | None) -> str:
  """`operator` for a key URI, `ephemeral` without one (the registry's
  `label_key_mode`; the key itself is never recorded)."""
  return "operator" if uri else "ephemeral"


def is_label_key_uri(uri: str) -> bool:
  """Whether `uri` is one of the three forms `read_label_key` reads (a
  Secret Manager version, a `gs://` object, an absolute path) — for a
  caller that must refuse a malformed one before anything runs."""
  return bool(
      _SECRET_RE.fullmatch(uri) or uri.startswith("gs://") or
      os.path.isabs(uri))


def _secret(name: str, session_factory: Callable[[str], Any]) -> bytes:
  project = name.split("/")[1]
  response = session_factory(project).get(_SECRET_API.format(name=name))
  status = int(response.status_code)
  if status != _HTTP_OK:
    # the status only: a Secret Manager error body never carries the key
    raise PermissionError(f"Secret Manager {name}: HTTP {status}")
  data = response.json().get("payload", {}).get("data", "")
  return base64.b64decode(data)


def read_label_key(uri: str,
                   *,
                   session_factory: Callable[[str], Any]
                   | None = None) -> bytes:
  """The operator key at `uri`: a Secret Manager version (REST with ADC),
  a `gs://` object or a local file (Beam's `FileSystems`).

  Raises:
    ValueError: `uri` is none of those.
    PermissionError: Secret Manager refused the read.
  """
  if _SECRET_RE.fullmatch(uri):
    if session_factory is None:
      from sdfb_evaluation.context.gcp import make_session  # pylint: disable=import-outside-toplevel  # credentials only on the worker that reads a secret
      session_factory = make_session
    return _secret(uri, session_factory)
  if uri.startswith("gs://") or os.path.isabs(uri):
    from apache_beam.io.filesystem import CompressionTypes  # pylint: disable=import-outside-toplevel  # a worker-side read
    from apache_beam.io.filesystems import FileSystems  # pylint: disable=import-outside-toplevel  # same
    # UNCOMPRESSED: the key is the object's bytes, whatever its suffix
    # (AUTO would gunzip a `key.gz`)
    with FileSystems.open(
        uri, compression_type=CompressionTypes.UNCOMPRESSED) as handle:
      data: bytes = handle.read()
    return data
  raise ValueError("label key URI must be a Secret Manager version "
                   "(projects/P/secrets/S/versions/V), a gs:// object or an "
                   "absolute local path")


def resolve_label_key(uri: str | None, reader: Reader | None = None) -> bytes:
  """The label key: `os.urandom(32)` without a URI (ephemeral), else the
  bytes `reader` (default `read_label_key`) returns for it (operator).

  Raises:
    ValueError: the operator key is empty.
  """
  if not uri:
    return os.urandom(EPHEMERAL_KEY_BYTES)
  key = (reader or read_label_key)(uri)
  if not isinstance(key, bytes) or not key:
    raise ValueError("the operator label key is empty (Ruling R64)")
  return key


class LabelKey(beam.PTransform):
  """`PBegin → PCollection[bytes]` with one element, the label key, made
  on a worker (module docstring). `reader` replaces `read_label_key`
  (tests); it is pickled into the graph, so it must not hold the key."""

  def __init__(self, uri: str | None = None, *, reader: Reader | None = None):
    super().__init__()
    self._uri = uri or ""
    self._reader = reader

  def expand(self, input_or_inputs: beam.pvalue.PBegin) -> beam.PCollection:
    keys: beam.PCollection = (
        input_or_inputs
        | "Uri" >> beam.Create([self._uri])
        | "Resolve" >> beam.Map(resolve_label_key, self._reader))
    return keys
