"""Per-batch seed derivation for the no-``--seed`` case.

Without this, ``seed=None`` reaches the engines' ``_mix_seed(None) → 0`` path
and every batch replays an identical draw (the 97.6 %-duplicate defect from
the 2026-07 E2E report). Deriving from ``(run_id, batch_id)`` keeps runs
reproducible — re-running the same run_id reproduces the exact output — while
guaranteeing no two batches share an RNG stream.
"""

from __future__ import annotations

import hashlib


def derive_batch_seed(run_id: str, batch_id: int) -> int:
  """Stable, collision-resistant seed from (run_id, batch_id), in [0, 2^63)."""
  digest = hashlib.blake2b(
      f"{run_id}\x1f{batch_id}".encode(), digest_size=8).digest()
  return int.from_bytes(digest, "big") >> 1


def derive_key_seed(run_id: str, key: tuple, salt: str = "") -> int:
  """Stable seed for one parent key's children (design 2026-09-10):
    the same parent yields the same children on a re-run of ``run_id``
    and on a retried bundle. ``repr`` keeps mixed-type tuples total.

    ``salt`` (design 2026-09-11, ADR 0037) mixes in a second stream for
    the same ``(run_id, key)`` — e.g. one per conditional edge, so two
    edges shuffle a key's candidates independently. The default keeps
    every seed derived before ``salt`` existed byte-identical: an empty
    salt hashes exactly the pre-``salt`` payload, no separator added.
    """
  payload = f"{run_id}\x1f{key!r}"
  if salt:
    payload = f"{payload}\x1f{salt}"
  digest = hashlib.blake2b(payload.encode(), digest_size=8).digest()
  return int.from_bytes(digest, "big") >> 1
