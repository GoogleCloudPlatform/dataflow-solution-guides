"""Shared GCS → worker-local warm-pull (ADR 0012).

Offline loaders (`transformers`, vLLM) read weights from a **local
directory** only — they cannot open a `gs://` URI. So any weight prefix
staged in GCS must be pulled onto worker-local disk once, before the
loader touches it. Both the vLLM `ModelClient` (LLM weights) and the
generation DoFn (B.1's embedder) need this, so the pull lives here in
exactly one place rather than being duplicated per caller.

The pull is **single-flight, idempotent, and atomic** (2026-07-24 E2E
postmortem). Beam runs many bundle threads in one SDK process, and every
DoFn setup() used to re-download the prefix onto its final paths —
`download_to_filename` truncates in place, so a thread re-pulling
`model.safetensors` while another thread had it mmap'd inside
`from_pretrained` killed the whole harness with SIGBUS ("bus error"). Now:

  * a per-destination `threading.Lock` collapses concurrent callers to one
    pull (the rest wait, then hit the completion marker and return);
  * a `.sdfb_pull_complete` marker recording the source URI makes repeat
    setup() calls (and harness restarts on a warm disk) free;
  * blobs download into a sibling temp dir and are `os.replace`d into
    place — a rename swaps the directory entry, so a concurrent reader's
    mmap keeps the old inode and can never observe truncation, even from
    another process.

Uses the `google-cloud-storage` Python client, authenticated via ADC on
the worker. Never shells out to `gsutil`. The heavy import stays inside
the function so importing this module on a bare laptop is dependency-free.
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Completion marker written inside the destination dir after a successful
# pull; holds the source URI so a re-pull of a *different* prefix into the
# same dir (e.g. an embedder version bump) is never mistaken for done.
PULL_MARKER = ".sdfb_pull_complete"

_DIR_LOCKS: dict[str, threading.Lock] = {}
_DIR_LOCKS_GUARD = threading.Lock()


def _dir_lock(local_dir: str) -> threading.Lock:
  with _DIR_LOCKS_GUARD:
    return _DIR_LOCKS.setdefault(local_dir, threading.Lock())


def split_gs_uri(uri: str) -> tuple[str, str]:
  """Split `gs://bucket/path/to/prefix/` → `("bucket", "path/to/prefix/")`.

    The returned prefix keeps any trailing slash so `blob.name[len(prefix):]`
    yields paths relative to the model directory.
    """
  if not uri.startswith("gs://"):
    raise ValueError(f"Not a gs:// URI: {uri!r}")
  parts = urlsplit(uri)
  bucket = parts.netloc
  prefix = parts.path.lstrip("/")
  if not bucket:
    raise ValueError(f"gs:// URI has no bucket: {uri!r}")
  return bucket, prefix


def localize_gcs_prefix(uri: str, local_dir: str) -> str:
  """Warm-pull every blob under `uri` (gs://) into `local_dir`.

    Returns `local_dir` for call-site convenience. Raises `RuntimeError` if
    the prefix contains no blobs (a mistyped URI would otherwise silently
    yield an empty directory that the loader then fails on cryptically).

    Safe to call from any number of threads and repeatedly across DoFn
    setup() invocations: the first caller pulls, everyone else reuses (see
    module docstring).
    """
  from pathlib import Path

  with _dir_lock(local_dir):
    dest_root = Path(local_dir)
    marker = dest_root / PULL_MARKER
    if marker.is_file() and marker.read_text(encoding="utf-8") == uri:
      logger.info("Reusing warm-pulled files at %s (marker hit)", local_dir)
      return local_dir
    return _pull_locked(uri, dest_root, marker)


def _pull_locked(uri: str, dest_root, marker) -> str:
  """Do the actual pull; caller holds the destination lock."""
  from google.cloud import storage

  bucket_name, prefix = split_gs_uri(uri)
  logger.info("Warm-pulling gs://%s/%s → %s", bucket_name, prefix, dest_root)
  # A stale marker (different URI) must not survive a failed re-pull.
  if marker.is_file():
    marker.unlink()

  client = storage.Client()
  dest_root.mkdir(parents=True, exist_ok=True)
  # Sibling temp dir — same filesystem, so os.replace() below is an
  # atomic rename, never a copy.
  tmp_root = tempfile.mkdtemp(
      prefix=dest_root.name + ".pull-", dir=str(dest_root.parent))
  try:
    staged: list[tuple[str, str]] = []
    for blob in client.list_blobs(bucket_name, prefix=prefix):
      rel = blob.name[len(prefix):].lstrip("/")
      if not rel:
        # The prefix "directory" placeholder blob, if present.
        continue
      tmp_dest = os.path.join(tmp_root, rel)
      os.makedirs(os.path.dirname(tmp_dest), exist_ok=True)
      blob.download_to_filename(tmp_dest)
      staged.append((rel, tmp_dest))
    if not staged:
      raise RuntimeError(
          f"No blobs found under gs://{bucket_name}/{prefix} — check "
          f"the URI. Nothing was pulled to {dest_root}.")
    for rel, tmp_dest in staged:
      final = dest_root / rel
      final.parent.mkdir(parents=True, exist_ok=True)
      os.replace(tmp_dest, final)
    # Marker last, and itself atomically: its presence must imply a
    # complete tree.
    marker_tmp = os.path.join(tmp_root, PULL_MARKER)
    with open(marker_tmp, "w", encoding="utf-8") as f:
      f.write(uri)
    os.replace(marker_tmp, marker)
  finally:
    shutil.rmtree(tmp_root, ignore_errors=True)
  logger.info("Pulled %d files to %s", len(staged), dest_root)
  return str(dest_root)


__all__ = ["PULL_MARKER", "localize_gcs_prefix", "split_gs_uri"]
