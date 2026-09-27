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
"""Unit tests for `row_digest` — the pure digest feeding the uniqueness gate."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

from sdfb_core.validation.uniqueness import row_digest


def test_digest_stable_across_key_order():
  assert row_digest({"a": 1, "b": "x"}) == row_digest({"b": "x", "a": 1})


def test_digest_differs_on_value_change():
  assert row_digest({"a": 1}) != row_digest({"a": 2})


def test_non_json_types_hash_via_str():
  from datetime import date

  assert row_digest({"d": date(2026, 1,
                               1)}) == row_digest({"d": date(2026, 1, 1)})
