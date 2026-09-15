"""`BgeEmbedder` construction must be serialized per process.

2026-07-24 E2E postmortem: with 8 Beam bundle threads running DoFn setup()
concurrently in one SDK process, the lazy `from transformers import
AutoModel` raced transformers v5's non-thread-safe `_LazyModule` and died
with `ImportError: cannot import name 'AutoModel'` (reproduced 30/30 locally
with 16 threads on transformers 5.8.1). Concurrent `from_pretrained` calls
additionally mmap-load the same weights side by side. Serializing the whole
constructor fixes both; these tests pin that behavior with fake
`transformers`/`torch` modules so they run on a bare laptop.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=broad-exception-caught,import-outside-toplevel,redefined-outer-name,unused-argument

from __future__ import annotations

import sys
import threading
import time
import types

import pytest


@pytest.fixture
def fake_hf_stack(monkeypatch):
  """Fake `transformers` + `torch` recording construction concurrency."""

  state = {"active": 0, "max_active": 0, "calls": 0}
  gauge_lock = threading.Lock()

  class _FakeModel:

    def to(self, device):
      return self

    def eval(self):
      return self

  class _FromPretrained:

    @staticmethod
    def from_pretrained(path, **kwargs):
      with gauge_lock:
        state["active"] += 1
        state["calls"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
      time.sleep(0.02)  # widen the race window
      with gauge_lock:
        state["active"] -= 1
      return _FakeModel()

  transformers_mod = types.ModuleType("transformers")
  transformers_mod.AutoModel = _FromPretrained
  transformers_mod.AutoTokenizer = _FromPretrained
  torch_mod = sys.modules.get("torch") or types.ModuleType("torch")

  monkeypatch.setitem(sys.modules, "transformers", transformers_mod)
  monkeypatch.setitem(sys.modules, "torch", torch_mod)
  return state


def test_bge_embedder_constructions_never_overlap(fake_hf_stack, tmp_path):
  from sdfb_core.rag.embedding import BgeEmbedder

  errors: list[Exception] = []

  def build():
    try:
      BgeEmbedder(str(tmp_path)).ensure_loaded()
    except Exception as e:  # pragma: no cover - failure diagnostics
      errors.append(e)

  threads = [threading.Thread(target=build) for _ in range(8)]
  for t in threads:
    t.start()
  for t in threads:
    t.join()

  assert not errors
  # 8 loads happened (2 from_pretrained calls each) …
  assert fake_hf_stack["calls"] == 16
  # … but never two loader calls in flight at once.
  assert fake_hf_stack["max_active"] == 1
