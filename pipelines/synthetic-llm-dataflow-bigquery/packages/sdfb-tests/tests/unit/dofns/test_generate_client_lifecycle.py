"""GenerateRecordsDoFn + lazy ModelClient ignition (WS1 §3b).

History: the 2026-07-10 E2E defect was nobody calling
`VLLMModelClient.setup()` → silent 100% memorization. The first fix made
the DoFn call it eagerly — which the 2026-07-20 b2 run showed wastes
~230 s / 519 GPU-s when no column ever reaches the LLM. The contract is
now: the CLIENT self-ignites on first `generate_json()` (idempotent,
lock-serialized), the DoFn never ignites eagerly, and teardown remains
the DoFn's job. Loud-failure is preserved: a boot error raises out of the
first LLM call instead of being swallowed.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=unused-argument

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pytest
from sdfb_beam.dofns import generate as generate_mod
from sdfb_beam.dofns.generate import GenerateRecordsDoFn


class _Record:

  def __init__(self, i: int) -> None:
    self._i = i

  def model_dump(self, mode="python"):
    return {"i": self._i}


class _LlmEngine:
  """Engine that calls the LLM during setup (the B.1 shape)."""

  events: ClassVar[list[str]] = []

  def setup(self, model_client, ctx):
    _LlmEngine.events.append("engine_setup")
    model_client.generate_json(prompt="p", json_schema={})

  def generate_batch(self, n, cfg):
    for i in range(n):
      yield _Record(i)

  def teardown(self):
    _LlmEngine.events.append("engine_teardown")


class _NoLlmEngine:
  """Engine that never touches the LLM (b2 with only empirical/
    identifier/jitter columns — the 2026-07-20 wasted-ignition case)."""

  events: ClassVar[list[str]] = []

  def setup(self, model_client, ctx):
    _NoLlmEngine.events.append("engine_setup")

  def generate_batch(self, n, cfg):
    for i in range(n):
      yield _Record(i)

  def teardown(self):
    _NoLlmEngine.events.append("engine_teardown")


class _LazyClient:
  """The new VLLMModelClient shape: generate_json self-ignites."""

  def __init__(self, events: list[str]) -> None:
    self.events = events
    self.ready = False

  def setup(self):
    self.events.append("client_setup")
    self.ready = True

  def teardown(self):
    self.events.append("client_teardown")
    self.ready = False

  def generate_json(self, prompt, json_schema, **kw):
    if not self.ready:
      self.setup()
    return [{}]


class _BoomLazyClient(_LazyClient):
  """Boot failure at first LLM use must raise loudly, never degrade."""

  def setup(self):
    raise RuntimeError("vllm boom")


class _NoLifecycleClient:
  """FakeModelClient shape — no setup()/teardown()."""

  def generate_json(self, prompt, json_schema, **kw):
    return [{}]


def _ctx():
  return SimpleNamespace(
      embedder_uri="",
      table_schema=SimpleNamespace(columns=[]),
      identity_columns=[],
      pipeline_run_id="lifecycle-test",
      rag_chunks_table=None,
      chunk_store=None,
  )


def _dofn(client, engine_cls, monkeypatch):
  monkeypatch.setattr(generate_mod, "get_engine", lambda name: engine_cls)
  return GenerateRecordsDoFn(
      engine_name="stub", model_client=client, ctx=_ctx(), seed=7)


def test_no_ignition_when_engine_never_calls_llm(monkeypatch):
  _NoLlmEngine.events = []
  client = _LazyClient(_NoLlmEngine.events)
  dofn = _dofn(client, _NoLlmEngine, monkeypatch)
  dofn.setup()
  rows = list(dofn.process({"n": 2, "batch_id": 0}))
  dofn.teardown()
  assert len(rows) == 2
  # The whole point of WS1 §3b: no client_setup anywhere in the run.
  assert _NoLlmEngine.events == [
      "engine_setup",
      "engine_teardown",
      "client_teardown",
  ]


def test_lazy_ignition_fires_during_first_llm_call(monkeypatch):
  _LlmEngine.events = []
  client = _LazyClient(_LlmEngine.events)
  dofn = _dofn(client, _LlmEngine, monkeypatch)
  dofn.setup()
  dofn.teardown()
  assert _LlmEngine.events == [
      "engine_setup",
      "client_setup",
      "engine_teardown",
      "client_teardown",
  ]


def test_client_without_lifecycle_still_works(monkeypatch):
  _NoLlmEngine.events = []
  dofn = _dofn(_NoLifecycleClient(), _NoLlmEngine, monkeypatch)
  dofn.setup()
  rows = list(dofn.process({"n": 2, "batch_id": 0}))
  dofn.teardown()
  assert len(rows) == 2
  assert _NoLlmEngine.events == ["engine_setup", "engine_teardown"]


def test_boot_failure_raises_out_of_first_llm_call(monkeypatch):
  """B.1 shape: the engine's setup calls the LLM, so a boot failure still
    crashes DoFn.setup() — same blast radius as the old eager contract."""
  _LlmEngine.events = []
  client = _BoomLazyClient(_LlmEngine.events)
  dofn = _dofn(client, _LlmEngine, monkeypatch)
  with pytest.raises(RuntimeError, match="vllm boom"):
    dofn.setup()
