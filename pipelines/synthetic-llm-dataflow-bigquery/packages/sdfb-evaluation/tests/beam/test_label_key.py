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
"""Tests for `sdfb_evaluation.beam.label_key` (Ruling R68): the hashed-
label key is resolved on a worker, from an operator URI or ephemerally.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from sdfb_evaluation.beam.label_key import (
    EPHEMERAL_KEY_BYTES,
    label_key_mode,
    read_label_key,
    resolve_label_key,
)

_SECRET = "projects/demo-project/secrets/label-key/versions/3"


class _Response:

  def __init__(self, status: int, body: dict[str, Any]):
    self.status_code = status
    self._body = body

  def json(self) -> dict[str, Any]:
    return self._body


class _Session:
  """A fake authorised session for Secret Manager's `:access`."""

  def __init__(self, response: _Response):
    self.response = response
    self.urls: list[str] = []

  def get(self, url: str) -> _Response:
    self.urls.append(url)
    return self.response


def test_ephemeral_keys_are_fresh_random_bytes():
  first, second = resolve_label_key(None), resolve_label_key("")
  assert len(first) == len(second) == EPHEMERAL_KEY_BYTES
  assert first != second
  assert label_key_mode(None) == label_key_mode("") == "ephemeral"
  assert label_key_mode(_SECRET) == "operator"


def test_operator_key_comes_from_the_reader():
  assert resolve_label_key(
      "gs://b/k", lambda uri: b"from:" + uri.encode()) == b"from:gs://b/k"
  with pytest.raises(ValueError, match="empty"):
    resolve_label_key("gs://b/k", lambda uri: b"")


def test_read_label_key_reads_a_local_file_verbatim(tmp_path):
  path = tmp_path / "label.key"
  path.write_bytes(b"\x00secret\n")
  assert read_label_key(str(path)) == b"\x00secret\n"


def test_a_compressed_looking_name_is_still_read_verbatim(tmp_path):
  path = tmp_path / "label.key.gz"  # not gzip: AUTO would try to inflate it
  path.write_bytes(b"raw-key-bytes")
  assert read_label_key(str(path)) == b"raw-key-bytes"


def test_read_label_key_reads_a_secret_manager_version():
  session = _Session(
      _Response(200, {"payload": {
          "data": base64.b64encode(b"k3y").decode()
      }}))
  seen: list[str] = []

  def factory(project: str) -> _Session:
    seen.append(project)
    return session

  assert read_label_key(_SECRET, session_factory=factory) == b"k3y"
  assert seen == ["demo-project"]
  assert session.urls == [
      f"https://secretmanager.googleapis.com/v1/{_SECRET}:access"
  ]


def test_a_refused_secret_names_the_status_only():
  session = _Session(_Response(403, {"error": {"message": "denied"}}))
  with pytest.raises(PermissionError, match="HTTP 403"):
    read_label_key(_SECRET, session_factory=lambda project: session)


def test_an_unknown_uri_is_refused():
  with pytest.raises(ValueError, match="Secret Manager"):
    read_label_key("relative/path.key")
