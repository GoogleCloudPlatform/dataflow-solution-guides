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
"""Row/identity digests feeding the uniqueness gate rules."""

from __future__ import annotations

import hashlib
import json


def row_digest(record: dict) -> str:
  canon = json.dumps(record, sort_keys=True, default=str, separators=(",", ":"))
  return hashlib.blake2b(canon.encode(), digest_size=16).hexdigest()
