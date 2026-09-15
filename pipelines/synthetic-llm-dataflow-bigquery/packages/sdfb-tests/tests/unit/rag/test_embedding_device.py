"""BgeEmbedder device selection + VRAM release (2026-07-25 E2E).

Both T4s sat idle while 33,610 chunks embedded on CPU (1,506 s). "auto"
uses CUDA when available; demote_to_cpu() releases VRAM afterward so
vLLM's ignition (which sizes its KV-cache budget from free memory) never
competes with a resident embedder. Fake torch modules keep this laptop-
runnable and deterministic.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,redefined-outer-name,reimported,unused-argument

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


def test_auto_resolves_to_cuda_when_available(monkeypatch, tmp_path):
  from sdfb_core.rag.embedding import BgeEmbedder

  log: dict = {}
  _fake_stack(monkeypatch, cuda_available=True, log=log)
  emb = BgeEmbedder(str(tmp_path), device="auto")
  emb.ensure_loaded()
  assert emb.device == "cuda"
  assert log["moves"] == ["cuda"]


def test_auto_falls_back_to_cpu(monkeypatch, tmp_path):
  from sdfb_core.rag.embedding import BgeEmbedder

  log: dict = {}
  _fake_stack(monkeypatch, cuda_available=False, log=log)
  emb = BgeEmbedder(str(tmp_path), device="auto")
  emb.ensure_loaded()
  assert emb.device == "cpu"


def test_demote_to_cpu_moves_and_frees(monkeypatch, tmp_path):
  from sdfb_core.rag.embedding import BgeEmbedder

  log: dict = {}
  _fake_stack(monkeypatch, cuda_available=True, log=log)
  emb = BgeEmbedder(str(tmp_path), device="auto")
  emb.ensure_loaded()
  emb.demote_to_cpu()
  assert emb.device == "cpu"
  assert log["moves"] == ["cuda", "cpu"]
  assert log["emptied"] == 1
  emb.demote_to_cpu()  # idempotent — no second move/empty
  assert log["moves"] == ["cuda", "cpu"]
  assert log["emptied"] == 1


def test_explicit_cpu_never_touches_cuda(monkeypatch, tmp_path):
  from sdfb_core.rag.embedding import BgeEmbedder

  log: dict = {}
  _fake_stack(monkeypatch, cuda_available=True, log=log)
  emb = BgeEmbedder(str(tmp_path))
  emb.ensure_loaded()
  assert emb.device == "cpu"
  assert log["moves"] == ["cpu"]
  emb.demote_to_cpu()
  assert log.get("emptied") is None


def _milestones(caplog) -> list[dict]:
  from sdfb_core.observability import parse_milestone

  return [
      m for m in (parse_milestone(r.getMessage()) for r in caplog.records) if m
  ]


def test_load_logs_embedder_device_milestone(monkeypatch, tmp_path, caplog):
  from sdfb_core.rag.embedding import BgeEmbedder

  log: dict = {}
  _fake_stack(monkeypatch, cuda_available=True, log=log)
  with caplog.at_level("INFO"):
    BgeEmbedder(str(tmp_path), device="auto").ensure_loaded()
  devs = [m for m in _milestones(caplog) if m["name"] == "embedder_device"]
  assert devs and devs[0]["device"] == "cuda" and devs[0]["requested"] == "auto"


def test_demote_logs_embedder_demoted_only_when_leaving_cuda(
    monkeypatch, tmp_path, caplog):
  from sdfb_core.rag.embedding import BgeEmbedder

  log: dict = {}
  _fake_stack(monkeypatch, cuda_available=True, log=log)
  emb = BgeEmbedder(str(tmp_path), device="auto")
  emb.ensure_loaded()
  with caplog.at_level("INFO"):
    emb.demote_to_cpu()
    emb.demote_to_cpu()  # idempotent second call must not log again
  demotes = [m for m in _milestones(caplog) if m["name"] == "embedder_demoted"]
  assert len(demotes) == 1

  caplog.clear()
  cpu_emb = BgeEmbedder(str(tmp_path))  # explicit cpu
  cpu_emb.ensure_loaded()
  with caplog.at_level("INFO"):
    cpu_emb.demote_to_cpu()
  assert not [m for m in _milestones(caplog) if m["name"] == "embedder_demoted"]


# ---------------------------------------------------------------------------
# WS6 W2 — "auto" must mean CUDA IF THERE IS ROOM, not "a GPU exists".
#
# 2026-07-26_17_10_37 E2E: on a DoFn.setup() RETRY the module-level vLLM
# server reuse (ADR 0014) means vLLM is still resident and holds 13.80 of
# 14.56 GiB. cuda.is_available() is still True, so the embedder tried CUDA
# and OOMed asking for 2 MiB — 12 times. Availability is not capacity.
# ---------------------------------------------------------------------------
def _with_mem(monkeypatch, *, free_bytes: int, cuda_available: bool, log: dict):
  _fake_stack(monkeypatch, cuda_available=cuda_available, log=log)
  import sys

  torch_mod = sys.modules["torch"]
  torch_mod.cuda.mem_get_info = lambda: (free_bytes, 16 * 1024**3)
  return torch_mod


def test_auto_stays_on_cpu_when_the_gpu_is_full(monkeypatch, tmp_path):
  """The exact 2026-07-26 condition: a GPU exists but vLLM owns it."""
  from sdfb_core.rag.embedding import BgeEmbedder

  log: dict = {}
  _with_mem(monkeypatch, free_bytes=2 * 1024**2, cuda_available=True, log=log)
  emb = BgeEmbedder(str(tmp_path), device="auto")
  emb.ensure_loaded()
  assert emb.device == "cpu"
  assert log.get("moves") == ["cpu"], "must never attempt the .to(cuda)"


def test_auto_uses_cuda_when_there_is_ample_room(monkeypatch, tmp_path):
  from sdfb_core.rag.embedding import BgeEmbedder

  log: dict = {}
  _with_mem(monkeypatch, free_bytes=8 * 1024**3, cuda_available=True, log=log)
  emb = BgeEmbedder(str(tmp_path), device="auto")
  emb.ensure_loaded()
  assert emb.device == "cuda"


def test_auto_still_works_when_mem_get_info_is_unavailable(
    monkeypatch, tmp_path):
  """Older/stubbed torch: fall back to today's availability check rather
    than refusing the GPU outright."""
  from sdfb_core.rag.embedding import BgeEmbedder

  log: dict = {}
  _fake_stack(monkeypatch, cuda_available=True, log=log)
  emb = BgeEmbedder(str(tmp_path), device="auto")
  emb.ensure_loaded()
  assert emb.device == "cuda"


def test_an_oom_on_move_degrades_to_cpu_instead_of_failing_the_bundle(
    monkeypatch, tmp_path, caplog):
  """Belt and braces: a race can still fill the card between the check and
    the move. bge-small on CPU is slower, not wrong — never fail setup()."""
  import sys

  from sdfb_core.rag.embedding import BgeEmbedder

  log: dict = {}
  _with_mem(monkeypatch, free_bytes=8 * 1024**3, cuda_available=True, log=log)

  class _OomError(RuntimeError):
    pass

  sys.modules["torch"].OutOfMemoryError = _OomError
  real_to = None

  class _FlakyModel:

    def to(self, device):
      log["moves"] = [*log.get("moves", []), device]
      if device == "cuda":
        raise _OomError("CUDA out of memory. Tried to allocate 2.00 MiB")
      return self

    def eval(self):
      return self

  sys.modules["transformers"].AutoModel = type(
      "_L", (), {"from_pretrained": staticmethod(lambda p, **k: _FlakyModel())})
  del real_to
  with caplog.at_level("WARNING"):
    emb = BgeEmbedder(str(tmp_path), device="auto")
    emb.ensure_loaded()
  assert emb.device == "cpu"
  assert log["moves"] == ["cuda", "cpu"]
  assert "embedder_cuda_oom_fallback" in caplog.text
