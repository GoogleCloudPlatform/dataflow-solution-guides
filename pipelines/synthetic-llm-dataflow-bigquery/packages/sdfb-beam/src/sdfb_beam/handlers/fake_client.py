"""Deterministic `ModelClient` for laptop / DirectRunner testing.

Two construction modes (exactly one must be given):

  - `canned`: `FakeModelClient(responses=[{...}, {...}])`
      Cycles through the response list in order, one per `generate_json`
      call (regardless of `n` — `n` advances the cursor by `n`). Use when
      a test needs known outputs in known order.

  - `echo`: `FakeModelClient(reference_pool=[{...}, ...])`
      For each requested record, hashes `(prompt, json_schema, idx, seed)`
      to pick an index into the pool. Same hash key → same draw, across
      processes / Beam workers. Use for DirectRunner end-to-end tests and
      reproducibility checks.

Schema-aware (generate-from-schema) mode is intentionally not
implemented: real LLM outputs are noisy, and the `reference_pool`
strategy gives the engine valid records to round-trip through the
validation stack — closer to what production looks like with a
constrained-decoding-enabled vLLM.

The class is a structural `ModelClient` (Protocol; runtime-checkable).
It does not subclass anything from `sdfb_core.engines.base` — engines
test interchangeability via `isinstance(client, ModelClient)`.
"""

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

import hashlib
import json
from typing import Any


class FakeModelClient:
  """Deterministic test/dev implementation of the `ModelClient` Protocol."""

  def __init__(
      self,
      *,
      responses: list[dict] | None = None,
      reference_pool: list[dict] | None = None,
  ) -> None:
    if (responses is None) == (reference_pool is None):
      raise ValueError(
          "FakeModelClient: pass exactly one of `responses=` or "
          "`reference_pool=`. Got "
          f"responses={'set' if responses is not None else 'unset'}, "
          f"reference_pool={'set' if reference_pool is not None else 'unset'}.")
    self._responses: list[dict] | None = list(responses) if responses else None
    self._reference_pool: list[dict] | None = (
        list(reference_pool) if reference_pool else None)
    self._mode: str = "canned" if responses else "echo"
    self._idx: int = 0
    self.call_count: int = 0

  @property
  def mode(self) -> str:
    return self._mode

  def generate_json(
      self,
      prompt: str,
      json_schema: dict,
      *,
      # ModelClient Protocol signature; the fake ignores the sampling knobs.
      max_tokens: int = 2048,  # pylint: disable=unused-argument
      temperature: float = 0.7,  # pylint: disable=unused-argument
      n: int = 1,
      seed: int | None = None,
      top_p: float | None = None,  # pylint: disable=unused-argument
      top_k: int | None = None,  # pylint: disable=unused-argument
  ) -> list[dict]:
    self.call_count += 1
    if self._mode == "canned":
      assert self._responses is not None
      out: list[dict] = []
      for _ in range(n):
        out.append(self._responses[self._idx % len(self._responses)])
        self._idx += 1
      return out

    # echo mode
    assert self._reference_pool is not None
    pool = self._reference_pool
    out = []
    for i in range(n):
      h = self._hash(prompt, json_schema, i, seed)
      out.append(pool[h % len(pool)])
    return out

  @staticmethod
  def _hash(
      prompt: str,
      json_schema: dict[str, Any],
      idx: int,
      seed: int | None,
  ) -> int:
    h = hashlib.sha256()
    h.update(prompt.encode("utf-8"))
    h.update(b"\x00")
    h.update(
        json.dumps(json_schema, sort_keys=True, default=str).encode("utf-8"))
    h.update(b"\x00")
    h.update(str(idx).encode("utf-8"))
    if seed is not None:
      h.update(b"\x00")
      h.update(str(seed).encode("utf-8"))
    # First 8 bytes → 64-bit int; plenty of room for pool indices.
    return int.from_bytes(h.digest()[:8], "big")
