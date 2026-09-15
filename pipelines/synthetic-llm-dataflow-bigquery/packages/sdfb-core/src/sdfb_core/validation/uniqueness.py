"""Row/identity digests feeding the uniqueness gate rules."""

from __future__ import annotations

import hashlib
import json


def row_digest(record: dict) -> str:
  canon = json.dumps(record, sort_keys=True, default=str, separators=(",", ":"))
  return hashlib.blake2b(canon.encode(), digest_size=16).hexdigest()
