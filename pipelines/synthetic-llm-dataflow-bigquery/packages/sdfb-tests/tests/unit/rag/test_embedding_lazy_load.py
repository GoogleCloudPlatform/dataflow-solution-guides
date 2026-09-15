"""`BgeEmbedder` loads weights on first use, not at construction (ADR 0034).

Every generate DoFn builds an engine, and every B.1 engine builds an
embedder — 32 per table on the 2026-08-29 R6 pair — yet a store-warm
generate setup never embeds a single text: row-doc vectors come from
`rag_chunks` and pools from `freetext_pools`. Eager construction still
imported transformers, loaded 130 MB of weights and opened a CUDA context
per instance (~600 MiB VRAM held next to vLLM). Lazy loading makes the
warm path free and keeps the cold path (bulk embed on CUDA, then demote)
byte-identical.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,unused-argument

from __future__ import annotations

import sys
import types


def _fake_stack(monkeypatch, cuda_available: bool, log: dict):

  class _FakeModel:

    def to(self, device):
      log["moves"] = [*log.get("moves", []), device]
      return self

    def eval(self):
      return self

  class _Loader:

    @staticmethod
    def from_pretrained(path, **kwargs):
      log["loads"] = log.get("loads", 0) + 1
      return _FakeModel()

  transformers_mod = types.ModuleType("transformers")
  transformers_mod.AutoModel = _Loader
  transformers_mod.AutoTokenizer = _Loader
  torch_mod = types.ModuleType("torch")
  torch_mod.cuda = types.SimpleNamespace(
      is_available=lambda: cuda_available,
      empty_cache=lambda: log.__setitem__("emptied",
                                          log.get("emptied", 0) + 1),
  )
  monkeypatch.setitem(sys.modules, "transformers", transformers_mod)
  monkeypatch.setitem(sys.modules, "torch", torch_mod)


def test_construction_loads_nothing(monkeypatch, tmp_path):
  from sdfb_core.rag.embedding import BgeEmbedder

  log: dict = {}
  _fake_stack(monkeypatch, cuda_available=True, log=log)
  emb = BgeEmbedder(str(tmp_path), device="auto")
  assert log.get("loads", 0) == 0
  assert log.get("moves", []) == []
  assert emb.dim == 384
  assert emb.loaded is False


def test_ensure_loaded_resolves_device_and_loads_once(monkeypatch, tmp_path):
  from sdfb_core.rag.embedding import BgeEmbedder

  log: dict = {}
  _fake_stack(monkeypatch, cuda_available=True, log=log)
  emb = BgeEmbedder(str(tmp_path), device="auto")
  emb.ensure_loaded()
  emb.ensure_loaded()  # idempotent
  assert log["loads"] == 2  # tokenizer + model, once
  assert log["moves"] == ["cuda"]
  assert emb.device == "cuda"
  assert emb.loaded is True


def test_demote_before_load_pins_the_later_load_to_cpu(monkeypatch, tmp_path):
  """The store-warm generate path: setup demotes right after the index
    is built, before anything was loaded — no VRAM was held, nothing is
    released, and a later seed-example embed must load on CPU."""
  from sdfb_core.rag.embedding import BgeEmbedder

  log: dict = {}
  _fake_stack(monkeypatch, cuda_available=True, log=log)
  emb = BgeEmbedder(str(tmp_path), device="auto")
  emb.demote_to_cpu()
  assert log.get("loads", 0) == 0
  assert log.get("emptied", 0) == 0
  emb.ensure_loaded()
  assert log["moves"] == ["cpu"]
  assert emb.device == "cpu"


def test_device_milestone_fires_at_load_not_construction(
    monkeypatch, tmp_path, caplog):
  import logging

  from sdfb_core.rag.embedding import BgeEmbedder

  log: dict = {}
  _fake_stack(monkeypatch, cuda_available=False, log=log)
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    emb = BgeEmbedder(str(tmp_path), device="auto")
    before = [
        r.message for r in caplog.records if "embedder_device" in r.message
    ]
    emb.ensure_loaded()
    after = [
        r.message for r in caplog.records if "embedder_device" in r.message
    ]
  assert before == []
  assert len(after) == 1
  assert "device=cpu" in after[0] and "requested=auto" in after[0]
