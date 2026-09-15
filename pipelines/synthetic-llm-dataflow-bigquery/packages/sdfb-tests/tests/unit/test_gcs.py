"""Unit tests for `sdfb_beam.gcs` — the shared GCS → local warm-pull.

The `google.cloud.storage` client is mocked by injecting a fake module into
`sys.modules`, so these run on a bare laptop with no GCP deps installed.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=broad-exception-caught,import-outside-toplevel,missing-class-docstring,redefined-outer-name

from __future__ import annotations

import sys
import threading
import time
import types

import pytest
from sdfb_beam.gcs import localize_gcs_prefix, split_gs_uri


@pytest.fixture
def fake_gcs(monkeypatch):
  """Inject a fake `google.cloud.storage` module + return its recorder.

    Blobs write a real file at the destination (content = blob name) so
    tests can assert on the final on-disk layout, and record every path
    they were asked to write — which the atomicity tests use to prove the
    final tree is never written directly.
    """

  class _FakeBlob:

    def __init__(self, name, delay=0.0):
      self.name = name
      self.downloaded_to = None
      self.delay = delay

    def download_to_filename(self, dest):
      if self.delay:
        time.sleep(self.delay)
      self.downloaded_to = dest
      with open(dest, "w", encoding="utf-8") as f:
        f.write(self.name)

  recorder: dict = {"list_calls": [], "blobs": []}

  class _FakeStorageClient:

    def list_blobs(self, bucket, prefix=""):
      recorder["list_calls"].append((bucket, prefix))
      return list(recorder["blobs"])

  storage_mod = types.ModuleType("google.cloud.storage")
  storage_mod.Client = lambda *_a, **_k: _FakeStorageClient()

  cloud_mod = types.ModuleType("google.cloud")
  cloud_mod.storage = storage_mod
  google_mod = sys.modules.get("google") or types.ModuleType("google")

  monkeypatch.setitem(sys.modules, "google", google_mod)
  monkeypatch.setitem(sys.modules, "google.cloud", cloud_mod)
  monkeypatch.setitem(sys.modules, "google.cloud.storage", storage_mod)
  recorder["blob_cls"] = _FakeBlob
  return recorder


def test_split_gs_uri_returns_bucket_and_prefix():
  assert split_gs_uri("gs://bkt/a/b/c/") == ("bkt", "a/b/c/")


@pytest.mark.parametrize("bad", ["/local/path", "s3://x/y", "gs://"])
def test_split_gs_uri_rejects_non_gcs(bad):
  with pytest.raises(ValueError):
    split_gs_uri(bad)


def test_import_does_not_require_google_cloud():
  """Importing the module must not drag in google.cloud.storage."""
  # The lazy import lives inside localize_gcs_prefix, not at module scope.
  import importlib

  import sdfb_beam.gcs as gcs_mod

  importlib.reload(gcs_mod)
  # Nothing to assert beyond a clean import; the reload would raise if the
  # heavy dep were imported at module scope on a laptop without it.


def test_localize_downloads_blobs_relative_to_prefix(fake_gcs, tmp_path):
  blob_cls = fake_gcs["blob_cls"]
  prefix = "synthetic/models/embedders/bge-small-en-v1.5/v1/"
  fake_gcs["blobs"] = [
      blob_cls(prefix),  # directory placeholder — must be skipped
      blob_cls(prefix + "config.json"),
      blob_cls(prefix + "model.safetensors"),
      blob_cls(prefix + "1_Pooling/config.json"),  # nested
  ]
  dest = str(tmp_path / "emb")

  out = localize_gcs_prefix(f"gs://my-bucket/{prefix}", dest)

  assert out == dest
  assert fake_gcs["list_calls"] == [("my-bucket", prefix)]
  for rel in ("config.json", "model.safetensors", "1_Pooling/config.json"):
    assert (tmp_path / "emb" / rel).is_file(), rel


def test_localize_raises_when_no_blobs(fake_gcs, tmp_path):
  fake_gcs["blobs"] = []
  with pytest.raises(RuntimeError, match="No blobs found"):
    localize_gcs_prefix("gs://empty/no/such/prefix/", str(tmp_path / "emb"))


# ---------------------------------------------------------------------------
# Idempotency / atomicity / single-flight (2026-07-24 E2E postmortem).
#
# The 16:39 corp run's SDK harness died twice with SIGBUS: a second DoFn
# setup() re-pulled the embedder prefix and `download_to_filename` truncated
# `model.safetensors` in place while another thread had it mmap'd inside
# `from_pretrained`. These tests pin the contract that makes that impossible.
# ---------------------------------------------------------------------------


def _seed_blobs(fake_gcs, prefix, *rels, delay=0.0):
  blob_cls = fake_gcs["blob_cls"]
  fake_gcs["blobs"] = [blob_cls(prefix + rel, delay=delay) for rel in rels]


def test_localize_second_call_is_a_no_op(fake_gcs, tmp_path):
  """A completed pull must never be repeated for the same URI."""
  prefix = "models/emb/v1/"
  _seed_blobs(fake_gcs, prefix, "config.json", "model.safetensors")
  dest = str(tmp_path / "emb")
  uri = f"gs://bkt/{prefix}"

  localize_gcs_prefix(uri, dest)
  localize_gcs_prefix(uri, dest)

  assert len(fake_gcs["list_calls"]) == 1


def test_localize_repulls_when_dir_holds_a_different_uri(fake_gcs, tmp_path):
  """Same local dir, different source prefix → must re-pull, not reuse."""
  prefix_a, prefix_b = "models/emb/v1/", "models/emb/v2/"
  dest = str(tmp_path / "emb")

  _seed_blobs(fake_gcs, prefix_a, "model.safetensors")
  localize_gcs_prefix(f"gs://bkt/{prefix_a}", dest)
  _seed_blobs(fake_gcs, prefix_b, "model.safetensors")
  localize_gcs_prefix(f"gs://bkt/{prefix_b}", dest)

  assert len(fake_gcs["list_calls"]) == 2
  content = (tmp_path / "emb" / "model.safetensors").read_text()
  assert content == prefix_b + "model.safetensors"


def test_localize_never_downloads_onto_final_paths(fake_gcs, tmp_path):
  """Blobs must land in a temp area and be renamed into place.

    `download_to_filename` opens with `wb` — writing the final path directly
    truncates a file a concurrent reader may have mmap'd (SIGBUS). A rename
    swaps the directory entry instead; existing maps keep the old inode.
    """
  prefix = "models/emb/v1/"
  _seed_blobs(fake_gcs, prefix, "config.json", "1_Pooling/config.json")
  dest = str(tmp_path / "emb")

  localize_gcs_prefix(f"gs://bkt/{prefix}", dest)

  final_paths = {
      str(tmp_path / "emb" / "config.json"),
      str(tmp_path / "emb" / "1_Pooling" / "config.json"),
  }
  written = {b.downloaded_to for b in fake_gcs["blobs"] if b.downloaded_to}
  assert written.isdisjoint(final_paths), written
  for rel in ("config.json", "1_Pooling/config.json"):
    assert (tmp_path / "emb" / rel).is_file(), rel


def test_localize_failed_pull_leaves_no_completion_state(fake_gcs, tmp_path):
  """After a no-blobs failure the next call must attempt a fresh pull."""
  dest = str(tmp_path / "emb")
  fake_gcs["blobs"] = []
  with pytest.raises(RuntimeError):
    localize_gcs_prefix("gs://bkt/models/emb/v1/", dest)

  _seed_blobs(fake_gcs, "models/emb/v1/", "model.safetensors")
  localize_gcs_prefix("gs://bkt/models/emb/v1/", dest)
  assert (tmp_path / "emb" / "model.safetensors").is_file()


def test_localize_concurrent_callers_pull_once(fake_gcs, tmp_path):
  """8 threads racing the same prefix → exactly one download pass.

    Mirrors the production topology: one SDK process, 8 bundle threads, all
    DoFn setup()s warm-pulling the same `EMBEDDER_LOCAL_DIR`.
    """
  prefix = "models/emb/v1/"
  _seed_blobs(fake_gcs, prefix, "model.safetensors", delay=0.05)
  dest = str(tmp_path / "emb")
  uri = f"gs://bkt/{prefix}"
  results, errors = [], []

  def call():
    try:
      results.append(localize_gcs_prefix(uri, dest))
    except Exception as e:  # pragma: no cover - failure diagnostics
      errors.append(e)

  threads = [threading.Thread(target=call) for _ in range(8)]
  for t in threads:
    t.start()
  for t in threads:
    t.join()

  assert not errors
  assert results == [dest] * 8
  assert len(fake_gcs["list_calls"]) == 1
  assert (tmp_path / "emb" / "model.safetensors").is_file()
