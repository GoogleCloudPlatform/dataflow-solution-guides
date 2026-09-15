"""`VLLMModelClient` under Dataflow's default multi-SDK-container topology
(ADR 0034): one vLLM server per WORKER, spawned by whichever SDK process
wins a cross-process mutex, reused by every other.

Today every GPU tier pins `no_use_multiple_sdk_containers` — one SDK
process per worker — because eight sibling processes would each spawn a
server into the one GPU (2026-07-16: mutual CUDA OOM, 5.4 h of retries).
That pin is also why generation is GIL-bound: the 2026-08-29 R6 pair ran
8 harness threads per worker on one Python interpreter and used ~1 of 8
vCPUs per worker for its 10M-row generate stages. Lifting the pin needs
the spawn window to be exclusive ACROSS processes, not just across
threads. SDK containers on a Dataflow worker share the host network, so a
bound loopback port is a mutex every process can see — and the same
network is what makes the existing reuse probe work across them.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,protected-access,unused-argument,use-implicit-booleaness-not-comparison

from __future__ import annotations

import io
import json
import socket
import threading
import time
import urllib.request
from unittest import mock

import pytest
from sdfb_beam.handlers.vllm_client import VLLMModelClient, _PortMutex


class _FakeModelsResponse(io.BytesIO):

  def __init__(self, payload: dict) -> None:
    super().__init__(json.dumps(payload).encode("utf-8"))
    self.status = 200

  def __enter__(self):
    return self

  def __exit__(self, *exc):
    self.close()
    return False


def _free_port() -> int:
  s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  s.bind(("127.0.0.1", 0))
  port = s.getsockname()[1]
  s.close()
  return port


@pytest.fixture(autouse=True)
def _fresh_server_registry(monkeypatch):
  import sdfb_beam.handlers.vllm_client as mod

  monkeypatch.setattr(mod, "_SERVER_REFS", {})
  monkeypatch.setattr(mod, "_PARKED_SERVERS", {})
  monkeypatch.setattr(mod, "_SPAWN_FAILURES", {})


def _patched(**overrides):
  patches = {
      "_pull_weights": lambda self: None,
      "_assert_gpu_dtype_compatible": lambda self: None,
      "_wait_until_ready": lambda self: None,
      "_build_openai_client": lambda self: object(),
  }
  patches.update(overrides)
  return [mock.patch.object(VLLMModelClient, k, v) for k, v in patches.items()]


def test_port_mutex_is_exclusive_until_released():
  port = _free_port()
  a, b = _PortMutex("127.0.0.1", port), _PortMutex("127.0.0.1", port)
  assert a.try_acquire() is True
  assert b.try_acquire() is False
  a.release()
  assert b.try_acquire() is True
  b.release()


def test_default_clients_are_single_process_and_lock_port_is_derived():
  c = VLLMModelClient(model_uri="gs://b/m/v1/", port=8000)
  assert c.cross_process is False
  assert c.spawn_lock_port == 8001


def test_cross_process_setup_waits_for_a_sibling_spawn_and_reuses(monkeypatch):
  """Another SDK process holds the spawn window; its server comes up
    0.2 s later. Ours must wait, see it, bind to it and never spawn."""
  port, lock_port = _free_port(), _free_port()
  server_up: list = []

  def fake_urlopen(*a, **k):
    if not server_up:
      raise ConnectionRefusedError("nothing listening yet")
    return _FakeModelsResponse({"data": [{"id": "/local-ssd/model"}]})

  monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
  holder = _PortMutex("127.0.0.1", lock_port)
  assert holder.try_acquire()

  def sibling_spawns():
    time.sleep(0.2)
    server_up.append(True)
    holder.release()

  threading.Thread(target=sibling_spawns).start()
  spawned: list = []

  def record_spawn(self):
    spawned.append(self)

  with mock.patch.multiple(
      VLLMModelClient,
      _pull_weights=lambda self: None,
      _assert_gpu_dtype_compatible=lambda self: None,
      _spawn_server=record_spawn,
      _wait_until_ready=lambda self: None,
      _build_openai_client=lambda self: object(),
  ):
    c = VLLMModelClient(
        model_uri="gs://b/m/v1/",
        port=port,
        cross_process=True,
        spawn_lock_port=lock_port,
        poll_interval_s=0.05,
    )
    c.setup()
  assert spawned == []
  assert c._client is not None


def test_cross_process_setup_spawns_when_it_wins_the_mutex_and_releases_it(
    monkeypatch, caplog):
  import logging

  port, lock_port = _free_port(), _free_port()
  server_up: list = []

  def fake_urlopen(*a, **k):
    if not server_up:
      raise ConnectionRefusedError("nothing listening yet")
    return _FakeModelsResponse({"data": [{"id": "/local-ssd/model"}]})

  monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
  spawned: list = []

  def fake_spawn(self):
    spawned.append(self)
    server_up.append(True)

  with mock.patch.multiple(
      VLLMModelClient,
      _pull_weights=lambda self: None,
      _assert_gpu_dtype_compatible=lambda self: None,
      _spawn_server=fake_spawn,
      _wait_until_ready=lambda self: None,
      _build_openai_client=lambda self: object(),
  ), caplog.at_level(
      logging.INFO, logger="sdfb.milestone"):
    c = VLLMModelClient(
        model_uri="gs://b/m/v1/",
        port=port,
        cross_process=True,
        spawn_lock_port=lock_port,
        poll_interval_s=0.05,
    )
    c.setup()
  assert len(spawned) == 1
  assert "name=vllm_spawn_lock_acquired" in caplog.text
  # The window is released once the server is ready.
  probe = _PortMutex("127.0.0.1", lock_port)
  assert probe.try_acquire() is True
  probe.release()


def test_cross_process_teardown_keeps_the_server_alive(monkeypatch, caplog):
  """Another process may be bound to this server; its refcount is
    invisible to us. The worker VM reaps the subprocess at job end."""
  import logging

  port, lock_port = _free_port(), _free_port()
  fake_proc = mock.MagicMock()
  fake_proc.pid = 4321
  server_up: list = []

  def fake_urlopen(*a, **k):
    if not server_up:
      raise ConnectionRefusedError("nothing listening yet")
    return _FakeModelsResponse({"data": [{"id": "/local-ssd/model"}]})

  monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

  def fake_spawn(self):
    self._server = fake_proc
    server_up.append(True)

  with mock.patch.multiple(
      VLLMModelClient,
      _pull_weights=lambda self: None,
      _assert_gpu_dtype_compatible=lambda self: None,
      _spawn_server=fake_spawn,
      _wait_until_ready=lambda self: None,
      _build_openai_client=lambda self: object(),
  ):
    c = VLLMModelClient(
        model_uri="gs://b/m/v1/",
        port=port,
        cross_process=True,
        spawn_lock_port=lock_port,
        poll_interval_s=0.05,
    )
    c.setup()
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    c.teardown()
  fake_proc.terminate.assert_not_called()
  fake_proc.kill.assert_not_called()
  assert "name=vllm_server_kept_alive" in caplog.text
  assert c._client is None


def test_launcher_resolves_cross_process_from_the_sdk_container_topology():
  from sdfb_beam.cli.run_pipeline import build_model_client, resolve_cross_process

  assert resolve_cross_process("DataflowRunner", ["use_runner_v2"]) is True
  assert (resolve_cross_process(
      "DataflowRunner", ["use_runner_v2", "no_use_multiple_sdk_containers"])
          is False)
  assert resolve_cross_process("DirectRunner", []) is False
  c = build_model_client("vllm", "gs://b/m/v1/", cross_process=True)
  assert c.cross_process is True
  assert build_model_client("vllm", "gs://b/m/v1/").cross_process is False


def test_cross_process_teardown_of_a_client_that_never_bound_is_silent(caplog):
  """2026-09-07 R7m: 40 `vllm_server_kept_alive` lines — one per DoFn
    teardown, including the 38 clients that never ignited. The milestone
    is evidence that a server was left running; it must fire only from a
    client that was bound to (or spawned) one."""
  import logging

  c = VLLMModelClient(model_uri="gs://b/m/v1/", cross_process=True)
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    c.teardown()
  assert "vllm_server_kept_alive" not in caplog.text
